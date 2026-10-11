"""P3-T18: the regeneration loop.

The loop is the only reason the verbatim check is survivable: a model *will* paraphrase,
so the gate has to be able to say "no, copy it exactly" and get a better answer. These
tests script that conversation with `FakeLlm` -- which is the whole reason `LlmPort` exists.

Three outcomes, and the distinction between them is the point:

* **pass** -- cleared every check, publishable.
* **exhausted** -- every attempt failed. Aborts *without publishing*; a non-compliant note
  is worse than no note.
* **fatal** -- PII reached the rendered note. Aborts immediately without retrying, because
  that is a stage 3 defect and regenerating would treat a scrubber bug as a drafting one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse.analysis.draft import DraftError, draft_note
from pulse.config import Settings, load_settings
from pulse.llm import FakeLlm
from pulse.models import QuoteCandidate, Theme, ThemeStats

ORIGINAL = "Withdrawal has been pending for four days with no update from support."
SECOND = "App crashes every time I open the chart on this phone."
THIRD = "Got charged an AMC I was never told about at signup."

THEMES = [Theme.PAYMENTS_WITHDRAWALS, Theme.APP_RELIABILITY, Theme.CHARGES_SUPPORT]
TEXTS = {
    Theme.PAYMENTS_WITHDRAWALS: ORIGINAL,
    Theme.APP_RELIABILITY: SECOND,
    Theme.CHARGES_SUPPORT: THIRD,
}


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


def stats() -> list[ThemeStats]:
    return [
        ThemeStats(
            theme=theme,
            review_count=10,
            share_of_week=0.2,
            mean_rating=2.0,
            delta_vs_baseline=0.01,
            quote_candidates=[
                QuoteCandidate(
                    review_id=chr(ord("a") + index) * 16,
                    text=TEXTS[theme],
                    rating=index + 1,
                )
            ],
        )
        for index, theme in enumerate(THEMES)
    ]


def reply(*, quotes: list[str] | None = None, headline: str = "A short headline") -> dict:
    """A note payload. `quotes` overrides the quote text, to script a paraphrase."""
    texts = quotes or [TEXTS[t] for t in THEMES]
    return {
        "iso_week": "2026-W41",
        "headline": headline,
        "top_themes": [
            {
                "theme": theme.value,
                "summary": f"{theme.value} short summary.",
                "review_count": 10,
                "direction": "flat",
            }
            for theme in THEMES
        ],
        "quotes": [
            {
                "text": text,
                "theme": theme.value,
                # Deliberately wrong: the pipeline derives the real values.
                "rating": 5,
                "review_id": f"r00{index}",
            }
            for index, (theme, text) in enumerate(zip(THEMES, texts, strict=True))
        ],
        "actions": [
            {
                "theme": theme.value,
                "action": f"Fix {theme.value}.",
                "rationale": "Reviews say so.",
            }
            for theme in THEMES
        ],
    }


def run(settings: Settings, llm: FakeLlm):
    return draft_note(stats(), llm=llm, settings=settings, iso_week="2026-W41", corpus_size=241)


# --------------------------------------------------------------------------------------
# Pass
# --------------------------------------------------------------------------------------


def test_a_compliant_first_attempt_is_accepted(settings: Settings) -> None:
    outcome = run(settings, FakeLlm(responses=[reply()]))

    assert outcome.result.ok
    assert outcome.report.accepted_on_attempt == 1
    assert outcome.report.attempts == 1
    assert outcome.markdown.startswith("# Groww Review Pulse")


def test_the_accepted_note_carries_derived_provenance(settings: Settings) -> None:
    """The model sent rating 5 and id "r000" for every quote; neither survives."""
    outcome = run(settings, FakeLlm(responses=[reply()]))

    assert outcome.report.provenance_corrections == 3
    assert [q.rating for q in outcome.note.quotes] == [1, 2, 3]
    assert [q.review_id for q in outcome.note.quotes] == ["a" * 16, "b" * 16, "c" * 16]
    # And the rendered markdown shows the corrected ratings, not the model's.
    assert "— 1★" in outcome.markdown
    assert "— 5★" not in outcome.markdown


def test_the_model_is_never_shown_the_full_corpus(settings: Settings) -> None:
    """It gets three themes and their pools -- which is what bounds the verbatim check."""
    llm = FakeLlm(responses=[reply()])
    run(settings, llm)

    sent = llm.calls[0]["user"]
    assert ORIGINAL in sent
    assert "REVIEWS IN WINDOW: 241" in sent
    # Provenance is not shown: displaying ids would only invite the model to echo them.
    assert "a" * 16 not in sent


# --------------------------------------------------------------------------------------
# Fail then pass -- the loop's reason for existing
# --------------------------------------------------------------------------------------


def test_a_paraphrase_is_rejected_then_a_verbatim_quote_accepted(
    settings: Settings,
) -> None:
    paraphrased = reply(quotes=["Withdrawal pending four days, no update.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased, reply()])

    outcome = run(settings, llm)

    assert outcome.result.ok
    assert outcome.report.attempts == 2
    assert outcome.report.accepted_on_attempt == 2
    assert outcome.report.failures_by_check["verbatim"] == 1


def test_the_retry_names_the_specific_failure(settings: Settings) -> None:
    """ "Try again" produces a differently-wrong note; naming the fault does not."""
    paraphrased = reply(quotes=["Withdrawal pending four days, no update.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased, reply()])
    run(settings, llm)

    retry_prompt = llm.calls[1]["user"]
    assert "was rejected for these reasons" in retry_prompt
    assert "character-for-character" in retry_prompt
    # The first attempt carried no feedback.
    assert "was rejected" not in llm.calls[0]["user"]


def test_an_unparseable_reply_is_retried(settings: Settings) -> None:
    llm = FakeLlm(responses=["not json at all", reply()])
    outcome = run(settings, llm)

    assert outcome.result.ok
    assert outcome.report.parse_failures == 1
    assert outcome.report.attempts == 2


def test_wrong_cardinality_is_retried_as_a_parse_failure(settings: Settings) -> None:
    """Four quotes never parses, because the count is a PulseNote constraint."""
    four = reply()
    four["quotes"] = [*four["quotes"], dict(four["quotes"][0])]
    llm = FakeLlm(responses=[four, reply()])

    outcome = run(settings, llm)

    assert outcome.result.ok
    assert outcome.report.parse_failures == 1


# --------------------------------------------------------------------------------------
# Exhausted -- aborts without publishing
# --------------------------------------------------------------------------------------


def test_three_failures_abort_without_a_note(settings: Settings) -> None:
    paraphrased = reply(quotes=["Something nobody wrote.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased, paraphrased, paraphrased])

    with pytest.raises(DraftError, match="No compliant note"):
        run(settings, llm)

    assert len(llm.calls) == settings.pulse.draft.max_attempts


def test_the_abort_message_names_the_failing_checks(settings: Settings) -> None:
    paraphrased = reply(quotes=["Something nobody wrote.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased] * 3)

    with pytest.raises(DraftError) as exc_info:
        run(settings, llm)

    message = str(exc_info.value)
    assert "verbatim" in message
    assert "not published" in message


def test_the_attempt_budget_is_respected(settings: Settings) -> None:
    tuned = settings.model_copy(
        update={
            "pulse": settings.pulse.model_copy(
                update={"draft": settings.pulse.draft.model_copy(update={"max_attempts": 2})}
            )
        }
    )
    paraphrased = reply(quotes=["Something nobody wrote.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased] * 5)

    with pytest.raises(DraftError):
        draft_note(stats(), llm=llm, settings=tuned, iso_week="2026-W41", corpus_size=241)

    assert len(llm.calls) == 2


# --------------------------------------------------------------------------------------
# Fatal -- PII aborts immediately, without retrying
# --------------------------------------------------------------------------------------


def test_pii_aborts_on_the_first_attempt_without_retrying(settings: Settings) -> None:
    """A PII hit means stage 3 has a gap. Retrying would treat a scrubber bug as a
    drafting problem, and might succeed by luck while leaving the defect in place."""
    leaky = reply(headline="Mail notareal@example.com about this now")
    llm = FakeLlm(responses=[leaky, reply()])

    with pytest.raises(DraftError, match="Aborting"):
        run(settings, llm)

    # One call only: the second scripted (compliant) reply was never requested.
    assert len(llm.calls) == 1


def test_the_fatal_message_does_not_echo_the_pii(settings: Settings) -> None:
    leaky = reply(headline="Mail notareal@example.com about this now")

    with pytest.raises(DraftError) as exc_info:
        run(settings, FakeLlm(responses=[leaky]))

    assert "notareal@example.com" not in str(exc_info.value)


# --------------------------------------------------------------------------------------
# Report hygiene
# --------------------------------------------------------------------------------------


def test_the_report_does_not_contain_the_rejected_note(settings: Settings) -> None:
    """Counts and check names only -- a rejected draft is not evidence worth persisting."""
    paraphrased = reply(quotes=["A rejected paraphrase nobody wrote.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased, reply()])
    outcome = run(settings, llm)

    assert "A rejected paraphrase" not in str(outcome.report.summary())


def test_the_history_records_every_attempt(settings: Settings) -> None:
    paraphrased = reply(quotes=["Something nobody wrote.", SECOND, THIRD])
    llm = FakeLlm(responses=[paraphrased, reply()])
    outcome = run(settings, llm)

    assert [r.attempt for r in outcome.history] == [1, 2]
    assert not outcome.history[0].ok
    assert outcome.history[1].ok
