"""Stage dispatch: runs a contiguous range of pipeline stages (P1-T9).

Stages 1-3 are implemented. Stages 4-10 are registered but raise `StageNotImplemented`
naming the phase that delivers them, so `--to 3` succeeds today and the range checking,
artifact handling, and ledger bookkeeping are already exercised.

Keeping dispatch here rather than in `orchestrator.py` preserves the layering rule from
architecture.md §5: `orchestrator` owns run directories and the ledger and imports nothing
from `ingest/` or `analysis/`; this module is where the stages are composed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pulse import logging as pulse_logging
from pulse.analysis.aggregate import AggregateError, aggregate, select_top_themes
from pulse.analysis.classify import (
    DEFAULT_CACHE_PATH,
    ClassificationCache,
    classify_reviews,
)
from pulse.analysis.draft import DraftError, draft_note
from pulse.analysis.scrub import scrub_reviews
from pulse.config import Settings
from pulse.ingest.base import build_source
from pulse.ingest.normalize import (
    NormalizeError,
    filter_to_window,
    normalize,
    validate_window,
)
from pulse.llm import LlmPort, build_llm, usage_for_ledger
from pulse.models import (
    ClassifiedReview,
    LlmUsage,
    PulseNote,
    QuoteCandidate,
    RawReview,
    Review,
    Theme,
    ThemeStats,
)
from pulse.orchestrator import RunContext
from pulse.prompts.taxonomy import taxonomy_prompt
from pulse.rendering.render import render_note
from pulse.rendering.validate import assert_publishable, validate_note

log = pulse_logging.get_logger("pipeline")

FIRST_STAGE = 1
LAST_STAGE = 10

#: Which phase delivers each not-yet-implemented stage, for the error message.
_PENDING_STAGES: dict[int, str] = {
    9: "Phase 4 (publish to Google Docs via MCP)",
    10: "Phase 4 (create the Gmail draft via MCP)",
}


class StageNotImplemented(Exception):
    """Raised when a requested stage has not been built yet."""


class PipelineError(Exception):
    """Raised when a stage cannot complete."""


@dataclass
class PipelineOptions:
    settings: Settings
    input_path: Path
    target_week: str
    from_stage: int = FIRST_STAGE
    to_stage: int = LAST_STAGE
    source_name: str = "file_drop"
    #: `--llm` override, applied over the configured provider.
    llm_override: str | None = None
    #: An already-built port. Tests inject a `FakeLlm` here; the CLI leaves it None.
    llm: LlmPort | None = None
    cache_path: Path = DEFAULT_CACHE_PATH
    use_cache: bool = True
    #: Stamped into the rendered note. A parameter rather than `date.today()` so a render
    #: is a pure function of its inputs and the P6 golden test can compare two of them.
    generated_on: date | None = None


# --------------------------------------------------------------------------------------
# Stage 1 — ingest
# --------------------------------------------------------------------------------------


def stage_ingest(context: RunContext, options: PipelineOptions, detail: dict[str, object]) -> None:
    """Read the export into `RawReview[]` and persist it as `01_raw.json`.

    This artifact is the one that may contain PII as exported (architecture.md §18); every
    later artifact is scrubbed. Retention of it is open decision O6, settled in P6-T7.
    """
    source = build_source(options.source_name, path=options.input_path)
    raw_reviews, report = source.load()

    context.write_artifact(1, raw_reviews)
    detail.update(report.summary())


# --------------------------------------------------------------------------------------
# Stage 2 — normalize
# --------------------------------------------------------------------------------------


def stage_normalize(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Type, deduplicate, and window-filter the corpus into `02_normalized.json`."""
    raw_reviews = context.read_models(1, RawReview)

    reviews, report = normalize(raw_reviews)
    windowed, window = filter_to_window(
        reviews, options.target_week, options.settings.pulse.window.weeks
    )

    try:
        present = validate_window(windowed, window, options.settings.pulse.window.min_weeks)
    except NormalizeError as exc:
        raise PipelineError(str(exc)) from exc

    context.write_artifact(2, windowed)
    context.ledger.corpus_size = len(windowed)
    context.ledger.window_weeks = len(window)
    detail.update(
        {
            **report.summary(),
            "window": f"{window[0]}..{window[-1]}",
            "weeks_in_corpus": len(present),
            "reviews_in_window": len(windowed),
        }
    )


# --------------------------------------------------------------------------------------
# Stage 3 — scrub
# --------------------------------------------------------------------------------------


def stage_scrub(context: RunContext, options: PipelineOptions, detail: dict[str, object]) -> None:
    """Remove PII before anything leaves the process, writing `03_scrubbed.json`.

    The hard gate: no model call and no published artifact may read anything earlier than
    this stage's output (architecture.md §3).
    """
    reviews = context.read_models(2, Review)
    scrubbed, report = scrub_reviews(reviews)

    if not scrubbed:
        raise PipelineError(
            "Every review was dropped during scrubbing — the corpus is entirely PII. "
            "This is almost certainly a scrubber bug; check tests/test_scrub.py."
        )

    context.write_artifact(3, scrubbed)
    context.ledger.corpus_size = len(scrubbed)
    detail.update(report.summary())


# --------------------------------------------------------------------------------------
# Stage 4 — classify
# --------------------------------------------------------------------------------------


def stage_classify(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Label every review with a theme, writing `04_classified.json`.

    The first stage that calls a model, and the first that can cost money. It reads only
    `03_scrubbed.json`, so nothing identifying has ever been sent to a provider
    (architecture.md §3).
    """
    reviews = context.read_models(3, Review)

    # `is None` rather than `or`, so an injected port is always honoured regardless of
    # whether it happens to be falsy.
    llm = (
        options.llm
        if options.llm is not None
        else build_llm(options.settings, override=options.llm_override)
    )
    cache = ClassificationCache(
        options.cache_path,
        taxonomy_version=options.settings.taxonomy_version,
        model=llm.model,
        prompt=taxonomy_prompt(options.settings),
        enabled=options.use_cache,
    )

    classified, report = classify_reviews(reviews, llm=llm, settings=options.settings, cache=cache)

    context.write_artifact(4, classified)
    context.ledger.theme_counts = dict(report.theme_counts)
    context.ledger.usage = LlmUsage.model_validate(usage_for_ledger(llm))
    detail.update(report.summary())


# --------------------------------------------------------------------------------------
# Stage 5 — aggregate
# --------------------------------------------------------------------------------------


def stage_aggregate(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Rank themes and build quote candidate pools, writing `05_stats.json`.

    Deterministic: this is where "top 3 themes" is decided, so it must be reproducible
    (architecture.md §8.2).
    """
    reviews = context.read_models(3, Review)
    classified = context.read_models(4, ClassifiedReview)

    try:
        ranked, report = aggregate(
            reviews,
            classified,
            settings=options.settings,
            target_week=options.target_week,
        )
        selected = select_top_themes(ranked, report)
    except AggregateError as exc:
        raise PipelineError(str(exc)) from exc

    # The full ranking is stored, not just the top three: a reader asking "why wasn't
    # payments on the page?" should be able to answer it from the artifact.
    context.write_artifact(
        5,
        {
            "selected": [s.model_dump(mode="json") for s in selected],
            "ranked": [s.model_dump(mode="json") for s in ranked],
            "report": report.summary(),
        },
    )
    detail.update(report.summary())


# --------------------------------------------------------------------------------------
# Stages 6-8 — draft, validate, render
# --------------------------------------------------------------------------------------


def stage_draft(context: RunContext, options: PipelineOptions, detail: dict[str, object]) -> None:
    """Draft the note, gating and regenerating until it validates (stages 6 and 7).

    Stages 6 and 7 are one unit because the gate feeds back into the draft: splitting them
    across artifact boundaries would mean persisting a note that failed validation, which
    is exactly the thing that must never be publishable.
    """
    payload = context.read_artifact(5)
    selected = [ThemeStats.model_validate(item) for item in payload["selected"]]
    corpus_size = len(context.read_models(3, Review))

    llm = (
        options.llm
        if options.llm is not None
        else build_llm(options.settings, override=options.llm_override)
    )

    try:
        outcome = draft_note(
            selected,
            llm=llm,
            settings=options.settings,
            iso_week=options.target_week,
            corpus_size=corpus_size,
            generated_on=options.generated_on,
        )
    except DraftError as exc:
        # Every attempt's failures are persisted before the abort, so a developer can see
        # what the model actually got wrong rather than guessing from a log line.
        context.write_artifact(7, {"status": "aborted", "reason": str(exc)})
        raise PipelineError(str(exc)) from exc

    context.write_artifact(6, outcome.note)
    context.write_artifact(
        7,
        {
            "status": "ok",
            "attempts": [r.summary() for r in outcome.history],
            "report": outcome.report.summary(),
        },
    )

    context.ledger.draft_attempts = outcome.report.attempts
    context.ledger.usage = LlmUsage.model_validate(usage_for_ledger(llm))
    detail.update(outcome.report.summary())


def stage_validate(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Re-gate the stored note against the stored candidate pools.

    Stage 7 also runs *inside* stage 6 during a fresh draft, because the gate feeds the
    retry loop and a note that failed must never be persisted as though it had passed.
    This standalone form exists for the other direction — re-checking an artifact after
    the fact, which is what `pulse validate` does and what keeps a published note
    auditable later.
    """
    note = context.read_model(6, PulseNote)
    corpus_size = len(context.read_models(3, Review))
    markdown = render_note(note, corpus_size=corpus_size, generated_on=options.generated_on)

    result = validate_note(
        note,
        markdown,
        allowed_themes=[s.theme for s in note.top_themes],
        candidates_by_theme=_candidates_from_artifact(context),
    )
    detail.update(result.summary())

    if not result.ok:
        raise PipelineError(
            "The stored note no longer passes validation: " + "; ".join(result.messages())
        )


def stage_render(context: RunContext, options: PipelineOptions, detail: dict[str, object]) -> None:
    """Write `pulse.md` — the single source for both the doc and the email bodies."""
    note = context.read_model(6, PulseNote)
    corpus_size = len(context.read_models(3, Review))

    markdown = render_note(note, corpus_size=corpus_size, generated_on=options.generated_on)

    # Re-gated rather than trusted: this is the exact text that will be published, and the
    # cost of checking twice is microseconds.
    result = validate_note(
        note,
        markdown,
        allowed_themes=[s.theme for s in note.top_themes],
        candidates_by_theme=_candidates_from_artifact(context),
    )
    assert_publishable(result, markdown)

    path = context.write_note(markdown)
    detail.update({"path": str(path), "words": result.word_count})


def _candidates_from_artifact(context: RunContext) -> dict[Theme, list[QuoteCandidate]]:
    payload = context.read_artifact(5)
    selected = [ThemeStats.model_validate(item) for item in payload["selected"]]
    return {stats.theme: list(stats.quote_candidates) for stats in selected}


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------

StageFn = Callable[[RunContext, PipelineOptions, dict[str, object]], None]

_STAGES: dict[int, StageFn] = {
    1: stage_ingest,
    2: stage_normalize,
    3: stage_scrub,
    4: stage_classify,
    5: stage_aggregate,
    6: stage_draft,
    7: stage_validate,
    8: stage_render,
}


def stage_is_implemented(stage: int) -> bool:
    return stage in _STAGES


def run_stages(context: RunContext, options: PipelineOptions) -> None:
    """Execute stages `from_stage`..`to_stage` inclusive.

    Each stage reads its predecessor's artifact from disk rather than taking it in memory,
    which is what makes `--from` replay work identically to a fresh run.
    """
    if not FIRST_STAGE <= options.from_stage <= LAST_STAGE:
        raise PipelineError(f"--from must be {FIRST_STAGE}-{LAST_STAGE}.")
    if not FIRST_STAGE <= options.to_stage <= LAST_STAGE:
        raise PipelineError(f"--to must be {FIRST_STAGE}-{LAST_STAGE}.")
    if options.from_stage > options.to_stage:
        raise PipelineError(f"--from ({options.from_stage}) is after --to ({options.to_stage}).")

    for stage in range(options.from_stage, options.to_stage + 1):
        handler = _STAGES.get(stage)
        if handler is None:
            raise StageNotImplemented(
                f"Stage {stage} is not implemented yet; it arrives in "
                f"{_PENDING_STAGES.get(stage, 'a later phase')}. "
                f"Use --to {max(_STAGES)} to run what exists."
            )
        with context.stage(stage) as detail:
            handler(context, options, detail)
