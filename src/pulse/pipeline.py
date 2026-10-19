"""Stage dispatch: runs a contiguous range of pipeline stages (P1-T9).

Keeping dispatch here rather than in `orchestrator.py` preserves the layering rule from
architecture.md §5: `orchestrator` owns run directories and the ledger and imports nothing
from `ingest/` or `analysis/`; this module is where the stages are composed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Literal

import anyio

from pulse import logging as pulse_logging
from pulse.analysis.aggregate import AggregateError, aggregate, select_top_themes
from pulse.analysis.classify import (
    DEFAULT_CACHE_PATH,
    ClassificationCache,
    classify_reviews,
)
from pulse.analysis.draft import DraftError, draft_note
from pulse.analysis.scrub import assert_clean, scrub_reviews
from pulse.config import ConfigError, Settings, require_document_id, require_recipient
from pulse.ingest.base import build_source
from pulse.ingest.normalize import (
    NormalizeError,
    filter_substantive,
    filter_to_window,
    normalize,
    validate_window,
)
from pulse.llm import LlmPort, build_llm, usage_for_ledger
from pulse.mcp.adapters import ROLES, McpMode, connect_docs, connect_gmail, probe
from pulse.mcp.contract import ErrorCode
from pulse.mcp.errors import McpClientError, McpToolError
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
from pulse.publish.docs import PublishedSection, previous_publication, publish_section
from pulse.publish.gmail import create_pulse_draft
from pulse.publish.ports import Block, DraftRef
from pulse.rendering.render import email_subject, render_blocks, render_email_body, render_note
from pulse.rendering.validate import assert_publishable, validate_note

log = pulse_logging.get_logger("pipeline")

FIRST_STAGE = 1
LAST_STAGE = 10
DOC_STAGE = 9
EMAIL_STAGE = 10


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
    #: Which MCP servers stages 9-10 publish through. "none" makes them unreachable.
    mcp_mode: Literal["none", "stub", "live"] = "none"
    #: Append the week's section even if a previous run already published it.
    republish: bool = False


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
    if context.ledger.raw_artifact_deleted and not context.artifact_path(1).exists():
        raise PipelineError(
            "01_raw.json was deleted after this run succeeded (retention, O6). "
            "Re-ingest with `--from 1`, or set retention.keep_raw_artifact = true."
        )
    raw_reviews = context.read_models(1, RawReview)

    reviews, report = normalize(raw_reviews)
    windowed, window = filter_to_window(
        reviews, options.target_week, options.settings.pulse.window.weeks
    )

    # Substance filtering runs after windowing so both counts are available: what the
    # window received, and how much of it says anything. The note discloses both.
    substantive, substance = filter_substantive(
        windowed, min_words=options.settings.pulse.thresholds.min_review_words
    )

    try:
        present = validate_window(substantive, window, options.settings.pulse.window.min_weeks)
    except NormalizeError as exc:
        raise PipelineError(str(exc)) from exc

    context.write_artifact(2, substantive)
    context.ledger.corpus_size = len(substantive)
    context.ledger.corpus_total = len(windowed)
    context.ledger.window_weeks = len(window)
    detail.update(
        {
            **report.summary(),
            "window": f"{window[0]}..{window[-1]}",
            "weeks_in_corpus": len(present),
            "reviews_in_window": len(windowed),
            "substance": substance.summary(),
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

    before = usage_for_ledger(llm)
    classified, report = classify_reviews(reviews, llm=llm, settings=options.settings, cache=cache)

    context.write_artifact(4, classified)
    context.ledger.theme_counts = dict(report.theme_counts)
    context.ledger.classification_cache_hit_rate = round(report.cache_hit_rate, 3)
    _record_usage(context, "classify", before, usage_for_ledger(llm))
    detail.update(report.summary())


def _record_usage(
    context: RunContext, stage_name: str, before: dict[str, Any], after: dict[str, Any]
) -> None:
    """Store what one stage spent, and recompute the run total from every stage.

    A difference of snapshots, not the provider's running total, so a port shared across
    stages (as tests inject one) is not counted twice.
    """
    counters = ("calls", "input_tokens", "output_tokens", "cached_input_tokens")
    spent: dict[str, Any] = {
        "provider": after["provider"],
        "model": after["model"],
        **{key: after.get(key, 0) - before.get(key, 0) for key in counters},
    }
    cost_after, cost_before = after.get("estimated_cost_usd"), before.get("estimated_cost_usd")
    spent["estimated_cost_usd"] = (
        cost_after - (cost_before or 0.0) if cost_after is not None else None
    )
    context.ledger.usage_by_stage[stage_name] = LlmUsage.model_validate(spent)

    stages = list(context.ledger.usage_by_stage.values())
    costs = [s.estimated_cost_usd for s in stages]
    context.ledger.usage = LlmUsage(
        provider=after["provider"],
        model=after["model"],
        **{key: sum(getattr(s, key) for s in stages) for key in counters},
        estimated_cost_usd=None if any(c is None for c in costs) else sum(costs),  # type: ignore[arg-type]
    )


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
    corpus_total = context.ledger.corpus_total

    llm = (
        options.llm
        if options.llm is not None
        else build_llm(options.settings, override=options.llm_override)
    )
    before = usage_for_ledger(llm)

    try:
        outcome = draft_note(
            selected,
            llm=llm,
            settings=options.settings,
            iso_week=options.target_week,
            corpus_size=corpus_size,
            corpus_total=corpus_total,
            generated_on=options.generated_on,
        )
    except DraftError as exc:
        # Every attempt's failures are persisted before the abort, so a developer can see
        # what the model actually got wrong rather than guessing from a log line.
        context.write_artifact(7, {"status": "aborted", "reason": str(exc)})
        # A rejected draft still cost tokens.
        _record_usage(context, "draft", before, usage_for_ledger(llm))
        # A replay that fails must not leave the previous draft behind: stages 9-10 read
        # these files, so a later `--from 9` would publish a note this run just rejected.
        context.artifact_path(6).unlink(missing_ok=True)
        context.note_path.unlink(missing_ok=True)
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
    _record_usage(context, "draft", before, usage_for_ledger(llm))
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
    markdown = render_note(
        note,
        corpus_size=corpus_size,
        corpus_total=context.ledger.corpus_total,
        generated_on=options.generated_on,
    )

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

    # corpus_total is passed so the note discloses the substantive subset against the whole
    # window (§5.1). Omitting it silently dropped that disclosure from every pulse.md.
    markdown = render_note(
        note,
        corpus_size=corpus_size,
        corpus_total=context.ledger.corpus_total,
        generated_on=options.generated_on,
    )

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
# Stages 9-10 — publish through MCP
# --------------------------------------------------------------------------------------


def _require_mcp(options: PipelineOptions) -> McpMode:
    if options.mcp_mode == "none":
        raise PipelineError(
            "Stages 9-10 publish through MCP; choose `--mcp stub` or `--mcp live`, "
            "or stop at stage 8 with `--dry-run`."
        )
    return options.mcp_mode


def _publishable_markdown(context: RunContext) -> str:
    """`pulse.md`, re-scanned for PII: it is a file on disk and could have been edited."""
    if not context.note_path.exists():
        raise PipelineError(
            f"{context.note_path} is missing; run stages 6-8 (or `--from 6`) first."
        )
    markdown = context.note_path.read_text(encoding="utf-8")
    assert_clean(markdown, context=context.note_path.name)
    return markdown


def _doc_blocks(context: RunContext, options: PipelineOptions) -> tuple[PulseNote, list[Block]]:
    """The note as blocks, held to the same guarantees as `pulse.md` before it leaves."""
    note = context.read_model(6, PulseNote)
    blocks = render_blocks(
        note,
        corpus_size=len(context.read_models(3, Review)),
        corpus_total=context.ledger.corpus_total,
        generated_on=options.generated_on,
    )
    text = "\n".join(block.text for block in blocks)
    assert_clean(text, context="the Google Doc blocks")
    # A second render path must not undo stage 7's verbatim guarantee (P5-T5).
    for quote in note.quotes:
        if not any(quote.text in block.text for block in blocks):
            raise PipelineError(f"Quote from {quote.review_id} did not survive block rendering.")
    return note, blocks


def stage_publish_doc(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Append the week's section to the standing Google Doc (implementation-plan.md §8.2)."""
    mode = _require_mcp(options)
    _publishable_markdown(context)
    try:
        document_id = require_document_id(options.settings)
    except ConfigError as exc:
        raise PipelineError(str(exc)) from exc
    _, blocks = _doc_blocks(context, options)
    previous = previous_publication(context.run_dir.parent, document_id, context.iso_week)

    async def publish() -> PublishedSection:
        async with connect_docs(options.settings, mode) as docs:
            return await publish_section(
                docs,
                document_id=document_id,
                iso_week=context.iso_week,
                heading=blocks[0].text,
                blocks=blocks,
                previously_published_in=previous,
                republish=options.republish,
            )

    try:
        published = anyio.run(publish)
    except McpClientError as exc:
        raise PipelineError(str(exc)) from exc

    if published.status == "skipped":
        log.warning(
            "week already published; not appending again",
            extra={"iso_week": context.iso_week, "run": published.previously_published_in},
        )
    elif published.status == "dry_run":
        log.warning("the MCP server is in DRY_RUN mode: nothing was written to the doc")

    # A skip must never overwrite the record of a real publication. Replaying into the run
    # that published would otherwise replace "appended" with "skipped" -- erasing the only
    # evidence the week is in the doc, so the next run would append it again.
    if published.previously_published_in != context.run_dir.name or published.status != "skipped":
        context.write_artifact(9, published.summary())
    context.ledger.document_url = published.url
    detail.update(published.summary())


def _earlier_draft(context: RunContext, document_url: str) -> dict[str, str] | None:
    """The draft already made for this week's publication, if any.

    The publication is this run's own (stage 9 appended here) or an earlier run's (stage 9
    skipped, pointing at it). Either way, a draft that run already made is not repeated --
    but only if it links to the same doc. Found on the first live run: the scratch-doc
    draft would otherwise have stopped the standing doc from ever getting one.
    """
    if not context.artifact_path(9).exists():
        return None
    published = context.read_artifact(9)
    if published.get("status") == "appended":
        publisher = context.run_dir.name
    elif published.get("status") == "skipped":
        publisher = published.get("previously_published_in") or ""
    else:
        return None
    path = context.run_dir.parent / publisher / context.artifact_path(10).name
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    draft_id = record.get("draft_id")
    if not draft_id or record.get("document_url") != document_url:
        return None
    return {"draft_id": draft_id, "drafted_in": publisher}


def stage_draft_email(
    context: RunContext, options: PipelineOptions, detail: dict[str, object]
) -> None:
    """Create the Gmail draft: the full note plus the doc link. Never sends."""
    mode = _require_mcp(options)
    try:
        to = require_recipient(options.settings)
    except ConfigError as exc:
        raise PipelineError(str(exc)) from exc

    document_url = context.ledger.document_url
    if not document_url:
        raise PipelineError(
            "No published document is recorded for this run; run stage 9 first "
            "(`pulse run --from 9`)."
        )
    note = context.read_model(6, PulseNote)
    body = render_email_body(note, _publishable_markdown(context), document_url)
    subject = email_subject(note)

    # A week already published was drafted then too; a second draft is just clutter.
    if (earlier := _earlier_draft(context, document_url)) and not options.republish:
        log.warning("week already drafted; not creating another", extra=earlier)
        record: dict[str, object] = {"status": "skipped", **earlier, "subject": subject}
        if earlier["drafted_in"] != context.run_dir.name:
            context.write_artifact(10, record)
        detail.update(record)
        return

    async def draft() -> DraftRef:
        async with connect_gmail(options.settings, mode) as gmail:
            return await create_pulse_draft(gmail, to=to, subject=subject, body=body)

    try:
        created = anyio.run(draft)
    except McpClientError as exc:
        raise PipelineError(str(exc)) from exc

    if created.dry_run:
        log.warning("the MCP server is in DRY_RUN mode: no draft was created")
    created_record: dict[str, object] = {
        "status": "dry_run" if created.dry_run else "created",
        "draft_id": created.draft_id,
        "dry_run": created.dry_run,
        "subject": subject,
        "document_url": document_url,
    }
    context.write_artifact(10, created_record)
    context.ledger.draft_id = created.draft_id
    detail.update(created_record)


# --------------------------------------------------------------------------------------
# Startup probe (P4-T13, P5-T7)
# --------------------------------------------------------------------------------------

_PROBE_ADVICE: dict[ErrorCode, str] = {
    ErrorCode.AUTH_REQUIRED: (
        "The MCP server is not authorized with Google. Re-run `npm run auth` for the server "
        "and update its GOOGLE_TOKEN_JSON_B64 -- this agent never handles Google credentials."
    ),
    ErrorCode.NOT_FOUND: (
        "The configured Google Doc was not found, or is not shared with the account the MCP "
        "server is authorized as. Check PULSE_DOC_ID."
    ),
    ErrorCode.PERMISSION_DENIED: (
        "The MCP server refused access to the configured doc (no edit access, or not on its "
        "ALLOWED_DOCUMENT_IDS)."
    ),
    ErrorCode.FEATURE_DISABLED: (
        "A tool this agent needs is disabled on the server; it needs ENABLE_DOCS_APPEND and "
        "ENABLE_GMAIL_DRAFTS set to true."
    ),
}


def preflight(context: RunContext, options: PipelineOptions) -> None:
    """Prove the MCP stages can succeed *before* any stage runs.

    Discovering an unauthorized server after classifying 1,700 reviews would waste the
    whole run's spend (architecture.md §11.6), so this runs ahead of stage 1, not stage 9.
    """
    publishes = options.from_stage <= DOC_STAGE <= options.to_stage
    drafts = options.from_stage <= EMAIL_STAGE <= options.to_stage
    if not (publishes or drafts):
        return

    mode = _require_mcp(options)
    try:
        document_id = require_document_id(options.settings) if publishes else None
        if drafts:
            require_recipient(options.settings)
    except ConfigError as exc:
        raise PipelineError(str(exc)) from exc

    roles = (ROLES["docs"] if publishes else ()) + (ROLES["gmail"] if drafts else ())
    result = anyio.run(partial(probe, options.settings, mode, roles, document_id=document_id))
    for warning in result.warnings:
        log.warning("mcp probe warning", extra={"detail": warning})
    if result.ok:
        log.info("mcp probe ok", extra={"detail": result.detail})
        return

    error = result.error
    advice = _PROBE_ADVICE.get(error.code) if isinstance(error, McpToolError) else None
    raise PipelineError("MCP startup probe failed: " + (advice or result.detail))


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
    9: stage_publish_doc,
    10: stage_draft_email,
}


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

    preflight(context, options)

    for stage in range(options.from_stage, options.to_stage + 1):
        with context.stage(stage) as detail:
            _STAGES[stage](context, options, detail)

    _prune_raw_artifact(context, options)


def _prune_raw_artifact(context: RunContext, options: PipelineOptions) -> None:
    """Delete `01_raw.json` once the run has succeeded past the scrubber (O6, P6-T7).

    It is the only run artifact that may hold PII as exported. Kept when a run fails (this
    is not reached) and when the run stopped before stage 3, so ingestion can still be
    debugged; the export in `data/raw/` rebuilds it either way.
    """
    raw = context.artifact_path(1)
    if options.settings.pulse.retention.keep_raw_artifact or not raw.exists():
        return
    if not context.artifact_path(3).exists():
        return
    raw.unlink()
    context.ledger.raw_artifact_deleted = True
    context.save_ledger()
    log.info("raw artifact deleted after a successful run", extra={"path": str(raw)})
