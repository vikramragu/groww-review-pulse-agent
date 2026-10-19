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
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
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


# --------------------------------------------------------------------------------------
# Substance filtering
#
# A third of real Groww reviews are a single word ("good", "ok", "nice") and two thirds
# are three words or fewer. Those carry no theme, so left in they would land in
# `unclassified` and swell the denominator every theme share is measured against -- making
# "payments was 25% of the week" a statement mostly about noise.
#
# Applied *after* windowing, so both counts are available: how many reviews the week
# actually received, and how many of them say anything. The note reports both, because
# these filters are sample selection and the shares are not population shares.
# --------------------------------------------------------------------------------------

#: Inclusive minimum: a review needs at least this many words. 9 implements the rule
#: "more than 8 words" (decided 2026-10-15). Configurable -- thresholds.min_review_words.
DEFAULT_MIN_WORDS = 9

#: A review whose letters are mostly non-Latin is in a script the English classifier
#: prompt cannot read.
MAX_NON_LATIN_RATIO = 0.3

#: Bump whenever HINDI_MARKERS or ENGLISH_MARKERS change, so a corpus-size shift between
#: two runs can be traced to the lexicon rather than to the reviews.
ROMANIZED_LEXICON_VERSION = 1

#: Romanized-Hindi function words and very common content words. Deliberately excludes
#: anything that is also an ordinary English word ("me", "main", "par", "to", "hi", "so",
#: "us", "mat", "ya", "ha"), because a false hit there drops an English review.
HINDI_MARKERS = frozenset(
    """
    hai hain hoon hu tha thi nahi nahin nhi nai ni ka ki ke ko se mein mai pe tak bhi toh
    kya kyu kyun kyon kaise kab kaha kahan jab tab agar lekin magar aur kyunki mera meri
    mere mujhe muje mujhko hum hame humein hamara aap apna apne apni tum tumhara tera teri
    yeh ye woh wo isme isse iska iski iske uska uski uske unka kuch koi sab sabse bahut
    bohot bhut bahot zyada jyada kam thoda accha acha achha achchha bekar bakwas badiya
    badhiya bura ganda kar karo karna karta karte karti kare karke kiya kiye kr krne krna
    krta krte raha rahe rahi rha rhe rhi hua hue hui hota hoti hote hoga hogi ho gaya gaye
    gayi gya gye gyi diya diye liya liye dena lena dete lete deta leta jata jati jate
    jaata jaate wala wale wali chahiye abhi bhai yaha yahan waha wahan sirf bilkul paisa
    paise rupaye kaam samajh pata haan kabhi kariye kijiye karein karen baat baad pehle
    phir fir yaar jo jis jisme jisse isko usko unko inko hamesha hamare apka aapka apke
    aapke apki aapki wajah kitna jitna utna itna sath saath mila milta milti aata aati aa
    dikhta dikhata dikhati lagta lagti chalta chalti wapas jaldi kyonki matlab sahi galat
    glt thik theek mujhse humko tumko
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

#: English function words. A review dense in these is English even if it borrows a few
#: Hindi words ("withdrawal nahi aaya, please check this issue").
ENGLISH_MARKERS = frozenset(
    """
    the a an is are was were be been being it its this that these those to of in on for
    with and but or not no my i i'm you your me we our they their he she have has had do
    does did very so can could will would should from at by as if all there what when why
    how which who after before because also just only than then
    """.split()  # noqa: SIM905 -- a word list reads better as prose
)

#: A review is romanized Hindi when at least this share of its words are Hindi markers...
MIN_HINDI_MARKER_RATIO = 0.2
#: ...and fewer than this share are English markers...
MAX_ENGLISH_MARKER_RATIO = 0.15
#: ...or when Hindi markers alone reach this share. Needed because "do" and "the" are
#: Hindi words too, so a Hinglish review can trip the English side by accident.
HINDI_DOMINANT_RATIO = 0.4

_LATIN_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")


@lru_cache(maxsize=4096)
def _is_latin_letter(char: str) -> bool:
    if char.isascii():
        return char.isalpha()
    return "LATIN" in unicodedata.name(char, "")


def word_count(text: str) -> int:
    """Whitespace-separated tokens containing at least one letter.

    Emoji, "!!!" and "₹500" are not words, so they cannot carry a review over the floor.
    """
    return sum(1 for token in _WHITESPACE.split(text) if has_letters(token))


def is_romanized_hindi(text: str) -> bool:
    """True when the review is Hindi written in Latin script (Hinglish).

    Two-sided on purpose: Hindi markers alone would drop English reviews that borrow a
    word or two, so the review must also be short on English function words -- unless
    Hindi markers dominate outright.
    """
    words = _LATIN_WORD.findall(text.lower())
    if not words:
        return False
    hindi = sum(1 for word in words if word in HINDI_MARKERS) / len(words)
    english = sum(1 for word in words if word in ENGLISH_MARKERS) / len(words)
    if hindi >= HINDI_DOMINANT_RATIO:
        return True
    return hindi >= MIN_HINDI_MARKER_RATIO and english < MAX_ENGLISH_MARKER_RATIO


def non_latin_ratio(text: str) -> float:
    """Share of this text's letters that are not Latin. 0.0 when it has no letters."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if not _is_latin_letter(char)) / len(letters)


def has_letters(text: str) -> bool:
    return any(char.isalpha() for char in text)


@dataclass
class SubstanceReport:
    """Counts only. `in_window` is the denominator the note must disclose."""

    in_window: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    min_words: int = DEFAULT_MIN_WORDS

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    @property
    def total_dropped(self) -> int:
        return sum(self.dropped.values())

    def summary(self) -> dict[str, Any]:
        return {
            "in_window": self.in_window,
            "kept": self.kept,
            "kept_share": round(self.kept / self.in_window, 3) if self.in_window else 0.0,
            "dropped": dict(sorted(self.dropped.items())),
            "min_words": self.min_words,
            "romanized_lexicon_version": ROMANIZED_LEXICON_VERSION,
        }


def filter_substantive(
    reviews: list[Review],
    *,
    min_words: int = DEFAULT_MIN_WORDS,
) -> tuple[list[Review], SubstanceReport]:
    """Keep only reviews that can carry a theme.

    Four rules, each counted separately so the cost of the filtering is visible:

    * **no letters** -- emoji and punctuation only; nothing to classify.
    * **non-Latin script** -- a language the English classifier prompt cannot read.
    * **romanized Hindi** -- not English, even though it is in Latin script.
    * **too few words** -- below the floor, there is no topic to find.
    """
    report = SubstanceReport(in_window=len(reviews), min_words=min_words)
    kept: list[Review] = []

    for review in reviews:
        text = review.text
        if not has_letters(text):
            report.drop("emoji or punctuation only")
            continue
        if non_latin_ratio(text) > MAX_NON_LATIN_RATIO:
            report.drop("non-Latin script")
            continue
        if is_romanized_hindi(text):
            report.drop("non-English (romanized)")
            continue
        if word_count(text) < min_words:
            report.drop(f"fewer than {min_words} words")
            continue
        kept.append(review)

    report.kept = len(kept)
    log.info("substance filter complete", extra=report.summary())
    return kept, report
