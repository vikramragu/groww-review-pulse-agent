"""Run directories, stage artifacts, and the ledger (P0-T6).

The ISO-week tests here are the ones worth keeping forever: the year-boundary case is a
silent-corruption bug (reviews misfiled into the wrong week, baseline quietly wrong) rather
than a crash, so it needs a test that would catch a regression nobody would otherwise see.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from pulse.models import Review, RunLedger
from pulse.orchestrator import (
    STAGE_ARTIFACTS,
    STAGE_NAMES,
    RunError,
    allocate_run_dir,
    current_iso_week,
    iso_week_of,
    latest_run_dir,
    open_run,
    start_run,
)

# --------------------------------------------------------------------------------------
# ISO week derivation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 7), "2026-W41"),
        (date(2026, 1, 1), "2026-W01"),
        (date(2026, 12, 31), "2026-W53"),
    ],
)
def test_iso_week_of_ordinary_dates(day: date, expected: str) -> None:
    assert iso_week_of(day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # Each of these falls in an ISO year that is NOT its calendar year. Formatting
        # with `date.year` instead of `isocalendar()[0]` would misfile every one of them
        # into a week that does not contain the date -- silently, and the trailing
        # baseline would be wrong rather than the run failing.
        (date(2027, 1, 1), "2026-W53"),
        (date(2025, 12, 29), "2026-W01"),
        (date(2024, 12, 30), "2025-W01"),
        (date(2023, 1, 1), "2022-W52"),
        (date(2021, 1, 1), "2020-W53"),
    ],
)
def test_iso_week_uses_the_iso_year_not_the_calendar_year(day: date, expected: str) -> None:
    assert day.year != int(expected[:4]), "test case must straddle a year boundary"
    assert iso_week_of(day) == expected


def test_iso_week_is_always_zero_padded() -> None:
    assert iso_week_of(date(2026, 3, 2)) == "2026-W10"
    assert iso_week_of(date(2026, 1, 5)) == "2026-W02"


def test_current_iso_week_is_well_formed() -> None:
    week = current_iso_week()
    assert (
        Review.model_validate(
            {
                "review_id": "x",
                "source": "play_store",
                "rating": 3,
                "text": "ok",
                "submitted_on": "2026-10-07",
                "iso_week": week,
            }
        ).iso_week
        == week
    )


# --------------------------------------------------------------------------------------
# Run directory allocation
# --------------------------------------------------------------------------------------


def test_allocates_the_first_run_as_sequence_one(tmp_path: Path) -> None:
    run_dir, run_id = allocate_run_dir("2026-W41", tmp_path)
    assert run_id == "2026-W41-01"
    assert run_dir == tmp_path / "2026-W41-01"
    assert run_dir.is_dir()


def test_allocation_increments_rather_than_overwriting(tmp_path: Path) -> None:
    """A re-run must never destroy the previous attempt's evidence."""
    first, _ = allocate_run_dir("2026-W41", tmp_path)
    (first / "marker.txt").write_text("keep me", encoding="utf-8")

    _, second_id = allocate_run_dir("2026-W41", tmp_path)

    assert second_id == "2026-W41-02"
    assert (first / "marker.txt").read_text(encoding="utf-8") == "keep me"


def test_allocation_is_scoped_per_week(tmp_path: Path) -> None:
    allocate_run_dir("2026-W40", tmp_path)
    _, run_id = allocate_run_dir("2026-W41", tmp_path)
    assert run_id == "2026-W41-01"


def test_allocation_ignores_unrelated_directories(tmp_path: Path) -> None:
    (tmp_path / "scratch").mkdir()
    (tmp_path / "not-a-run").mkdir()
    _, run_id = allocate_run_dir("2026-W41", tmp_path)
    assert run_id == "2026-W41-01"


def test_allocation_continues_past_a_gap(tmp_path: Path) -> None:
    (tmp_path / "2026-W41-01").mkdir(parents=True)
    (tmp_path / "2026-W41-07").mkdir()
    _, run_id = allocate_run_dir("2026-W41", tmp_path)
    assert run_id == "2026-W41-08"


# --------------------------------------------------------------------------------------
# Stage artifacts
# --------------------------------------------------------------------------------------


def test_stage_tables_cover_the_documented_pipeline() -> None:
    """Ten stages (architecture.md §6); seven of them write numbered JSON (§13)."""
    assert sorted(STAGE_NAMES) == list(range(1, 11))
    assert sorted(STAGE_ARTIFACTS) == list(range(1, 8))


def test_writes_and_reads_a_model_list_artifact(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    reviews = [
        Review.model_validate(
            {
                "review_id": "a" * 16,
                "source": "play_store",
                "rating": 2,
                "text": "Withdrawal pending for days.",
                "submitted_on": "2026-10-07",
                "iso_week": "2026-W41",
            }
        )
    ]

    path = context.write_artifact(3, reviews)

    assert path.name == "03_scrubbed.json"
    assert context.read_models(3, Review) == reviews


def test_artifacts_are_byte_identical_for_identical_input(tmp_path: Path) -> None:
    """Deterministic serialization is what makes the P6 golden tests meaningful."""
    payload = {"b": 2, "a": [3, 1]}
    first = start_run("2026-W41", tmp_path)
    second = start_run("2026-W41", tmp_path)

    assert (
        first.write_artifact(5, payload).read_bytes()
        == second.write_artifact(5, payload).read_bytes()
    )


def test_reading_a_missing_artifact_suggests_the_fix(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    with pytest.raises(RunError, match="--from"):
        context.read_artifact(4)


def test_reading_an_artifact_that_does_not_match_the_model_fails(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    context.write_artifact(3, [{"not": "a review"}])
    with pytest.raises(RunError, match="does not match Review"):
        context.read_models(3, Review)


def test_reading_a_list_artifact_as_a_single_model_fails(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    context.write_artifact(6, [1, 2, 3])
    with pytest.raises(RunError):
        context.read_model(6, Review)


def test_stages_without_json_artifacts_are_rejected(tmp_path: Path) -> None:
    """Stages 8-10 produce pulse.md and ledger fields, not numbered JSON."""
    context = start_run("2026-W41", tmp_path)
    for stage in (8, 9, 10):
        with pytest.raises(RunError, match="no JSON artifact"):
            context.artifact_path(stage)


def test_completed_stages_reflects_what_exists_on_disk(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    assert context.completed_stages() == []
    context.write_artifact(1, [])
    context.write_artifact(2, [])
    assert context.completed_stages() == [1, 2]


def test_writes_the_note_to_pulse_md(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    path = context.write_note("# Pulse\n")
    assert path.name == "pulse.md"
    assert path.read_text(encoding="utf-8") == "# Pulse\n"


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


def test_start_run_writes_a_ledger_immediately(tmp_path: Path) -> None:
    """A crash before stage 1 should still leave evidence that a run was attempted."""
    context = start_run("2026-W41", tmp_path, mcp_mode="stub")
    ledger_path = context.run_dir / "run.json"

    assert ledger_path.exists()
    reloaded = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    assert reloaded.run_id == "2026-W41-01"
    assert reloaded.status == "running"
    assert reloaded.mcp_mode == "stub"


def test_stage_context_records_success_with_detail(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)

    with context.stage(2) as detail:
        detail["reviews_in"] = 310

    record = context.ledger.stages[-1]
    assert (record.stage, record.name, record.status) == (2, "normalize", "ok")
    assert record.detail["reviews_in"] == 310
    assert record.duration_ms >= 0


def test_stage_context_records_failure_and_reraises(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)

    with pytest.raises(ValueError, match="boom"), context.stage(4):
        raise ValueError("boom")

    record = context.ledger.stages[-1]
    assert record.status == "failed"
    assert "ValueError: boom" in str(record.detail["error"])


def test_stage_records_are_persisted_as_they_happen(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    with context.stage(1):
        pass

    on_disk = RunLedger.model_validate_json(
        (context.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert [record.stage for record in on_disk.stages] == [1]


def test_finish_records_terminal_status_and_reason(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    context.finish("aborted", abort_reason="corpus window shorter than 8 weeks")

    on_disk = RunLedger.model_validate_json(
        (context.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert on_disk.status == "aborted"
    assert on_disk.abort_reason == "corpus window shorter than 8 weeks"
    assert isinstance(on_disk.finished_at, datetime)


# --------------------------------------------------------------------------------------
# Reopening runs
# --------------------------------------------------------------------------------------


def test_open_run_round_trips_a_started_run(tmp_path: Path) -> None:
    created = start_run("2026-W41", tmp_path)
    created.write_artifact(1, [])

    reopened = open_run(created.run_dir)

    assert reopened.run_id == created.run_id
    assert reopened.iso_week == "2026-W41"
    assert reopened.completed_stages() == [1]


def test_open_run_rejects_a_directory_that_is_not_a_run(tmp_path: Path) -> None:
    (tmp_path / "random").mkdir()
    with pytest.raises(RunError, match="not a run directory"):
        open_run(tmp_path / "random")


def test_open_run_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RunError, match="Not a run directory"):
        open_run(tmp_path / "absent")


def test_open_run_rejects_a_corrupt_ledger(tmp_path: Path) -> None:
    context = start_run("2026-W41", tmp_path)
    (context.run_dir / "run.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RunError, match="Corrupt ledger"):
        open_run(context.run_dir)


def test_latest_run_dir_picks_the_highest_sequence(tmp_path: Path) -> None:
    allocate_run_dir("2026-W40", tmp_path)
    allocate_run_dir("2026-W41", tmp_path)
    expected, _ = allocate_run_dir("2026-W41", tmp_path)
    assert latest_run_dir(tmp_path) == expected


def test_latest_run_dir_is_none_when_there_are_no_runs(tmp_path: Path) -> None:
    assert latest_run_dir(tmp_path / "absent") is None
