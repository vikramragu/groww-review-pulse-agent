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

from pulse.models import PulseNote, Theme, ThemeSummary
from pulse.publish.ports import Block

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


def figures_text(summary: ThemeSummary) -> str:
    """A theme's numbers, printed by code from stage 5's values -- never by the model.

    The change is in percentage points of the week's share, against the pooled prior
    weeks: "+5.6 pts vs prior 11 wks", which is what `delta_vs_baseline` actually measures.
    Falls back to the bare count for a note drafted before figures were attached.
    """
    figures = summary.figures
    if figures is None:
        return f"{summary.review_count} reviews"
    parts = [f"{figures.review_count} reviews", f"{figures.share_of_week:.1%} of week"]
    if figures.baseline_weeks:
        parts.append(
            f"{figures.delta_vs_baseline * 100:+.1f} pts vs prior {figures.baseline_weeks} wks"
        )
    parts.append(f"{figures.mean_rating:.1f}★")
    return ", ".join(parts)


def render_note(
    note: PulseNote,
    *,
    corpus_size: int,
    corpus_total: int | None = None,
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
        _metadata_line(note, corpus_size, corpus_total, generated_on),
        "",
        f"**{note.headline}**",
        "",
        "## Top themes",
        "",
    ]

    for stats in note.top_themes:
        mark = DIRECTION_MARKS.get(stats.direction, "")
        lines.append(
            f"- **{theme_label(stats.theme)}** {mark} ({figures_text(stats)}) — {stats.summary}"
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


def render_blocks(
    note: PulseNote,
    *,
    corpus_size: int,
    corpus_total: int | None = None,
    generated_on: date | None = None,
) -> list[Block]:
    """The same note as typed blocks, for a Docs server that takes blocks, not markdown.

    Mirrors `render_note` line for line, with the markup removed: the server supports only
    whole-block bold, so inline emphasis and block quotes become plain paragraphs. Quote
    text is passed through untouched -- stage 7 proved it verbatim, and this second render
    path must not undo that (implementation-plan.md P5-T5).
    """
    blocks = [
        Block("heading1", f"Groww Review Pulse — {note.iso_week}"),
        Block("paragraph", _metadata_text(corpus_size, corpus_total, generated_on)),
        Block("paragraph", note.headline, bold=True),
        Block("heading2", "Top themes"),
    ]
    for stats in note.top_themes:
        mark = DIRECTION_MARKS.get(stats.direction, "")
        blocks.append(
            Block(
                "bullet",
                f"{theme_label(stats.theme)} {mark} ({figures_text(stats)}) — {stats.summary}",
            )
        )

    blocks.append(Block("heading2", "What users said"))
    for quote in note.quotes:
        blocks.append(Block("paragraph", f"“{quote.text}”"))
        blocks.append(Block("paragraph", f"— {quote.rating}★, {theme_label(quote.theme)}"))

    blocks.append(Block("heading2", "What to do next"))
    for action in note.actions:
        blocks.append(
            Block(
                "numbered",
                f"{action.action} ({theme_label(action.theme)}) — {action.rationale}",
            )
        )
    return blocks


def _metadata_text(corpus_size: int, corpus_total: int | None, generated_on: date | None) -> str:
    # Both counts, deliberately. The substance filters are sample selection, so a theme
    # share is a share of *substantive* reviews -- stating only the filtered figure invites
    # it to be read as a population statistic.
    if corpus_total and corpus_total > corpus_size:
        parts = [f"{corpus_size:,} substantive reviews of {corpus_total:,} in window"]
    else:
        parts = [f"{corpus_size:,} reviews in window"]
    if generated_on is not None:
        parts.append(f"generated {generated_on.isoformat()}")
    return " · ".join(parts)


def _metadata_line(
    note: PulseNote,
    corpus_size: int,
    corpus_total: int | None,
    generated_on: date | None,
) -> str:
    return "*" + _metadata_text(corpus_size, corpus_total, generated_on) + "*"


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
