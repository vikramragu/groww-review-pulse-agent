"""Stage 3: PII scrubbing, and the detector the pre-publish rescan reuses (P1-T7, P1-T8).

Implements layers 2 and 3 of architecture.md §10. Layer 1 is structural and lives in
`ingest/normalize.py`; layer 4 is stage 7 calling `scan_for_pii` on the rendered note.

**One rule table, two callers.** `scan_for_pii` detects and `scrub_text` redacts, both from
`RULES`. If they were separate implementations they would drift, and the pre-publish rescan
would stop being a real check on the scrubber.

**Why no LLM here.** Architecture.md §10 rules it out: it would be non-deterministic,
unauditable, and would require sending unscrubbed text to a model — the exact thing this
stage exists to prevent.

**Ordering is load-bearing.** Specific rules run before general ones. If the long-digit
catch-all ran first it would turn every phone number and Aadhaar into `[number]`, and the
more informative labels would never fire. Rule order is asserted by a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from pulse import logging as pulse_logging
from pulse.models import Review

log = pulse_logging.get_logger("analysis.scrub")


class Rule(NamedTuple):
    """One named redaction rule."""

    name: str
    pattern: re.Pattern[str]
    label: str
    #: Which capture group to redact; 0 means the whole match.
    group: int = 0


class PiiHit(NamedTuple):
    """One detection.

    `matched` holds the actual PII. It exists for tests and interactive debugging and must
    never be logged, persisted, or put in an error message — use `masked()` for anything
    that leaves the process. Reporting a leak by printing the leak would be its own leak.
    """

    rule: str
    label: str
    start: int
    end: int
    matched: str

    def masked(self) -> str:
        """Output-safe form: first character, then asterisks."""
        if not self.matched:
            return ""
        return self.matched[0] + "*" * (len(self.matched) - 1)

    def describe(self) -> str:
        """Output-safe description for logs and abort messages."""
        return f"{self.rule} at {self.start}-{self.end} ({self.masked()})"


# --------------------------------------------------------------------------------------
# Layer 2 — pattern redaction
# --------------------------------------------------------------------------------------

#: Known UPI handle suffixes. A UPI handle has no TLD dot, so the email rule above cannot
#: claim it; this rule exists precisely for that shape.
_UPI_SUFFIXES = (
    "okaxis|oksbi|okhdfcbank|okicici|okbizaxis|ybl|ibl|axl|apl|upi|paytm|"
    "airtel|freecharge|jupiteraxis|fam|naviaxis|slice|yesg|timecosmos"
)

#: Words that follow "I am" far more often than a name does. Without this guard, the
#: self-identification rule would redact "I am unable to withdraw" — extremely common in
#: review text — and quietly destroy quote candidates across the whole corpus.
_NOT_A_NAME = frozenset(
    {
        "a",
        "able",
        "about",
        "afraid",
        "again",
        "also",
        "always",
        "an",
        "angry",
        "annoyed",
        "asking",
        "at",
        "attaching",
        "being",
        "chasing",
        "confused",
        "disappointed",
        "done",
        "extremely",
        "facing",
        "fed",
        "feeling",
        "forced",
        "frustrated",
        "getting",
        "going",
        "happy",
        "having",
        "helpless",
        "here",
        "hoping",
        "in",
        "just",
        "left",
        "literally",
        "looking",
        "losing",
        "made",
        "my",
        "never",
        "not",
        "now",
        "on",
        "one",
        "only",
        "really",
        "rating",
        "regular",
        "removing",
        "requesting",
        "satisfied",
        "shocked",
        "so",
        "sorry",
        "still",
        "stuck",
        "surprised",
        "switching",
        "the",
        "tired",
        "to",
        "today",
        "trying",
        "unable",
        "uninstalling",
        "unhappy",
        "upset",
        "using",
        "very",
        "waiting",
        "writing",
    }
)

# Rules run in this order. Earlier rules consume text before later, broader rules see it.
RULES: tuple[Rule, ...] = (
    Rule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[email]",
    ),
    Rule(
        "url",
        re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE),
        "[link]",
    ),
    Rule(
        "upi",
        re.compile(rf"\b[A-Za-z0-9._\-]{{2,}}@(?:{_UPI_SUFFIXES})\b", re.IGNORECASE),
        "[upi]",
    ),
    Rule(
        "pan",
        re.compile(r"\b[A-Za-z]{5}[0-9]{4}[A-Za-z]\b"),
        "[id]",
    ),
    # Aadhaar is checked by Verhoeff below; a 12-digit run that fails the checksum falls
    # through to the digit-run rules and is still redacted, just with a vaguer label. Either
    # way nothing leaks — the checksum only buys a more accurate label.
    Rule(
        "aadhaar",
        re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}\b"),
        "[id]",
    ),
    Rule(
        "ifsc",
        re.compile(r"\b[A-Za-z]{4}0[A-Za-z0-9]{6}\b"),
        "[account]",
    ),
    Rule(
        "phone",
        re.compile(r"(?:(?:\+|00)\s?91[\s\-]?|\b0)?\b[6-9]\d{4}[\s\-]?\d{5}\b"),
        "[phone]",
    ),
    Rule(
        "account",
        re.compile(r"\b\d{9,18}\b"),
        "[account]",
    ),
    Rule(
        "handle",
        re.compile(r"(?<![\w.@])@[A-Za-z0-9_]{2,}\b"),
        "[handle]",
    ),
    # Backstop: any remaining run of 9+ digits, including ones broken up by single spaces or
    # hyphens that the specific rules above did not match. Counting digits rather than
    # characters keeps an ISO date ("2026-10-01", 8 digits) from being redacted as a number.
    Rule(
        "digits",
        re.compile(r"\b\d(?:[\s\-]?\d){8,}\b"),
        "[number]",
    ),
    # ---- Layer 3: self-identification -------------------------------------------------
    #
    # Split into two rules because the triggers differ in how much they prove. After
    # "my name is", whatever follows *is* a name and needs no further test. After "I am"
    # it usually is not ("I am unable to withdraw"), so that rule carries two guards.
    Rule(
        "self_name",
        re.compile(
            r"\b(?:my name is|my good name is|name is|myself)\s+"
            r"([A-Za-z][\w'\-]*(?:\s+[A-Za-z][\w'\-]*){0,2})",
            re.IGNORECASE,
        ),
        "[name]",
        group=1,
    ),
    Rule(
        "self_name_weak",
        re.compile(
            r"\b(?:i am|i'm|this is)\s+"
            r"([A-Za-z][\w'\-]*(?:\s+[A-Za-z][\w'\-]*){0,2})",
            re.IGNORECASE,
        ),
        "[name]",
        group=1,
    ),
    # Backstop for contact details the specific rules above cannot match — spelled-out
    # digits, for instance. Capped short and stopped at a comma to limit how much ordinary
    # prose it can swallow; the specific rules claim their spans first, so this only sees
    # what they left.
    Rule(
        "self_contact",
        re.compile(
            r"\b(?:contact|call|reach|ping|whatsapp|message|mail|email)\s+me\s+"
            r"(?:at|on|via)\s+([^.!?,\n]{1,40})",
            re.IGNORECASE,
        ),
        "[contact]",
        group=1,
    ),
)

RULE_ORDER: tuple[str, ...] = tuple(rule.name for rule in RULES)

#: Every label a rule can emit. Used to assert scrubbed text is clean and that a redacted
#: span is recognizable downstream (the quote-candidate filter drops reviews containing one).
LABELS: frozenset[str] = frozenset(rule.label for rule in RULES)

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def is_verhoeff_valid(digits: str) -> bool:
    """Verhoeff checksum — the check digit scheme Aadhaar numbers use."""
    if not digits.isdigit():
        return False
    checksum = 0
    for index, char in enumerate(reversed(digits)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[index % 8][int(char)]]
    return checksum == 0


def _contains_label(text: str) -> bool:
    return any(label in text for label in LABELS)


def _rule_applies(rule: Rule, match: re.Match[str]) -> bool:
    """Per-rule acceptance beyond what the regex can express."""
    # No rule may claim a span that is already redacted. Without this, scrubbing is not
    # idempotent -- `self_contact` would match "contact me at [email] for details" and
    # collapse the whole clause -- and, worse, stage 7's pre-publish rescan would report a
    # PII hit on text that is already clean, aborting a perfectly good run.
    if _contains_label(match.group(rule.group)):
        return False

    if rule.name == "aadhaar":
        # A 12-digit run that fails the checksum is not an Aadhaar number. It still gets
        # redacted by the digit-run rules below, just with a vaguer label, so declining
        # here costs accuracy of labelling and never safety.
        return is_verhoeff_valid(re.sub(r"[^\d]", "", match.group(0)))

    if rule.name == "self_name_weak":
        # "I am ..." is followed by a name far less often than by a complaint. Two guards,
        # both of which must pass:
        #   1. the token is capitalized in the original text, and
        #   2. it is not a word that commonly follows "I am".
        # Either alone is too weak: the stopword list cannot be exhaustive ("I am giving
        # 2 stars"), and capitalization alone fails at sentence starts ("I am Unable...").
        first_word = match.group(1).split()[0]
        return first_word[:1].isupper() and first_word.lower() not in _NOT_A_NAME

    return True


def _overlaps(start: int, end: int, taken: list[tuple[int, int]]) -> bool:
    return any(start < claimed_end and claimed_start < end for claimed_start, claimed_end in taken)


def scan_for_pii(text: str) -> list[PiiHit]:
    """Find every PII span in `text`, earlier rules winning overlaps.

    The single detector behind both stage 3 (scrubbing) and stage 7's pre-publish rescan.
    Returns hits sorted by position.
    """
    if not text:
        return []

    hits: list[PiiHit] = []
    taken: list[tuple[int, int]] = []

    for rule in RULES:
        for match in rule.pattern.finditer(text):
            start, end = match.span(rule.group)
            if start == end or not _rule_applies(rule, match):
                continue
            if _overlaps(start, end, taken):
                continue
            taken.append((start, end))
            hits.append(
                PiiHit(
                    rule=rule.name,
                    label=rule.label,
                    start=start,
                    end=end,
                    matched=text[start:end],
                )
            )

    return sorted(hits, key=lambda hit: hit.start)


def has_pii(text: str) -> bool:
    return bool(scan_for_pii(text))


def scrub_text(text: str) -> tuple[str, list[PiiHit]]:
    """Redact every detected span. Returns the scrubbed text and what was removed."""
    hits = scan_for_pii(text)
    if not hits:
        return text, []

    pieces: list[str] = []
    cursor = 0
    for hit in hits:
        pieces.append(text[cursor : hit.start])
        pieces.append(hit.label)
        cursor = hit.end
    pieces.append(text[cursor:])

    return "".join(pieces), hits


@dataclass
class ScrubReport:
    """Counts only. Deliberately holds no review text and no matched spans."""

    reviews_in: int = 0
    reviews_scrubbed: int = 0
    reviews_dropped: int = 0
    hits_by_rule: dict[str, int] = field(default_factory=dict)

    def record(self, hits: list[PiiHit]) -> None:
        for hit in hits:
            self.hits_by_rule[hit.rule] = self.hits_by_rule.get(hit.rule, 0) + 1

    @property
    def total_hits(self) -> int:
        return sum(self.hits_by_rule.values())

    def summary(self) -> dict[str, Any]:
        return {
            "reviews_in": self.reviews_in,
            "reviews_scrubbed": self.reviews_scrubbed,
            "reviews_dropped": self.reviews_dropped,
            "pii_hits": self.total_hits,
            "hits_by_rule": dict(sorted(self.hits_by_rule.items())),
        }


def scrub_reviews(reviews: list[Review]) -> tuple[list[Review], ScrubReport]:
    """Scrub a corpus. Every returned `Review` has `scrubbed=True`.

    A review whose text is *entirely* redacted is dropped: it carries no remaining signal
    and could never be quoted.
    """
    report = ScrubReport(reviews_in=len(reviews))
    scrubbed: list[Review] = []

    for review in reviews:
        text, hits = scrub_text(review.text)
        report.record(hits)
        if hits:
            report.reviews_scrubbed += 1

        remaining = text
        for label in LABELS:
            remaining = remaining.replace(label, "")
        if not remaining.strip():
            report.reviews_dropped += 1
            continue

        scrubbed.append(review.model_copy(update={"text": text, "scrubbed": True}))

    log.info("scrub complete", extra=report.summary())
    return scrubbed, report


def assert_clean(text: str, context: str) -> None:
    """Raise if `text` still contains PII. Used by stage 7 (architecture.md §9).

    The error names rules and masked spans, never the matched text — reporting a leak by
    printing the leak would be its own leak.
    """
    hits = scan_for_pii(text)
    if hits:
        details = "; ".join(hit.describe() for hit in hits)
        raise PiiLeakError(
            f"PII detected in {context} after scrubbing: {details}. "
            f"This indicates a gap in stage 3, not a drafting problem."
        )


class PiiLeakError(Exception):
    """Raised when PII survives into an artifact that is about to be published."""
