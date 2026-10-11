"""The classification prompt (P2-T2).

Stability is the property worth testing. The system prompt is the cache prefix on providers
that offer one, and it is the only thing that makes labels comparable between runs — so
anything volatile in it (a timestamp, a corpus size, a dict iteration order) would be both
a silent cost regression and a silent quality shift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pulse.config import Settings, load_settings
from pulse.models import Review, Source, Theme
from pulse.prompts.taxonomy import TAXONOMY_RULES, render_batch, taxonomy_prompt


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


def a_review(text: str, rating: int = 2) -> Review:
    return Review(
        review_id="a" * 16,
        source=Source.PLAY_STORE,
        rating=rating,
        text=text,
        submitted_on="2026-10-07",  # type: ignore[arg-type]
        iso_week="2026-W41",
        scrubbed=True,
    )


# --------------------------------------------------------------------------------------
# Stability
# --------------------------------------------------------------------------------------


def test_the_prompt_is_byte_stable(settings: Settings) -> None:
    assert taxonomy_prompt(settings) == taxonomy_prompt(settings)


def test_the_prompt_is_stable_across_freshly_loaded_settings() -> None:
    """Guards against TOML parse order leaking into the prompt."""
    first = taxonomy_prompt(load_settings(Path("config")))
    second = taxonomy_prompt(load_settings(Path("config")))
    assert first == second


def test_the_prompt_contains_no_dates_or_times(settings: Settings) -> None:
    """A timestamp in the prefix would miss the cache on every single call."""
    prompt = taxonomy_prompt(settings)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", prompt)
    assert not re.search(r"\d{4}-W\d{2}", prompt)
    assert "today" not in prompt.lower()


def test_the_prompt_mentions_no_corpus_size(settings: Settings) -> None:
    """Only the theme count and the rule numbering should be numeric."""
    prompt = taxonomy_prompt(settings)
    numbers = {int(n) for n in re.findall(r"\b\d+\b", prompt)}
    allowed = {0, 1, 5} | set(range(1, len(TAXONOMY_RULES) + 1))
    assert numbers <= allowed, f"unexpected numbers in the prompt: {sorted(numbers - allowed)}"


# --------------------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------------------


def test_every_theme_appears_with_its_configured_description(settings: Settings) -> None:
    prompt = taxonomy_prompt(settings)
    for theme in Theme:
        definition = settings.themes.definition(theme)
        assert theme.value in prompt
        assert definition.label in prompt
        assert definition.covers in prompt


def test_themes_appear_in_enum_order_not_config_order(settings: Settings) -> None:
    prompt = taxonomy_prompt(settings)
    positions = [prompt.index(theme.value) for theme in Theme]
    assert positions == sorted(positions)


def test_the_prompt_states_the_theme_count(settings: Settings) -> None:
    assert f"exactly {len(Theme)} themes" in taxonomy_prompt(settings)


def test_the_prompt_forbids_inventing_a_theme(settings: Settings) -> None:
    """The ≤5 constraint is enforced by the schema, but the prompt should not fight it."""
    prompt = taxonomy_prompt(settings)
    assert "may not invent" in prompt


def test_the_prompt_allows_declining_to_classify(settings: Settings) -> None:
    prompt = taxonomy_prompt(settings)
    assert "null" in prompt
    assert "rather than guessing" in prompt


def test_the_prompt_explains_scrub_placeholders(settings: Settings) -> None:
    """Scrubbed reviews contain `[email]` and friends; the model should ignore them."""
    prompt = taxonomy_prompt(settings)
    assert "[email]" in prompt
    assert "removed personal data" in prompt


def test_every_theme_has_at_least_two_examples(settings: Settings) -> None:
    prompt = taxonomy_prompt(settings)
    for theme in Theme:
        section_start = prompt.index(theme.value)
        section = prompt[section_start : section_start + 800]
        assert section.count("Example:") >= 2, f"{theme.value} needs more examples"


def test_changing_the_taxonomy_changes_the_prompt(settings: Settings) -> None:
    """Otherwise a taxonomy edit would not be visible in what the model is told."""
    original = taxonomy_prompt(settings)

    edited_theme = settings.themes.definition(Theme.APP_RELIABILITY).model_copy(
        update={"covers": "Something completely different"}
    )
    edited = settings.model_copy(
        update={
            "themes": settings.themes.model_copy(
                update={"themes": {**settings.themes.themes, "app_reliability": edited_theme}}
            )
        }
    )

    assert taxonomy_prompt(edited) != original


# --------------------------------------------------------------------------------------
# Batch rendering
# --------------------------------------------------------------------------------------


def test_reviews_are_numbered_from_one(settings: Settings) -> None:
    rendered = render_batch([a_review("First."), a_review("Second.")])
    assert rendered.splitlines() == ["[1] (2/5) First.", "[2] (2/5) Second."]


def test_the_rating_is_included(settings: Settings) -> None:
    """Rating is real signal for the theme and costs almost nothing."""
    assert "(5/5)" in render_batch([a_review("Charts are great.", rating=5)])


def test_review_ids_are_not_sent_to_the_model() -> None:
    """Indices are used instead, because a small integer is far harder to corrupt.

    A hallucinated 16-character hex id looks plausible and would be hard to detect; an
    out-of-range integer is caught by reconciliation immediately.
    """
    review = a_review("Withdrawal stuck.")
    assert review.review_id not in render_batch([review])


def test_rendering_is_deterministic() -> None:
    reviews = [a_review("One."), a_review("Two.")]
    assert render_batch(reviews) == render_batch(reviews)


def test_an_empty_batch_renders_empty() -> None:
    assert render_batch([]) == ""
