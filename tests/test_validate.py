"""P3-T17: the validation gate.

The highest-stakes test file after `test_scrub.py`. Every hard constraint from the brief
that is not already a schema constraint is enforced here, and each one fails *silently* in
production if this file is wrong — a paraphrased quote, a 260-word note, and a fabricated
star rating all look completely fine to a human reviewer.

The verbatim tests deserve particular attention. They assert that the check rejects exactly
the edits a model makes when it "improves" a quote: fixed spelling, adjusted punctuation,
trimmed filler, merged clauses. Normalizing those away would leave the check passing on
text no reviewer wrote, which is the same as not having the check at all.
"""

from __future__ import annotations

import json

import pytest

from pulse.analysis.scrub import PiiLeakError
from pulse.llm import json_schema_for
from pulse.models import (
    ActionIdea,
    PulseNote,
    Quote,
    QuoteCandidate,
    Theme,
    ThemeStats,
    direction_of,
)
from pulse.rendering.render import render_note
from pulse.rendering.validate import (
    WORD_LIMIT,
    assert_publishable,
    count_note_words,
    find_verbatim_source,
    ground_quote,
    ground_theme_summaries,
    normalize_for_comparison,
    validate_note,
)

ORIGINAL = "Withdrawal has been pending for four days with no update from support."
SECOND = "App crashes every time I open the chart on this phone."
THIRD = "Got charged an AMC I was never told about at signup."

THEMES = [Theme.PAYMENTS_WITHDRAWALS, Theme.APP_RELIABILITY, Theme.CHARGES_SUPPORT]


def candidate(text: str, review_id: str, rating: int = 2) -> QuoteCandidate:
    return QuoteCandidate(review_id=review_id, text=text, rating=rating)


def pools() -> dict[Theme, list[QuoteCandidate]]:
    return {
        Theme.PAYMENTS_WITHDRAWALS: [candidate(ORIGINAL, "a" * 16, 1)],
        Theme.APP_RELIABILITY: [candidate(SECOND, "b" * 16, 2)],
        Theme.CHARGES_SUPPORT: [candidate(THIRD, "c" * 16, 3)],
    }


def a_note(**overrides: object) -> PulseNote:
    payload: dict[str, object] = {
        "iso_week": "2026-W41",
        "headline": "Withdrawal delays dominate this week",
        "top_themes": [
            {
                "theme": theme.value,
                "summary": f"{theme.value} summary.",
                "review_count": 10,
                "direction": "flat",
            }
            for theme in THEMES
        ],
        "quotes": [
            {"text": ORIGINAL, "theme": "payments_withdrawals", "rating": 1, "review_id": "x"},
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ],
        "actions": [
            {
                "theme": theme.value,
                "action": f"Do something about {theme.value}.",
                "rationale": "Because the reviews say so.",
            }
            for theme in THEMES
        ],
    }
    payload.update(overrides)
    return PulseNote.model_validate(payload)


def check(note: PulseNote, **kwargs: object):
    markdown = render_note(note, corpus_size=241)
    return validate_note(
        note,
        markdown,
        allowed_themes=THEMES,
        candidates_by_theme=pools(),
        **kwargs,  # type: ignore[arg-type]
    )


def failed_checks(result) -> set[str]:
    return {failure.check for failure in result.failures}


def an_over_long_note() -> PulseNote:
    """A note that blows the word budget using the fields that actually carry prose.

    `headline` is capped at 100 characters by the schema, so it cannot be used to build
    one -- the length has to come from the summaries and rationales, which is also where
    a real over-long note would come from.
    """
    filler_summary = "padding word " * 16  # ~220 chars, the field maximum
    filler_action = "padding action word " * 8  # ~160 chars
    filler_rationale = "padding rationale word " * 8  # ~200 chars
    return a_note(
        top_themes=[
            {
                "theme": theme.value,
                "summary": filler_summary[:220].strip(),
                "review_count": 10,
                "direction": "flat",
            }
            for theme in THEMES
        ],
        actions=[
            {
                "theme": theme.value,
                "action": filler_action[:160].strip(),
                "rationale": filler_rationale[:200].strip(),
            }
            for theme in THEMES
        ],
    )


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_a_compliant_note_passes() -> None:
    result = check(a_note())
    assert result.ok, result.messages()
    assert not result.fatal


def test_a_compliant_note_is_grounded() -> None:
    """Even a passing note gets its provenance replaced with the source review's."""
    result = check(a_note())
    assert result.grounded_note is not None
    assert [q.review_id for q in result.grounded_note.quotes] == [
        "a" * 16,
        "b" * 16,
        "c" * 16,
    ]
    assert [q.rating for q in result.grounded_note.quotes] == [1, 2, 3]


# --------------------------------------------------------------------------------------
# The verbatim check (P3-T13) — the edits a model actually makes
# --------------------------------------------------------------------------------------


PARAPHRASES = [
    pytest.param(
        "Withdrawal has been pending for four days with no update from Support.",
        id="capitalised-a-word",
    ),
    pytest.param(
        "Withdrawal has been pending for four days, with no update from support.",
        id="added-a-comma",
    ),
    pytest.param(
        "Withdrawal has been pending four days with no update from support.",
        id="trimmed-filler",
    ),
    pytest.param(
        "My withdrawal has been pending for four days with no update from support.",
        id="added-a-word",
    ),
    pytest.param(
        "Withdrawal pending four days, no update from support.",
        id="rewritten-shorter",
    ),
    pytest.param(
        "Withdrawal has been pending for 4 days with no update from support.",
        id="digit-for-word",
    ),
    pytest.param(
        "Withdrawal has been pending for four days with no update from support!",
        id="changed-punctuation",
    ),
]


@pytest.mark.parametrize("paraphrase", PARAPHRASES)
def test_a_paraphrased_quote_is_rejected(paraphrase: str) -> None:
    """Each of these reads fine to a human and violates the brief."""
    note = a_note(
        quotes=[
            {"text": paraphrase, "theme": "payments_withdrawals", "rating": 1, "review_id": "x"},
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    result = check(note)

    assert not result.ok
    assert "verbatim" in failed_checks(result)


def test_a_verbatim_quote_is_accepted() -> None:
    assert check(a_note()).ok


def test_dropping_the_trailing_punctuation_is_accepted() -> None:
    """Not a paraphrase: the text without its final full stop is still an exact substring.

    Worth pinning explicitly, because it looks like the kind of edit the check should
    reject. It is not -- nothing was added or altered, so the quoted words are still
    exactly the reviewer's.
    """
    note = a_note(
        quotes=[
            {
                "text": ORIGINAL.rstrip("."),
                "theme": "payments_withdrawals",
                "rating": 1,
                "review_id": "x",
            },
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    assert check(note).ok


def test_a_verbatim_substring_is_accepted() -> None:
    """Quoting a clause from a longer review is legitimate."""
    note = a_note(
        quotes=[
            {
                "text": "pending for four days with no update from support.",
                "theme": "payments_withdrawals",
                "rating": 1,
                "review_id": "x",
            },
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    assert check(note).ok


def test_only_whitespace_is_normalized() -> None:
    """Whitespace differences are forgiven; nothing else is."""
    assert normalize_for_comparison("a   b\n c ") == "a b c"
    # Case and punctuation survive normalization, so they still fail comparison.
    assert normalize_for_comparison("Support.") != normalize_for_comparison("support.")


def test_a_whitespace_only_difference_is_accepted() -> None:
    spaced = ORIGINAL.replace(" for ", "  for  ")
    note = a_note(
        quotes=[
            {"text": spaced, "theme": "payments_withdrawals", "rating": 1, "review_id": "x"},
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    assert check(note).ok


def test_the_rejection_message_tells_the_model_what_to_do() -> None:
    """Feedback, not a bare retry: "try again" produces a differently-wrong note."""
    note = a_note(
        quotes=[
            {
                "text": "Withdrawal pending four days, no update.",
                "theme": "payments_withdrawals",
                "rating": 1,
                "review_id": "x",
            },
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    (message,) = [m for m in check(note).messages() if "verbatim" in m]
    assert "character-for-character" in message
    assert "do not fix spelling" in message


def test_find_verbatim_source_returns_the_matching_candidate() -> None:
    pool = [candidate(SECOND, "b" * 16), candidate(ORIGINAL, "a" * 16, 1)]
    source = find_verbatim_source(ORIGINAL, pool)
    assert source is not None and source.review_id == "a" * 16


def test_find_verbatim_source_returns_none_for_an_empty_quote() -> None:
    assert find_verbatim_source("   ", [candidate(ORIGINAL, "a" * 16)]) is None


# --------------------------------------------------------------------------------------
# Provenance grounding — fabricated ids and ratings
# --------------------------------------------------------------------------------------


def test_fabricated_provenance_is_corrected_and_counted() -> None:
    """Observed in a real run: the model invented ids (`r001`) and guessed ratings.

    The text was correctly verified while the attribution beside it was fiction. A
    verbatim quote under the wrong star rating is still a misleading artifact, so the
    pipeline derives both from the matched review rather than trusting them.
    """
    result = check(a_note())

    assert result.provenance_corrections == 3
    assert result.grounded_note is not None
    assert all(q.review_id in {"a" * 16, "b" * 16, "c" * 16} for q in result.grounded_note.quotes)


def test_correct_provenance_is_not_counted_as_a_correction() -> None:
    note = a_note(
        quotes=[
            {"text": ORIGINAL, "theme": "payments_withdrawals", "rating": 1, "review_id": "a" * 16},
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "b" * 16},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "c" * 16},
        ]
    )
    assert check(note).provenance_corrections == 0


def test_ground_quote_replaces_both_fields() -> None:
    quote = Quote(text=ORIGINAL, theme=Theme.PAYMENTS_WITHDRAWALS, rating=5, review_id="made-up")
    source = candidate(ORIGINAL, "a" * 16, rating=1)

    corrected, changed = ground_quote(quote, source)

    assert changed
    assert corrected.rating == 1
    assert corrected.review_id == "a" * 16
    assert corrected.text == quote.text  # the verified text is never touched


# --------------------------------------------------------------------------------------
# Word count (P3-T12)
# --------------------------------------------------------------------------------------


def test_the_word_limit_boundary() -> None:
    """Tested at exactly the limit and one over, because an off-by-one here makes the
    retry loop fail in ways that look like model disobedience."""
    body = " ".join(["word"] * (WORD_LIMIT - 4))
    at_limit = f"# Title\n\n*meta*\n\n**{body} one two three four**\n"
    over = f"# Title\n\n*meta*\n\n**{body} one two three four five**\n"

    assert count_note_words(at_limit) == WORD_LIMIT
    assert count_note_words(over) == WORD_LIMIT + 1


def test_an_over_long_note_is_rejected() -> None:
    result = check(an_over_long_note())

    assert not result.ok
    assert "word_count" in failed_checks(result)
    assert result.word_count > WORD_LIMIT


def test_the_word_count_message_says_how_much_to_cut() -> None:
    (message,) = [m for m in check(an_over_long_note()).messages() if "words" in m]
    assert "Cut about" in message
    # And it tells the model where *not* to cut from.
    assert "must stay verbatim" in message


def test_headings_are_not_counted() -> None:
    assert count_note_words("# A Long Heading With Many Words\n") == 0


def test_the_metadata_line_is_not_counted() -> None:
    assert count_note_words("*241 reviews in window - generated 2026-10-12*\n") == 0


def test_bold_headline_text_is_counted() -> None:
    assert count_note_words("**three counted words**\n") == 3


def test_markdown_syntax_is_not_counted_as_words() -> None:
    plain = count_note_words("one two three\n")
    marked = count_note_words("- **one** _two_ `three`\n")
    assert plain == marked == 3


def test_list_numbering_is_not_counted() -> None:
    assert count_note_words("1. one two three\n") == 3


def test_block_quote_markers_are_not_counted() -> None:
    assert count_note_words("> one two three\n>\n") == 3


def test_the_doc_link_footer_is_not_counted() -> None:
    """It is a pointer, not part of the note."""
    assert count_note_words("[Open the full pulse](https://docs.example/d/abc)\n") == 0


def test_quote_and_action_text_are_counted() -> None:
    """Explicitly, because they are the parts a model would try to exclude."""
    note = a_note()
    markdown = render_note(note, corpus_size=241)
    without_quotes = count_note_words(
        "\n".join(line for line in markdown.splitlines() if not line.startswith(">"))
    )
    assert count_note_words(markdown) > without_quotes


# --------------------------------------------------------------------------------------
# Theme, quote and action constraints (P3-T14)
# --------------------------------------------------------------------------------------


def test_a_theme_outside_this_weeks_top_three_is_rejected() -> None:
    note = a_note(
        top_themes=[
            {
                "theme": "onboarding_kyc",  # not in THEMES
                "summary": "Not this week's theme.",
                "review_count": 5,
                "direction": "up",
            },
            {
                "theme": "app_reliability",
                "summary": "s",
                "review_count": 5,
                "direction": "flat",
            },
            {
                "theme": "charges_support",
                "summary": "s",
                "review_count": 5,
                "direction": "flat",
            },
        ]
    )
    assert "theme_validity" in failed_checks(check(note))


def test_repeating_a_theme_is_rejected() -> None:
    repeated = {
        "theme": "payments_withdrawals",
        "summary": "s",
        "review_count": 5,
        "direction": "flat",
    }
    note = a_note(top_themes=[repeated, repeated, dict(repeated)])
    assert "theme_distinctness" in failed_checks(check(note))


def test_two_quotes_from_the_same_review_are_rejected() -> None:
    """The brief asks for three quotes, which means three reviews."""
    note = a_note(
        quotes=[
            {"text": ORIGINAL, "theme": "payments_withdrawals", "rating": 1, "review_id": "x"},
            {
                "text": "pending for four days with no update",
                "theme": "payments_withdrawals",
                "rating": 1,
                "review_id": "x",
            },
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    assert "quote_distinctness" in failed_checks(check(note))


def test_a_quote_attributed_to_an_off_page_theme_is_rejected() -> None:
    note = a_note(
        quotes=[
            {"text": ORIGINAL, "theme": "onboarding_kyc", "rating": 1, "review_id": "x"},
            {"text": SECOND, "theme": "app_reliability", "rating": 2, "review_id": "y"},
            {"text": THIRD, "theme": "charges_support", "rating": 3, "review_id": "z"},
        ]
    )
    assert "quote_theme" in failed_checks(check(note))


def test_an_action_naming_an_off_page_theme_is_rejected() -> None:
    note = a_note(
        actions=[
            {
                "theme": "onboarding_kyc",
                "action": "Fix onboarding.",
                "rationale": "Not on the page.",
            },
            {"theme": "app_reliability", "action": "a", "rationale": "r"},
            {"theme": "charges_support", "action": "a", "rationale": "r"},
        ]
    )
    assert "action_grounding" in failed_checks(check(note))


# --------------------------------------------------------------------------------------
# Cardinality is a schema constraint, not a gate check
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 4])
@pytest.mark.parametrize("field", ["top_themes", "quotes", "actions"])
def test_wrong_cardinality_never_parses(field: str, count: int) -> None:
    """Enforced by `PulseNote` itself, so a wrong count cannot reach the gate at all."""
    from pydantic import ValidationError

    base = a_note().model_dump(mode="json")
    template = base[field][0]
    with pytest.raises(ValidationError):
        PulseNote.model_validate({**base, field: [template] * count})


# --------------------------------------------------------------------------------------
# PII rescan (P3-T15) — fatal, not retryable
# --------------------------------------------------------------------------------------


def test_pii_in_the_note_is_fatal() -> None:
    """A PII hit here means stage 3 has a gap. Regenerating would treat a scrubber bug as
    a drafting problem — and might succeed by luck while leaving the defect in place."""
    leaky = "Withdrawal stuck, mail me at notareal@example.com about it now please."
    note = a_note(headline=leaky)
    markdown = render_note(note, corpus_size=241)
    result = validate_note(
        note,
        markdown,
        allowed_themes=THEMES,
        candidates_by_theme=pools(),
    )

    assert not result.ok
    assert result.fatal
    assert "pii" in failed_checks(result)


def test_the_pii_message_does_not_echo_the_pii() -> None:
    """Reporting a leak by printing the leak would be its own leak."""
    note = a_note(headline="Contact notareal@example.com for the details of this.")
    markdown = render_note(note, corpus_size=241)
    result = validate_note(note, markdown, allowed_themes=THEMES, candidates_by_theme=pools())

    (message,) = [m for m in result.messages() if "PII" in m]
    assert "notareal@example.com" not in message
    assert "stage 3" in message


# --------------------------------------------------------------------------------------
# All failures are reported together
# --------------------------------------------------------------------------------------


def test_every_failure_is_reported_in_one_pass() -> None:
    """So the retry can fix everything at once instead of discovering one problem per
    attempt and burning the budget."""
    note = an_over_long_note().model_copy(
        update={
            "quotes": [
                Quote(
                    text="A quote nobody ever wrote down anywhere at all.",
                    theme=Theme.PAYMENTS_WITHDRAWALS,
                    rating=1,
                    review_id="x",
                ),
                Quote(text=SECOND, theme=Theme.APP_RELIABILITY, rating=2, review_id="y"),
                Quote(text=THIRD, theme=Theme.CHARGES_SUPPORT, rating=3, review_id="z"),
            ],
            # Keeps the padding that puts the note over the limit, while pointing one
            # action at a theme that is not on the page.
            "actions": [
                ActionIdea(
                    theme=Theme.ONBOARDING_KYC,
                    action=("padding action word " * 8)[:160].strip(),
                    rationale=("padding rationale word " * 8)[:200].strip(),
                ),
                ActionIdea(
                    theme=Theme.APP_RELIABILITY,
                    action=("padding action word " * 8)[:160].strip(),
                    rationale=("padding rationale word " * 8)[:200].strip(),
                ),
                ActionIdea(
                    theme=Theme.CHARGES_SUPPORT,
                    action=("padding action word " * 8)[:160].strip(),
                    rationale=("padding rationale word " * 8)[:200].strip(),
                ),
            ],
        }
    )
    checks = failed_checks(check(note))
    assert {"word_count", "verbatim", "action_grounding"} <= checks


# --------------------------------------------------------------------------------------
# The final publish guard
# --------------------------------------------------------------------------------------


def test_assert_publishable_accepts_a_passing_note() -> None:
    note = a_note()
    markdown = render_note(note, corpus_size=241)
    assert_publishable(check(note), markdown)


def test_assert_publishable_refuses_a_failing_note() -> None:
    note = an_over_long_note()
    markdown = render_note(note, corpus_size=241)
    with pytest.raises(ValueError, match="failed validation"):
        assert_publishable(check(note), markdown)


def test_assert_publishable_raises_pii_leak_for_a_fatal_result() -> None:
    note = a_note(headline="Mail notareal@example.com about this issue right now.")
    markdown = render_note(note, corpus_size=241)
    result = validate_note(note, markdown, allowed_themes=THEMES, candidates_by_theme=pools())
    with pytest.raises(PiiLeakError):
        assert_publishable(result, markdown)


# --------------------------------------------------------------------------------------
# Figures belong to the pipeline, not the prose
# --------------------------------------------------------------------------------------


def theme_stats(theme: Theme, delta: float) -> ThemeStats:
    return ThemeStats(
        theme=theme,
        review_count=37,
        share_of_week=0.257,
        mean_rating=2.62,
        delta_vs_baseline=delta,
        quote_candidates=[],
        baseline_weeks=11,
    )


def test_the_model_never_sees_a_figures_field() -> None:
    assert "figures" not in json.dumps(json_schema_for(PulseNote))


def test_grounding_replaces_the_models_count_and_direction() -> None:
    """The model said 10 reviews, flat; stage 5 says 37, up 5.6 points."""
    stats = [theme_stats(t, 0.056) for t in THEMES]
    grounded = ground_theme_summaries(a_note(), stats)

    for summary in grounded.top_themes:
        assert summary.review_count == 37
        assert summary.direction == "up"
        assert summary.figures is not None
        assert summary.figures.baseline_weeks == 11
        assert summary.figures.delta_vs_baseline == 0.056


def test_direction_follows_the_one_point_band() -> None:
    assert direction_of(0.011) == "up"
    assert direction_of(-0.011) == "down"
    assert direction_of(0.01) == "flat"
    assert direction_of(-0.004) == "flat"


def test_grounding_leaves_an_off_page_theme_for_the_validity_check() -> None:
    stats = [theme_stats(Theme.APP_RELIABILITY, 0.0)]
    grounded = ground_theme_summaries(a_note(), stats)
    untouched = [s for s in grounded.top_themes if s.theme is not Theme.APP_RELIABILITY]
    assert all(s.figures is None for s in untouched)


@pytest.mark.parametrize(
    "summary",
    [
        "Up 5.6% week-over-week as login failures spread.",
        "Login failures in 37 reviews.",
        "A quarter of reviews (25.7%) mention login.",
    ],
)
def test_a_number_in_a_summary_is_rejected(summary: str) -> None:
    themes = [
        {"theme": t.value, "summary": summary, "review_count": 10, "direction": "flat"}
        for t in THEMES
    ]
    result = check(a_note(top_themes=themes))
    assert "figures" in failed_checks(result)
    assert "write words only" in " ".join(result.messages())


def test_a_number_in_the_headline_is_rejected() -> None:
    result = check(a_note(headline="Reliability up 5.6 points this week"))
    assert "figures" in failed_checks(result)


def test_figures_printed_by_the_renderer_do_not_trip_the_check() -> None:
    """The check reads the model's prose, not the rendered line that carries figures."""
    note = ground_theme_summaries(a_note(), [theme_stats(t, 0.056) for t in THEMES])
    result = check(note)
    assert result.ok, result.messages()
    assert all(s.figures is not None for s in result.grounded_note.top_themes)  # type: ignore[union-attr]
