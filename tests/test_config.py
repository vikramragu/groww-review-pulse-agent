"""Config loading, env overrides, and the taxonomy/enum consistency guard.

The last of these matters disproportionately: the `Theme` enum enforces the <=5 constraint
while `themes.toml` is what the classifier prompt is built from. If they drift, the prompt
describes a taxonomy the schema will reject, and the symptom is confusing classification
failures rather than the configuration error it actually is.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from pulse.config import (
    ConfigError,
    DocsConfig,
    load_env_file,
    load_settings,
    require_recipient,
)
from pulse.models import Theme

VALID_PULSE_TOML = """
[window]
weeks = 12
min_weeks = 8

[input]
path = "data/raw"

[draft]
max_attempts = 3

[thresholds]
min_week_reviews = 20
min_confidence = 0.5

[docs]
title_template = "Groww Review Pulse — {week}"
folder_id = ""
"""

VALID_MCP_TOML = """
contract = "pulse-mcp/1"

[docs]
command = "python"
args = ["-m", "pulse_mcp_servers.docs"]

[docs.tools]
find = "docs.find_document"

[gmail]
command = "python"
args = ["-m", "pulse_mcp_servers.gmail"]

[gmail.tools]
create_draft = "gmail.create_draft"
"""


def _themes_toml(theme_keys: list[str], version: str = "1") -> str:
    blocks = "\n".join(
        textwrap.dedent(
            f"""
            [themes.{key}]
            label = "{key}"
            covers = "covers {key}"
            """
        )
        for key in theme_keys
    )
    return f'version = "{version}"\n{blocks}'


def write_config(
    directory: Path,
    *,
    pulse: str = VALID_PULSE_TOML,
    themes: str | None = None,
    mcp: str = VALID_MCP_TOML,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pulse.toml").write_text(pulse, encoding="utf-8")
    (directory / "themes.toml").write_text(
        themes if themes is not None else _themes_toml([t.value for t in Theme]),
        encoding="utf-8",
    )
    (directory / "mcp.toml").write_text(mcp, encoding="utf-8")
    return directory


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path / "config")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the ambient environment out of these tests."""
    for name in (
        "PULSE_WINDOW_WEEKS",
        "PULSE_MIN_WINDOW_WEEKS",
        "PULSE_MAX_DRAFT_ATTEMPTS",
        "PULSE_INPUT",
        "PULSE_RECIPIENT",
        "PULSE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_loads_the_committed_repository_config() -> None:
    """The config shipped in this repo must itself be valid."""
    settings = load_settings(Path("config"))
    assert settings.mcp.contract == "pulse-mcp/1"
    assert settings.pulse.window.weeks >= settings.pulse.window.min_weeks
    assert set(settings.themes.themes) == {theme.value for theme in Theme}


def test_loads_a_valid_config_directory(config_path: Path) -> None:
    settings = load_settings(config_path)
    assert settings.pulse.window.weeks == 12
    assert settings.pulse.draft.max_attempts == 3
    assert settings.taxonomy_version == "1"
    assert settings.recipient is None


# --------------------------------------------------------------------------------------
# Missing and malformed files
# --------------------------------------------------------------------------------------


def test_missing_config_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"pulse\.toml"):
        load_settings(tmp_path / "nope")


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    directory = write_config(tmp_path / "config", pulse="[window\nweeks = 12")
    with pytest.raises(ConfigError, match="Could not parse"):
        load_settings(directory)


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """A typo'd key must fail loudly instead of being silently ignored."""
    directory = write_config(tmp_path / "config", pulse=VALID_PULSE_TOML + "\n[window]\nweks = 4\n")
    with pytest.raises(ConfigError):
        load_settings(directory)


def test_out_of_range_value_is_rejected(tmp_path: Path) -> None:
    directory = write_config(
        tmp_path / "config", pulse=VALID_PULSE_TOML.replace("max_attempts = 3", "max_attempts = 0")
    )
    with pytest.raises(ConfigError, match=r"draft\.max_attempts"):
        load_settings(directory)


def test_min_window_larger_than_window_is_rejected(tmp_path: Path) -> None:
    directory = write_config(
        tmp_path / "config", pulse=VALID_PULSE_TOML.replace("weeks = 12", "weeks = 4")
    )
    with pytest.raises(ConfigError, match="exceeds"):
        load_settings(directory)


# --------------------------------------------------------------------------------------
# Taxonomy consistency with the Theme enum
# --------------------------------------------------------------------------------------


def test_taxonomy_missing_a_theme_is_rejected(tmp_path: Path) -> None:
    keys = [theme.value for theme in Theme][:-1]
    directory = write_config(tmp_path / "config", themes=_themes_toml(keys))
    with pytest.raises(ConfigError, match=r"missing from themes\.toml"):
        load_settings(directory)


def test_taxonomy_with_a_sixth_theme_is_rejected(tmp_path: Path) -> None:
    """Guards architecture.md D4: change the enum, never add a sixth theme."""
    keys = [theme.value for theme in Theme] + ["customer_delight"]
    directory = write_config(tmp_path / "config", themes=_themes_toml(keys))
    with pytest.raises(ConfigError, match="not in the Theme enum"):
        load_settings(directory)


def test_taxonomy_error_points_at_both_sides(tmp_path: Path) -> None:
    keys = [*[theme.value for theme in Theme][:-1], "something_else"]
    directory = write_config(tmp_path / "config", themes=_themes_toml(keys))
    with pytest.raises(ConfigError) as exc_info:
        load_settings(directory)
    message = str(exc_info.value)
    assert "missing from themes.toml" in message
    assert "not in the Theme enum" in message
    assert "bump `version`" in message


# --------------------------------------------------------------------------------------
# Environment overrides (architecture.md §12)
# --------------------------------------------------------------------------------------


def test_env_overrides_window_weeks(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_WINDOW_WEEKS", "10")
    assert load_settings(config_path).pulse.window.weeks == 10


def test_env_overrides_both_window_bounds_together(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting both must not have the second override discard the first."""
    monkeypatch.setenv("PULSE_WINDOW_WEEKS", "9")
    monkeypatch.setenv("PULSE_MIN_WINDOW_WEEKS", "9")
    window = load_settings(config_path).pulse.window
    assert (window.weeks, window.min_weeks) == (9, 9)


def test_env_overrides_input_path(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_INPUT", "/tmp/exports")
    assert load_settings(config_path).pulse.input.path == Path("/tmp/exports")


def test_env_overrides_max_draft_attempts(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_MAX_DRAFT_ATTEMPTS", "2")
    assert load_settings(config_path).pulse.draft.max_attempts == 2


def test_non_numeric_env_override_is_rejected(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_WINDOW_WEEKS", "twelve")
    with pytest.raises(ConfigError, match="must be an integer"):
        load_settings(config_path)


def test_empty_env_override_falls_back_to_the_file(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_WINDOW_WEEKS", "")
    assert load_settings(config_path).pulse.window.weeks == 12


def test_recipient_comes_from_the_environment(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_RECIPIENT", "pulse@example.com")
    assert load_settings(config_path).recipient == "pulse@example.com"


# --------------------------------------------------------------------------------------
# Deferred requirements
# --------------------------------------------------------------------------------------


def test_recipient_is_optional_at_load_time(config_path: Path) -> None:
    """A --dry-run must not require a stage-10 value (open decision O3)."""
    assert load_settings(config_path).recipient is None


def test_require_recipient_fails_with_an_actionable_message(config_path: Path) -> None:
    settings = load_settings(config_path)
    with pytest.raises(ConfigError, match="PULSE_RECIPIENT"):
        require_recipient(settings)


def test_require_recipient_returns_the_configured_value(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_RECIPIENT", "pulse@example.com")
    assert require_recipient(load_settings(config_path)) == "pulse@example.com"


# --------------------------------------------------------------------------------------
# Doc title convention — idempotency depends on it (architecture.md §11.5)
# --------------------------------------------------------------------------------------


def test_doc_title_substitutes_the_week() -> None:
    assert DocsConfig().title_for("2026-W41") == "Groww Review Pulse — 2026-W41"


# --------------------------------------------------------------------------------------
# .env loading
#
# This function handles secrets, so the tests check two things beyond "it works": that a
# real environment variable is never overridden by a file, and that neither the return
# value nor any log line can carry a value.
# --------------------------------------------------------------------------------------


def test_env_file_values_are_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk-from-file\n", encoding="utf-8")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    applied = load_env_file(env_file)

    assert applied == ["GROQ_API_KEY"]
    assert os.environ["GROQ_API_KEY"] == "gsk-from-file"


def test_a_real_environment_variable_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that silently overrode an explicit export would be miserable to debug."""
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-from-shell")

    applied = load_env_file(env_file)

    assert applied == []
    assert os.environ["GROQ_API_KEY"] == "gsk-from-shell"


def test_load_env_file_never_returns_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=super-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert "super-secret-value" not in str(load_env_file(env_file))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("GROQ_API_KEY=plain", "plain"),
        ("export GROQ_API_KEY=exported", "exported"),
        ("  GROQ_API_KEY = spaced  ", "spaced"),
        ('GROQ_API_KEY="double quoted"', "double quoted"),
        ("GROQ_API_KEY='single quoted'", "single quoted"),
        ("GROQ_API_KEY=has=equals=inside", "has=equals=inside"),
    ],
)
def test_env_file_line_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, expected: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    load_env_file(env_file)

    assert os.environ["GROQ_API_KEY"] == expected


def test_comments_and_blank_lines_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\n   \nGROQ_API_KEY=real\n# PULSE_LLM_MODEL=commented-out\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("PULSE_LLM_MODEL", raising=False)

    assert load_env_file(env_file) == ["GROQ_API_KEY"]
    assert "PULSE_LLM_MODEL" not in os.environ


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "absent.env") == []


def test_malformed_lines_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "this line has no equals sign\n=novalue\nGROQ_API_KEY=ok\n", encoding="utf-8"
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert load_env_file(env_file) == ["GROQ_API_KEY"]


def test_the_env_file_path_is_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("GROQ_API_KEY=from-custom\n", encoding="utf-8")
    monkeypatch.setenv("PULSE_ENV_FILE", str(env_file))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    load_env_file()

    assert os.environ["GROQ_API_KEY"] == "from-custom"


def test_load_settings_picks_up_the_env_file(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end point of the loader: a `.env` is equivalent to exporting."""
    env_file = tmp_path / ".env"
    env_file.write_text("PULSE_LLM_MODEL=openai/gpt-oss-120b\n", encoding="utf-8")
    monkeypatch.setenv("PULSE_ENV_FILE", str(env_file))
    monkeypatch.delenv("PULSE_LLM_MODEL", raising=False)

    assert load_settings(config_path).pulse.llm.model == "openai/gpt-oss-120b"


def test_the_committed_example_file_names_no_real_key() -> None:
    """`.env.example` is committed, so it must only ever hold placeholders."""
    example = Path(".env.example").read_text(encoding="utf-8")
    assert "gsk_your_key_here" in example
    for line in example.splitlines():
        if line.startswith("GROQ_API_KEY="):
            assert line.split("=", 1)[1].startswith("gsk_your_")
