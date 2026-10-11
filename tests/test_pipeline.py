"""Stages 1-3 wired together (P1-T9), exercised against the committed fixture.

The two tests that matter most here are the artifact-level privacy assertions: they read
`03_scrubbed.json` the way a reviewer would and check that nothing identifying survived the
whole pipeline, not just a single call to `scrub_text`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.analysis.scrub import has_pii
from pulse.config import Settings, load_settings
from pulse.llm import FakeLlm, LlmNotConfigured, NullLlm
from pulse.models import ClassifiedReview, RawReview, Review, Theme
from pulse.orchestrator import RunContext, start_run
from pulse.pipeline import (
    PipelineError,
    PipelineOptions,
    StageNotImplemented,
    run_stages,
    stage_is_implemented,
)

FIXTURE = Path("tests/fixtures/reviews_sample.json")
FIXTURE_WEEK = "2026-W41"


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


def options(settings: Settings, tmp_path: Path, **overrides: object) -> PipelineOptions:
    base: dict[str, object] = {
        "settings": settings,
        "input_path": FIXTURE,
        "target_week": FIXTURE_WEEK,
        "from_stage": 1,
        "to_stage": 3,
    }
    base.update(overrides)
    return PipelineOptions(**base)  # type: ignore[arg-type]


def run_to_scrub(settings: Settings, tmp_path: Path) -> object:
    context = start_run(FIXTURE_WEEK, tmp_path)
    run_stages(context, options(settings, tmp_path))
    return context


# --------------------------------------------------------------------------------------
# Stage availability
# --------------------------------------------------------------------------------------


def test_stages_one_to_four_are_implemented() -> None:
    assert [stage for stage in range(1, 11) if stage_is_implemented(stage)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("stage", "phase"),
    [(5, "Phase 3"), (7, "Phase 3"), (9, "Phase 4"), (10, "Phase 4")],
)
def test_a_later_stage_names_the_phase_that_delivers_it(
    settings: Settings, tmp_path: Path, stage: int, phase: str
) -> None:
    """Dispatch is checked before any artifact is read, so this needs no earlier stages."""
    context = start_run(FIXTURE_WEEK, tmp_path)
    with pytest.raises(StageNotImplemented, match=phase):
        run_stages(context, options(settings, tmp_path, from_stage=stage, to_stage=stage))


def test_stage_range_is_validated(settings: Settings, tmp_path: Path) -> None:
    context = start_run(FIXTURE_WEEK, tmp_path)

    with pytest.raises(PipelineError, match="--from"):
        run_stages(context, options(settings, tmp_path, from_stage=0))
    with pytest.raises(PipelineError, match="--to"):
        run_stages(context, options(settings, tmp_path, to_stage=11))
    with pytest.raises(PipelineError, match="is after"):
        run_stages(context, options(settings, tmp_path, from_stage=3, to_stage=2))


# --------------------------------------------------------------------------------------
# Happy path over the committed fixture
# --------------------------------------------------------------------------------------


def test_stages_one_to_three_write_their_artifacts(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)

    assert context.completed_stages() == [1, 2, 3]  # type: ignore[attr-defined]
    for stage, name in ((1, "01_raw.json"), (2, "02_normalized.json"), (3, "03_scrubbed.json")):
        assert context.artifact_path(stage).name == name  # type: ignore[attr-defined]


def test_the_corpus_narrows_at_each_stage(settings: Settings, tmp_path: Path) -> None:
    """Raw rows -> normalized reviews -> scrubbed reviews, each a subset of the last."""
    context = run_to_scrub(settings, tmp_path)

    raw = context.read_models(1, RawReview)  # type: ignore[attr-defined]
    normalized = context.read_models(2, Review)  # type: ignore[attr-defined]
    scrubbed = context.read_models(3, Review)  # type: ignore[attr-defined]

    assert len(raw) > len(normalized) >= len(scrubbed) > 100
    assert {review.review_id for review in scrubbed} <= {r.review_id for r in normalized}


def test_the_fixture_spans_the_configured_window(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)
    reviews = context.read_models(2, Review)  # type: ignore[attr-defined]

    weeks = {review.iso_week for review in reviews}
    assert len(weeks) >= settings.pulse.window.min_weeks
    assert max(weeks) == FIXTURE_WEEK


def test_duplicates_in_the_fixture_are_collapsed(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)
    reviews = context.read_models(2, Review)  # type: ignore[attr-defined]

    ids = [review.review_id for review in reviews]
    assert len(ids) == len(set(ids))


def test_every_normalized_rating_is_in_range(settings: Settings, tmp_path: Path) -> None:
    """The fixture contains 0 and 7; both must have been clamped."""
    context = run_to_scrub(settings, tmp_path)
    reviews = context.read_models(2, Review)  # type: ignore[attr-defined]

    assert all(1 <= review.rating <= 5 for review in reviews)


# --------------------------------------------------------------------------------------
# Privacy, asserted at the artifact level
# --------------------------------------------------------------------------------------


def test_no_review_in_the_scrubbed_artifact_contains_pii(
    settings: Settings, tmp_path: Path
) -> None:
    """The headline guarantee, checked on what actually lands on disk."""
    context = run_to_scrub(settings, tmp_path)
    reviews = context.read_models(3, Review)  # type: ignore[attr-defined]

    offenders = [review.review_id for review in reviews if has_pii(review.text)]
    assert offenders == [], f"{len(offenders)} scrubbed review(s) still contain PII"


def test_the_scrubbed_artifact_contains_no_fixture_pii_values(
    settings: Settings, tmp_path: Path
) -> None:
    """Belt-and-braces: grep the raw file for the exact values the fixture planted."""
    context = run_to_scrub(settings, tmp_path)
    scrubbed_text = context.artifact_path(3).read_text(encoding="utf-8")  # type: ignore[attr-defined]

    planted = [
        "notareal@example.com",
        "also.notreal@example.org",
        "+91 90000 00000",
        "9000000000",
        "ABCDE1234F",
        "2994 1908 4318",
        "123456789012",
        "notareal@okaxis",
        "https://example.invalid",
        "@notarealhandle",
        "Not A Realperson",
    ]
    for value in planted:
        assert value not in scrubbed_text, f"{value!r} survived into 03_scrubbed.json"


def test_the_fixture_really_does_contain_the_pii_we_claim_to_remove() -> None:
    """Without this, the test above could pass against a fixture with no PII at all."""
    raw = FIXTURE.read_text(encoding="utf-8")
    for value in ("notareal@example.com", "ABCDE1234F", "2994 1908 4318", "Not A Realperson"):
        assert value in raw, f"fixture no longer plants {value!r}"


def test_identity_columns_never_reach_any_artifact(settings: Settings, tmp_path: Path) -> None:
    """Known identity columns are dropped at ingest, not merely at normalization.

    Architecture.md §18 only requires that identity stop at stage 2, accepting that
    `01_raw.json` "may contain PII as exported". The adapter is stricter: `_unmapped()`
    excludes `IDENTITY_COLUMNS`, so a `userName` or `device_id` column is dropped before
    anything is written. That shrinks the sensitive surface of the one retained raw
    artifact for free, so it is worth keeping and asserting.
    """
    context = run_to_scrub(settings, tmp_path)

    for stage in (1, 2, 3):
        text = context.artifact_path(stage).read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert "fake-device-0001" not in text, f"identity value reached stage {stage}"
        assert "device_id" not in text, f"identity column reached stage {stage}"
        assert "userName" not in text and "user_name" not in text


def test_the_raw_artifact_still_holds_unscrubbed_review_text(
    settings: Settings, tmp_path: Path
) -> None:
    """Why `01_raw.json` remains the one sensitive artifact, and why O6 still matters.

    Dropping identity *columns* does not make the raw artifact safe: PII written inside the
    review body is untouched until stage 3. Retention of this file is open decision O6,
    settled in P6-T7.
    """
    context = run_to_scrub(settings, tmp_path)

    raw_text = context.artifact_path(1).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    normalized_text = context.artifact_path(2).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    scrubbed_text = context.artifact_path(3).read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert "notareal@example.com" in raw_text
    assert "notareal@example.com" in normalized_text
    assert "notareal@example.com" not in scrubbed_text


def test_every_scrubbed_review_is_flagged_scrubbed(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)
    reviews = context.read_models(3, Review)  # type: ignore[attr-defined]

    assert all(review.scrubbed for review in reviews)
    assert not any(review.scrubbed for review in context.read_models(2, Review))  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


def test_the_ledger_records_each_stage(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)
    ledger = json.loads((context.run_dir / "run.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]

    assert [record["stage"] for record in ledger["stages"]] == [1, 2, 3]
    assert all(record["status"] == "ok" for record in ledger["stages"])
    assert ledger["corpus_size"] > 100
    assert ledger["window_weeks"] == settings.pulse.window.weeks


def test_the_ledger_contains_no_review_text(settings: Settings, tmp_path: Path) -> None:
    """Architecture.md §16: the ledger is counts and identifiers, never content."""
    context = run_to_scrub(settings, tmp_path)
    ledger_text = (context.run_dir / "run.json").read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert "notareal" not in ledger_text
    assert "Withdrawal has been pending" not in ledger_text


def test_stage_detail_reports_what_was_skipped(settings: Settings, tmp_path: Path) -> None:
    context = run_to_scrub(settings, tmp_path)
    ledger = json.loads((context.run_dir / "run.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]

    ingest, normalize_record, scrub = ledger["stages"]
    assert ingest["detail"]["rows_skipped"]
    assert normalize_record["detail"]["duplicates_removed"] >= 1
    assert normalize_record["detail"]["ratings_clamped"] == 2
    assert scrub["detail"]["pii_hits"] > 0


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def test_a_later_stage_can_be_replayed_from_stored_artifacts(
    settings: Settings, tmp_path: Path
) -> None:
    """`--from` reads the previous stage off disk, which is what makes replay identical."""
    context = start_run(FIXTURE_WEEK, tmp_path)
    run_stages(context, options(settings, tmp_path, to_stage=2))

    first = context.artifact_path(2).read_bytes()
    run_stages(context, options(settings, tmp_path, from_stage=3, to_stage=3))

    assert context.artifact_path(2).read_bytes() == first
    assert context.completed_stages() == [1, 2, 3]


def test_replaying_without_the_previous_artifact_fails_clearly(
    settings: Settings, tmp_path: Path
) -> None:
    from pulse.orchestrator import RunError

    context = start_run(FIXTURE_WEEK, tmp_path)
    with pytest.raises(RunError, match="--from"):
        run_stages(context, options(settings, tmp_path, from_stage=3, to_stage=3))


def test_scrub_output_is_byte_stable_across_runs(settings: Settings, tmp_path: Path) -> None:
    """Deterministic artifacts are what make the P6 golden test possible."""
    first = run_to_scrub(settings, tmp_path)
    second = run_to_scrub(settings, tmp_path)

    assert first.artifact_path(3).read_bytes() == second.artifact_path(3).read_bytes()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Window failure
# --------------------------------------------------------------------------------------


def test_a_too_narrow_corpus_aborts_the_run(settings: Settings, tmp_path: Path) -> None:
    narrow = tmp_path / "narrow.json"
    narrow.write_text(
        json.dumps(
            [
                {"rating": 2, "text": "Only one week of reviews here.", "date": "2026-10-07"},
                {"rating": 3, "text": "Also from the same week only.", "date": "2026-10-08"},
            ]
        ),
        encoding="utf-8",
    )

    context = start_run(FIXTURE_WEEK, tmp_path)
    with pytest.raises(PipelineError, match="spans 1 week"):
        run_stages(context, options(settings, tmp_path, input_path=narrow))


# --------------------------------------------------------------------------------------
# Stage 4 — classification, with an injected FakeLlm so nothing is spent
# --------------------------------------------------------------------------------------


def fake_classifier(reviews_per_batch: int, total: int) -> FakeLlm:
    """A FakeLlm scripted to label every batch, cycling through the themes."""
    themes = [theme.value for theme in Theme]
    responses: list[object] = []
    remaining = total
    while remaining > 0:
        size = min(reviews_per_batch, remaining)
        responses.append(
            {
                "results": [
                    {
                        "index": index,
                        "theme": themes[index % len(themes)],
                        "confidence": 0.9,
                    }
                    for index in range(1, size + 1)
                ]
            }
        )
        remaining -= size
    return FakeLlm(responses=responses)


def run_to_classify(settings: Settings, tmp_path: Path) -> tuple[RunContext, FakeLlm]:
    context = start_run(FIXTURE_WEEK, tmp_path)
    run_stages(context, options(settings, tmp_path, to_stage=3))

    corpus_size = len(context.read_models(3, Review))
    llm = fake_classifier(settings.pulse.llm.batch_size, corpus_size)
    run_stages(
        context,
        options(
            settings,
            tmp_path,
            from_stage=4,
            to_stage=4,
            llm=llm,
            cache_path=tmp_path / "classify.json",
        ),
    )
    return context, llm


def test_stage_four_classifies_the_whole_corpus(settings: Settings, tmp_path: Path) -> None:
    context, _ = run_to_classify(settings, tmp_path)

    scrubbed = context.read_models(3, Review)
    classified = context.read_models(4, ClassifiedReview)

    assert context.completed_stages() == [1, 2, 3, 4]
    assert len(classified) == len(scrubbed)
    assert [c.review_id for c in classified] == [r.review_id for r in scrubbed]


def test_stage_four_reads_only_scrubbed_text(settings: Settings, tmp_path: Path) -> None:
    """The privacy-critical ordering: no PII has ever been sent to a provider.

    Stage 4 reads `03_scrubbed.json`, so whatever the FakeLlm was shown is what a real
    provider would have been shown.
    """
    _, llm = run_to_classify(settings, tmp_path)

    sent = "\n".join(str(call["user"]) for call in llm.calls)
    for planted in (
        "notareal@example.com",
        "ABCDE1234F",
        "2994 1908 4318",
        "Not A Realperson",
        "9000000000",
    ):
        assert planted not in sent, f"{planted!r} was sent to the model"
    assert not has_pii(sent)


def test_stage_four_populates_theme_counts_and_usage(settings: Settings, tmp_path: Path) -> None:
    context, _ = run_to_classify(settings, tmp_path)
    ledger = json.loads((context.run_dir / "run.json").read_text(encoding="utf-8"))

    assert sum(ledger["theme_counts"].values()) > 100
    assert set(ledger["theme_counts"]) <= {theme.value for theme in Theme}
    assert ledger["usage"]["provider"] == "fake"
    assert ledger["usage"]["calls"] > 0


def test_stage_four_reports_no_prompt_cache_on_this_provider(
    settings: Settings, tmp_path: Path
) -> None:
    """Groq has no cache breakpoint, so the ledger says so rather than implying a miss."""
    context, _ = run_to_classify(settings, tmp_path)
    ledger = json.loads((context.run_dir / "run.json").read_text(encoding="utf-8"))

    classify_record = next(r for r in ledger["stages"] if r["stage"] == 4)
    assert classify_record["detail"]["prompt_cache_supported"] is False
    assert classify_record["detail"]["cache_hit_rate"] == 0.0


def test_replaying_stage_four_hits_the_on_disk_cache(settings: Settings, tmp_path: Path) -> None:
    """A second pass must make no call at all — the weekly-re-run property."""
    context, _ = run_to_classify(settings, tmp_path)

    empty = FakeLlm(responses=[])
    run_stages(
        context,
        options(
            settings,
            tmp_path,
            from_stage=4,
            to_stage=4,
            llm=empty,
            cache_path=tmp_path / "classify.json",
        ),
    )

    assert empty.calls == []
    ledger = json.loads((context.run_dir / "run.json").read_text(encoding="utf-8"))
    last = [r for r in ledger["stages"] if r["stage"] == 4][-1]
    assert last["detail"]["cache_hit_rate"] == 1.0
    assert last["detail"]["api_calls"] == 0


def test_the_classified_artifact_contains_no_review_text(
    settings: Settings, tmp_path: Path
) -> None:
    """`04_classified.json` is ids and labels; text stays in `03_scrubbed.json`."""
    context, _ = run_to_classify(settings, tmp_path)
    written = context.artifact_path(4).read_text(encoding="utf-8")

    assert "Withdrawal" not in written
    assert set(json.loads(written)[0]) == {"review_id", "theme", "confidence"}


def test_stage_four_without_a_provider_fails_before_writing_an_artifact(
    settings: Settings, tmp_path: Path
) -> None:
    context = start_run(FIXTURE_WEEK, tmp_path)
    run_stages(context, options(settings, tmp_path, to_stage=3))

    with pytest.raises(LlmNotConfigured):
        run_stages(
            context,
            options(settings, tmp_path, from_stage=4, to_stage=4, llm=NullLlm()),
        )

    assert context.completed_stages() == [1, 2, 3]
