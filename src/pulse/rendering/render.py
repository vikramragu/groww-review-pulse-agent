"""Stage 8: render the note as markdown (P3-T10).

**One template, two consumers.** The same output becomes the Google Doc body and the Gmail
draft body. Rendering them separately would let the published doc and the emailed note
drift apart, and nobody would notice until a reader compared them.

**Deliberately plain CommonMark.** Architecture.md §11.2 commits the Docs MCP server only
to headings, bold/italic, bullet lists, block quotes, and links. A markdown table here
would render as something the server is not contracted to handle — so the renderer stays
inside that subset, and a test enforces it.
"""

from __future__ import annotations

from datetime import date

from pulse.models import PulseNote, Theme

#: Human labels for the enum. Kept here rather than read from config so rendering stays a
#: pure function of the note: a doc whose headings depend on mutable config would not be
#: reproducible from its stored artifact.
THEME_LABELS: dict[Theme, str] = {
    Theme.ONBOARDING_KYC: "Onboarding & KYC",
    Theme.ORDERS_EXECUTION: "Orders & Execution",
    Theme.PAYMENTS_WITHDRAWALS: "Payments & Withdrawals",
    Theme.APP_RELIABILITY: "App Reliability",
    Theme.CHARGES_SUPPORT: "Charges & Support",
}

DIRECTION_MARKS = {"up": "\u25b2", "down": "\u25bc", "flat": "-"}


def theme_label(theme: Theme) -> str:
    return THEME_LABELS.get(theme, theme.value)


def render_note(
    note: PulseNote,
    *,
    corpus_size: int,
    generated_on: date | None = None,
    document_url: str | None = None,
) -> str:
    """Render the pulse as markdown.

    `generated_on` is a parameter rather than `date.today()` so the output is a pure
    function of its inputs — otherwise the golden test in P6 could never compare two
    renders, and the same note would produce different bytes on different days.
    """
    lines: list[str] = [
        f"# Groww Review Pulse — {note.iso_week}",
        "",
        _metadata_line(note, corpus_size, generated_on),
        "",
        f"**{note.headline}**",
        "",
        "## Top themes",
        "",
    ]

    for stats in note.top_themes:
        mark = DIRECTION_MARKS.get(stats.direction, "")
        lines.append(
            f"- **{theme_label(stats.theme)}** {mark} "
            f"({stats.review_count} reviews) — {stats.summary}"
        )

    lines.extend(["", "## What users said", ""])
    for quote in note.quotes:
        # Block quote, with the attribution on its own line so the quoted span is exactly
        # the reviewer's words -- the same span stage 7 verified.
        lines.append(f"> {quote.text}")
        lines.append(">")
        lines.append(f"> — {quote.rating}★, {theme_label(quote.theme)}")
        lines.append("")

    lines.extend(["## What to do next", ""])
    for index, action in enumerate(note.actions, start=1):
        lines.append(
            f"{index}. **{action.action}** ({theme_label(action.theme)}) — {action.rationale}"
        )

    if document_url:
        lines.extend(["", f"[Open the full pulse]({document_url})"])

    return "\n".join(lines).rstrip() + "\n"


def _metadata_line(note: PulseNote, corpus_size: int, generated_on: date | None) -> str:
    parts = [f"{corpus_size} reviews in window"]
    if generated_on is not None:
        parts.append(f"generated {generated_on.isoformat()}")
    return "*" + " · ".join(parts) + "*"


def render_email_body(note: PulseNote, markdown: str, document_url: str | None) -> str:
    """The Gmail draft body: the full note plus a pointer to the doc.

    The brief asks for a draft containing the note "or a clear pointer to it". Including
    both means the email is useful on a phone without following a link, and the link is
    there when the reader wants the shared copy.
    """
    if not document_url:
        return markdown
    return f"{markdown}\n---\n\nShared document: {document_url}\n"


def email_subject(note: PulseNote) -> str:
    return f"Groww Review Pulse — {note.iso_week}"
