"""Stage 2: normalization, identity, dedupe, and the corpus window (P1-T5, P1-T6).

This is **layer 1 of the PII defence** (architecture.md §10). `Review` has no field for
reviewer identity and forbids extras, so the only way identity could survive stage 2 is if
this module explicitly put it there. It does not: every `Review` is built from a fixed set
of keys, and `RawReview.model_extra` — which is where an export's identity columns land —
is never read.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pulse import logging as pulse_logging
from pulse.models import RawReview, Review, Source

log = pulse_logging.get_logger("ingest.normalize")

MIN_RATING = 1
MAX_RATING = 5
REVIEW_ID_LENGTH = 16

_WHITESPACE = re.compile(r"\s+")

#: Date formats tried after `datetime.fromisoformat`, which already covers ISO 8601
#: (including a trailing `Z` and offsets) on Python 3.11+.
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%B %d, %Y",
)


class NormalizeError(Exception):
    """Raised when the corpus as a whole is unusable — not for individual bad rows."""


@dataclass
class NormalizeReport:
    """Counts for the ledger. Never contains review text."""

    rows_in: int = 0
    reviews_out: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    ratings_clamped: int = 0
    duplicates_removed: int = 0
    weeks_present: list[str] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "reviews_out": self.reviews_out,
            "dropped": dict(sorted(self.dropped.items())),
            "ratings_clamped": self.ratings_clamped,
            "duplicates_removed": self.duplicates_removed,
            "weeks_present": self.weeks_present,
            "week_span": len(self.weeks_present),
        }


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip.

    Used both for the review id and for stored text, so the two can never disagree about
    what "the same review" means.
    """
    return _WHITESPACE.sub(" ", text).strip()


def iso_week_of(day: date) -> str:
    """ISO week label, e.g. `2026-W41`.

    Takes the year from `isocalendar()[0]`, **not** `date.year`. They differ at year
    boundaries — 2027-01-01 is in `2026-W53` — and using the calendar year there misfiles
    reviews silently rather than failing.
    """
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def iso_week_start(iso_week: str) -> date:
    """Monday of the given ISO week."""
    try:
        year_part, week_part = iso_week.split("-W")
        return date.fromisocalendar(int(year_part), int(week_part), 1)
    except (ValueError, TypeError) as exc:
        raise NormalizeError(f"Malformed ISO week {iso_week!r}; expected e.g. 2026-W41.") from exc


def weeks_in_window(target_week: str, window_weeks: int) -> list[str]:
    """The `window_weeks` ISO weeks ending at (and including) `target_week`, oldest first.

    Derived by walking back 7 days at a time from the target Monday rather than by
    subtracting from the week number, because week numbers do not arithmetic cleanly
    across years that have 53 weeks.
    """
    if window_weeks < 1:
        raise NormalizeError(f"window_weeks must be at least 1, got {window_weeks}.")

    monday = iso_week_start(target_week)
    weeks = [iso_week_of(monday - timedelta(days=7 * offset)) for offset in range(window_weeks)]
    return list(reversed(weeks))


def parse_submitted_on(value: str) -> date | None:
    """Parse an export's date field into a `date`, or None if unrecognizable."""
    text = value.strip()
    if not text:
        return None

    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def review_id_for(source: Source, text: str, submitted_on: date) -> str:
    """Stable, non-identifying review id.

    `sha256(source | collapsed_text | date)`, truncated. Three deliberate exclusions:

    * **No reviewer field** — the id can never be identity-derived.
    * **No rating or title** — an export that corrects a rating must not fork the id, or
      the classification cache (keyed on review id) would miss on every corrected row.
    * **Pre-scrub text** — ids are assigned at stage 2, before scrubbing at stage 3, so
      tightening a scrubber rule later does not invalidate every cached classification.
    """
    digest = hashlib.sha256(
        f"{source.value}|{collapse_whitespace(text)}|{submitted_on.isoformat()}".encode()
    )
    return digest.hexdigest()[:REVIEW_ID_LENGTH]


def _choose_text(raw: RawReview) -> str | None:
    """Pick the text to analyse and quote from.

    Play Store reviews have no title, so `text` is almost always the whole review. When
    text is missing but a title is present, the title becomes the text.

    We deliberately do **not** concatenate title and text. Every `Review.text` must be a
    contiguous span copied from the export, because stage 7 verifies published quotes are
    exact substrings of it (architecture.md §9). Joining two fields with punctuation we
    invented would let a "verbatim" quote straddle text no reviewer ever wrote.
    """
    text = collapse_whitespace(raw.text) if raw.text else ""
    if text:
        return text
    title = collapse_whitespace(raw.title) if raw.title else ""
    return title or None


def _clamp_rating(rating: int | None, report: NormalizeReport) -> int | None:
    if rating is None:
        return None
    if MIN_RATING <= rating <= MAX_RATING:
        return rating
    report.ratings_clamped += 1
    return min(max(rating, MIN_RATING), MAX_RATING)


def normalize(
    raw_reviews: list[RawReview],
    *,
    report: NormalizeReport | None = None,
) -> tuple[list[Review], NormalizeReport]:
    """Convert raw rows into validated `Review` objects, deduplicated.

    Order is preserved apart from deduplication, which keeps the first occurrence.
    """
    report = report or NormalizeReport()
    report.rows_in = len(raw_reviews)

    seen: set[str] = set()
    reviews: list[Review] = []

    for raw in raw_reviews:
        text = _choose_text(raw)
        if text is None:
            report.drop("empty text")
            continue

        if raw.submitted_at is None:
            report.drop("missing date")
            continue
        submitted_on = parse_submitted_on(raw.submitted_at)
        if submitted_on is None:
            report.drop("unparseable date")
            continue

        rating = _clamp_rating(raw.rating, report)
        if rating is None:
            report.drop("missing rating")
            continue

        review_id = review_id_for(raw.source, text, submitted_on)
        if review_id in seen:
            report.duplicates_removed += 1
            continue
        seen.add(review_id)

        reviews.append(
            Review(
                review_id=review_id,
                source=raw.source,
                rating=rating,
                text=text,
                submitted_on=submitted_on,
                iso_week=iso_week_of(submitted_on),
                scrubbed=False,
            )
        )

    report.reviews_out = len(reviews)
    report.weeks_present = sorted({review.iso_week for review in reviews})
    log.info("normalize complete", extra=report.summary())
    return reviews, report


def filter_to_window(
    reviews: list[Review],
    target_week: str,
    window_weeks: int,
) -> tuple[list[Review], list[str]]:
    """Keep only reviews inside the rolling window ending at `target_week`."""
    window = weeks_in_window(target_week, window_weeks)
    allowed = set(window)
    kept = [review for review in reviews if review.iso_week in allowed]
    return kept, window


def validate_window(
    reviews: list[Review],
    window: list[str],
    min_weeks: int,
) -> list[str]:
    """Abort unless the corpus actually spans `min_weeks` distinct weeks.

    The brief asks for roughly 8-12 weeks of reviews. A corpus covering three weeks would
    still produce a confident-looking pulse with a meaningless week-over-week baseline, so
    this is a hard stop rather than a warning (architecture.md §15).
    """
    present = sorted({review.iso_week for review in reviews})
    if not present:
        raise NormalizeError(
            f"No reviews fall inside the {len(window)}-week window "
            f"{window[0]}..{window[-1]}. Check --week and the export's date range."
        )
    if len(present) < min_weeks:
        raise NormalizeError(
            f"Corpus spans {len(present)} week(s) ({present[0]}..{present[-1]}), "
            f"below the required minimum of {min_weeks}. "
            f"Import a wider export, or lower PULSE_MIN_WINDOW_WEEKS deliberately."
        )
    return present
