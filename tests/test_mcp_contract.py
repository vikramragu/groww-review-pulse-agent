"""P5-T2: the consumer-driven contract with google-workspace-mcp, and the stub against it.

The contract records what the agent sends and reads. These tests pin its shape, the error
classification the agent relies on (the server's errors are prose only), and that the
compatibility check actually catches the server drifting away from us.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types

from pulse.config import Settings, load_settings
from pulse.mcp.adapters import ALL_ROLES, check_conformance
from pulse.mcp.contract import (
    ContractError,
    ErrorCode,
    ServerVersion,
    load_contract,
    request_schema_problems,
)
from pulse.mcp.discovery import Binding, compatibility_problems
from tests.mcp_support import TOKEN_ENV, http_stub, live_settings
from tests.stubs.stub_mcp_server import FAULT_MESSAGES, SCHEMAS

CONTRACT = Path("contract/google-workspace-mcp-0.1.json")


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


def test_the_contract_names_the_deployed_server() -> None:
    contract = load_contract(CONTRACT)
    assert contract.server == "google-workspace-mcp"
    assert contract.version == ServerVersion(0, 1)


def test_the_configured_contract_file_is_this_one(settings: Settings) -> None:
    assert settings.mcp.contract_file == CONTRACT


def test_it_covers_exactly_the_three_tools_the_agent_uses() -> None:
    contract = load_contract(CONTRACT)
    assert {t.role: t.name for t in contract.tools.values()} == {
        "get_info": "docs_get_info",
        "append": "docs_append_content",
        "create_draft": "gmail_create_draft",
    }
    assert set(ALL_ROLES) == {t.role for t in contract.tools.values()}


def test_no_send_tool_can_be_bound() -> None:
    """D-1: the pulse is a draft. The agent refuses any tool with `send` in its name."""
    contract = load_contract(CONTRACT)
    assert contract.forbids("gmail_send_email")
    assert contract.forbids("SendMessage")
    assert not contract.forbids("gmail_create_draft")


# --------------------------------------------------------------------------------------
# Error classification -- the server sends prose, never a code
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("AUTH", ErrorCode.AUTH_REQUIRED),
        ("SCOPE", ErrorCode.AUTH_REQUIRED),
        ("NOT_FOUND", ErrorCode.NOT_FOUND),
        ("NO_ACCESS", ErrorCode.PERMISSION_DENIED),
        ("ALLOWLIST", ErrorCode.PERMISSION_DENIED),
        ("UNAVAILABLE", ErrorCode.UNAVAILABLE),
        ("DISABLED", ErrorCode.FEATURE_DISABLED),
        ("BOOM", ErrorCode.UPSTREAM_ERROR),
    ],
)
def test_every_server_message_is_classified(kind: str, code: ErrorCode) -> None:
    """The messages are copied verbatim from the server's src/errors.ts."""
    assert load_contract(CONTRACT).classify_error(FAULT_MESSAGES[kind]) is code


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Input validation error: blocks must have at least 1 item", ErrorCode.INVALID_INPUT),
        ("Invalid subject: contains CR/LF", ErrorCode.INVALID_INPUT),
        ("Tab not found: t.9", ErrorCode.INVALID_INPUT),
        ("Recipient domain not allowed: a@b.com", ErrorCode.PERMISSION_DENIED),
        ("MCP error -32602: Tool docs_append_content not found", ErrorCode.FEATURE_DISABLED),
        ("Something entirely new went wrong", ErrorCode.UPSTREAM_ERROR),
    ],
)
def test_other_messages_are_classified(message: str, code: ErrorCode) -> None:
    assert load_contract(CONTRACT).classify_error(message) is code


# --------------------------------------------------------------------------------------
# Compatibility: does the server still accept what we send and declare what we read?
# --------------------------------------------------------------------------------------


def binding_for(tools: dict[str, dict[str, Any]], outputs: dict[str, dict[str, Any]]) -> Binding:
    advertised = {
        name: types.Tool(name=name, input_schema=schema, output_schema=outputs.get(name))
        for name, schema in tools.items()
    }
    contract = load_contract(CONTRACT)
    return Binding(
        label="test",
        server_version=ServerVersion(0, 1),
        tools={t.role: t.name for t in contract.tools.values()},
        advertised=advertised,
    )


def stub_outputs() -> dict[str, dict[str, Any]]:
    from tests.stubs.stub_mcp_server import OUTPUT_SCHEMAS

    return OUTPUT_SCHEMAS


def test_the_stubs_schemas_are_compatible() -> None:
    binding = binding_for(SCHEMAS, stub_outputs())
    assert compatibility_problems(binding, load_contract(CONTRACT), ALL_ROLES) == []


def test_a_renamed_input_field_is_caught() -> None:
    renamed = {**SCHEMAS}
    renamed["docs_get_info"] = {
        "type": "object",
        "properties": {"doc": {"type": "string"}},
        "required": ["doc"],
        "additionalProperties": False,
    }
    problems = compatibility_problems(
        binding_for(renamed, stub_outputs()), load_contract(CONTRACT), ALL_ROLES
    )
    assert any("docs_get_info rejects our request" in p for p in problems)


def test_a_dropped_response_field_is_caught() -> None:
    outputs = {**stub_outputs()}
    outputs["docs_append_content"] = {"type": "object", "properties": {"status": {}}}
    problems = compatibility_problems(
        binding_for(SCHEMAS, outputs), load_contract(CONTRACT), ALL_ROLES
    )
    assert any("no longer declares field(s) we read" in p and "url" in p for p in problems)


def test_draft_07_schemas_are_validated_as_draft_07() -> None:
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", **SCHEMAS["docs_get_info"]}
    assert request_schema_problems(schema, {"document": "x"}) == []
    assert request_schema_problems(schema, {"document": "x", "extra": 1})


def test_a_malformed_contract_file_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"server": "x"}', encoding="utf-8")
    with pytest.raises(ContractError, match="Malformed"):
        load_contract(broken)


# --------------------------------------------------------------------------------------
# The stub, over both transports
# --------------------------------------------------------------------------------------


def test_the_stub_conforms_over_stdio(settings: Settings) -> None:
    assert settings.mcp.stub is not None
    result = anyio.run(partial(check_conformance, settings, settings.mcp.stub, label="stub"))
    assert result.status == "ok", result.lines
    assert any("gmail_send_email" in line and "never call" in line for line in result.lines)


def test_the_stub_conforms_over_http(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "good-token")
    with http_stub() as url:
        configured = live_settings(settings, url)
        result = anyio.run(
            partial(check_conformance, configured, configured.mcp.live, label="live")
        )
    assert result.status == "ok", result.lines
