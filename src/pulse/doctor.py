"""Environment checks behind `pulse doctor` (P0-T8).

Kept separate from `cli.py` so the checks are unit-testable without invoking Typer, and so
presentation never gets entangled with detection.

Design rule: **every check is read-only and spends nothing.** `doctor` is the command
people run when something is already wrong, so it must not create files or spend money to
tell them what it found. The MCP probes do spawn the configured servers, but call only
`list_tools()` and the read-only `find_document`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

import anyio

from pulse.config import ConfigError, LlmProvider, Settings, config_dir, load_settings
from pulse.llm import GROQ_API_KEY_ENV
from pulse.mcp.adapters import ALL_ROLES, McpMode, ProbeResult, probe
from pulse.mcp.contract import ErrorCode
from pulse.mcp.errors import McpCredentialError, McpTokenMissing, McpToolError, McpUnavailable
from pulse.models import Theme
from pulse.orchestrator import DEFAULT_RUNS_ROOT, last_complete_iso_week

MIN_PYTHON = (3, 11)

#: Template values that mean "nobody has filled this in yet".
#:
#: Deliberately excludes the empty string: an unset key and an unedited template are
#: different problems with different fixes ("run `pulse init-env`" versus "edit the file
#: you already have"), so they get different messages.
PLACEHOLDER_KEYS = frozenset({"gsk_your_key_here", "gsk_your_actual_key", "changeme"})
REVIEW_SUFFIXES = {".json", ".csv"}


class CheckStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    hint: str | None = None


def check_python() -> CheckResult:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= MIN_PYTHON:
        return CheckResult("python", CheckStatus.OK, f"{version}")
    required = ".".join(str(part) for part in MIN_PYTHON)
    return CheckResult(
        "python",
        CheckStatus.FAIL,
        f"{version} is older than the required {required}+",
        "Install a newer Python and recreate the virtualenv.",
    )


def check_config() -> tuple[CheckResult, Settings | None]:
    """Parse and validate all configuration. Everything downstream depends on this."""
    directory = config_dir()
    if not directory.is_dir():
        return (
            CheckResult(
                "config",
                CheckStatus.FAIL,
                f"config directory not found: {directory}",
                "Run from the repository root, or set PULSE_CONFIG_DIR.",
            ),
            None,
        )
    try:
        settings = load_settings(directory)
    except ConfigError as exc:
        first_line = str(exc).splitlines()[0]
        return (
            CheckResult("config", CheckStatus.FAIL, first_line, str(exc)),
            None,
        )
    return (
        CheckResult(
            "config",
            CheckStatus.OK,
            f"{directory}/ parsed; taxonomy v{settings.taxonomy_version}",
        ),
        settings,
    )


def check_taxonomy(settings: Settings | None) -> CheckResult:
    """Confirm the configured taxonomy is exactly the five themes the enum declares."""
    if settings is None:
        return CheckResult("taxonomy", CheckStatus.SKIP, "config did not load")

    configured = set(settings.themes.themes)
    declared = {theme.value for theme in Theme}
    if configured != declared:
        return CheckResult(
            "taxonomy",
            CheckStatus.FAIL,
            "themes.toml and models.Theme disagree",
            f"configured={sorted(configured)} enum={sorted(declared)}",
        )
    return CheckResult("taxonomy", CheckStatus.OK, f"{len(declared)} themes, matching models.Theme")


def check_llm(settings: Settings | None) -> list[CheckResult]:
    """Check the configured LLM provider, model, and credential. No network call.

    Provider-aware rather than Anthropic-specific: the provider is a configuration choice
    (architecture.md D6), so hardcoding one here would make `doctor` lie about a correctly
    configured run.
    """
    if settings is None:
        return [CheckResult("llm", CheckStatus.SKIP, "config did not load")]

    llm = settings.pulse.llm
    results = [CheckResult("llm-provider", CheckStatus.OK, f"{llm.provider.value}")]

    if llm.provider is LlmProvider.NONE:
        results.append(
            CheckResult(
                "llm-model",
                CheckStatus.WARN,
                "provider is 'none'; stages 4 and 6 will refuse to run",
                "Set `[llm] provider` in config/pulse.toml.",
            )
        )
        return results

    if llm.provider is LlmProvider.FAKE:
        results.append(
            CheckResult("llm-model", CheckStatus.OK, "fake provider; no credential or model needed")
        )
        return results

    # --- groq ---
    if llm.model.strip():
        results.append(CheckResult("llm-model", CheckStatus.OK, llm.model))
    else:
        results.append(
            CheckResult(
                "llm-model",
                CheckStatus.WARN,
                "no model chosen yet",
                "Run `pulse models` to list what your account offers, then set "
                "`[llm] model` in config/pulse.toml.",
            )
        )

    key = os.environ.get(GROQ_API_KEY_ENV, "").strip()
    if key in PLACEHOLDER_KEYS:
        # `pulse init-env` writes a template containing a placeholder. Reporting that as a
        # configured credential would be a false green: the run would reach stage 4 and
        # only then fail with an auth error from the provider.
        results.append(
            CheckResult(
                "llm-auth",
                CheckStatus.FAIL,
                f"{GROQ_API_KEY_ENV} is still the placeholder from .env.example",
                "Edit .env and replace it with your real key from https://console.groq.com",
            )
        )
    elif key:
        results.append(CheckResult("llm-auth", CheckStatus.OK, f"{GROQ_API_KEY_ENV} is set"))
    else:
        results.append(
            CheckResult(
                "llm-auth",
                CheckStatus.FAIL,
                f"{GROQ_API_KEY_ENV} is not set",
                "Run `pulse init-env` to create a .env, then paste your key into it. "
                "Stages 1-3 (`pulse run --to 3`) and the whole test suite work without it.",
            )
        )

    return results


def check_input(settings: Settings | None, override: Path | None = None) -> CheckResult:
    """Look for a review export. Absence is a warning, not a failure.

    The export source is still an open decision (O1), and stages that need data say so
    themselves — so `doctor` reporting FAIL here would block a legitimate fresh checkout.
    """
    if override is not None:
        path = override
    elif settings is not None:
        path = settings.pulse.input.path
    else:
        return CheckResult("review-input", CheckStatus.SKIP, "config did not load")

    if not path.exists():
        return CheckResult(
            "review-input",
            CheckStatus.WARN,
            f"{path} does not exist",
            "Drop a public Play Store export there, or pass --input. "
            "Export format is open decision O1.",
        )

    if path.is_file():
        if path.suffix.lower() not in REVIEW_SUFFIXES:
            return CheckResult(
                "review-input",
                CheckStatus.WARN,
                f"{path} has an unexpected suffix {path.suffix!r}",
                f"The file-drop adapter reads {sorted(REVIEW_SUFFIXES)}.",
            )
        size_kb = path.stat().st_size / 1024
        return CheckResult("review-input", CheckStatus.OK, f"{path} ({size_kb:.1f} KiB)")

    candidates = [child for child in path.iterdir() if child.suffix.lower() in REVIEW_SUFFIXES]
    if not candidates:
        return CheckResult(
            "review-input",
            CheckStatus.WARN,
            f"{path}/ contains no .json or .csv export",
            "Drop a public Play Store export there, or pass --input.",
        )
    return CheckResult(
        "review-input", CheckStatus.OK, f"{path}/ has {len(candidates)} export file(s)"
    )


def check_runs_dir(runs_root: Path = DEFAULT_RUNS_ROOT) -> CheckResult:
    """Confirm run artifacts can be written, without leaving anything behind."""
    if runs_root.exists():
        if not runs_root.is_dir():
            return CheckResult(
                "runs-dir",
                CheckStatus.FAIL,
                f"{runs_root} exists but is not a directory",
                "Remove or rename it.",
            )
        if not os.access(runs_root, os.W_OK):
            return CheckResult(
                "runs-dir", CheckStatus.FAIL, f"{runs_root} is not writable", "Fix permissions."
            )
        existing = sum(1 for child in runs_root.iterdir() if child.is_dir())
        return CheckResult("runs-dir", CheckStatus.OK, f"{runs_root}/ ({existing} run(s))")

    parent = runs_root.parent if str(runs_root.parent) else Path(".")
    if not os.access(parent, os.W_OK):
        return CheckResult(
            "runs-dir",
            CheckStatus.FAIL,
            f"cannot create {runs_root} — {parent} is not writable",
            "Fix permissions or run from a writable checkout.",
        )
    return CheckResult("runs-dir", CheckStatus.OK, f"{runs_root}/ will be created on first run")


def check_recipient(settings: Settings | None) -> CheckResult:
    """The Gmail recipient is only needed for stage 10, so absence is a warning."""
    if settings is None:
        return CheckResult("draft-recipient", CheckStatus.SKIP, "config did not load")
    if settings.recipient:
        return CheckResult("draft-recipient", CheckStatus.OK, settings.recipient)
    return CheckResult(
        "draft-recipient",
        CheckStatus.WARN,
        "PULSE_RECIPIENT is not set",
        "Required for stage 10 only (open decision O3).",
    )


_TOOL_ERROR_HINTS: dict[ErrorCode, tuple[str, str]] = {
    ErrorCode.AUTH_REQUIRED: (
        "server is reachable but not authorized with Google",
        "Re-run `npm run auth` for the server and update GOOGLE_TOKEN_JSON_B64; this agent "
        "never handles Google credentials.",
    ),
    ErrorCode.NOT_FOUND: (
        "the configured Google Doc was not found or is not shared with the server's account",
        "Check PULSE_DOC_ID against the doc's URL: docs.google.com/document/d/<ID>/edit.",
    ),
    ErrorCode.PERMISSION_DENIED: (
        "the server refused access to the configured doc",
        "Give the server's account edit access, or add the doc to ALLOWED_DOCUMENT_IDS.",
    ),
    ErrorCode.FEATURE_DISABLED: (
        "a tool this agent needs is disabled on the server",
        "Set ENABLE_DOCS_APPEND and ENABLE_GMAIL_DRAFTS to true on the server.",
    ),
}


def _probe_result(name: str, result: ProbeResult, *, live: bool) -> CheckResult:
    if result.ok:
        # The stub exposes a send tool on purpose, so tests can prove it is never called;
        # warning about it there would be noise. On the live server it is worth a WARN.
        if result.warnings and live:
            return CheckResult(name, CheckStatus.WARN, result.detail, result.warnings[0])
        return CheckResult(name, CheckStatus.OK, result.detail)

    error = result.error
    if isinstance(error, McpToolError) and error.code in _TOOL_ERROR_HINTS:
        detail, hint = _TOOL_ERROR_HINTS[error.code]
        return CheckResult(name, CheckStatus.FAIL, detail, hint)
    if isinstance(error, McpTokenMissing):
        # Not yet configured, like PULSE_RECIPIENT: a gap to fill, not a fault.
        return CheckResult(
            name, CheckStatus.WARN, "no MCP token configured", result.detail.splitlines()[0]
        )
    if isinstance(error, McpCredentialError):
        return CheckResult(name, CheckStatus.FAIL, result.detail.splitlines()[0])
    if isinstance(error, McpUnavailable) and live:
        # The deployed server being down is a state to report, not a fault in this
        # environment -- the analysis half still works.
        return CheckResult(
            name,
            CheckStatus.WARN,
            result.detail.splitlines()[0],
            "`pulse run --mcp stub` and `--dry-run` work without it.",
        )
    return CheckResult(name, CheckStatus.FAIL, result.detail.splitlines()[0])


def check_mcp_servers(settings: Settings | None) -> list[CheckResult]:
    """Connect to each server, check identity and version, bind every tool the agent uses,
    and -- when a doc is configured -- prove it is reachable with the read-only get-info."""
    if settings is None:
        return [CheckResult("mcp", CheckStatus.SKIP, "config did not load")]

    document_id = settings.pulse.docs.document_id.strip() or None
    targets: list[tuple[str, McpMode]] = [("mcp-live", "live")]
    if settings.mcp.stub is not None:
        targets.append(("mcp-stub", "stub"))

    results: list[CheckResult] = []
    for name, mode in targets:
        # The stub only knows its own document, so it is probed without one.
        doc = document_id if mode == "live" else None
        result = anyio.run(partial(probe, settings, mode, ALL_ROLES, document_id=doc))
        results.append(_probe_result(name, result, live=mode == "live"))

    if document_id is None:
        results.append(
            CheckResult(
                "mcp-doc",
                CheckStatus.WARN,
                "no Google Doc configured",
                "Required for stage 9: set PULSE_DOC_ID (implementation-plan.md D-4).",
            )
        )
    return results


def run_checks(input_override: Path | None = None) -> list[CheckResult]:
    """Run every check in dependency order and return the results."""
    results = [check_python()]

    config_result, settings = check_config()
    results.append(config_result)
    results.append(check_taxonomy(settings))
    results.extend(check_llm(settings))
    results.append(check_input(settings, input_override))
    results.append(check_runs_dir())
    results.append(check_recipient(settings))
    results.extend(check_mcp_servers(settings))
    results.append(
        CheckResult(
            "target-week",
            CheckStatus.OK,
            f"{last_complete_iso_week()} (last complete ISO week)",
        )
    )
    return results


def exit_code(results: list[CheckResult]) -> int:
    """0 when nothing failed. Warnings do not fail the command."""
    return 1 if any(result.status is CheckStatus.FAIL for result in results) else 0
