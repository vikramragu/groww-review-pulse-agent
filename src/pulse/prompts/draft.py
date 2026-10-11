"""The drafting prompt (P3-T7).

Two things in here are load-bearing rather than decorative.

**The word budget is stated as the validator measures it.** If the prompt's notion of
"250 words" differs from `count_note_words()` even slightly, the regeneration loop fails
in ways that look like model disobedience. The rule is written out, not implied.

**Quotes must be copied, not written.** A model asked for "verbatim" quotes will tidy
punctuation, fix spelling, and merge clauses — all of which look fine to a human reviewer
and all of which violate a hard requirement. The prompt says so explicitly and the
validator checks it mechanically; the prompt alone was never going to be enough.
"""

from __future__ import annotations

from pulse.models import ThemeStats

WORD_BUDGET = 250

DRAFT_SYSTEM_PROMPT = f"""\
You write a weekly one-page pulse from Groww app review data, for a product team to scan \
in minutes.

You are given the three themes that mattered most this week, their numbers, and a pool of \
real review quotes for each. Write the note from those inputs only.

HARD REQUIREMENTS

1. Exactly three themes, three quotes, and three action ideas. Not two, not four.
2. **Quotes must be copied character-for-character from the supplied pool.** Do not fix \
spelling, adjust punctuation, trim filler, merge sentences, or shorten them. A quote that \
reads slightly better but differs by one character is rejected. Copy and paste.
3. Each of the three quotes must come from a different review.
   `review_id` and `rating` on each quote are overwritten by the pipeline from the review \
the quote actually came from -- put anything there, it is ignored. Do not try to guess them.
4. The note body must be at most {WORD_BUDGET} words. Words are whitespace-separated \
tokens in the headline, theme summaries, quotes, and action text. Aim for about \
{WORD_BUDGET - 40} so there is headroom.
5. Each action idea must name one of the three themes on the page, and must be something \
a team could actually start on Monday -- not "improve reliability".

STYLE

Write for a product lead who has thirty seconds. Lead with what changed, not with \
pleasantries. Use the numbers you are given; do not invent any. Where a theme is \
improving, say so -- a pulse that only ever reports problems stops being read.

`direction` is "up" if the theme grew as a share of the week, "down" if it shrank, "flat" \
if it barely moved. Use the delta figures supplied; do not guess.

Redacted spans appear as bracketed placeholders such as [email]. No supplied quote should \
contain one -- if one does, choose a different quote from the pool.

Return only the JSON object. No commentary, no markdown fences.
"""


def render_draft_input(stats: list[ThemeStats], *, iso_week: str, corpus_size: int) -> str:
    """Render the drafting input: three themes, their numbers, and their quote pools.

    Deliberately **not** the full corpus. The model does not need 1,200 reviews to write
    250 words, and withholding them removes any possibility of quoting outside the
    verified pool — which is what keeps the stage 7 substring check bounded and
    meaningful.
    """
    lines = [
        f"WEEK: {iso_week}",
        f"REVIEWS IN WINDOW: {corpus_size}",
        "",
    ]

    for position, theme in enumerate(stats, start=1):
        direction = (
            "up"
            if theme.delta_vs_baseline > 0.01
            else "down"
            if theme.delta_vs_baseline < -0.01
            else "flat"
        )
        lines.extend(
            [
                f"THEME {position}: {theme.theme.value}",
                f"  reviews this week: {theme.review_count}",
                f"  share of week: {theme.share_of_week:.1%}",
                f"  mean rating: {theme.mean_rating:.1f} / 5",
                f"  change vs trailing weeks: {theme.delta_vs_baseline:+.1%} ({direction})",
                "  quotes you may use, copy one exactly:",
            ]
        )
        # Numbered so the model can refer to a choice, and indented so the boundary of
        # each quote is unambiguous -- a quote whose extent is unclear is a quote that
        # gets "tidied".
        # Only the text is shown. `review_id` and `rating` are facts the pipeline already
        # holds, and stage 7 fills them in from the matched candidate -- showing them here
        # would only invite the model to restate them, which it does inaccurately.
        lines.extend(
            f"    [{index}] {candidate.text}"
            for index, candidate in enumerate(theme.quote_candidates, start=1)
        )
        lines.append("")

    return "\n".join(lines)


def render_retry_feedback(failures: list[str], attempt: int) -> str:
    """Append the specific validation failures to the next attempt.

    "Try again" produces a differently-wrong note. Naming the fault is what makes the
    second attempt better rather than merely different.
    """
    bullets = "\n".join(f"  - {failure}" for failure in failures)
    return (
        f"\n\nYour previous attempt (number {attempt}) was rejected for these reasons:\n"
        f"{bullets}\n\n"
        f"Fix exactly these problems and return the whole note again. In particular, if a "
        f"quote was not verbatim, copy one of the supplied quotes character-for-character "
        f"instead of rewriting it."
    )
