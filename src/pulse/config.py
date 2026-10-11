"""Configuration loading: TOML files plus environment overrides.

Precedence is CLI > environment > `.env` file > TOML > built-in default. CLI flags are
applied by the caller (see `cli.py`); everything below that is resolved here.

Two things this module deliberately does *not* do:

* It never reads or stores a Google credential. Those belong to the separate MCP servers
  project (architecture.md D8).
* It never reads the Anthropic API key into a config object. The SDK resolves credentials
  itself from `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile,
  so a bare `anthropic.Anthropic()` is correct and no key is handled here.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulse.models import Theme

ModelT = TypeVar("ModelT", bound=BaseModel)

CONFIG_DIR_ENV = "PULSE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("config")

ENV_FILE_ENV = "PULSE_ENV_FILE"
DEFAULT_ENV_FILE = Path(".env")
ENV_EXAMPLE_FILE = Path(".env.example")


def load_env_file(path: Path | None = None) -> list[str]:
    """Load `KEY=value` lines from a `.env` file into the environment.

    Deliberately dependency-free (about fifteen lines beats adding `python-dotenv` for
    this), and deliberately **non-overriding**: a variable already present in the real
    environment wins. A file that silently overrode an explicit `export` would be a
    genuinely confusing thing to debug.

    Returns the names it set, never the values — this function handles secrets, so nothing
    it returns or logs may contain one.
    """
    target = path or Path(os.environ.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE)
    if not target.is_file():
        return []

    applied: list[str] = []
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate `export KEY=value`, which is what people paste out of shell snippets.
        line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key not in os.environ:
            os.environ[key] = value
            applied.append(key)

    return applied


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or internally inconsistent.

    Carries a message written for whoever has to fix it: what was wrong, where, and what
    was expected.
    """


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class WindowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weeks: Annotated[int, Field(ge=1)] = 12
    min_weeks: Annotated[int, Field(ge=1)] = 8


class InputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("data/raw")


class DraftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3


class LlmProvider(StrEnum):
    GROQ = "groq"
    FAKE = "fake"
    NONE = "none"


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider = LlmProvider.GROQ
    #: Empty until chosen by probing the account; `require_model()` enforces it at use.
    model: str = ""
    batch_size: Annotated[int, Field(ge=1, le=200)] = 40
    classify_max_tokens: Annotated[int, Field(ge=256)] = 8000
    draft_max_tokens: Annotated[int, Field(ge=256)] = 4000
    #: Reasoning models spend output tokens deliberating. Classification is not a
    #: reasoning-heavy task (architecture.md §8.1), so it runs low; drafting is the
    #: quality-sensitive step and gets more room. Ignored by non-reasoning models.
    classify_reasoning_effort: Literal["low", "medium", "high"] = "low"
    draft_reasoning_effort: Literal["low", "medium", "high"] = "medium"
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.0

    @property
    def needs_api_key(self) -> bool:
        return self.provider is LlmProvider.GROQ


class ThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_week_reviews: Annotated[int, Field(ge=0)] = 20
    min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5


class DocsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_template: str = "Groww Review Pulse — {week}"
    folder_id: str = ""

    def title_for(self, iso_week: str) -> str:
        return self.title_template.format(week=iso_week)


class PulseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowConfig = Field(default_factory=WindowConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    draft: DraftConfig = Field(default_factory=DraftConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    docs: DocsConfig = Field(default_factory=DocsConfig)


class ThemeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    covers: str


class ThemesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    themes: dict[str, ThemeDefinition]

    def definition(self, theme: Theme) -> ThemeDefinition:
        return self.themes[theme.value]


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: str
    docs: McpServerConfig
    gmail: McpServerConfig
    stub: McpServerConfig | None = None


class Settings(BaseModel):
    """Everything the pipeline needs to run, resolved and validated."""

    model_config = ConfigDict(extra="forbid")

    pulse: PulseConfig
    themes: ThemesConfig
    mcp: McpConfig
    config_dir: Path
    # Required only for stage 10 (open decision O3), so it is optional here and checked
    # at the point of use rather than blocking a --dry-run.
    recipient: str | None = None

    @property
    def taxonomy_version(self) -> str:
        return self.themes.version


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Expected it in the config directory (override with {CONFIG_DIR_ENV})."
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc


def _validate(model: type[ModelT], data: dict[str, Any], source: Path) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration in {source}:\n{details}") from exc


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _apply_env(pulse: PulseConfig) -> PulseConfig:
    """Overlay environment variables documented in architecture.md §12."""
    updates: dict[str, Any] = {}

    if (weeks := _env_int("PULSE_WINDOW_WEEKS")) is not None:
        updates["window"] = pulse.window.model_copy(update={"weeks": weeks})
    if (min_weeks := _env_int("PULSE_MIN_WINDOW_WEEKS")) is not None:
        window = updates.get("window", pulse.window)
        updates["window"] = window.model_copy(update={"min_weeks": min_weeks})
    if (attempts := _env_int("PULSE_MAX_DRAFT_ATTEMPTS")) is not None:
        updates["draft"] = pulse.draft.model_copy(update={"max_attempts": attempts})
    if raw_input := os.environ.get("PULSE_INPUT"):
        updates["input"] = pulse.input.model_copy(update={"path": Path(raw_input)})

    llm_updates: dict[str, Any] = {}
    if raw_provider := os.environ.get("PULSE_LLM_PROVIDER"):
        try:
            llm_updates["provider"] = LlmProvider(raw_provider.strip().lower())
        except ValueError as exc:
            valid = ", ".join(p.value for p in LlmProvider)
            raise ConfigError(
                f"PULSE_LLM_PROVIDER must be one of {valid}, got {raw_provider!r}"
            ) from exc
    if raw_model := os.environ.get("PULSE_LLM_MODEL"):
        llm_updates["model"] = raw_model.strip()
    if llm_updates:
        updates["llm"] = pulse.llm.model_copy(update=llm_updates)

    return pulse.model_copy(update=updates) if updates else pulse


def _check_taxonomy_matches_enum(themes: ThemesConfig, source: Path) -> None:
    """The config and the `Theme` enum must describe exactly the same five themes.

    The enum is what structurally enforces the <=5 constraint; the config is what the
    classifier prompt is built from. If they drift, the prompt describes a taxonomy the
    schema will not accept — which surfaces as confusing classification failures rather
    than as the configuration error it actually is.
    """
    configured = set(themes.themes)
    declared = {theme.value for theme in Theme}

    if configured == declared:
        return

    missing = sorted(declared - configured)
    extra = sorted(configured - declared)
    problems = []
    if missing:
        problems.append(f"missing from {source.name}: {', '.join(missing)}")
    if extra:
        problems.append(f"not in the Theme enum: {', '.join(extra)}")

    raise ConfigError(
        "Theme taxonomy mismatch between config and models.Theme — "
        + "; ".join(problems)
        + "\nEdit both together and bump `version` in themes.toml (architecture.md D4)."
    )


def _validate_window(pulse: PulseConfig) -> None:
    if pulse.window.min_weeks > pulse.window.weeks:
        raise ConfigError(
            f"window.min_weeks ({pulse.window.min_weeks}) exceeds window.weeks "
            f"({pulse.window.weeks}); the minimum cannot be larger than the window."
        )


def config_dir() -> Path:
    return Path(os.environ.get(CONFIG_DIR_ENV) or DEFAULT_CONFIG_DIR)


def load_settings(directory: Path | None = None) -> Settings:
    """Load and validate all configuration. Raises `ConfigError` with an actionable message."""
    # Before reading any environment override, so a `.env` file is equivalent to having
    # exported the same variables. Real environment variables still win.
    load_env_file()
    base = directory or config_dir()

    pulse_path = base / "pulse.toml"
    themes_path = base / "themes.toml"
    mcp_path = base / "mcp.toml"

    pulse = _validate(PulseConfig, _read_toml(pulse_path), pulse_path)
    pulse = _apply_env(pulse)
    _validate_window(pulse)

    themes = _validate(ThemesConfig, _read_toml(themes_path), themes_path)
    _check_taxonomy_matches_enum(themes, themes_path)

    mcp = _validate(McpConfig, _read_toml(mcp_path), mcp_path)

    return Settings(
        pulse=pulse,
        themes=themes,
        mcp=mcp,
        config_dir=base,
        recipient=os.environ.get("PULSE_RECIPIENT") or None,
    )


def require_model(settings: Settings) -> str:
    """Resolve the LLM model, failing clearly when it has not been chosen yet.

    Left empty in the committed config on purpose: which Groq models accept a json_schema
    response format is model-dependent, so the choice is made by probing the account rather
    than by assuming (D6a).
    """
    model = settings.pulse.llm.model.strip()
    if not model:
        raise ConfigError(
            "No LLM model configured. Run `pulse models` to list what your account offers, "
            "then set `[llm] model` in config/pulse.toml or export PULSE_LLM_MODEL."
        )
    return model


def require_recipient(settings: Settings) -> str:
    """Resolve the Gmail draft recipient, failing clearly when it is unset (stage 10 only)."""
    if not settings.recipient:
        raise ConfigError(
            "No draft recipient configured. Set PULSE_RECIPIENT to the address or alias "
            "that should receive the weekly pulse draft (open decision O3)."
        )
    return settings.recipient
