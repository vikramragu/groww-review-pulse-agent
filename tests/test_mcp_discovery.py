"""P4-T18 / P5-T1: discovery -- server identity and version, tool binding, transports.

Everything here must fail at startup with a message that says what to do, never later as
a confusing failure in stage 9.
"""

from __future__ import annotations

import dataclasses
import json
from functools import partial
from pathlib import Path

import anyio
import pytest
from mcp import types

from pulse.config import McpServerConfig, Settings, load_settings
from pulse.mcp.adapters import ALL_ROLES, McpMode, ProbeResult, connect_docs, probe
from pulse.mcp.contract import ServerVersion, load_contract
from pulse.mcp.discovery import check_server, resolve_tools
from pulse.mcp.errors import (
    ContractMismatch,
    McpCredentialError,
    McpTokenMissing,
    McpUnavailable,
    MissingTool,
)
from pulse.mcp.session import open_session
from tests.mcp_support import TOKEN_ENV, http_stub, live_settings, stub_settings

CONTRACT = load_contract(Path("contract/google-workspace-mcp-0.1.json"))
SERVER = "google-workspace-mcp"


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


# --------------------------------------------------------------------------------------
# Identity and version
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.1.0", "0.1.7", "0.1"])
def test_the_contracted_version_is_silent(version: str) -> None:
    """Patch releases are ignored."""
    assert check_server(SERVER, version, CONTRACT, label="live") is None


@pytest.mark.parametrize("version", ["0.2.0", "0.0.9", "1.0.0"])
def test_a_different_0x_minor_or_major_aborts(version: str) -> None:
    """Under semver, 0.x minors may break; the agent will not guess."""
    with pytest.raises(ContractMismatch, match=r"0\.1"):
        check_server(SERVER, version, CONTRACT, label="live")


def test_after_1_0_a_minor_bump_only_warns() -> None:
    stable = dataclasses.replace(CONTRACT, version=ServerVersion(1, 0))
    warning = check_server(SERVER, "1.3.0", stable, label="live")
    assert warning is not None and "1.3" in warning
    with pytest.raises(ContractMismatch):
        check_server(SERVER, "2.0.0", stable, label="live")


def test_a_different_server_aborts() -> None:
    with pytest.raises(ContractMismatch, match="some-other-mcp"):
        check_server("some-other-mcp", "0.1.0", CONTRACT, label="live")


def test_an_unparseable_version_aborts() -> None:
    with pytest.raises(ContractMismatch, match="semver"):
        check_server(SERVER, "latest", CONTRACT, label="live")


# --------------------------------------------------------------------------------------
# Tool binding
# --------------------------------------------------------------------------------------


def a_tool(name: str) -> types.Tool:
    return types.Tool(name=name, input_schema={"type": "object"})


def test_configured_tools_resolve_to_advertised_names() -> None:
    resolved = resolve_tools(
        [a_tool("docs_get_info")],
        {"get_info": "docs_get_info"},
        ["get_info"],
        CONTRACT,
        label="live",
    )
    assert resolved == {"get_info": "docs_get_info"}


def test_a_missing_tool_lists_what_the_server_actually_offers() -> None:
    with pytest.raises(MissingTool) as caught:
        resolve_tools(
            [a_tool("ping"), a_tool("docs_get_info")],
            {"append": "docs_append_content"},
            ["append"],
            CONTRACT,
            label="live",
        )
    assert "docs_append_content" in str(caught.value)
    assert "docs_get_info, ping" in str(caught.value)


def test_a_send_tool_is_never_bound_even_if_configured() -> None:
    """Defence in depth for D-1: editing config cannot turn the draft into a send."""
    with pytest.raises(MissingTool, match="never binds"):
        resolve_tools(
            [a_tool("gmail_send_email")],
            {"create_draft": "gmail_send_email"},
            ["create_draft"],
            CONTRACT,
            label="live",
        )


def test_a_role_missing_from_config_is_named() -> None:
    with pytest.raises(MissingTool, match="no `append` entry"):
        resolve_tools([a_tool("x")], {}, ["append"], CONTRACT, label="live")


# --------------------------------------------------------------------------------------
# Against the stub (stdio)
# --------------------------------------------------------------------------------------


def run_probe(
    settings: Settings, mode: McpMode = "stub", document_id: str | None = None
) -> ProbeResult:
    return anyio.run(partial(probe, settings, mode, ALL_ROLES, document_id=document_id))


def test_the_stub_binds_and_reports_its_send_tool(settings: Settings, tmp_path: Path) -> None:
    result = run_probe(stub_settings(settings, tmp_path / "s.json"), document_id="stub-doc")
    assert result.ok, result.detail
    assert "is reachable" in result.detail
    assert any("gmail_send_email" in w for w in result.warnings)


def test_a_version_the_contract_does_not_cover_aborts(settings: Settings, tmp_path: Path) -> None:
    tuned = stub_settings(settings, tmp_path / "s.json", PULSE_STUB_VERSION="0.2.0")
    result = run_probe(tuned)
    assert isinstance(result.error, ContractMismatch)


def test_a_different_server_aborts_at_startup(settings: Settings, tmp_path: Path) -> None:
    tuned = stub_settings(settings, tmp_path / "s.json", PULSE_STUB_NAME="some-other-mcp")
    assert isinstance(run_probe(tuned).error, ContractMismatch)


def test_a_disabled_tool_aborts_naming_what_exists(settings: Settings, tmp_path: Path) -> None:
    """ENABLE_DOCS_APPEND=false on the server omits the tool from tools/list."""
    tuned = stub_settings(settings, tmp_path / "s.json", PULSE_STUB_HIDE="docs_append_content")
    result = run_probe(tuned)
    assert isinstance(result.error, MissingTool)
    assert "docs_append_content" in result.detail


def test_a_stale_state_file_does_not_break_the_stub(settings: Settings, tmp_path: Path) -> None:
    """Found for real: Phase 4's stub left a state file the Phase 5 stub could not read."""
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"documents": {"doc-0001": {"title": "old"}}}), encoding="utf-8")
    assert run_probe(stub_settings(settings, state), document_id="stub-doc").ok


# --------------------------------------------------------------------------------------
# Over HTTP, the way the deployed server is reached
# --------------------------------------------------------------------------------------


def test_http_with_the_right_token_works(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "good-token")
    with http_stub() as url:
        result = run_probe(live_settings(settings, url), "live", document_id="stub-doc")
    assert result.ok, result.detail


def test_a_wrong_token_is_reported_as_a_wrong_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK hides the 401 behind a generic error; the session recovers it."""
    monkeypatch.setenv(TOKEN_ENV, "wrong-token")
    with http_stub() as url:
        result = run_probe(live_settings(settings, url), "live")
    assert isinstance(result.error, McpCredentialError)
    assert "HTTP 401" in result.detail
    assert "wrong-token" not in result.detail


def test_a_missing_token_says_where_to_put_it(settings: Settings) -> None:
    result = run_probe(live_settings(settings, "http://127.0.0.1:9/mcp"), "live")
    assert isinstance(result.error, McpTokenMissing)
    assert TOKEN_ENV in result.detail and ".env" in result.detail


def test_an_unreachable_server_is_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "t")
    result = run_probe(live_settings(settings, "http://127.0.0.1:9/mcp"), "live")
    assert isinstance(result.error, McpUnavailable)
    assert "--mcp stub" in result.detail


# --------------------------------------------------------------------------------------
# A stdio server that cannot be used at all
# --------------------------------------------------------------------------------------


async def _open(config: McpServerConfig) -> None:
    async with open_session(config, label="stub", connect_timeout=15):
        pass


def test_a_command_that_does_not_exist_suggests_the_stub() -> None:
    config = McpServerConfig(command="definitely-not-a-real-command-xyz")
    with pytest.raises(McpUnavailable) as caught:
        anyio.run(_open, config)
    assert "definitely-not-a-real-command-xyz" in str(caught.value)
    assert "--mcp stub" in str(caught.value)


def test_a_server_that_crashes_on_startup_shows_its_stderr() -> None:
    config = McpServerConfig(command="python", args=["-m", "no_such_mcp_server_module"])
    with pytest.raises(McpUnavailable) as caught:
        anyio.run(_open, config)
    assert "No module named" in str(caught.value)


def test_an_error_inside_a_session_arrives_unwrapped(settings: Settings, tmp_path: Path) -> None:
    """The SDK wraps everything in nested ExceptionGroups; callers must see the real error."""

    async def body() -> None:
        async with connect_docs(stub_settings(settings, tmp_path / "s.json"), "stub"):
            raise KeyError("the caller's own bug")

    with pytest.raises(KeyError, match="the caller's own bug"):
        anyio.run(body)
