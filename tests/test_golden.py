"""P6-T2: the whole pipeline, end to end, offline, byte for byte.

Fixture corpus → stages 1-3 → the committed real-model labels (`classified_golden.json`)
as stage 4 → stages 5-8 with a scripted draft → stages 9-10 against the stub server. The
rendered `pulse.md` is compared with `tests/fixtures/pulse_golden.md`.

Only the model's prose is scripted. Everything a reader could be misled by -- which three
themes, their figures, which quotes and their ratings, the word count, the disclosure line
-- comes from the deterministic stages, so a change to any of them shows up here as a diff.

After an intended change, regenerate the golden note and review the diff before committing:

    UPDATE_GOLDEN=1 pytest tests/test_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pulse.analysis.scrub import has_pii
from pulse.config import Settings, load_settings
from pulse.llm import FakeLlm
from pulse.models import ClassifiedReview, ThemeStats
from pulse.orchestrator import RunContext, start_run
from pulse.pipeline import PipelineOptions, run_stages
from pulse.publish import retry
from pulse.rendering.validate import WORD_LIMIT, count_note_words
from tests.mcp_support import appended_sections, stub_settings, stub_state

FIXTURE = Path("tests/fixtures/reviews_sample.json")
GOLDEN_LABELS = Path("tests/fixtures/classified_golden.json")
GOLDEN_NOTE = Path("tests/fixtures/pulse_golden.md")
WEEK = "2026-W41"
GENERATED_ON = date(2026, 10, 12)


def label(stats: ThemeStats) -> str:
    return stats.theme.value.replace("_", " ")


def scripted_draft(selected: list[ThemeStats]) -> dict[str, Any]:
    """The model's part: words only. Quotes are the first candidate in each pool, copied
    exactly; review_id, rating, count and direction are deliberately wrong, because the
    pipeline must replace them."""
    return {
        "iso_week": WEEK,
        "headline": "Withdrawals and reliability lead this week's complaints",
        "top_themes": [
            {
                "theme": stats.theme.value,
                "summary": f"Reviewers describe recurring {label(stats)} problems.",
                "review_count": 999,
                "direction": "flat",
            }
            for stats in selected
        ],
        "quotes": [
            {
                "text": stats.quote_candidates[0].text,
                "theme": stats.theme.value,
                "rating": 5,
                "review_id": "invented",
            }
            for stats in selected
        ],
        "actions": [
            {
                "theme": stats.theme.value,
                "action": f"Assign an owner for {label(stats)} fixes.",
                "rationale": "The theme ranks in this week's top three.",
            }
            for stats in selected
        ],
    }


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "WAIT_SECONDS", 0.0)


@pytest.fixture(scope="module")
def golden_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[RunContext, Path]:
    tmp = tmp_path_factory.mktemp("golden")
    state = tmp / "stub.json"
    settings: Settings = stub_settings(load_settings(Path("config")), state)

    def options(first: int, last: int, **extra: Any) -> PipelineOptions:
        return PipelineOptions(
            settings=settings,
            input_path=FIXTURE,
            target_week=WEEK,
            from_stage=first,
            to_stage=last,
            generated_on=GENERATED_ON,
            cache_path=tmp / "cache.json",
            **extra,
        )

    context = start_run(WEEK, tmp / "runs")
    run_stages(context, options(1, 3))

    # Stage 4 from the committed labels a real model produced -- no tokens spent.
    labels = json.loads(GOLDEN_LABELS.read_text(encoding="utf-8"))
    context.write_artifact(4, [ClassifiedReview.model_validate(item) for item in labels])
    run_stages(context, options(5, 5))

    selected = [ThemeStats.model_validate(s) for s in context.read_artifact(5)["selected"]]
    llm = FakeLlm(responses=[scripted_draft(selected)])
    run_stages(context, options(6, 10, llm=llm, mcp_mode="stub"))
    return context, state


def test_the_rendered_note_matches_the_golden_file(golden_run: tuple[RunContext, Path]) -> None:
    context, _ = golden_run
    rendered = context.note_path.read_text(encoding="utf-8")

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_NOTE.write_text(rendered, encoding="utf-8")
        pytest.skip(f"regenerated {GOLDEN_NOTE}; review the diff before committing")

    assert rendered == GOLDEN_NOTE.read_text(encoding="utf-8"), (
        "pulse.md changed. If intended, run UPDATE_GOLDEN=1 pytest tests/test_golden.py "
        "and review the diff."
    )


def test_the_note_meets_the_brief(golden_run: tuple[RunContext, Path]) -> None:
    context, _ = golden_run
    note = context.read_artifact(6)
    markdown = context.note_path.read_text(encoding="utf-8")

    assert len(note["top_themes"]) == len(note["quotes"]) == len(note["actions"]) == 3
    assert count_note_words(markdown) <= WORD_LIMIT
    assert not has_pii(markdown)


def test_the_pipeline_not_the_model_supplied_every_fact(
    golden_run: tuple[RunContext, Path],
) -> None:
    context, _ = golden_run
    note = context.read_artifact(6)
    selected = {s["theme"]: s for s in context.read_artifact(5)["selected"]}

    for summary in note["top_themes"]:
        assert summary["review_count"] == selected[summary["theme"]]["review_count"] != 999
        assert summary["figures"]["share_of_week"] == selected[summary["theme"]]["share_of_week"]
    for quote in note["quotes"]:
        source = selected[quote["theme"]]["quote_candidates"][0]
        assert (quote["review_id"], quote["rating"]) == (source["review_id"], source["rating"])


def test_the_same_note_reached_the_doc_and_a_draft(golden_run: tuple[RunContext, Path]) -> None:
    context, state = golden_run
    (section,) = appended_sections(state)
    stored = stub_state(state)

    texts = "\n".join(block["text"] for block in section)
    for quote in context.read_artifact(6)["quotes"]:
        assert quote["text"] in texts
    (draft,) = stored["drafts"]
    assert context.note_path.read_text(encoding="utf-8") in draft["body"]
    assert stored["sent"] == []


def test_no_artifact_left_holds_raw_text(golden_run: tuple[RunContext, Path]) -> None:
    """Retention (O6): the run succeeded, so the unscrubbed artifact is gone."""
    context, _ = golden_run
    assert not context.artifact_path(1).exists()
    assert context.ledger.raw_artifact_deleted
