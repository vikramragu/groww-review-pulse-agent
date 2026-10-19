"""The committed classification artifact (P2-T10).

`tests/fixtures/classified_golden.json` holds the labels a real model produced for the
substance-filtered fixture corpus (`openai/gpt-oss-120b`, taxonomy v1): 236 labelled on
2026-10-11, plus the 2 rows P2-T18 added, labelled on 2026-10-15. It exists so Phases 3
and 6 can be tested offline: the note pipeline can be exercised end to end without
spending tokens or depending on a model being reachable.

These tests guard the artifact's *integrity* — that it still matches the corpus it was
produced from — rather than asserting particular labels. Pinning exact labels would make
every prompt improvement look like a regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.analysis.scrub import scrub_reviews
from pulse.ingest.base import build_source
from pulse.ingest.normalize import filter_substantive, filter_to_window, normalize
from pulse.models import ClassifiedReview, Theme

GOLDEN = Path("tests/fixtures/classified_golden.json")
FIXTURE = Path("tests/fixtures/reviews_sample.json")
FIXTURE_WEEK = "2026-W41"


@pytest.fixture(scope="module")
def golden() -> list[ClassifiedReview]:
    return [
        ClassifiedReview.model_validate(item)
        for item in json.loads(GOLDEN.read_text(encoding="utf-8"))
    ]


@pytest.fixture(scope="module")
def corpus_ids() -> list[str]:
    """Re-derive the corpus stage 4 classifies: windowed, substance-filtered, scrubbed."""
    raw, _ = build_source("file_drop", path=FIXTURE).load()
    reviews, _ = normalize(raw)
    reviews, _ = filter_to_window(reviews, FIXTURE_WEEK, 12)
    reviews, _ = filter_substantive(reviews)
    reviews, _ = scrub_reviews(reviews)
    return [review.review_id for review in reviews]


def test_the_golden_artifact_parses_as_classified_reviews(
    golden: list[ClassifiedReview],
) -> None:
    assert golden


def test_it_covers_exactly_the_fixture_corpus(
    golden: list[ClassifiedReview], corpus_ids: list[str]
) -> None:
    """If the fixture changes, the golden labels must be regenerated, not silently reused.

    This is the test that catches a stale artifact — the failure mode where Phase 3 is
    developed against labels for reviews that no longer exist.
    """
    assert [c.review_id for c in golden] == corpus_ids


def test_every_label_is_in_the_closed_taxonomy(golden: list[ClassifiedReview]) -> None:
    assert all(c.theme is None or c.theme in set(Theme) for c in golden)


def test_review_ids_are_unique(golden: list[ClassifiedReview]) -> None:
    ids = [c.review_id for c in golden]
    assert len(ids) == len(set(ids))


def test_it_contains_no_review_text(golden: list[ClassifiedReview]) -> None:
    """Labels and ids only — the text lives in `03_scrubbed.json`."""
    written = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert all(set(item) == {"review_id", "theme", "confidence"} for item in written)


def test_most_of_the_corpus_is_classified(golden: list[ClassifiedReview]) -> None:
    """A sanity floor, not a quality score.

    If a future regeneration leaves most of the corpus unlabelled, the aggregation in
    Phase 3 would be built on almost nothing — and would still produce a confident-looking
    note. Deliberately loose: this catches collapse, not drift.
    """
    classified = sum(1 for c in golden if c.theme is not None)
    assert classified / len(golden) > 0.80


def test_every_theme_is_represented(golden: list[ClassifiedReview]) -> None:
    """Phase 3 needs at least three themes with reviews in them to write a note."""
    present = {c.theme for c in golden if c.theme is not None}
    assert present == set(Theme)
