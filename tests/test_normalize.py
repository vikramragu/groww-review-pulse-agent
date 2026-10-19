"""P1-T11: normalization, identity, dedupe, and the corpus window.

Two groups here earn their keep beyond ordinary coverage:

* **ISO week assignment at year boundaries** — a wrong answer misfiles reviews into a week
  that does not contain them, silently, and corrupts the trailing baseline the whole pulse
  is measured against. Nothing fails; the numbers are just wrong.
* **Identity exclusion** — the structural half of the PII defence (architecture.md §10,
  layer 1). These tests assert that identity columns present in an export cannot reach
  stage 2's output even in principle.
"""

from __future__ import annotations

from datetime import date

import pytest

from pulse.ingest.normalize import (
    ENGLISH_MARKERS,
    HINDI_MARKERS,
    NormalizeError,
    SubstanceReport,
    collapse_whitespace,
    filter_substantive,
    filter_to_window,
    is_romanized_hindi,
    iso_week_of,
    iso_week_start,
    non_latin_ratio,
    normalize,
    parse_submitted_on,
    review_id_for,
    validate_window,
    weeks_in_window,
    word_count,
)
from pulse.models import RawReview, Review, Source


def a_raw(**overrides: object) -> RawReview:
    payload: dict[str, object] = {
        "source": "play_store",
        "rating": 2,
        "text": "Withdrawal has been pending for four days.",
        "submitted_at": "2026-10-07",
    }
    payload.update(overrides)
    return RawReview.model_validate(payload)


# --------------------------------------------------------------------------------------
# Whitespace
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  leading and trailing  ", "leading and trailing"),
        ("multiple   internal    spaces", "multiple internal spaces"),
        ("line\nbreaks\tand\ttabs", "line breaks and tabs"),
        ("", ""),
    ],
)
def test_collapse_whitespace(raw: str, expected: str) -> None:
    assert collapse_whitespace(raw) == expected


# --------------------------------------------------------------------------------------
# ISO weeks
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 7), "2026-W41"),
        (date(2026, 1, 1), "2026-W01"),
        (date(2026, 3, 2), "2026-W10"),
        (date(2026, 12, 31), "2026-W53"),
    ],
)
def test_iso_week_of_ordinary_dates(day: date, expected: str) -> None:
    assert iso_week_of(day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2027, 1, 1), "2026-W53"),
        (date(2025, 12, 29), "2026-W01"),
        (date(2024, 12, 30), "2025-W01"),
        (date(2023, 1, 1), "2022-W52"),
        (date(2021, 1, 1), "2020-W53"),
    ],
)
def test_iso_week_uses_the_iso_year_not_the_calendar_year(day: date, expected: str) -> None:
    """Each case falls in an ISO year that is not its calendar year."""
    assert day.year != int(expected[:4])
    assert iso_week_of(day) == expected


def test_iso_week_start_is_a_monday() -> None:
    for week in ("2026-W01", "2026-W41", "2026-W53", "2020-W53"):
        monday = iso_week_start(week)
        assert monday.isoweekday() == 1
        assert iso_week_of(monday) == week


def test_iso_week_start_rejects_malformed_input() -> None:
    for bad in ("2026-41", "not-a-week", "2026-W99"):
        with pytest.raises(NormalizeError):
            iso_week_start(bad)


def test_weeks_in_window_is_oldest_first_and_inclusive() -> None:
    weeks = weeks_in_window("2026-W41", 4)
    assert weeks == ["2026-W38", "2026-W39", "2026-W40", "2026-W41"]


def test_weeks_in_window_crosses_a_53_week_year_correctly() -> None:
    """2026 has 53 ISO weeks, so week arithmetic cannot just subtract from the number."""
    weeks = weeks_in_window("2027-W02", 4)
    assert weeks == ["2026-W52", "2026-W53", "2027-W01", "2027-W02"]


def test_weeks_in_window_rejects_a_non_positive_window() -> None:
    with pytest.raises(NormalizeError):
        weeks_in_window("2026-W41", 0)


# --------------------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-07", date(2026, 10, 7)),
        ("2026-10-07T09:15:00", date(2026, 10, 7)),
        ("2026-10-07T09:15:00Z", date(2026, 10, 7)),
        ("2026-10-07T09:15:00+05:30", date(2026, 10, 7)),
        ("2026-10-07 09:15:00", date(2026, 10, 7)),
        ("2026/10/07", date(2026, 10, 7)),
        ("07/10/2026", date(2026, 10, 7)),
        ("07-10-2026", date(2026, 10, 7)),
        ("Oct 07, 2026", date(2026, 10, 7)),
        ("7 Oct 2026", date(2026, 10, 7)),
        ("  2026-10-07  ", date(2026, 10, 7)),
    ],
)
def test_parse_submitted_on_accepts_common_export_formats(raw: str, expected: date) -> None:
    assert parse_submitted_on(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", "2026-13-45", "yesterday"])
def test_parse_submitted_on_returns_none_for_junk(raw: str) -> None:
    assert parse_submitted_on(raw) is None


# --------------------------------------------------------------------------------------
# review_id
# --------------------------------------------------------------------------------------


def test_review_id_is_stable_for_the_same_content() -> None:
    first = review_id_for(Source.PLAY_STORE, "Withdrawal is slow.", date(2026, 10, 7))
    second = review_id_for(Source.PLAY_STORE, "Withdrawal is slow.", date(2026, 10, 7))
    assert first == second
    assert len(first) == 16


def test_review_id_ignores_whitespace_differences() -> None:
    spaced = review_id_for(Source.PLAY_STORE, "Withdrawal   is  slow.", date(2026, 10, 7))
    plain = review_id_for(Source.PLAY_STORE, "Withdrawal is slow.", date(2026, 10, 7))
    assert spaced == plain


def test_review_id_changes_with_text_and_date() -> None:
    base = review_id_for(Source.PLAY_STORE, "Withdrawal is slow.", date(2026, 10, 7))
    other_text = review_id_for(Source.PLAY_STORE, "Withdrawal is fast.", date(2026, 10, 7))
    other_date = review_id_for(Source.PLAY_STORE, "Withdrawal is slow.", date(2026, 10, 8))

    assert base != other_text
    assert base != other_date


def test_review_id_does_not_depend_on_rating() -> None:
    """A corrected rating must not fork the id, or the P2 classification cache misses."""
    reviews, _ = normalize(
        [
            a_raw(rating=1, text="Same text and date.", submitted_at="2026-10-07"),
            a_raw(rating=4, text="Same text and date.", submitted_at="2026-10-07"),
        ]
    )
    assert len(reviews) == 1


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


def test_normalize_produces_a_valid_review() -> None:
    reviews, report = normalize([a_raw()])
    (review,) = reviews

    assert review.source is Source.PLAY_STORE
    assert review.rating == 2
    assert review.submitted_on == date(2026, 10, 7)
    assert review.iso_week == "2026-W41"
    assert review.scrubbed is False
    assert report.reviews_out == 1


@pytest.mark.parametrize(("raw_rating", "expected"), [(0, 1), (-3, 1), (6, 5), (99, 5)])
def test_out_of_range_ratings_are_clamped(raw_rating: int, expected: int) -> None:
    reviews, report = normalize([a_raw(rating=raw_rating)])
    assert reviews[0].rating == expected
    assert report.ratings_clamped == 1


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_in_range_ratings_are_untouched(rating: int) -> None:
    reviews, report = normalize([a_raw(rating=rating)])
    assert reviews[0].rating == rating
    assert report.ratings_clamped == 0


def test_rows_without_a_rating_are_dropped() -> None:
    reviews, report = normalize([a_raw(rating=None)])
    assert reviews == []
    assert report.dropped == {"missing rating": 1}


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_rows_without_text_are_dropped(text: str) -> None:
    reviews, report = normalize([a_raw(text=text, title=None)])
    assert reviews == []
    assert report.dropped == {"empty text": 1}


def test_rows_with_an_unparseable_date_are_dropped() -> None:
    reviews, report = normalize([a_raw(submitted_at="not-a-date")])
    assert reviews == []
    assert report.dropped == {"unparseable date": 1}


def test_rows_with_no_date_are_dropped() -> None:
    reviews, report = normalize([a_raw(submitted_at=None)])
    assert reviews == []
    assert report.dropped == {"missing date": 1}


def test_title_is_used_only_when_text_is_absent() -> None:
    """Title and text are never concatenated — see `_choose_text` for why.

    A quote must be a contiguous span of what a reviewer actually wrote, because stage 7
    verifies quotes as exact substrings of `Review.text`. Joining two fields with invented
    punctuation would let a "verbatim" quote straddle text nobody wrote.
    """
    from_text, _ = normalize([a_raw(text="Body text.", title="A title")])
    from_title, _ = normalize([a_raw(text=None, title="A title")])

    assert from_text[0].text == "Body text."
    assert "A title" not in from_text[0].text
    assert from_title[0].text == "A title"


def test_text_is_whitespace_collapsed() -> None:
    reviews, _ = normalize([a_raw(text="  too   many \n spaces  ")])
    assert reviews[0].text == "too many spaces"


# --------------------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------------------


def test_exact_duplicates_collapse_to_one() -> None:
    reviews, report = normalize([a_raw(), a_raw(), a_raw()])
    assert len(reviews) == 1
    assert report.duplicates_removed == 2


def test_whitespace_only_differences_are_duplicates() -> None:
    reviews, report = normalize(
        [
            a_raw(text="Withdrawal pending for four days."),
            a_raw(text="Withdrawal   pending  for four days."),
        ]
    )
    assert len(reviews) == 1
    assert report.duplicates_removed == 1


def test_the_first_occurrence_is_kept() -> None:
    reviews, _ = normalize([a_raw(rating=1, text="Same text."), a_raw(rating=5, text="Same text.")])
    assert reviews[0].rating == 1


def test_different_dates_are_not_duplicates() -> None:
    reviews, report = normalize(
        [a_raw(submitted_at="2026-10-07"), a_raw(submitted_at="2026-10-08")]
    )
    assert len(reviews) == 2
    assert report.duplicates_removed == 0


# --------------------------------------------------------------------------------------
# Identity exclusion — PII defence layer 1
# --------------------------------------------------------------------------------------


def test_identity_columns_in_the_export_do_not_reach_stage_two() -> None:
    """`RawReview` may carry identity in `model_extra`; `Review` has nowhere to put it."""
    raw = RawReview.model_validate(
        {
            "source": "play_store",
            "rating": 1,
            "text": "Withdrawal is stuck.",
            "submitted_at": "2026-10-07",
            "user_name": "Not A Realperson",
            "author": "notareal@example.com",
            "device_id": "fake-device-0001",
        }
    )
    assert raw.model_extra  # the raw row really does carry identity

    reviews, _ = normalize([raw])
    serialized = reviews[0].model_dump_json()

    for leaked in ("Not A Realperson", "notareal@example.com", "fake-device-0001"):
        assert leaked not in serialized


def test_review_id_is_not_derived_from_any_identity_field() -> None:
    """Two rows differing only in reviewer identity must produce the same id."""
    with_identity = RawReview.model_validate(
        {
            "source": "play_store",
            "rating": 1,
            "text": "Withdrawal is stuck.",
            "submitted_at": "2026-10-07",
            "user_name": "Someone",
        }
    )
    without = a_raw(rating=1, text="Withdrawal is stuck.", submitted_at="2026-10-07")

    reviews, report = normalize([with_identity, without])
    assert len(reviews) == 1
    assert report.duplicates_removed == 1


# --------------------------------------------------------------------------------------
# Window filtering and validation
# --------------------------------------------------------------------------------------


def windowed_reviews(weeks: list[str]) -> list[Review]:
    return [
        Review(
            review_id=f"{index:016d}",
            source=Source.PLAY_STORE,
            rating=3,
            text=f"Review from {week}.",
            submitted_on=iso_week_start(week),
            iso_week=week,
            scrubbed=False,
        )
        for index, week in enumerate(weeks)
    ]


def test_filter_to_window_keeps_only_weeks_in_range() -> None:
    reviews = windowed_reviews(["2026-W36", "2026-W39", "2026-W41"])
    kept, window = filter_to_window(reviews, "2026-W41", 4)

    assert [review.iso_week for review in kept] == ["2026-W39", "2026-W41"]
    assert window[0] == "2026-W38"


def test_filter_to_window_excludes_weeks_after_the_target() -> None:
    """A future week is outside the window, not merely late."""
    reviews = windowed_reviews(["2026-W41", "2026-W42"])
    kept, _ = filter_to_window(reviews, "2026-W41", 12)
    assert [review.iso_week for review in kept] == ["2026-W41"]


def test_validate_window_accepts_a_wide_enough_corpus() -> None:
    weeks = weeks_in_window("2026-W41", 12)
    present = validate_window(windowed_reviews(weeks), weeks, 8)
    assert len(present) == 12


def test_validate_window_aborts_on_a_narrow_corpus() -> None:
    """The brief asks for 8-12 weeks. A 3-week corpus yields a meaningless baseline."""
    weeks = weeks_in_window("2026-W41", 12)
    narrow = windowed_reviews(weeks[-3:])

    with pytest.raises(NormalizeError, match="spans 3 week"):
        validate_window(narrow, weeks, 8)


def test_validate_window_aborts_on_an_empty_corpus() -> None:
    weeks = weeks_in_window("2026-W41", 12)
    with pytest.raises(NormalizeError, match="No reviews"):
        validate_window([], weeks, 8)


def test_validate_window_error_names_the_actual_span() -> None:
    weeks = weeks_in_window("2026-W41", 12)
    narrow = windowed_reviews(weeks[-2:])

    with pytest.raises(NormalizeError) as exc_info:
        validate_window(narrow, weeks, 8)

    message = str(exc_info.value)
    assert "2026-W40" in message
    assert "minimum of 8" in message


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def test_report_summary_has_no_review_text() -> None:
    _, report = normalize([a_raw(text="Mail notareal@example.com about this.")])
    assert "notareal" not in str(report.summary())


def test_report_counts_reconcile() -> None:
    raws = [a_raw(), a_raw(), a_raw(text=""), a_raw(submitted_at="junk")]
    reviews, report = normalize(raws)

    assert report.rows_in == 4
    assert report.reviews_out == len(reviews) == 1
    assert report.duplicates_removed == 1
    assert sum(report.dropped.values()) == 2


# --------------------------------------------------------------------------------------
# Substance filtering
#
# A third of real Groww reviews are a single word and two thirds are three or fewer. Left
# in, they land in `unclassified` and swell the denominator every theme share is measured
# against -- so "payments was 25% of the week" would mostly be measuring noise.
#
# These filters are sample selection, which is why `SubstanceReport` keeps both counts and
# the rendered note discloses both.
# --------------------------------------------------------------------------------------


def substantive(texts: list[str], **kwargs: object) -> tuple[list[str], SubstanceReport]:
    reviews = [a_review_with(text) for text in texts]
    kept, report = filter_substantive(reviews, **kwargs)  # type: ignore[arg-type]
    return [r.text for r in kept], report


def a_review_with(text: str) -> Review:
    return Review(
        review_id=f"{abs(hash(text)):016x}"[:16],
        source=Source.PLAY_STORE,
        rating=3,
        text=text,
        submitted_on=date(2026, 10, 7),
        iso_week="2026-W41",
        scrubbed=True,
    )


# -- word counting ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("one", 1),
        ("one two", 2),
        ("  spaced   out  words ", 3),
        ("", 0),
        ("a\nb\tc", 3),
        ("good 👍👍 app", 2),
        ("lost ₹500 today", 2),
        ("!!! wow ...", 1),
        ("T+1 settlement", 2),
    ],
)
def test_word_count(text: str, expected: int) -> None:
    """Only tokens with a letter are words, so emoji and bare numbers cannot pad a review."""
    assert word_count(text) == expected


# -- the word floor --------------------------------------------------------------------


def test_reviews_below_the_word_floor_are_dropped() -> None:
    long_enough = "withdrawal has been pending for four days with no update"
    kept, report = substantive(["good", "ok", "very good app", long_enough])
    assert kept == [long_enough]
    assert report.dropped == {"fewer than 9 words": 3}


def test_the_default_rule_is_more_than_eight_words() -> None:
    eight = "one two three four five six seven eight"
    nine = "one two three four five six seven eight nine"
    kept, report = substantive([eight, nine])
    assert kept == [nine]
    assert report.min_words == 9


def test_emoji_cannot_carry_a_review_over_the_floor() -> None:
    """Eight real words plus an emoji is still eight words."""
    kept, report = substantive(["really great app for new investors thank you 🙏"])
    assert kept == []
    assert report.dropped == {"fewer than 9 words": 1}


def test_the_floor_is_configurable() -> None:
    """Left configurable because the right value is a judgement about signal, not a fact."""
    texts = ["withdrawal pending since last week"]  # 5 words
    assert len(substantive(texts, min_words=4)[0]) == 1
    assert len(substantive(texts, min_words=5)[0]) == 1
    assert len(substantive(texts, min_words=8)[0]) == 0


def test_the_four_to_seven_word_band_is_the_cost_of_a_floor_of_eight() -> None:
    """Pinning the known trade-off rather than leaving it implicit.

    "withdrawal pending since last week" is five words and clearly classifiable as
    payments. At the configured floor of 8 it is dropped; at 5 it survives. Recorded so
    the cost of the threshold is visible in the test suite, not only in a comment.
    """
    short_but_real = ["withdrawal pending since last week"]
    assert substantive(short_but_real, min_words=5)[0] == short_but_real
    assert substantive(short_but_real, min_words=8)[0] == []


# -- emoji and punctuation -------------------------------------------------------------


@pytest.mark.parametrize("text", ["👍👍👍", "!!!", "⭐⭐⭐⭐⭐", "😡😡 😡 😡 😡 😡"])
def test_reviews_with_no_letters_are_dropped(text: str) -> None:
    """Counted separately from the word floor: some emoji strings have many "words"."""
    kept, report = substantive([text])
    assert kept == []
    assert report.dropped == {"emoji or punctuation only": 1}


def test_emoji_alongside_real_text_is_kept() -> None:
    kept, _ = substantive(["Withdrawal has been pending for four days 😡😡 with no update"])
    assert len(kept) == 1


# -- script ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ईजी और यह एक बहुत बड़ा योगदान दिया है",
        "আমার জানে কে যে জিজ্ঞেস করো যে প্রকাশ করস",
    ],
)
def test_non_latin_script_reviews_are_dropped(text: str) -> None:
    """The classifier prompt is English; it cannot read these."""
    kept, report = substantive([text])
    assert kept == []
    assert report.dropped == {"non-Latin script": 1}


def test_hinglish_in_latin_script_is_dropped() -> None:
    """Decided 2026-10-15: only English reviews are classified."""
    kept, report = substantive(["paisa nahi aaya withdrawal se bahut din ho gaye hai ab"])
    assert kept == []
    assert report.dropped == {"non-English (romanized)": 1}


@pytest.mark.parametrize(
    "text",
    [
        "paisa nahi aaya withdrawal se bahut din ho gaye hai ab",
        "ye app bahut acha hai mujhe koi problem nahi hui abhi tak",
        "chart open hone mein bahut time lagta hai, please fix karo",
        # "do" and "the" are Hindi words too; the dominance rule still catches this.
        "do din se paise katte hain pata nahin kis cheez ke liye the",
    ],
)
def test_romanized_hindi_is_detected(text: str) -> None:
    assert is_romanized_hindi(text)


@pytest.mark.parametrize(
    "text",
    [
        "bad luck for me, the order failed again today",
        "I am using it since 2021 and it is the best app for mutual funds",
        "Main screen keeps crashing when I open the portfolio tab",
        "To be honest the app is ok but the charges are high",
        # English that borrows one Hindi word stays English.
        "withdrawal pending for a week, paisa still not credited to my bank account",
        "",
        "👍👍👍",
    ],
)
def test_english_is_not_mistaken_for_romanized_hindi(text: str) -> None:
    """The precision side: a false hit here silently drops a real English review."""
    assert not is_romanized_hindi(text)


def test_the_hindi_lexicon_contains_no_common_english_words() -> None:
    assert not HINDI_MARKERS & {"me", "main", "par", "to", "hi", "so", "us", "mat", "ya", "ha"}
    assert not HINDI_MARKERS & ENGLISH_MARKERS


def test_a_little_non_latin_text_does_not_disqualify_a_review() -> None:
    kept, _ = substantive(["Withdrawal pending for four days now, paisa नहीं aaya at all yet"])
    assert len(kept) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [("hello", 0.0), ("नमस्ते", 1.0), ("", 0.0), ("123 !!!", 0.0)],
)
def test_non_latin_ratio(text: str, expected: float) -> None:
    assert non_latin_ratio(text) == pytest.approx(expected)


# -- the report ------------------------------------------------------------------------


def test_the_report_keeps_both_counts() -> None:
    """The denominator the note must disclose, so a theme share is not read as a
    population share."""
    _, report = substantive(
        ["good", "ok", "withdrawal has been pending for four days with no update"]
    )

    assert report.in_window == 3
    assert report.kept == 1
    assert report.summary()["kept_share"] == pytest.approx(1 / 3, abs=0.01)


def test_each_drop_reason_is_counted_separately() -> None:
    """So the cost of each filter is visible rather than lumped together."""
    _, report = substantive(
        [
            "good",
            "👍",
            "नमस्ते जी कैसे हैं आप",
            "ye app bahut acha hai mujhe koi problem nahi hui abhi tak",
            "a real review with quite enough words in it to pass",
        ]
    )

    assert set(report.dropped) == {
        "fewer than 9 words",
        "emoji or punctuation only",
        "non-Latin script",
        "non-English (romanized)",
    }
    assert report.total_dropped == 4


def test_the_report_contains_no_review_text() -> None:
    _, report = substantive(
        ["please mail me at notareal@example.com about my stuck withdrawal right now"]
    )
    assert "notareal" not in str(report.summary())


def test_an_empty_corpus_filters_to_empty() -> None:
    kept, report = filter_substantive([])
    assert kept == []
    assert report.in_window == 0
    assert report.summary()["kept_share"] == 0.0
