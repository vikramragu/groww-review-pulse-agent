"""Stage 5: rank themes and assemble quote candidate pools (P3-T1 → P3-T5).

**Entirely deterministic — no model involvement.** This is where "top 3 themes" is
decided, so architecture.md §8.2 puts it in code: the same corpus must always produce the
same ranking, or the week-over-week deltas that make a *weekly* pulse worth reading stop
being comparable.

The drafting step that follows only ever sees what this module hands it: three themes,
their numbers, and a pre-vetted pool of quotable reviews. It never sees the full corpus.
That is what bounds the verbatim check in stage 7 to a small, known set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pulse import logging as pulse_logging
from pulse.analysis.scrub import LABELS
from pulse.config import Settings
from pulse.models import ClassifiedReview, QuoteCandidate, Review, Theme, ThemeStats

log = pulse_logging.get_logger("analysis.aggregate")

TOP_THEME_COUNT = 3
CANDIDATES_PER_THEME = 8

#: Quote length bounds, in characters. Below the floor a quote carries no argument; above
#: the ceiling it will not sit on a one-page note next to two others.
MIN_QUOTE_CHARS = 40
MAX_QUOTE_CHARS = 240

#: Ratings counted as negative for the severity term of the ranking score.
NEGATIVE_RATINGS = frozenset({1, 2})

#: Ranking weights from architecture.md §8.2. Volume first, severity second, momentum
#: third: raw volume alone buries a sharp new regression under a chronically large theme,
#: and delta alone over-reacts to small-sample noise.
WEIGHT_SHARE = 0.6
WEIGHT_NEGATIVE = 0.25
WEIGHT_DELTA = 0.15

_SENTENCE_END = re.compile(r"[.!?]")
_EMOJI_RUN = re.compile(r"[\U0001f000-\U0001faff☀-➿]{2,}")
_SHOUTING = re.compile(r"\b[A-Z]{4,}\b")


class AggregateError(Exception):
    """Raised when the corpus cannot support a meaningful note."""


@dataclass
class AggregateReport:
    """Counts and scores only — never review text."""

    target_week: str = ""
    week_review_count: int = 0
    baseline_weeks: int = 0
    baseline_review_count: int = 0
    unclassified_in_week: int = 0
    scores: dict[str, float] = field(default_factory=dict)
    ranked: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    substitutions: list[str] = field(default_factory=list)
    candidates_per_theme: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "target_week": self.target_week,
            "week_review_count": self.week_review_count,
            "baseline_weeks": self.baseline_weeks,
            "baseline_review_count": self.baseline_review_count,
            "unclassified_in_week": self.unclassified_in_week,
            "scores": {k: round(v, 4) for k, v in sorted(self.scores.items())},
            "ranked": self.ranked,
            "selected": self.selected,
            "substitutions": self.substitutions,
            "candidates_per_theme": dict(sorted(self.candidates_per_theme.items())),
        }


# --------------------------------------------------------------------------------------
# Quote candidate selection (P3-T4)
# --------------------------------------------------------------------------------------


def _contains_placeholder(text: str) -> bool:
    return any(label in text for label in LABELS)


def readability_score(text: str) -> float:
    """Heuristic for how well a review reads as a pull quote.

    Not a quality judgement of the complaint — only of its legibility on a one-page note
    sitting beside two others. Deterministic, because it decides which candidates the
    model is even allowed to choose from.
    """
    score = 0.0

    # A complete sentence reads better than a fragment.
    if _SENTENCE_END.search(text):
        score += 1.0
    # One or two sentences is ideal; a wall of them is not.
    sentences = len([s for s in _SENTENCE_END.split(text) if s.strip()])
    score += 1.0 if sentences <= 2 else 0.3

    # A concrete noun means the quote says something specific rather than just venting.
    concrete = (
        "withdrawal",
        "kyc",
        "order",
        "deposit",
        "chart",
        "statement",
        "charge",
        "login",
        "otp",
        "ipo",
        "crash",
        "refund",
        "brokerage",
        "settlement",
        "account",
        "payment",
        "mandate",
        "amc",
        "portfolio",
    )
    lowered = text.lower()
    score += 0.75 if any(word in lowered for word in concrete) else 0.0

    # Penalties for things that read badly in a published note.
    if _EMOJI_RUN.search(text):
        score -= 1.0
    if _SHOUTING.search(text):
        score -= 0.5

    # Mild preference for the middle of the length range.
    length = len(text)
    if 70 <= length <= 180:
        score += 0.5

    return score


def quote_candidates(
    reviews: list[Review],
    *,
    limit: int = CANDIDATES_PER_THEME,
) -> list[QuoteCandidate]:
    """The four-step filter from architecture.md §8.2, applied to one theme's week.

    Step 1 (filter to the week and theme) happens in the caller, which knows the
    classification; this applies steps 2-4.
    """
    eligible = [
        review
        for review in reviews
        if MIN_QUOTE_CHARS <= len(review.text) <= MAX_QUOTE_CHARS
        # A partially redacted quote reads badly and hints at what was removed, so a
        # review carrying a scrub placeholder is dropped rather than published with
        # "[phone]" in the middle of it.
        and not _contains_placeholder(review.text)
    ]

    # Sorted by score, then by review_id: the tie-break keeps the pool byte-stable for a
    # stable corpus, which is what makes the drafting input reproducible.
    ranked = sorted(eligible, key=lambda r: (-readability_score(r.text), r.review_id))
    return [
        QuoteCandidate(review_id=r.review_id, text=r.text, rating=r.rating) for r in ranked[:limit]
    ]


# --------------------------------------------------------------------------------------
# Stats and ranking (P3-T1 → P3-T3)
# --------------------------------------------------------------------------------------


def _ranking_score(stats: ThemeStats, max_negative: int, negative: int) -> float:
    """Score one theme. See the weights above for why the terms are ordered this way."""
    normalized_negative = negative / max_negative if max_negative else 0.0
    return (
        WEIGHT_SHARE * stats.share_of_week
        + WEIGHT_NEGATIVE * normalized_negative
        # Only *emerging* problems earn momentum credit; a theme in decline is not
        # promoted for having moved.
        + WEIGHT_DELTA * max(0.0, stats.delta_vs_baseline)
    )


def aggregate(
    reviews: list[Review],
    classified: list[ClassifiedReview],
    *,
    settings: Settings,
    target_week: str,
) -> tuple[list[ThemeStats], AggregateReport]:
    """Rank themes for `target_week` and build each one's quote candidate pool.

    Returns every theme's stats, ranked best-first. The caller takes the top three; the
    rest are kept so a theme with no quotable candidate can be substituted (P3-T5).
    """
    report = AggregateReport(target_week=target_week)

    theme_of = {c.review_id: c.theme for c in classified}

    week_reviews = [r for r in reviews if r.iso_week == target_week]
    baseline_reviews = [r for r in reviews if r.iso_week < target_week]
    baseline_weeks = {r.iso_week for r in baseline_reviews}

    report.week_review_count = len(week_reviews)
    report.baseline_review_count = len(baseline_reviews)
    report.baseline_weeks = len(baseline_weeks)
    report.unclassified_in_week = sum(1 for r in week_reviews if theme_of.get(r.review_id) is None)

    minimum = settings.pulse.thresholds.min_week_reviews
    if len(week_reviews) < minimum:
        # A confident-looking note built on a handful of reviews is worse than no note:
        # the reader cannot tell it is noise (architecture.md §15).
        raise AggregateError(
            f"Only {len(week_reviews)} review(s) in {target_week}, below the minimum of "
            f"{minimum}. The note would be statistically meaningless. "
            f"Check --week, or lower thresholds.min_week_reviews deliberately."
        )

    week_total = len(week_reviews)
    negatives: dict[Theme, int] = {}
    stats_by_theme: dict[Theme, ThemeStats] = {}

    for theme in Theme:
        in_week = [r for r in week_reviews if theme_of.get(r.review_id) is theme]
        in_baseline = [r for r in baseline_reviews if theme_of.get(r.review_id) is theme]

        share = len(in_week) / week_total if week_total else 0.0
        baseline_share = len(in_baseline) / len(baseline_reviews) if baseline_reviews else 0.0
        mean_rating = sum(r.rating for r in in_week) / len(in_week) if in_week else 0.0

        negatives[theme] = sum(1 for r in in_week if r.rating in NEGATIVE_RATINGS)
        candidates = quote_candidates(in_week)
        report.candidates_per_theme[theme.value] = len(candidates)

        stats_by_theme[theme] = ThemeStats(
            theme=theme,
            review_count=len(in_week),
            share_of_week=share,
            mean_rating=mean_rating,
            delta_vs_baseline=share - baseline_share,
            quote_candidates=candidates,
        )

    max_negative = max(negatives.values(), default=0)
    scored = [
        (
            _ranking_score(stats, max_negative, negatives[theme]),
            negatives[theme],
            theme.value,
            stats,
        )
        for theme, stats in stats_by_theme.items()
    ]

    # Tie-break: score, then negative volume, then theme name. The name is last so the
    # order is total and reproducible rather than dependent on dict iteration.
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))

    report.scores = {row[2]: row[0] for row in scored}
    report.ranked = [row[2] for row in scored]

    ranked_stats = [row[3] for row in scored]
    log.info("aggregate complete", extra=report.summary())
    return ranked_stats, report


# --------------------------------------------------------------------------------------
# Top-three selection with substitution (P3-T5)
# --------------------------------------------------------------------------------------


def select_top_themes(
    ranked: list[ThemeStats],
    report: AggregateReport,
    *,
    count: int = TOP_THEME_COUNT,
    required_candidates: int = 1,
) -> list[ThemeStats]:
    """Take the top `count` themes that can actually supply a quote.

    A theme with no quotable candidate is skipped in favour of the next ranked one, and
    the substitution is logged. The brief requires three quotes drawn from the themes on
    the page; promoting a theme that cannot supply one would guarantee a validation
    failure the regeneration loop could never fix, because the pool it draws from would
    be empty however many attempts it got.
    """
    selected: list[ThemeStats] = []

    for stats in ranked:
        if len(selected) == count:
            break
        if len(stats.quote_candidates) < required_candidates:
            report.substitutions.append(f"{stats.theme.value} skipped (no quotable candidate)")
            log.warning(
                "theme skipped: no quotable candidate",
                extra={"theme": stats.theme.value, "reviews": stats.review_count},
            )
            continue
        selected.append(stats)

    if len(selected) < count:
        raise AggregateError(
            f"Only {len(selected)} of {count} themes have a quotable review in "
            f"{report.target_week}. The note requires {count} themes each able to supply "
            f"a quote. Candidates per theme: {report.candidates_per_theme}."
        )

    report.selected = [stats.theme.value for stats in selected]
    return selected
