"""P3-T6: theme ranking and quote candidate selection.

Stage 5 decides which three themes appear on the page, with no model involved. Two
properties matter most:

* **Determinism.** The same corpus must always produce the same ranking, or the
  week-over-week deltas that make a *weekly* pulse worth reading stop being comparable.
* **Volume dominates momentum.** A three-review theme must never displace a hundred-review
  one just because it grew. That is the specific way a ranking formula with a delta term
  goes wrong, and architecture.md §8.2 weights the terms to prevent it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse.analysis.aggregate import (
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    AggregateError,
    aggregate,
    quote_candidates,
    readability_score,
    select_top_themes,
)
from pulse.config import Settings, load_settings
from pulse.ingest.normalize import iso_week_start
from pulse.models import ClassifiedReview, Review, Source, Theme


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


_counter = iter(range(100_000))


def a_review(
    *,
    week: str = "2026-W41",
    rating: int = 3,
    text: str = "Withdrawal has been pending for four days with no update at all.",
) -> Review:
    index = next(_counter)
    return Review(
        review_id=f"{index:016x}",
        source=Source.PLAY_STORE,
        rating=rating,
        text=text,
        submitted_on=iso_week_start(week),
        iso_week=week,
        scrubbed=True,
    )


def corpus(
    spec: dict[str, list[tuple[Theme | None, int, int]]],
) -> tuple[list[Review], list[ClassifiedReview]]:
    """Build a corpus from {week: [(theme, rating, count), ...]}."""
    reviews: list[Review] = []
    classified: list[ClassifiedReview] = []
    for week, rows in spec.items():
        for theme, rating, count in rows:
            for _ in range(count):
                review = a_review(week=week, rating=rating)
                reviews.append(review)
                classified.append(
                    ClassifiedReview(review_id=review.review_id, theme=theme, confidence=0.9)
                )
    return reviews, classified


def twelve_weeks(
    target_rows: list[tuple[Theme | None, int, int]],
    baseline_rows: list[tuple[Theme | None, int, int]] | None = None,
) -> tuple[list[Review], list[ClassifiedReview]]:
    """A target week plus eleven trailing weeks, so a baseline exists."""
    from pulse.ingest.normalize import weeks_in_window

    weeks = weeks_in_window("2026-W41", 12)
    spec: dict[str, list[tuple[Theme | None, int, int]]] = {weeks[-1]: target_rows}
    for week in weeks[:-1]:
        spec[week] = baseline_rows or [(Theme.APP_RELIABILITY, 3, 3)]
    return corpus(spec)


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------


def test_per_theme_stats_are_computed(settings: Settings) -> None:
    reviews, classified = twelve_weeks(
        [(Theme.PAYMENTS_WITHDRAWALS, 1, 15), (Theme.APP_RELIABILITY, 4, 5)]
    )
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")
    payments = next(s for s in ranked if s.theme is Theme.PAYMENTS_WITHDRAWALS)

    assert payments.review_count == 15
    assert payments.share_of_week == pytest.approx(15 / 20)
    assert payments.mean_rating == pytest.approx(1.0)
    assert report.week_review_count == 20


def test_unclassified_reviews_count_toward_the_week_but_no_theme(
    settings: Settings,
) -> None:
    """Otherwise an unlabelled corpus would inflate whichever theme it was dumped into."""
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 10), (None, 3, 10)])
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    assert report.week_review_count == 20
    assert report.unclassified_in_week == 10
    assert sum(s.review_count for s in ranked) == 10


def test_the_baseline_excludes_the_target_week(settings: Settings) -> None:
    """A theme compared against a baseline that includes itself has no signal."""
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 20)])
    _, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    assert report.baseline_weeks == 11
    assert report.week_review_count == 20


def test_delta_is_positive_for_a_growing_theme(settings: Settings) -> None:
    reviews, classified = twelve_weeks(
        target_rows=[(Theme.PAYMENTS_WITHDRAWALS, 1, 20)],
        baseline_rows=[(Theme.APP_RELIABILITY, 3, 10)],
    )
    ranked, _ = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    payments = next(s for s in ranked if s.theme is Theme.PAYMENTS_WITHDRAWALS)
    reliability = next(s for s in ranked if s.theme is Theme.APP_RELIABILITY)
    assert payments.delta_vs_baseline > 0
    assert reliability.delta_vs_baseline < 0


# --------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------


def test_ranking_is_deterministic(settings: Settings) -> None:
    reviews, classified = twelve_weeks(
        [
            (Theme.PAYMENTS_WITHDRAWALS, 1, 12),
            (Theme.APP_RELIABILITY, 2, 8),
            (Theme.CHARGES_SUPPORT, 4, 6),
        ]
    )
    first, _ = aggregate(reviews, classified, settings=settings, target_week="2026-W41")
    second, _ = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    assert [s.theme for s in first] == [s.theme for s in second]


def test_volume_outranks_a_tiny_but_surging_theme(settings: Settings) -> None:
    """The specific failure mode a delta term invites.

    `charges_support` is brand new this week (maximum possible momentum) but has three
    reviews; `payments_withdrawals` has a hundred. Momentum is weighted third for exactly
    this reason — a three-review blip must not take a slot from a standing complaint.
    """
    reviews, classified = twelve_weeks(
        target_rows=[(Theme.PAYMENTS_WITHDRAWALS, 1, 100), (Theme.CHARGES_SUPPORT, 1, 3)],
        baseline_rows=[(Theme.PAYMENTS_WITHDRAWALS, 1, 10)],
    )
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    assert ranked[0].theme is Theme.PAYMENTS_WITHDRAWALS
    assert report.scores["payments_withdrawals"] > report.scores["charges_support"]


def test_negative_volume_breaks_a_share_tie(settings: Settings) -> None:
    """Two themes with equal share: the one making people angry ranks higher."""
    reviews, classified = twelve_weeks(
        [(Theme.PAYMENTS_WITHDRAWALS, 1, 10), (Theme.CHARGES_SUPPORT, 5, 10)]
    )
    ranked, _ = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    order = [s.theme for s in ranked]
    assert order.index(Theme.PAYMENTS_WITHDRAWALS) < order.index(Theme.CHARGES_SUPPORT)


def test_a_declining_theme_gets_no_momentum_credit(settings: Settings) -> None:
    """Only emerging problems earn the delta term; shrinking is not an achievement.

    Share is relative, so the target week needs a second theme for the first one's share
    to actually fall -- a theme that is 100% of both the week and the baseline has a delta
    of zero, not a negative one.
    """
    reviews, classified = twelve_weeks(
        target_rows=[(Theme.APP_RELIABILITY, 3, 10), (Theme.PAYMENTS_WITHDRAWALS, 1, 15)],
        baseline_rows=[(Theme.APP_RELIABILITY, 3, 20)],
    )
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    reliability = next(s for s in ranked if s.theme is Theme.APP_RELIABILITY)
    assert reliability.delta_vs_baseline < 0
    # Score reflects share and severity only; the negative delta is clamped at zero.
    assert report.scores["app_reliability"] > 0


def test_every_theme_appears_in_the_ranking(settings: Settings) -> None:
    """The full ranking is stored so "why wasn't X on the page?" is answerable."""
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 25)])
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    assert len(ranked) == len(Theme)
    assert set(report.ranked) == {t.value for t in Theme}


def test_ties_break_on_theme_name_for_total_order(settings: Settings) -> None:
    """Without a final tie-break the order would depend on dict iteration."""
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 3, 25)])
    _ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    # The four empty themes all score zero and must appear in name order.
    empty = [name for name in report.ranked if report.scores[name] == 0.0]
    assert empty == sorted(empty)


# --------------------------------------------------------------------------------------
# Thin-week abort (P3-T5)
# --------------------------------------------------------------------------------------


def test_a_thin_week_aborts(settings: Settings) -> None:
    """A confident note built on five reviews is worse than no note: the reader cannot
    tell it is noise."""
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 5)])

    with pytest.raises(AggregateError, match="below the minimum"):
        aggregate(reviews, classified, settings=settings, target_week="2026-W41")


def test_the_thin_week_message_names_the_threshold_and_the_count(
    settings: Settings,
) -> None:
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 4)])
    with pytest.raises(AggregateError) as exc_info:
        aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    message = str(exc_info.value)
    assert "4 review" in message
    assert str(settings.pulse.thresholds.min_week_reviews) in message


# --------------------------------------------------------------------------------------
# Quote candidate pool (P3-T4)
# --------------------------------------------------------------------------------------


def test_candidates_carry_provenance() -> None:
    """Text alone would make the provenance check in stage 7 unimplementable."""
    review = a_review(text="Withdrawal pending four days with absolutely no update yet.")
    (candidate,) = quote_candidates([review])

    assert candidate.review_id == review.review_id
    assert candidate.rating == review.rating
    assert candidate.text == review.text


def test_too_short_reviews_are_excluded() -> None:
    assert quote_candidates([a_review(text="x" * (MIN_QUOTE_CHARS - 1))]) == []


def test_too_long_reviews_are_excluded() -> None:
    assert quote_candidates([a_review(text="x" * (MAX_QUOTE_CHARS + 1))]) == []


def test_reviews_at_the_length_boundaries_are_included() -> None:
    assert len(quote_candidates([a_review(text="y" * MIN_QUOTE_CHARS)])) == 1
    assert len(quote_candidates([a_review(text="y" * MAX_QUOTE_CHARS)])) == 1


def test_reviews_containing_a_scrub_placeholder_are_excluded() -> None:
    """A partially redacted quote reads badly and hints at what was removed."""
    redacted = a_review(text="Withdrawal stuck for days, please contact me at [email] right now.")
    assert quote_candidates([redacted]) == []


def test_the_pool_is_capped() -> None:
    reviews = [
        a_review(text=f"Withdrawal number {n} has been pending for four days now.")
        for n in range(20)
    ]
    assert len(quote_candidates(reviews, limit=8)) == 8


def test_the_pool_is_deterministic() -> None:
    """Byte-stable candidate pools make the drafting input reproducible."""
    reviews = [
        a_review(text=f"Withdrawal case {n} pending for four days with no update at all.")
        for n in range(20)
    ]
    assert quote_candidates(reviews) == quote_candidates(reviews)


def test_a_complete_sentence_outscores_a_fragment() -> None:
    complete = "Withdrawal has been pending for four days with no update."
    fragment = "withdrawal pending four days no update at all from the support team"
    assert readability_score(complete) > readability_score(fragment)


def test_shouting_and_emoji_runs_are_penalised() -> None:
    plain = "Withdrawal has been pending for four days with no update."
    shouting = "WITHDRAWAL has been pending for four days with no update."
    assert readability_score(plain) > readability_score(shouting)


# --------------------------------------------------------------------------------------
# Top-three selection and substitution (P3-T5)
# --------------------------------------------------------------------------------------


def quotable(theme: Theme, count: int, *, week: str = "2026-W41") -> list[tuple]:
    return [(theme, 2, count)]


def test_the_top_three_ranked_themes_are_selected(settings: Settings) -> None:
    reviews, classified = twelve_weeks(
        [
            (Theme.PAYMENTS_WITHDRAWALS, 1, 12),
            (Theme.APP_RELIABILITY, 2, 9),
            (Theme.CHARGES_SUPPORT, 2, 7),
            (Theme.ONBOARDING_KYC, 4, 3),
        ]
    )
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")
    selected = select_top_themes(ranked, report)

    assert len(selected) == 3
    assert [s.theme for s in selected] == [s.theme for s in ranked[:3]]
    assert report.selected == [s.theme.value for s in selected]


def test_a_theme_with_no_quotable_review_is_skipped(settings: Settings) -> None:
    """Promoting a theme that cannot supply a quote guarantees a validation failure the
    retry loop could never fix, because the pool it draws from would be empty."""
    weeks = __import__("pulse.ingest.normalize", fromlist=["weeks_in_window"]).weeks_in_window(
        "2026-W41", 12
    )

    reviews: list[Review] = []
    classified: list[ClassifiedReview] = []

    # Top-ranked theme: plenty of reviews, but all too short to quote.
    for _ in range(14):
        r = a_review(week=weeks[-1], rating=1, text="Bad app")
        reviews.append(r)
        classified.append(
            ClassifiedReview(
                review_id=r.review_id, theme=Theme.PAYMENTS_WITHDRAWALS, confidence=0.9
            )
        )
    # Three quotable themes behind it.
    for theme in (Theme.APP_RELIABILITY, Theme.CHARGES_SUPPORT, Theme.ONBOARDING_KYC):
        for n in range(5):
            r = a_review(
                week=weeks[-1],
                rating=2,
                text=f"{theme.value} issue {n}: the app keeps failing on every attempt.",
            )
            reviews.append(r)
            classified.append(ClassifiedReview(review_id=r.review_id, theme=theme, confidence=0.9))
    for week in weeks[:-1]:
        r = a_review(week=week, rating=3, text="An older review from a previous week.")
        reviews.append(r)
        classified.append(
            ClassifiedReview(review_id=r.review_id, theme=Theme.APP_RELIABILITY, confidence=0.9)
        )

    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")
    assert ranked[0].theme is Theme.PAYMENTS_WITHDRAWALS
    assert ranked[0].quote_candidates == []

    selected = select_top_themes(ranked, report)

    assert Theme.PAYMENTS_WITHDRAWALS not in [s.theme for s in selected]
    assert len(selected) == 3
    assert any("payments_withdrawals skipped" in s for s in report.substitutions)


def test_selection_fails_when_too_few_themes_can_supply_a_quote(
    settings: Settings,
) -> None:
    reviews, classified = twelve_weeks([(Theme.PAYMENTS_WITHDRAWALS, 1, 25)])
    ranked, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    with pytest.raises(AggregateError, match="quotable review"):
        select_top_themes(ranked, report)


# --------------------------------------------------------------------------------------
# Report hygiene
# --------------------------------------------------------------------------------------


def test_the_report_contains_no_review_text(settings: Settings) -> None:
    reviews, classified = twelve_weeks(
        [(Theme.PAYMENTS_WITHDRAWALS, 1, 25)],
        baseline_rows=[(Theme.APP_RELIABILITY, 3, 5)],
    )
    _, report = aggregate(reviews, classified, settings=settings, target_week="2026-W41")

    rendered = str(report.summary())
    assert "Withdrawal has been pending" not in rendered
