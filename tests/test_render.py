"""P3-T11: the markdown renderer.

Two constraints to keep honest:

* **Pure function.** Rendering must depend only on its arguments, or the P6 golden test
  could never compare two renders and the same note would produce different bytes on
  different days. `generated_on` is a parameter for exactly that reason.
* **CommonMark subset only.** Architecture.md §11.2 commits the Docs MCP server to
  headings, bold/italic, bullet lists, block quotes and links -- nothing else. A markdown
  table here would publish something the server is not contracted to handle, and the
  failure would show up in Phase 5 as a mangled document.
"""

from __future__ import annotations

from datetime import date

from pulse.models import PulseNote, Theme, ThemeFigures
from pulse.rendering.render import (
    email_subject,
    figures_text,
    render_blocks,
    render_email_body,
    render_note,
    theme_label,
)

THEMES = [Theme.PAYMENTS_WITHDRAWALS, Theme.APP_RELIABILITY, Theme.CHARGES_SUPPORT]


def a_note() -> PulseNote:
    return PulseNote.model_validate(
        {
            "iso_week": "2026-W41",
            "headline": "Withdrawal delays dominate this week",
            "top_themes": [
                {
                    "theme": theme.value,
                    "summary": f"Something happened in {theme.value}.",
                    "review_count": 10 + index,
                    "direction": direction,
                }
                for index, (theme, direction) in enumerate(
                    zip(THEMES, ["up", "down", "flat"], strict=True)
                )
            ],
            "quotes": [
                {
                    "text": f"A real reviewer said this about {theme.value} honestly.",
                    "theme": theme.value,
                    "rating": index + 1,
                    "review_id": f"{index:016x}",
                }
                for index, theme in enumerate(THEMES)
            ],
            "actions": [
                {
                    "theme": theme.value,
                    "action": f"Fix {theme.value}.",
                    "rationale": "Because reviews say so.",
                }
                for theme in THEMES
            ],
        }
    )


# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------


def test_rendering_is_deterministic() -> None:
    note = a_note()
    assert render_note(note, corpus_size=241) == render_note(note, corpus_size=241)


def test_rendering_does_not_read_the_clock() -> None:
    """Without this the golden test in P6 could never compare two renders."""
    note = a_note()
    assert "generated" not in render_note(note, corpus_size=241)
    dated = render_note(note, corpus_size=241, generated_on=date(2026, 10, 12))
    assert "generated 2026-10-12" in dated


# --------------------------------------------------------------------------------------
# CommonMark subset
# --------------------------------------------------------------------------------------


def test_no_markdown_tables() -> None:
    """The Docs server is not contracted to render them (architecture.md §11.2)."""
    markdown = render_note(a_note(), corpus_size=241)
    for line in markdown.splitlines():
        assert not (line.strip().startswith("|") or "---|" in line)


def test_no_raw_html() -> None:
    markdown = render_note(a_note(), corpus_size=241)
    assert "<" not in markdown.replace("\\<", "")


def test_only_the_contracted_marks_are_used() -> None:
    markdown = render_note(a_note(), corpus_size=241, document_url="https://docs.example/d/abc")
    assert markdown.startswith("# ")  # heading
    assert "## Top themes" in markdown  # heading
    assert "**" in markdown  # bold
    assert "\n- **" in markdown  # bullet list
    assert "\n> " in markdown  # block quote
    assert "](https://docs.example/d/abc)" in markdown  # link


# --------------------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------------------


def test_the_week_appears_in_the_title() -> None:
    assert render_note(a_note(), corpus_size=241).startswith("# Groww Review Pulse — 2026-W41")


def test_all_three_sections_are_present() -> None:
    markdown = render_note(a_note(), corpus_size=241)
    for heading in ("## Top themes", "## What users said", "## What to do next"):
        assert heading in markdown


def test_every_theme_quote_and_action_is_rendered() -> None:
    note = a_note()
    markdown = render_note(note, corpus_size=241)

    for stats in note.top_themes:
        assert theme_label(stats.theme) in markdown
        assert stats.summary in markdown
    for quote in note.quotes:
        assert quote.text in markdown
    for action in note.actions:
        assert action.action in markdown


def test_quote_text_is_rendered_unaltered() -> None:
    """The published span must be the same one stage 7 verified as verbatim."""
    note = a_note()
    markdown = render_note(note, corpus_size=241)
    for quote in note.quotes:
        assert f"> {quote.text}" in markdown


def test_the_corpus_size_is_shown() -> None:
    assert "241 reviews in window" in render_note(a_note(), corpus_size=241)


def test_direction_marks_are_rendered() -> None:
    markdown = render_note(a_note(), corpus_size=241)
    assert "▲" in markdown  # up
    assert "▼" in markdown  # down


def test_the_doc_link_is_omitted_when_there_is_none() -> None:
    assert "Open the full pulse" not in render_note(a_note(), corpus_size=241)


# --------------------------------------------------------------------------------------
# Email body
# --------------------------------------------------------------------------------------


def test_the_email_body_carries_the_note_and_the_link() -> None:
    """The brief asks for the note "or a clear pointer to it"; both means the email is
    useful on a phone without following a link."""
    note = a_note()
    markdown = render_note(note, corpus_size=241)
    body = render_email_body(note, markdown, "https://docs.example/d/abc")

    assert markdown in body
    assert "https://docs.example/d/abc" in body


def test_the_email_body_without_a_url_is_just_the_note() -> None:
    note = a_note()
    markdown = render_note(note, corpus_size=241)
    assert render_email_body(note, markdown, None) == markdown


def test_the_subject_names_the_week() -> None:
    assert email_subject(a_note()) == "Groww Review Pulse — 2026-W41"


# --------------------------------------------------------------------------------------
# Blocks for the Docs server (P5-T5) -- a second render path held to the same guarantees
# --------------------------------------------------------------------------------------


def _markdown_lines_without_markup(markdown: str) -> list[str]:
    """Each content line of `pulse.md` with its markdown syntax removed."""
    lines = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line in ("", ">"):
            continue
        for prefix in ("## ", "# ", "- ", "> "):
            if line.startswith(prefix):
                line = line[len(prefix) :]
        if line[:3] in {f"{n}. " for n in range(1, 10)}:
            line = line[3:]
        lines.append(line.replace("**", "").strip("*"))
    return lines


def test_blocks_carry_every_line_of_the_markdown() -> None:
    """The doc and the email must not drift apart: same content, different markup."""
    note = a_note()
    markdown = render_note(note, corpus_size=241, corpus_total=900, generated_on=date(2026, 10, 12))
    blocks = render_blocks(note, corpus_size=241, corpus_total=900, generated_on=date(2026, 10, 12))
    texts = [block.text for block in blocks]

    for line in _markdown_lines_without_markup(markdown):
        assert any(line in text for text in texts), line


def test_quotes_survive_block_rendering_verbatim() -> None:
    note = a_note()
    texts = [b.text for b in render_blocks(note, corpus_size=241)]
    for quote in note.quotes:
        assert f"“{quote.text}”" in texts


def test_blocks_contain_no_markdown_syntax() -> None:
    for block in render_blocks(a_note(), corpus_size=241):
        assert "**" not in block.text
        assert not block.text.startswith(("#", "> ", "- "))


def test_blocks_use_only_the_servers_types_and_stay_within_its_limits() -> None:
    blocks = render_blocks(a_note(), corpus_size=241)
    assert {b.type for b in blocks} <= {"paragraph", "heading1", "heading2", "bullet", "numbered"}
    assert 0 < len(blocks) <= 200
    assert all(0 < len(b.text) <= 10_000 for b in blocks)


def test_the_section_opens_with_the_week_heading_and_bold_headline() -> None:
    blocks = render_blocks(a_note(), corpus_size=241)
    assert (blocks[0].type, blocks[0].text) == ("heading1", "Groww Review Pulse — 2026-W41")
    assert any(b.bold and b.text == "Withdrawal delays dominate this week" for b in blocks)


def test_blocks_disclose_the_substantive_subset() -> None:
    blocks = render_blocks(a_note(), corpus_size=241, corpus_total=900)
    assert any("241 substantive reviews of 900 in window" in b.text for b in blocks)


def test_block_rendering_is_deterministic() -> None:
    assert render_blocks(a_note(), corpus_size=241) == render_blocks(a_note(), corpus_size=241)


# --------------------------------------------------------------------------------------
# Figures are printed by code
# --------------------------------------------------------------------------------------


def with_figures(note: PulseNote, *, baseline_weeks: int = 11) -> PulseNote:
    figures = ThemeFigures(
        review_count=37,
        share_of_week=0.25694,
        delta_vs_baseline=0.05554,
        baseline_weeks=baseline_weeks,
        mean_rating=2.6216,
    )
    themes = [s.model_copy(update={"figures": figures}) for s in note.top_themes]
    return note.model_copy(update={"top_themes": themes})


def test_theme_lines_carry_the_pipelines_figures() -> None:
    markdown = render_note(with_figures(a_note()), corpus_size=241)
    assert "(37 reviews, 25.7% of week, +5.6 pts vs prior 11 wks, 2.6★)" in markdown


def test_the_change_is_labelled_in_points_against_the_baseline_not_week_over_week() -> None:
    markdown = render_note(with_figures(a_note()), corpus_size=241)
    assert "week-over-week" not in markdown
    assert "+5.6%" not in markdown


def test_no_baseline_means_no_change_figure() -> None:
    markdown = render_note(with_figures(a_note(), baseline_weeks=0), corpus_size=241)
    assert "(37 reviews, 25.7% of week, 2.6★)" in markdown
    assert "pts" not in markdown


def test_a_note_without_figures_falls_back_to_the_count() -> None:
    assert "(10 reviews)" in render_note(a_note(), corpus_size=241)


def test_blocks_carry_the_same_figures_as_the_markdown() -> None:
    note = with_figures(a_note())
    blocks = render_blocks(note, corpus_size=241)
    expected = figures_text(note.top_themes[0])
    assert any(expected in block.text for block in blocks if block.type == "bullet")
