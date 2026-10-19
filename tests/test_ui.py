"""The dashboard: its data layer, the public snapshot, and both app modes rendering.

The snapshot is the part that matters most. It is committed to a public repository, so
these tests pin what it must never contain -- review ids, quote candidates (other
reviewers' text), the draft id, the private doc link -- and that a tampered or failing
snapshot is refused rather than shown.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pulse.config import load_settings
from pulse.llm import FakeLlm
from pulse.models import ClassifiedReview, ThemeStats
from pulse.orchestrator import RunContext, start_run
from pulse.pipeline import PipelineOptions, run_stages
from pulse.ui.data import (
    WITHHELD,
    SnapshotError,
    export_snapshot,
    list_runs,
    load_run,
    load_snapshot,
    write_snapshot,
)
from tests.test_golden import GOLDEN_LABELS, scripted_draft

FIXTURE = Path("tests/fixtures/reviews_sample.json")
WEEK = "2026-W41"


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A finished dry run (stages 1-8) over the fixture, as `pulse run --dry-run` leaves it."""
    tmp = tmp_path_factory.mktemp("ui")
    settings = load_settings(Path("config"))

    def options(first: int, last: int, **extra: Any) -> PipelineOptions:
        return PipelineOptions(
            settings=settings,
            input_path=FIXTURE,
            target_week=WEEK,
            from_stage=first,
            to_stage=last,
            generated_on=date(2026, 10, 12),
            cache_path=tmp / "cache.json",
            **extra,
        )

    context: RunContext = start_run(WEEK, tmp / "runs")
    run_stages(context, options(1, 3))
    labels = json.loads(GOLDEN_LABELS.read_text(encoding="utf-8"))
    context.write_artifact(4, [ClassifiedReview.model_validate(item) for item in labels])
    run_stages(context, options(5, 5))
    selected = [ThemeStats.model_validate(s) for s in context.read_artifact(5)["selected"]]
    run_stages(context, options(6, 8, llm=FakeLlm(responses=[scripted_draft(selected)])))
    return context.run_dir


# --------------------------------------------------------------------------------------
# Local runs
# --------------------------------------------------------------------------------------


def test_a_finished_run_loads_with_every_check_passing(run_dir: Path) -> None:
    view = load_run(run_dir)
    assert [c.name for c in view.checks] == [
        "word_limit",
        "three_three_three",
        "quotes_verbatim",
        "no_pii",
        "figures_from_data",
        "grounded_in_themes",
    ]
    assert all(c.ok for c in view.checks), [c for c in view.checks if not c.ok]
    assert len(view.themes) == 5 and sum(t.on_page for t in view.themes) == 3


def test_weekly_counts_cover_every_week_and_add_up(run_dir: Path) -> None:
    view = load_run(run_dir)
    weeks = {row["iso_week"] for row in view.weekly}
    assert WEEK in weeks and len(weeks) >= 8
    target = [row for row in view.weekly if row["iso_week"] == WEEK]
    assert sum(row["count"] for row in target) == sum(view.ratings["week"].values())
    assert sum(row["share"] for row in target) == pytest.approx(1.0)


def test_a_note_whose_figures_were_written_by_the_model_fails_a_check(run_dir: Path) -> None:
    view = load_run(run_dir)
    stripped = view.note.model_copy(
        update={
            "top_themes": [s.model_copy(update={"figures": None}) for s in view.note.top_themes]
        }
    )
    from pulse.ui.data import run_checks

    checks = {c.name: c.ok for c in run_checks(stripped, view.markdown, view.themes, {})}
    assert checks["figures_from_data"] is False


def test_only_runs_with_a_note_are_listed(run_dir: Path, tmp_path: Path) -> None:
    assert list_runs(run_dir.parent) == [run_dir]
    (tmp_path / "2026-W40-01").mkdir()
    assert list_runs(tmp_path) == []


# --------------------------------------------------------------------------------------
# The public snapshot
# --------------------------------------------------------------------------------------


def test_the_snapshot_withholds_everything_identifying(run_dir: Path) -> None:
    payload = export_snapshot(run_dir)
    text = json.dumps(payload)

    assert all(q["review_id"] == WITHHELD for q in payload["note"]["quotes"])
    assert "quote_candidates" not in text
    assert payload["delivery"]["document_url"] is None

    review_ids = {r["review_id"] for r in json.loads((run_dir / "03_scrubbed.json").read_text())}
    assert not any(review_id in text for review_id in review_ids)


def test_the_snapshot_holds_only_the_published_review_text(run_dir: Path) -> None:
    """Every review body in the run except the three quoted ones stays out."""
    payload = export_snapshot(run_dir)
    text = json.dumps(payload, ensure_ascii=False)
    quoted = {q["text"] for q in payload["note"]["quotes"]}
    bodies = [r["text"] for r in json.loads((run_dir / "03_scrubbed.json").read_text())]
    leaked = [body for body in bodies if body not in quoted and len(body) > 30 and body in text]
    assert leaked == []


def test_a_snapshot_round_trips_and_re_verifies(run_dir: Path, tmp_path: Path) -> None:
    path = write_snapshot(export_snapshot(run_dir), tmp_path)
    view = load_snapshot(path)

    assert view.source == "snapshot"
    assert all(c.ok for c in view.checks)
    verbatim = next(c for c in view.checks if c.name == "quotes_verbatim")
    assert "at export" in verbatim.detail


def test_an_edited_snapshot_is_refused(run_dir: Path, tmp_path: Path) -> None:
    path = write_snapshot(export_snapshot(run_dir), tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["note"]["headline"] = "Something nobody checked"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SnapshotError, match="modified after export"):
        load_snapshot(path)


def test_pii_in_the_note_blocks_export(run_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / run_dir.name
    copy.mkdir()
    for item in run_dir.iterdir():
        (copy / item.name).write_bytes(item.read_bytes())
    note = json.loads((copy / "06_note.json").read_text())
    note["headline"] = "Write to someone@example.com"
    (copy / "06_note.json").write_text(json.dumps(note))
    markdown = (copy / "pulse.md").read_text()
    (copy / "pulse.md").write_text(markdown + "\nsomeone@example.com\n")

    with pytest.raises(SnapshotError):
        export_snapshot(copy)


def test_the_committed_snapshot_verifies() -> None:
    """The file the public app serves must load, verify, and pass every check."""
    snapshots = sorted(Path("snapshots").glob("*-W*.json"))
    if not snapshots:
        pytest.skip("no committed snapshot")
    view = load_snapshot(snapshots[-1])
    assert all(c.ok for c in view.checks)


# --------------------------------------------------------------------------------------
# The app renders, in both modes
# --------------------------------------------------------------------------------------


def _app(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    pytest.importorskip("streamlit", reason="the ui extra is not installed")
    from streamlit.testing.v1 import AppTest

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AppTest.from_file(
        str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=60
    ).run()


def test_the_public_view_renders_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    if not sorted(Path("snapshots").glob("*-W*.json")):
        pytest.skip("no committed snapshot")
    app = _app(monkeypatch, PULSE_UI_MODE="snapshot")

    assert not app.exception
    assert any("Read-only snapshot" in info.value for info in app.info)
    assert not [b for b in app.button if "Publish" in b.label]
    assert dict((m.label, m.value) for m in app.metric)["Checks passed"] == "6 / 6"


def test_the_local_view_renders_with_publishing_behind_a_confirmation(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch, PULSE_UI_MODE="local", PULSE_UI_RUNS=str(run_dir.parent))

    assert not app.exception
    publish = next(b for b in app.button if "Publish" in b.label)
    assert publish.disabled, "publishing must wait for the confirmation checkbox"
