"""P0-T9: every model accepts a valid payload and rejects an invalid one.

These tests are the first line of defence for the hard constraints in architecture.md §3.
The cardinality and bounds asserted here are not stylistic — they are what makes "at most
5 themes", "exactly 3 quotes", and "no out-of-scope source" structural rather than hoped
for. If one of these tests is ever loosened, a requirement is being loosened with it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

import pytest
from pydantic import ValidationError

from pulse.models import (
    ActionIdea,
    ClassifiedReview,
    LlmUsage,
    PulseNote,
    Quote,
    QuoteCandidate,
    RawReview,
    Review,
    RunLedger,
    Source,
    StageRecord,
    Theme,
    ThemeStats,
    ThemeSummary,
)

# --------------------------------------------------------------------------------------
# Builders — a valid baseline each test mutates one field of.
# --------------------------------------------------------------------------------------


def a_review(**overrides: object) -> dict[str, object]:
    return {
        "review_id": "a1b2c3d4e5f60718",
        "source": "play_store",
        "rating": 2,
        "text": "Withdrawal has been pending for four days with no update.",
        "submitted_on": "2026-10-07",
        "iso_week": "2026-W41",
    } | overrides


def a_quote(**overrides: object) -> dict[str, object]:
    return {
        "text": "Withdrawal has been pending for four days with no update.",
        "theme": "payments_withdrawals",
        "rating": 2,
        "review_id": "a1b2c3d4e5f60718",
    } | overrides


def a_theme_summary(**overrides: object) -> dict[str, object]:
    return {
        "theme": "payments_withdrawals",
        "summary": "Withdrawal delays dominate, mostly around settlement timing.",
        "review_count": 48,
        "direction": "up",
    } | overrides


def an_action(**overrides: object) -> dict[str, object]:
    return {
        "theme": "payments_withdrawals",
        "action": "Publish expected settlement timelines in the withdrawal flow.",
        "rationale": "Most complaints are about uncertainty, not the delay itself.",
    } | overrides


def a_note(**overrides: object) -> dict[str, object]:
    return {
        "iso_week": "2026-W41",
        "headline": "Withdrawal delays drive a quarter of this week's negative reviews",
        "top_themes": [
            a_theme_summary(),
            a_theme_summary(theme="app_reliability", direction="flat"),
            a_theme_summary(theme="onboarding_kyc", direction="down"),
        ],
        "quotes": [
            a_quote(),
            a_quote(review_id="1111111111111111", theme="app_reliability"),
            a_quote(review_id="2222222222222222", theme="onboarding_kyc"),
        ],
        "actions": [
            an_action(),
            an_action(theme="app_reliability"),
            an_action(theme="onboarding_kyc"),
        ],
    } | overrides


# --------------------------------------------------------------------------------------
# Source — the Play-Store-only constraint (D10)
# --------------------------------------------------------------------------------------


def test_source_has_exactly_one_member_while_ios_is_deferred() -> None:
    assert [source.value for source in Source] == ["play_store"]


def test_review_rejects_out_of_scope_source() -> None:
    """An adapter must not be able to emit an App Store review while iOS is deferred."""
    with pytest.raises(ValidationError):
        Review.model_validate(a_review(source="app_store"))


# --------------------------------------------------------------------------------------
# Theme — the <=5 constraint (D4)
# --------------------------------------------------------------------------------------


def test_theme_taxonomy_is_exactly_five_themes() -> None:
    """The enum *is* the "at most 5 themes" constraint. Adding a sixth breaks the brief."""
    assert len(list(Theme)) == 5


def test_theme_rejects_an_invented_theme() -> None:
    with pytest.raises(ValidationError):
        ClassifiedReview.model_validate(
            {"review_id": "x", "theme": "customer_delight", "confidence": 0.9}
        )


@pytest.mark.parametrize("enum_cls", [Source, Theme])
def test_enums_render_as_their_value_in_strings(enum_cls: type[StrEnum]) -> None:
    """Phase 2 renders theme names directly into the classifier prompt.

    With `(str, Enum)` instead of `StrEnum`, `f"{Theme.APP_RELIABILITY}"` would render as
    "Theme.APP_RELIABILITY" and silently corrupt that prompt with no error anywhere. This
    test exists so a future refactor back to `(str, Enum)` fails loudly here instead.
    """
    member = next(iter(enum_cls))
    assert str(member) == member.value
    assert f"{member}" == member.value
    assert ", ".join(m for m in enum_cls) == ", ".join(m.value for m in enum_cls)


# --------------------------------------------------------------------------------------
# RawReview
# --------------------------------------------------------------------------------------


def test_raw_review_accepts_sparse_export_rows() -> None:
    """Exports vary; only `source` is guaranteed at ingest time."""
    raw = RawReview.model_validate({"source": "play_store"})
    assert raw.rating is None
    assert raw.text is None


def test_raw_review_keeps_unknown_export_columns() -> None:
    """extra="allow" preserves columns a future adapter may need, without modelling them."""
    raw = RawReview.model_validate({"source": "play_store", "app_version": "5.2.1", "thumbs_up": 3})
    assert raw.model_extra == {"app_version": "5.2.1", "thumbs_up": 3}


def test_raw_review_requires_a_source() -> None:
    with pytest.raises(ValidationError):
        RawReview.model_validate({"text": "no source given"})


# --------------------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------------------


def test_review_round_trips() -> None:
    review = Review.model_validate(a_review())
    assert review.submitted_on == date(2026, 10, 7)
    assert review.scrubbed is True
    assert Review.model_validate(review.model_dump(mode="json")) == review


@pytest.mark.parametrize("rating", [0, 6, -1])
def test_review_rejects_out_of_range_rating(rating: int) -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(a_review(rating=rating))


def test_review_rejects_empty_text() -> None:
    """An empty review carries no signal and cannot be quoted; drop it at normalization."""
    with pytest.raises(ValidationError):
        Review.model_validate(a_review(text=""))


def test_review_has_no_field_for_reviewer_identity() -> None:
    """Structural PII defence (architecture.md §10, layer 1).

    There is nowhere on this model to put a username, so a careless adapter cannot carry
    identity forward even by mistake. Unknown keys are rejected rather than absorbed.
    """
    identity_fields = {"author", "user", "username", "reviewer_name", "email", "device_id"}
    assert identity_fields.isdisjoint(Review.model_fields)

    with pytest.raises(ValidationError):
        Review.model_validate(a_review(author="Some Person"))


@pytest.mark.parametrize(
    "bad_week",
    ["2026-41", "26-W41", "2026-W", "2026-W0", "2026-W00", "2026-W54", "2026W41", ""],
)
def test_review_rejects_malformed_iso_week(bad_week: str) -> None:
    """Guards the ISO-year boundary trap called out in the plan (P1)."""
    with pytest.raises(ValidationError):
        Review.model_validate(a_review(iso_week=bad_week))


@pytest.mark.parametrize("good_week", ["2026-W01", "2026-W41", "2026-W53", "2027-W01"])
def test_review_accepts_well_formed_iso_week(good_week: str) -> None:
    assert Review.model_validate(a_review(iso_week=good_week)).iso_week == good_week


# --------------------------------------------------------------------------------------
# ClassifiedReview
# --------------------------------------------------------------------------------------


def test_classified_review_allows_unclassified() -> None:
    """`None` means unclassified — it is not a sixth theme (architecture.md §8.1)."""
    classified = ClassifiedReview.model_validate(
        {"review_id": "x", "theme": None, "confidence": 0.2}
    )
    assert classified.theme is None


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_classified_review_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    with pytest.raises(ValidationError):
        ClassifiedReview.model_validate(
            {"review_id": "x", "theme": "app_reliability", "confidence": confidence}
        )


# --------------------------------------------------------------------------------------
# ThemeStats / Quote / ActionIdea / ThemeSummary
# --------------------------------------------------------------------------------------


def test_theme_stats_round_trips() -> None:
    stats = ThemeStats.model_validate(
        {
            "theme": "app_reliability",
            "review_count": 31,
            "share_of_week": 0.19,
            "mean_rating": 2.1,
            "delta_vs_baseline": 0.04,
            "quote_candidates": [
                {
                    "review_id": "c" * 16,
                    "text": "App crashes every time I open the chart.",
                    "rating": 1,
                }
            ],
        }
    )
    assert ThemeStats.model_validate(stats.model_dump(mode="json")) == stats


def test_quote_rejects_text_shorter_than_the_minimum() -> None:
    """Too short to be a meaningful quote on a one-page note."""
    with pytest.raises(ValidationError):
        Quote.model_validate(a_quote(text="Bad app"))


def test_quote_rejects_text_longer_than_the_maximum() -> None:
    with pytest.raises(ValidationError):
        Quote.model_validate(a_quote(text="x" * 241))


def test_action_idea_rejects_overlong_action() -> None:
    with pytest.raises(ValidationError):
        ActionIdea.model_validate(an_action(action="x" * 161))


def test_theme_summary_rejects_unknown_direction() -> None:
    with pytest.raises(ValidationError):
        ThemeSummary.model_validate(a_theme_summary(direction="sideways"))


# --------------------------------------------------------------------------------------
# PulseNote — the cardinality constraints from the brief
# --------------------------------------------------------------------------------------


def test_pulse_note_accepts_exactly_three_of_each() -> None:
    note = PulseNote.model_validate(a_note())
    assert len(note.top_themes) == len(note.quotes) == len(note.actions) == 3
    assert PulseNote.model_validate(note.model_dump(mode="json")) == note


@pytest.mark.parametrize("count", [0, 1, 2, 4, 5])
@pytest.mark.parametrize("field", ["top_themes", "quotes", "actions"])
def test_pulse_note_rejects_any_count_other_than_three(field: str, count: int) -> None:
    """The brief asks for exactly 3 themes, 3 quotes, and 3 actions — enforced by schema."""
    note = a_note()
    template = note[field][0]  # type: ignore[index]
    with pytest.raises(ValidationError):
        PulseNote.model_validate(note | {field: [template] * count})


def test_pulse_note_rejects_overlong_headline() -> None:
    with pytest.raises(ValidationError):
        PulseNote.model_validate(a_note(headline="x" * 101))


def test_pulse_note_schema_is_generatable_for_structured_outputs() -> None:
    """The model doubles as the structured-output schema, so it must serialize cleanly."""
    schema = PulseNote.model_json_schema()
    assert set(schema["required"]) == {
        "iso_week",
        "headline",
        "top_themes",
        "quotes",
        "actions",
    }


# --------------------------------------------------------------------------------------
# Ledger models
# --------------------------------------------------------------------------------------


def test_run_ledger_defaults_to_running_with_no_usage() -> None:
    ledger = RunLedger.model_validate(
        {"run_id": "2026-W41-01", "iso_week": "2026-W41", "started_at": datetime.now(UTC)}
    )
    assert ledger.status == "running"
    assert ledger.mcp_mode == "none"
    assert ledger.usage == LlmUsage()
    assert ledger.stages == []


def test_run_ledger_round_trips_through_json() -> None:
    ledger = RunLedger.model_validate(
        {
            "run_id": "2026-W41-02",
            "iso_week": "2026-W41",
            "started_at": datetime.now(UTC),
            "status": "ok",
            "mcp_mode": "stub",
            "corpus_size": 1203,
            "theme_counts": {"app_reliability": 31},
            "document_url": "https://docs.example/d/abc",
        }
    )
    assert RunLedger.model_validate_json(ledger.model_dump_json()) == ledger


@pytest.mark.parametrize("stage", [0, 11, -1])
def test_stage_record_rejects_out_of_range_stage(stage: int) -> None:
    with pytest.raises(ValidationError):
        StageRecord.model_validate(
            {
                "stage": stage,
                "name": "mystery",
                "status": "ok",
                "started_at": datetime.now(UTC),
                "duration_ms": 1,
            }
        )


def test_stage_record_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        StageRecord.model_validate(
            {
                "stage": 1,
                "name": "ingest",
                "status": "probably-fine",
                "started_at": datetime.now(UTC),
                "duration_ms": 1,
            }
        )


# --------------------------------------------------------------------------------------
# QuoteCandidate — provenance, added beyond §7
# --------------------------------------------------------------------------------------


def test_quote_candidate_carries_provenance() -> None:
    """Text alone made §9's provenance check unimplementable, and left `Quote.review_id`
    and `Quote.rating` as fields the model filled in freely — which it did, inventing ids
    like `r001` and guessing star ratings. Stage 7 now derives both from here."""
    candidate = QuoteCandidate.model_validate(
        {"review_id": "a" * 16, "text": "Withdrawal is stuck.", "rating": 2}
    )
    assert set(QuoteCandidate.model_fields) == {"review_id", "text", "rating"}
    assert candidate.rating == 2


@pytest.mark.parametrize("rating", [0, 6])
def test_quote_candidate_rejects_an_impossible_rating(rating: int) -> None:
    with pytest.raises(ValidationError):
        QuoteCandidate.model_validate({"review_id": "a" * 16, "text": "x", "rating": rating})
