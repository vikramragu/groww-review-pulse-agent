"""Stage 7: the validation gate (P3-T12 → P3-T15).

The gate that makes the brief's hard constraints real. Runs **entirely in code** — no
model involvement — because a model asked to police its own output will drift.

Architecture.md §9 lists the checks. Two carry most of the weight:

**Verbatim quotes.** A model asked for "verbatim" snippets routinely tidies grammar, trims
filler, or merges two sentences. Every one of those reads fine to a human reviewer and
every one violates the brief. The only reliable defence is mechanical: the quote must
appear as an exact substring of a supplied candidate. Whitespace is normalized on both
sides; case and punctuation are **not**, because those are exactly what gets "tidied".

**PII aborts, it does not retry.** A PII hit here means stage 3 has a gap. Regenerating
would treat a scrubber bug as a drafting problem, and might well succeed by luck while
leaving the real defect in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pulse import logging as pulse_logging
from pulse.analysis.scrub import PiiLeakError, scan_for_pii
from pulse.models import PulseNote, Quote, QuoteCandidate, Theme

log = pulse_logging.get_logger("rendering.validate")

WORD_LIMIT = 250

_WHITESPACE = re.compile(r"\s+")

#: Markdown syntax stripped before counting words. The count must measure the prose a
#: reader sees, not the punctuation that formats it.
_MARKDOWN_NOISE = re.compile(r"[*_`#>\[\]()]|^\s*[-+]\s|^\s*\d+\.\s", re.MULTILINE)


@dataclass(frozen=True)
class ValidationFailure:
    """One failed check, phrased so it can be fed straight back to the model."""

    check: str
    message: str
    #: True when the run must abort rather than regenerate.
    fatal: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass
class ValidationResult:
    attempt: int = 1
    failures: list[ValidationFailure] = field(default_factory=list)
    word_count: int = 0
    #: The note with each quote's provenance replaced by its source review's real values.
    #: Callers must publish *this*, not the note the model returned.
    grounded_note: PulseNote | None = None
    #: How many quotes had fabricated provenance. Surfaced in the ledger because a model
    #: that invents ids is worth knowing about even when the correction is automatic.
    provenance_corrections: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def fatal(self) -> bool:
        return any(failure.fatal for failure in self.failures)

    def messages(self) -> list[str]:
        return [failure.message for failure in self.failures]

    def summary(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "ok": self.ok,
            "word_count": self.word_count,
            "provenance_corrections": self.provenance_corrections,
            "failures": [
                {"check": f.check, "message": f.message, "fatal": f.fatal} for f in self.failures
            ],
        }


# --------------------------------------------------------------------------------------
# P3-T12 — word count
# --------------------------------------------------------------------------------------


def count_note_words(markdown: str) -> int:
    """Count the words in a rendered note, per architecture.md §9's definition.

    Whitespace-separated tokens in the body, **excluding** the title line, the italic
    metadata line, markdown syntax characters, and list numbering. Quote text and action
    text count.

    One function, one definition. If the drafting prompt's budget and this count disagree
    even slightly, the regeneration loop fails in ways that look like model disobedience
    rather than like an arithmetic mismatch — so the prompt states this rule verbatim.
    """
    counted: list[str] = []

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Title and section headings are structure, not prose.
        if line.startswith("#"):
            continue
        # The metadata line ("*241 reviews in window · generated ...*").
        if line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            continue
        # A bare block-quote continuation line.
        if line in {">", ">​"}:
            continue
        # The link footer, which is a pointer rather than part of the note.
        if line.startswith("[Open the full pulse]"):
            continue

        cleaned = _MARKDOWN_NOISE.sub(" ", line)
        counted.extend(token for token in _WHITESPACE.split(cleaned) if token)

    return len(counted)


# --------------------------------------------------------------------------------------
# P3-T13 — the verbatim check
# --------------------------------------------------------------------------------------


def normalize_for_comparison(text: str) -> str:
    """Collapse whitespace and strip. **Nothing else.**

    Deliberately does not touch case, punctuation, or quote characters. Those are exactly
    the things a model "improves" when it paraphrases, so normalizing them away would
    leave the check passing on text no reviewer wrote.
    """
    return _WHITESPACE.sub(" ", text).strip()


def find_verbatim_source(quote: str, candidates: list[QuoteCandidate]) -> QuoteCandidate | None:
    """The candidate containing `quote` verbatim, or None.

    A substring match rather than equality, so the model may legitimately quote a clause
    from a longer review.
    """
    needle = normalize_for_comparison(quote)
    if not needle:
        return None
    for candidate in candidates:
        if needle in normalize_for_comparison(candidate.text):
            return candidate
    return None


def ground_quote(quote: Quote, source: QuoteCandidate) -> tuple[Quote, bool]:
    """Replace a quote's provenance with the source review's actual values.

    Returns the corrected quote and whether anything had to be corrected.

    **Derived, not validated.** `review_id` and `rating` are facts the pipeline already
    holds, so asking the model to restate them only creates a way for them to be wrong —
    and they were: the model invented ids like `r001` and guessed star ratings, publishing
    fabricated provenance beside a quote whose text had been correctly verified. A
    verbatim quote attributed to the wrong rating is still a misleading artifact.
    """
    corrected = quote.model_copy(update={"review_id": source.review_id, "rating": source.rating})
    changed = quote.review_id != source.review_id or quote.rating != source.rating
    return corrected, changed


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def validate_note(
    note: PulseNote,
    markdown: str,
    *,
    allowed_themes: list[Theme],
    candidates_by_theme: dict[Theme, list[QuoteCandidate]],
    attempt: int = 1,
    word_limit: int = WORD_LIMIT,
) -> ValidationResult:
    """Run every check from architecture.md §9. Returns all failures, not just the first.

    Reporting them together matters: the retry can fix everything in one more attempt
    instead of burning the budget discovering one problem at a time.
    """
    result = ValidationResult(attempt=attempt)
    result.word_count = count_note_words(markdown)

    # --- word budget (P3-T12) ---
    if result.word_count > word_limit:
        result.failures.append(
            ValidationFailure(
                "word_count",
                f"The note is {result.word_count} words, over the {word_limit}-word "
                f"limit. Cut about {result.word_count - word_limit + 20} words, mostly "
                f"from the theme summaries and rationales -- not from the quotes, which "
                f"must stay verbatim.",
            )
        )

    allowed = set(allowed_themes)

    # --- themes are the ones the aggregator chose ---
    note_themes = [stats.theme for stats in note.top_themes]
    for theme in note_themes:
        if theme not in allowed:
            result.failures.append(
                ValidationFailure(
                    "theme_validity",
                    f"Theme {theme.value!r} is not one of this week's top three "
                    f"({', '.join(t.value for t in allowed_themes)}). Use only those.",
                )
            )
    if len(set(note_themes)) != len(note_themes):
        result.failures.append(
            ValidationFailure(
                "theme_distinctness",
                "The three themes must be three different themes.",
            )
        )

    # --- quotes: verbatim, provenance, distinctness (P3-T13, P3-T14) ---
    source_ids: list[str] = []
    grounded_quotes: list[Quote] = []
    for position, quote in enumerate(note.quotes, start=1):
        pool = candidates_by_theme.get(quote.theme, [])

        if quote.theme not in allowed:
            result.failures.append(
                ValidationFailure(
                    "quote_theme",
                    f"Quote {position} is attributed to {quote.theme.value!r}, which is "
                    f"not one of this week's themes.",
                )
            )
            continue

        source = find_verbatim_source(quote.text, pool)
        if source is None:
            result.failures.append(
                ValidationFailure(
                    "verbatim",
                    f"Quote {position} is not a verbatim copy of any supplied "
                    f"{quote.theme.value} quote. You wrote: {quote.text!r}. Copy one of "
                    f"the supplied quotes character-for-character instead of rewriting "
                    f"it -- do not fix spelling or punctuation.",
                )
            )
            continue

        corrected, changed = ground_quote(quote, source)
        if changed:
            result.provenance_corrections += 1
        grounded_quotes.append(corrected)
        source_ids.append(source.review_id)

    if len(set(source_ids)) != len(source_ids):
        result.failures.append(
            ValidationFailure(
                "quote_distinctness",
                "The three quotes must come from three different reviews; two of them "
                "are the same review.",
            )
        )

    # --- actions are grounded in the themes on the page (P3-T14) ---
    for position, action in enumerate(note.actions, start=1):
        if action.theme not in allowed:
            result.failures.append(
                ValidationFailure(
                    "action_grounding",
                    f"Action {position} names theme {action.theme.value!r}, which is not "
                    f"on the page. Each action must address one of the three themes.",
                )
            )

    # --- week matches ---
    # (cardinality is enforced by the PulseNote schema itself, so there is nothing to
    # check here -- an invalid count never parses)

    if len(grounded_quotes) == len(note.quotes):
        result.grounded_note = note.model_copy(update={"quotes": grounded_quotes})

    # --- PII rescan: fatal (P3-T15) ---
    hits = scan_for_pii(markdown)
    if hits:
        details = "; ".join(hit.describe() for hit in hits)
        result.failures.append(
            ValidationFailure(
                "pii",
                f"PII detected in the rendered note: {details}. This indicates a gap in "
                f"stage 3's scrubber, not a drafting problem.",
                fatal=True,
            )
        )

    log.info("validation complete", extra=result.summary())
    return result


def assert_publishable(result: ValidationResult, markdown: str) -> None:
    """Final guard before a note leaves the process.

    Belt and braces over the gate above: anything that reaches publishing has already
    passed validation, so this raising at all means a code path skipped the gate.
    """
    if result.fatal:
        raise PiiLeakError("Refusing to publish: " + "; ".join(result.messages()))
    if not result.ok:
        raise ValueError(
            "Refusing to publish a note that failed validation: " + "; ".join(result.messages())
        )
    if scan_for_pii(markdown):
        raise PiiLeakError("Refusing to publish: PII found in the final rendered note.")
