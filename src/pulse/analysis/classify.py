"""Stage 4: classify every review into the closed theme taxonomy (P2-T3 → P2-T6).

Three things here are doing more work than they look like they are.

**Reconciliation, not zipping.** The model is asked to echo an index per review. It will
occasionally omit one, repeat one, or invent one out of range. Pairing its results with the
input list positionally would mislabel reviews *silently* — the same class of failure as a
paraphrased quote. So results are indexed, the index set is checked against `1..n`, and
anything missing or spurious becomes `None` rather than a wrong label.

**The on-disk cache is the real cost lever.** Prompt caching (where a provider offers it)
saves input tokens within a run; this cache skips the call entirely. The 8-12 week window
overlaps ~90% between consecutive weekly runs, so a second run should be nearly free.

**Failure degrades to `None`, never to a guess.** A batch that cannot be parsed after a
retry marks its reviews unclassified. Unclassified reviews count toward the corpus total
but never toward a theme, so they cannot manufacture a trend (architecture.md §8.1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from pulse import logging as pulse_logging
from pulse.config import Settings
from pulse.llm import (
    LlmError,
    LlmPort,
    LlmResponseInvalid,
    json_schema_for,
    parse_as,
)
from pulse.models import ClassifiedReview, Review, Theme
from pulse.prompts.taxonomy import render_batch, taxonomy_prompt

log = pulse_logging.get_logger("analysis.classify")

DEFAULT_CACHE_PATH = Path(".cache/classify.json")


def prompt_fingerprint(prompt: str) -> str:
    """Short, stable digest of a prompt, for cache identity."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


SCHEMA_NAME = "review_classification"
MAX_BATCH_ATTEMPTS = 2


# --------------------------------------------------------------------------------------
# The model-facing schema
# --------------------------------------------------------------------------------------


class ClassifiedItem(BaseModel):
    """One classification as the model returns it, addressed by batch index."""

    index: Annotated[int, Field(ge=1)]
    theme: Theme | None
    confidence: Annotated[float, Field(ge=0, le=1)]


class ClassificationBatch(BaseModel):
    """The model-facing wrapper. The `Theme` enum in here *is* the ≤5 constraint."""

    results: list[ClassifiedItem]


# --------------------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------------------


class ClassificationCache:
    """Review-level label cache, invalidated when anything that produced a label changes.

    Identity is `(taxonomy_version, model, prompt_fingerprint)`. Only the first was in the
    plan's key; the other two were added because each would otherwise serve labels from a
    judge the current run is not using:

    * **model** — Groq-labelled rows served while a run uses a different model would give
      the corpus two different judges, quietly undermining the O5 comparison.
    * **prompt** — the prompt *is* what produced the labels. Sharpening a disambiguation
      rule in `prompts/taxonomy.py` changes the answers, and `taxonomy_version` lives in
      `themes.toml`, so it would not necessarily be bumped for that edit. Without the
      fingerprint, a prompt fix silently serves labels from the prompt it replaced.
    """

    def __init__(
        self,
        path: Path = DEFAULT_CACHE_PATH,
        *,
        taxonomy_version: str,
        model: str,
        prompt: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.taxonomy_version = taxonomy_version
        self.model = model
        self.prompt_fingerprint = prompt_fingerprint(prompt) if prompt is not None else ""
        self.enabled = enabled
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache is a performance problem, not a correctness one. Start over
            # rather than failing a run over it.
            log.warning(
                "classification cache unreadable; ignoring it",
                extra={"path": str(self.path)},
            )
            return

        if (
            payload.get("taxonomy_version") != self.taxonomy_version
            or payload.get("model") != self.model
            or payload.get("prompt_fingerprint", "") != self.prompt_fingerprint
        ):
            log.info(
                "classification cache discarded (taxonomy, model, or prompt changed)",
                extra={
                    "cached_taxonomy": payload.get("taxonomy_version"),
                    "cached_model": payload.get("model"),
                },
            )
            return

        entries = payload.get("entries")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, review_id: str) -> ClassifiedReview | None:
        if not self.enabled:
            return None
        entry = self._entries.get(review_id)
        if entry is None:
            return None
        try:
            return ClassifiedReview.model_validate({"review_id": review_id, **entry})
        except ValueError:
            return None

    def put(self, classified: ClassifiedReview) -> None:
        if not self.enabled:
            return
        self._entries[classified.review_id] = {
            "theme": classified.theme.value if classified.theme else None,
            "confidence": classified.confidence,
        }
        self._dirty = True

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "taxonomy_version": self.taxonomy_version,
                    "model": self.model,
                    "prompt_fingerprint": self.prompt_fingerprint,
                    "entries": self._entries,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


@dataclass
class ClassifyReport:
    """Counts only — never review text (architecture.md §16)."""

    reviews_in: int = 0
    from_cache: int = 0
    api_calls: int = 0
    batches: int = 0
    batch_retries: int = 0
    batches_failed: int = 0
    low_confidence: int = 0
    missing_from_response: int = 0
    spurious_indices: int = 0
    duplicate_indices: int = 0
    unclassified: int = 0
    theme_counts: dict[str, int] = field(default_factory=dict)
    prompt_cache_reads: int = 0
    prompt_cache_supported: bool = False

    @property
    def cache_hit_rate(self) -> float:
        return self.from_cache / self.reviews_in if self.reviews_in else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "reviews_in": self.reviews_in,
            "from_cache": self.from_cache,
            "cache_hit_rate": round(self.cache_hit_rate, 3),
            "api_calls": self.api_calls,
            "batches": self.batches,
            "batch_retries": self.batch_retries,
            "batches_failed": self.batches_failed,
            "low_confidence": self.low_confidence,
            "missing_from_response": self.missing_from_response,
            "spurious_indices": self.spurious_indices,
            "duplicate_indices": self.duplicate_indices,
            "unclassified": self.unclassified,
            "theme_counts": dict(sorted(self.theme_counts.items())),
            "prompt_cache_supported": self.prompt_cache_supported,
            "prompt_cache_reads": self.prompt_cache_reads,
        }


# --------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------


def _batches(reviews: list[Review], size: int) -> list[list[Review]]:
    return [reviews[start : start + size] for start in range(0, len(reviews), size)]


def _reconcile(
    batch: list[Review],
    items: list[ClassifiedItem],
    report: ClassifyReport,
    min_confidence: float,
) -> dict[str, ClassifiedReview]:
    """Map the model's indexed results onto review ids, rejecting anything inconsistent.

    Deliberately paranoid. Every discarded item is counted so a model that is quietly bad
    at echoing indices shows up in the ledger rather than as mysterious label drift.
    """
    by_index: dict[int, ClassifiedItem] = {}
    for item in items:
        if not 1 <= item.index <= len(batch):
            report.spurious_indices += 1
            continue
        if item.index in by_index:
            report.duplicate_indices += 1
            continue
        by_index[item.index] = item

    resolved: dict[str, ClassifiedReview] = {}
    for position, review in enumerate(batch, start=1):
        matched = by_index.get(position)
        if matched is None:
            report.missing_from_response += 1
            resolved[review.review_id] = ClassifiedReview(
                review_id=review.review_id, theme=None, confidence=0.0
            )
            continue

        theme = matched.theme
        if theme is not None and matched.confidence < min_confidence:
            # Below the floor the label is not trusted, but the model's own confidence is
            # kept so the discard is auditable.
            report.low_confidence += 1
            theme = None

        resolved[review.review_id] = ClassifiedReview(
            review_id=review.review_id, theme=theme, confidence=matched.confidence
        )

    return resolved


def _classify_batch(
    batch: list[Review],
    *,
    llm: LlmPort,
    system: str,
    schema: dict[str, Any],
    max_tokens: int,
    min_confidence: float,
    report: ClassifyReport,
    reasoning_effort: str | None = None,
) -> dict[str, ClassifiedReview]:
    """Classify one batch, retrying once with the validation failure fed back."""
    user = render_batch(batch)
    feedback = ""

    for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
        try:
            response = llm.generate_json(
                system=system,
                user=user + feedback,
                schema=schema,
                schema_name=SCHEMA_NAME,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
            report.api_calls += 1
            report.prompt_cache_reads += response.cached_input_tokens

            parsed = parse_as(response, ClassificationBatch)
            return _reconcile(batch, parsed.results, report, min_confidence)

        except LlmResponseInvalid as exc:
            if attempt < MAX_BATCH_ATTEMPTS:
                report.batch_retries += 1
                # Name the failure rather than saying "try again" — a bare retry tends to
                # produce a differently-wrong answer.
                feedback = (
                    f"\n\nYour previous reply was rejected: {exc}. "
                    f"Return one entry for each of the {len(batch)} indices above, "
                    f"as a single JSON object."
                )
                log.warning(
                    "classification batch rejected; retrying",
                    extra={"attempt": attempt, "reason": str(exc)[:200]},
                )
                continue

            report.batches_failed += 1
            log.error(
                "classification batch failed after retry; marking reviews unclassified",
                extra={"batch_size": len(batch), "reason": str(exc)[:200]},
            )
            report.missing_from_response += len(batch)
            return {
                review.review_id: ClassifiedReview(
                    review_id=review.review_id, theme=None, confidence=0.0
                )
                for review in batch
            }

        except LlmError:
            # Transport, auth, or rate-limit failures are not the batch's fault and must
            # not be silently downgraded to "unclassified" — that would publish a pulse
            # built on a corpus the model never actually saw.
            raise

    raise AssertionError("unreachable")  # pragma: no cover


def classify_reviews(
    reviews: list[Review],
    *,
    llm: LlmPort,
    settings: Settings,
    cache: ClassificationCache | None = None,
) -> tuple[list[ClassifiedReview], ClassifyReport]:
    """Classify a corpus. Output order matches input order.

    Order is preserved so `04_classified.json` is byte-stable for a stable corpus, which is
    what lets the Phase 6 golden test compare artifacts rather than re-running the model.
    """
    llm_config = settings.pulse.llm
    report = ClassifyReport(
        reviews_in=len(reviews), prompt_cache_supported=llm.supports_prompt_cache
    )

    if not reviews:
        return [], report

    # `is None`, not `or`: this class defines __len__, so an empty cache is falsy and
    # `cache or ...` would silently discard a caller-supplied one — including a disabled
    # cache from --no-cache — and write to the default path instead.
    # Built before the cache, because the prompt is part of the cache's identity.
    system = taxonomy_prompt(settings)

    if cache is None:
        cache = ClassificationCache(
            taxonomy_version=settings.taxonomy_version,
            model=llm.model,
            prompt=system,
        )

    resolved: dict[str, ClassifiedReview] = {}
    pending: list[Review] = []
    for review in reviews:
        if (hit := cache.get(review.review_id)) is not None:
            resolved[review.review_id] = hit
            report.from_cache += 1
        else:
            pending.append(review)

    if pending:
        schema = json_schema_for(ClassificationBatch)
        batches = _batches(pending, llm_config.batch_size)
        report.batches = len(batches)

        log.info(
            "classifying",
            extra={
                "reviews": len(reviews),
                "from_cache": report.from_cache,
                "to_classify": len(pending),
                "batches": len(batches),
                "model": llm.model,
            },
        )

        for batch in batches:
            batch_result = _classify_batch(
                batch,
                llm=llm,
                system=system,
                schema=schema,
                max_tokens=llm_config.classify_max_tokens,
                min_confidence=settings.pulse.thresholds.min_confidence,
                report=report,
                reasoning_effort=llm_config.classify_reasoning_effort,
            )
            resolved.update(batch_result)
            for classified in batch_result.values():
                cache.put(classified)

        cache.save()

    ordered = [resolved[review.review_id] for review in reviews]
    for classified in ordered:
        if classified.theme is None:
            report.unclassified += 1
        else:
            key = classified.theme.value
            report.theme_counts[key] = report.theme_counts.get(key, 0) + 1

    if report.api_calls and llm.supports_prompt_cache and not report.prompt_cache_reads:
        # Only meaningful where the provider offers a cache breakpoint. Groq does not, so
        # this is conditional rather than an unconditional assertion (architecture.md §8.1).
        log.warning(
            "provider supports prompt caching but reported no cache reads; "
            "something volatile may have leaked into the system prefix",
            extra={"model": llm.model},
        )

    log.info("classify complete", extra=report.summary())
    return ordered, report
