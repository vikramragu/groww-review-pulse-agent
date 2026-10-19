"""Checks behind `pulse doctor` (P0-T8).

The behaviour worth pinning down here is the status *policy*, not the wording: a missing
review export must warn (the export source is still open decision O1, and a fresh checkout
is legitimate), while a broken config must fail. And the MCP probes must report SKIP rather
than OK until Phase 4 — `doctor` claiming servers are fine when nothing contacted them
would be the exact false assurance the command exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse.doctor import (
    PLACEHOLDER_KEYS,
    CheckResult,
    CheckStatus,
    check_config,
    check_input,
    check_llm,
    check_mcp_servers,
    check_python,
    check_recipient,
    check_runs_dir,
    check_taxonomy,
    exit_code,
    run_checks,
)
from tests.test_config import write_config

MANAGED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GROQ_API_KEY",
    "PULSE_LLM_PROVIDER",
    "PULSE_LLM_MODEL",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*MANAGED_ENV, "PULSE_RECIPIENT", "PULSE_CONFIG_DIR", "PULSE_INPUT"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------------------


def test_python_check_passes_on_a_supported_interpreter() -> None:
    assert check_python().status is CheckStatus.OK


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def test_config_check_passes_on_the_repository_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    result, settings = check_config()
    assert result.status is CheckStatus.OK
    assert settings is not None


def test_config_check_fails_when_the_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", str(tmp_path / "absent"))
    result, settings = check_config()
    assert result.status is CheckStatus.FAIL
    assert settings is None


def test_config_check_fails_on_a_taxonomy_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pulse.models import Theme

    keys = [theme.value for theme in Theme] + ["sixth_theme"]
    from tests.test_config import _themes_toml

    directory = write_config(tmp_path / "config", themes=_themes_toml(keys))
    monkeypatch.setenv("PULSE_CONFIG_DIR", str(directory))

    result, settings = check_config()
    assert result.status is CheckStatus.FAIL
    assert settings is None


def test_taxonomy_check_skips_when_config_failed() -> None:
    assert check_taxonomy(None).status is CheckStatus.SKIP


def test_taxonomy_check_passes_on_the_repository_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    _, settings = check_config()
    assert check_taxonomy(settings).status is CheckStatus.OK


# --------------------------------------------------------------------------------------
# LLM provider, model, and credential
#
# Provider-aware rather than Anthropic-specific: the provider is a configuration choice
# (architecture.md D6/D6a), so a hardcoded check would make `doctor` lie about a correctly
# configured run on a different provider.
# --------------------------------------------------------------------------------------


def llm_results(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, CheckResult]:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _, settings = check_config()
    return {result.name: result for result in check_llm(settings)}


def test_groq_key_satisfies_the_credential_check(monkeypatch: pytest.MonkeyPatch) -> None:
    results = llm_results(monkeypatch, GROQ_API_KEY="gsk-not-a-real-key")
    assert results["llm-auth"].status is CheckStatus.OK
    assert results["llm-provider"].detail == "groq"


def test_missing_groq_key_fails_but_says_what_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = llm_results(monkeypatch)
    result = results["llm-auth"]

    assert result.status is CheckStatus.FAIL
    assert result.hint is not None
    assert "GROQ_API_KEY" in result.detail
    # Stages 1-3 and the test suite are unaffected; saying so prevents a false blocker.
    assert "--to 3" in result.hint


def test_an_unchosen_model_warns_rather_than_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchosen model is a warning, not a failure -- stages 1-3 still work.

    The model is cleared on the settings object rather than via `PULSE_LLM_MODEL=""`,
    because an empty environment variable means "not set, use the file" — the same rule
    the other overrides follow.
    """
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-not-a-real-key")
    _, loaded = check_config()
    assert loaded is not None
    unset = loaded.model_copy(
        update={
            "pulse": loaded.pulse.model_copy(
                update={"llm": loaded.pulse.llm.model_copy(update={"model": ""})}
            )
        }
    )

    results = {r.name: r for r in check_llm(unset)}
    result = results["llm-model"]

    assert result.status is CheckStatus.WARN
    assert result.hint is not None
    assert "pulse models" in result.hint


def test_a_chosen_model_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    results = llm_results(
        monkeypatch, GROQ_API_KEY="gsk-not-a-real-key", PULSE_LLM_MODEL="openai/gpt-oss-120b"
    )
    assert results["llm-model"].status is CheckStatus.OK
    assert results["llm-model"].detail == "openai/gpt-oss-120b"


def test_the_fake_provider_needs_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--llm fake` must leave `doctor` green so offline development is unblocked."""
    results = llm_results(monkeypatch, PULSE_LLM_PROVIDER="fake")

    assert results["llm-provider"].detail == "fake"
    assert results["llm-model"].status is CheckStatus.OK
    assert "llm-auth" not in results


def test_provider_none_warns_that_model_stages_will_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = llm_results(monkeypatch, PULSE_LLM_PROVIDER="none")

    assert results["llm-model"].status is CheckStatus.WARN
    assert "refuse" in results["llm-model"].detail


def test_llm_checks_skip_when_config_failed() -> None:
    (result,) = check_llm(None)
    assert result.status is CheckStatus.SKIP


def test_doctor_makes_no_network_call_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor` is run when something is already wrong; it must not spend money to say so.

    Constructing a Groq client requires a key, so the absence of one here also proves no
    client was built — the check is a pure environment read.
    """
    import pulse.llm as llm_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("doctor must not construct an LLM client")

    monkeypatch.setattr(llm_module, "_groq_client", explode)
    results = llm_results(monkeypatch, GROQ_API_KEY="gsk-not-a-real-key")
    assert results["llm-auth"].status is CheckStatus.OK


# --------------------------------------------------------------------------------------
# Review input — absence warns, it does not fail
# --------------------------------------------------------------------------------------


def test_missing_input_warns_rather_than_failing(tmp_path: Path) -> None:
    """A fresh checkout has no export yet; that must not make `doctor` fail."""
    result = check_input(None, tmp_path / "absent.json")
    assert result.status is CheckStatus.WARN
    assert result.hint is not None and "O1" in result.hint


def test_empty_input_directory_warns(tmp_path: Path) -> None:
    assert check_input(None, tmp_path).status is CheckStatus.WARN


def test_input_directory_with_an_export_passes(tmp_path: Path) -> None:
    (tmp_path / "reviews.json").write_text("[]", encoding="utf-8")
    result = check_input(None, tmp_path)
    assert result.status is CheckStatus.OK
    assert "1 export file" in result.detail


def test_input_file_passes(tmp_path: Path) -> None:
    export = tmp_path / "reviews.csv"
    export.write_text("rating,text\n", encoding="utf-8")
    assert check_input(None, export).status is CheckStatus.OK


def test_input_file_with_unexpected_suffix_warns(tmp_path: Path) -> None:
    export = tmp_path / "reviews.xlsx"
    export.write_text("not really a spreadsheet", encoding="utf-8")
    assert check_input(None, export).status is CheckStatus.WARN


def test_input_check_skips_without_config() -> None:
    assert check_input(None, None).status is CheckStatus.SKIP


# --------------------------------------------------------------------------------------
# Runs directory
# --------------------------------------------------------------------------------------


def test_runs_dir_passes_when_it_can_be_created(tmp_path: Path) -> None:
    result = check_runs_dir(tmp_path / "runs")
    assert result.status is CheckStatus.OK
    assert "will be created" in result.detail
    assert not (tmp_path / "runs").exists(), "doctor must not create anything"


def test_runs_dir_passes_and_counts_existing_runs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "2026-W41-01").mkdir(parents=True)
    result = check_runs_dir(runs)
    assert result.status is CheckStatus.OK
    assert "1 run" in result.detail


def test_runs_dir_fails_when_a_file_occupies_the_path(tmp_path: Path) -> None:
    occupied = tmp_path / "runs"
    occupied.write_text("in the way", encoding="utf-8")
    assert check_runs_dir(occupied).status is CheckStatus.FAIL


# --------------------------------------------------------------------------------------
# Deferred values
# --------------------------------------------------------------------------------------


def test_recipient_absence_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    _, settings = check_config()
    result = check_recipient(settings)
    assert result.status is CheckStatus.WARN
    assert result.hint is not None and "O3" in result.hint


def test_recipient_presence_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    monkeypatch.setenv("PULSE_RECIPIENT", "pulse@example.com")
    _, settings = check_config()
    assert check_recipient(settings).status is CheckStatus.OK


# --------------------------------------------------------------------------------------
# MCP probes stay honest until Phase 4
# --------------------------------------------------------------------------------------


def test_mcp_probes_contact_the_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub really answers. The live server, with no token configured yet, is a gap to
    fill (WARN) rather than a fault -- like PULSE_RECIPIENT."""
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    _, settings = check_config()

    results = {result.name: result for result in check_mcp_servers(settings)}

    assert results["mcp-stub"].status is CheckStatus.OK
    assert "google-workspace-mcp 0.1" in results["mcp-stub"].detail
    assert results["mcp-live"].status is CheckStatus.WARN
    assert "PULSE_MCP_TOKEN" in (results["mcp-live"].hint or "")
    assert results["mcp-doc"].status is CheckStatus.WARN
    assert "PULSE_DOC_ID" in (results["mcp-doc"].hint or "")


# --------------------------------------------------------------------------------------
# Aggregate behaviour
# --------------------------------------------------------------------------------------


def test_run_checks_returns_uniquely_named_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    results = run_checks()
    names = [result.name for result in results]
    assert len(names) == len(set(names))


def test_exit_code_is_zero_when_only_warnings_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", "config")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-not-a-real-key")
    monkeypatch.setenv("PULSE_LLM_MODEL", "openai/gpt-oss-120b")
    results = run_checks(input_override=Path("does-not-exist.json"))

    assert any(result.status is CheckStatus.WARN for result in results)
    assert exit_code(results) == 0


def test_exit_code_is_one_when_any_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_CONFIG_DIR", str(tmp_path / "absent"))
    assert exit_code(run_checks()) == 1


# --------------------------------------------------------------------------------------
# Placeholder detection
#
# `pulse init-env` writes a template containing a placeholder key. Reporting that as
# configured would be a false green -- the run would reach stage 4 before the provider
# rejected it, which is exactly the kind of late surprise `doctor` exists to prevent.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_KEYS))
def test_a_placeholder_key_is_not_treated_as_configured(
    monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    results = llm_results(monkeypatch, GROQ_API_KEY=placeholder)
    result = results["llm-auth"]

    assert result.status is CheckStatus.FAIL
    assert "placeholder" in result.detail
    assert result.hint is not None and ".env" in result.hint


def test_surrounding_whitespace_does_not_hide_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = llm_results(monkeypatch, GROQ_API_KEY="  gsk_your_key_here  ")
    assert results["llm-auth"].status is CheckStatus.FAIL


def test_a_real_looking_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    results = llm_results(monkeypatch, GROQ_API_KEY="gsk_aRealLookingKey123")
    assert results["llm-auth"].status is CheckStatus.OK


def test_an_empty_key_reports_as_unset_not_as_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different causes deserve different advice: "unset" points at `pulse init-env`."""
    results = llm_results(monkeypatch, GROQ_API_KEY="")
    result = results["llm-auth"]

    assert result.status is CheckStatus.FAIL
    assert "not set" in result.detail
    assert result.hint is not None and "pulse init-env" in result.hint
