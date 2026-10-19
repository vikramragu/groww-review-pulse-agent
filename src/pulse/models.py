"""Data model — the contract between pipeline stages.

Transcribed from architecture.md §7. These models are also the schemas handed to Claude for
structured outputs, which is why the cardinality constraints live here rather than in a
post-hoc check: the schema both constrains generation and validates the result.

Three additions beyond §7, all noted inline: `IsoWeek`, `StageRecord`, and `RunLedger`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.json_schema import SkipJsonSchema

# Addition (not in §7): ISO week strings are compared and sorted all over the pipeline, and
# architecture.md §10 / plan P1 both flag the ISO-year boundary as a silent-corruption trap
# (2026-12-31 belongs to ISO week 2027-W01). Constraining the format here turns a
# wrong-but-plausible value into a validation error at the stage boundary that produced it.
IsoWeek = Annotated[str, StringConstraints(pattern=r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")]


#
# `StrEnum` rather than `(str, Enum)` as architecture.md §7 writes it. The difference is
# not cosmetic: with `(str, Enum)`, `f"{Theme.APP_RELIABILITY}"` renders as
# "Theme.APP_RELIABILITY", not "app_reliability". Phase 2 renders theme names straight
# into the classifier prompt, so that spelling would silently degrade classification with
# no error anywhere. Pydantic's serialized JSON and generated schema are byte-identical
# either way, so structured outputs are unaffected.
#
class Source(StrEnum):
    """Review origin. Play Store only for now (D10); iOS would add APP_STORE here."""

    PLAY_STORE = "play_store"


class Theme(StrEnum):
    """Closed taxonomy. Exactly five themes — this enum *is* the <=5 constraint."""

    ONBOARDING_KYC = "onboarding_kyc"
    ORDERS_EXECUTION = "orders_execution"
    PAYMENTS_WITHDRAWALS = "payments_withdrawals"
    APP_RELIABILITY = "app_reliability"
    CHARGES_SUPPORT = "charges_support"


class RawReview(BaseModel):
    """Whatever the export provided, untyped and unvalidated beyond presence."""

    model_config = ConfigDict(extra="allow")

    source: Source  # only PLAY_STORE while iOS is deferred (D10)
    external_id: str | None = None
    rating: int | None = None
    title: str | None = None
    text: str | None = None
    submitted_at: str | None = None


class Review(BaseModel):
    """Normalized, scrubbed. No reviewer identity fields exist on this model by design.

    `extra="forbid"` (an addition beyond §7) makes the structural PII defence loud rather
    than silent. Pydantic's default would quietly drop an `author=` an adapter passed in —
    safe, but it hides the bug. Rejecting it surfaces the mistake at the stage boundary
    that made it, which is where it can still be fixed.
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str  # content hash — stable, non-identifying
    source: Source
    rating: Annotated[int, Field(ge=1, le=5)]
    text: Annotated[str, StringConstraints(min_length=1)]
    submitted_on: date
    iso_week: IsoWeek
    scrubbed: bool = True


class ClassifiedReview(BaseModel):
    review_id: str
    theme: Theme | None  # None == unclassified; not a 6th theme
    confidence: Annotated[float, Field(ge=0, le=1)]


class QuoteCandidate(BaseModel):
    """A quotable review, with the provenance needed to verify a quote drawn from it.

    Addition beyond §7, which typed `quote_candidates` as `list[str]`. Text alone makes
    §9's provenance check *unimplementable* — and worse, it leaves `Quote.review_id` and
    `Quote.rating` as fields the model fills in freely. Observed in practice: the model
    invented ids (`r001`, `r002`) and guessed star ratings, publishing fabricated
    provenance beside a correctly verified quote. Carrying the facts here lets stage 7
    derive them instead of trusting them.
    """

    review_id: str
    text: str
    rating: Annotated[int, Field(ge=1, le=5)]


class ThemeStats(BaseModel):
    theme: Theme
    review_count: int
    share_of_week: float
    mean_rating: float
    delta_vs_baseline: float  # share change vs trailing weeks
    quote_candidates: list[QuoteCandidate]  # verbatim, scrubbed, ranked, with provenance
    #: How many trailing weeks `delta_vs_baseline` is measured against; 0 = no baseline.
    baseline_weeks: int = 0


#: A delta inside this band (as a fraction of the week, so 1 percentage point) is "flat".
FLAT_BAND = 0.01


def direction_of(delta_vs_baseline: float) -> Literal["up", "down", "flat"]:
    """The one definition of a theme's direction, shared by the prompt and the gate."""
    if delta_vs_baseline > FLAT_BAND:
        return "up"
    if delta_vs_baseline < -FLAT_BAND:
        return "down"
    return "flat"


class Quote(BaseModel):
    text: Annotated[str, StringConstraints(min_length=20, max_length=240)]
    theme: Theme
    rating: Annotated[int, Field(ge=1, le=5)]
    review_id: str  # provenance for the verbatim check


class ActionIdea(BaseModel):
    theme: Theme
    action: Annotated[str, StringConstraints(max_length=160)]
    rationale: Annotated[str, StringConstraints(max_length=200)]


class ThemeFigures(BaseModel):
    """A theme's numbers exactly as stage 5 computed them. Never written by the model."""

    review_count: int
    share_of_week: float
    delta_vs_baseline: float
    baseline_weeks: int
    mean_rating: float


class ThemeSummary(BaseModel):
    theme: Theme
    summary: Annotated[str, StringConstraints(max_length=220)]
    review_count: int
    direction: Literal["up", "down", "flat"]
    #: Filled from `ThemeStats` before rendering (`ground_theme_summaries`), the same way
    #: quote provenance is. Hidden from the model's schema: a model asked to restate
    #: figures it was given mislabels them, and the first real note did ("+5.6% week-over-
    #: week" for a 5.6-point change against an 11-week baseline).
    figures: SkipJsonSchema[ThemeFigures | None] = None


class PulseNote(BaseModel):
    """The LLM's structured output. Cardinality constraints are enforced here."""

    iso_week: IsoWeek
    headline: Annotated[str, StringConstraints(max_length=100)]
    top_themes: Annotated[list[ThemeSummary], Field(min_length=3, max_length=3)]
    quotes: Annotated[list[Quote], Field(min_length=3, max_length=3)]
    actions: Annotated[list[ActionIdea], Field(min_length=3, max_length=3)]


# --------------------------------------------------------------------------------------
# Run ledger (addition, not in §7): architecture.md §16 specifies a per-run ledger at
# runs/<id>/run.json. Typing it here keeps the orchestrator's bookkeeping honest and makes
# the observability requirements checkable rather than aspirational.
# --------------------------------------------------------------------------------------


class StageRecord(BaseModel):
    """One stage boundary — what ran, how long it took, how it ended."""

    stage: Annotated[int, Field(ge=1, le=10)]
    name: str
    status: Literal["ok", "skipped", "failed"]
    started_at: datetime
    duration_ms: int
    detail: dict[str, object] = Field(default_factory=dict)


class LlmUsage(BaseModel):
    """Token accounting for the ledger. Provider-neutral.

    `estimated_cost_usd` is `None` rather than `0.0` when no price is known for the model:
    reporting an unknown cost as free would be worse than reporting nothing, since a run
    that looks free gets repeated without thought.

    `cached_input_tokens` is whatever prompt-cache reads the provider reports. It stays 0
    on providers with no cache breakpoint (Groq), which is expected rather than a problem —
    the on-disk classification cache is what makes re-runs cheap (architecture.md §8.1).
    """

    provider: str = ""
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: float | None = None


class RunLedger(BaseModel):
    """The audit trail for one run. Written incrementally so a crash still leaves evidence."""

    run_id: str
    iso_week: IsoWeek
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["running", "ok", "aborted", "partial"] = "running"
    mcp_mode: Literal["none", "stub", "live"] = "none"

    #: Reviews that survived substance filtering -- the analysable corpus.
    corpus_size: int | None = None
    #: Reviews the window actually received, before substance filtering. Kept because the
    #: filters are sample selection: the note must disclose both, or a theme share gets
    #: read as a population share.
    corpus_total: int | None = None
    window_weeks: int | None = None
    theme_counts: dict[str, int] = Field(default_factory=dict)

    #: The run's total: the sum of `usage_by_stage`.
    usage: LlmUsage = Field(default_factory=LlmUsage)
    #: Per model-calling stage ("classify", "draft"). A replay of a stage replaces its
    #: entry. Kept apart so replaying stage 6 cannot erase stage 4's tokens, which a single
    #: overwritten `usage` did.
    usage_by_stage: dict[str, LlmUsage] = Field(default_factory=dict)
    #: Share of reviews stage 4 served from the on-disk classification cache.
    classification_cache_hit_rate: float | None = None
    draft_attempts: int = 0

    document_url: str | None = None
    draft_id: str | None = None
    #: `01_raw.json` was deleted after the run succeeded (O6), so `--from 1` or `--from 2`
    #: into this run means re-ingesting from the export.
    raw_artifact_deleted: bool = False

    stages: list[StageRecord] = Field(default_factory=list)
    abort_reason: str | None = None
