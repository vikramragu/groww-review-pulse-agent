"""`McpDocsAdapter` / `McpGmailAdapter`: the ports, implemented over MCP (P4-T7, P5-T4).

The only module that maps a port operation to a tool. Actual tool *names* come from
config/mcp.toml via discovery; what each request and reply must look like comes from the
contract file. Every request is checked before sending and every reply before use: the
server is a separate program, and the agent does not trust it to keep its promises
(architecture.md §18).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from mcp import types

from pulse.config import McpServerConfig, Settings
from pulse.mcp.contract import Contract, ContractError, load_contract
from pulse.mcp.discovery import Binding, compatibility_problems, discover
from pulse.mcp.errors import (
    McpClientError,
    McpCredentialError,
    McpProtocolError,
    McpTokenMissing,
    McpToolError,
    McpUnavailable,
)
from pulse.mcp.session import Connection, open_session
from pulse.publish.ports import AppendResult, Block, DocInfo, DraftRef

McpMode = Literal["stub", "live"]
ServerKind = Literal["docs", "gmail"]

ROLES: dict[ServerKind, tuple[str, ...]] = {
    "docs": ("get_info", "append"),
    "gmail": ("create_draft",),
}
ALL_ROLES: tuple[str, ...] = ROLES["docs"] + ROLES["gmail"]


def _reply_text(result: types.CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, types.TextContent)).strip()


def _payload(result: types.CallToolResult) -> Any:
    """The reply as data: structuredContent, else the first text block parsed as JSON."""
    if result.structured_content is not None:
        return result.structured_content
    try:
        return json.loads(_reply_text(result))
    except json.JSONDecodeError:
        return None


class _McpAdapter:
    def __init__(self, connection: Connection, binding: Binding, contract: Contract) -> None:
        self._connection = connection
        self._binding = binding
        self._contract = contract

    async def _call(self, role: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self._contract.tool_for_role(role)
        name = self._binding.tools[role]

        # Our own arguments are checked too: a malformed request is our bug, and naming it
        # here beats decoding whatever the server makes of it.
        if problems := self._contract.violations("request", spec.name, args):
            raise ValueError(f"Refusing to send invalid {spec.name} request: {problems}")

        try:
            result = await self._connection.session.call_tool(name, args)
        except McpClientError:
            raise
        except Exception as exc:
            # The SDK raises for protocol-level errors (unknown tool, schema rejection).
            message = str(exc)
            raise McpToolError(name, self._contract.classify_error(message), message) from exc

        if not isinstance(result, types.CallToolResult):
            raise McpProtocolError(f"{name} returned {type(result).__name__}, not a tool result.")

        if result.is_error:
            # The server reports failures as prose (no code); classify by the contract's rules.
            message = _reply_text(result) or "(no message)"
            raise McpToolError(name, self._contract.classify_error(message), message)

        payload = _payload(result)
        if not isinstance(payload, dict):
            raise McpProtocolError(f"{name} returned no structured result.")
        if problems := self._contract.violations("response", spec.name, payload):
            raise McpProtocolError(f"{name} reply is missing what we read: {problems}")
        return payload


class McpDocsAdapter(_McpAdapter):
    async def get_info(self, document: str) -> DocInfo:
        reply = await self._call("get_info", {"document": document})
        return DocInfo(reply["document_id"], reply["title"], reply["url"])

    async def append(self, document: str, blocks: list[Block]) -> AppendResult:
        reply = await self._call(
            "append",
            {
                "document": document,
                "blocks": [asdict(block) for block in blocks],
                "add_separator": True,
            },
        )
        return AppendResult(
            document_id=reply["document_id"],
            url=reply["url"],
            blocks_appended=reply["blocks_appended"],
            dry_run=reply["status"] == "dry_run",
        )


class McpGmailAdapter(_McpAdapter):
    async def create_draft(self, to: list[str], subject: str, body: str) -> DraftRef:
        reply = await self._call(
            "create_draft", {"to": to, "subject": subject, "body": body, "body_format": "text"}
        )
        return DraftRef(draft_id=reply.get("draft_id"), dry_run=reply["status"] == "dry_run")


# --------------------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------------------


def server_config(settings: Settings, mode: McpMode) -> McpServerConfig:
    """One server serves both tool groups (X5); stub mode swaps in the in-repo stub."""
    if mode == "stub":
        if settings.mcp.stub is None:
            raise McpClientError("`--mcp stub` needs a [stub] server in config/mcp.toml.")
        return settings.mcp.stub
    return settings.mcp.live


def _load(settings: Settings) -> Contract:
    try:
        return load_contract(settings.mcp.contract_file)
    except ContractError as exc:
        raise McpClientError(str(exc)) from exc


@asynccontextmanager
async def _connect(
    settings: Settings, mode: McpMode, roles: tuple[str, ...]
) -> AsyncIterator[tuple[Connection, Binding, Contract]]:
    contract = _load(settings)
    config = server_config(settings, mode)
    async with open_session(config, label=mode) as connection:
        binding = await discover(
            connection, configured_tools=config.tools, required_roles=roles, contract=contract
        )
        yield connection, binding, contract


@asynccontextmanager
async def connect_docs(settings: Settings, mode: McpMode) -> AsyncIterator[McpDocsAdapter]:
    async with _connect(settings, mode, ROLES["docs"]) as (connection, binding, contract):
        yield McpDocsAdapter(connection, binding, contract)


@asynccontextmanager
async def connect_gmail(settings: Settings, mode: McpMode) -> AsyncIterator[McpGmailAdapter]:
    async with _connect(settings, mode, ROLES["gmail"]) as (connection, binding, contract):
        yield McpGmailAdapter(connection, binding, contract)


# --------------------------------------------------------------------------------------
# Conformance (`pulse contract check`)
# --------------------------------------------------------------------------------------


@dataclass
class ConformanceResult:
    label: str
    status: Literal["ok", "fail", "unavailable"]
    lines: list[str] = field(default_factory=list)


async def check_conformance(
    settings: Settings, config: McpServerConfig, *, label: str
) -> ConformanceResult:
    """Identity, version, tool binding, and request/response compatibility for one server."""
    try:
        contract = _load(settings)
        async with open_session(config, label=label) as connection:
            binding = await discover(
                connection,
                configured_tools=config.tools,
                required_roles=ALL_ROLES,
                contract=contract,
            )
            problems = compatibility_problems(binding, contract, ALL_ROLES)
    except (McpUnavailable, McpTokenMissing) as exc:
        return ConformanceResult(label, "unavailable", [str(exc).splitlines()[0]])
    except McpClientError as exc:
        return ConformanceResult(label, "fail", [str(exc)])

    if problems:
        return ConformanceResult(label, "fail", problems)
    summary = (
        f"{contract.server} {binding.server_version}: {len(ALL_ROLES)} tool(s) accept our "
        f"requests and declare the fields we read"
    )
    return ConformanceResult(label, "ok", [summary, *binding.warnings])


# --------------------------------------------------------------------------------------
# Probing (startup, `doctor`)
# --------------------------------------------------------------------------------------


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    warnings: list[str] = field(default_factory=list)
    error: McpClientError | None = None


async def probe(
    settings: Settings, mode: McpMode, roles: tuple[str, ...], *, document_id: str | None
) -> ProbeResult:
    """Connect, check identity and version, bind `roles`; with a document, prove access.

    `docs_get_info` on the configured doc is read-only and goes through the same Google
    OAuth token as Gmail, so one call proves the server is authorized, the doc exists, and
    this account can see it -- all before any LLM spend.
    """
    try:
        async with _connect(settings, mode, roles) as (connection, binding, contract):
            detail = f"{contract.server} {binding.server_version} at {connection.target}"
            if document_id is not None:
                docs = McpDocsAdapter(connection, binding, contract)
                info = await docs.get_info(document_id)
                detail += f'; doc "{info.title}" is reachable'
            return ProbeResult(True, detail, warnings=binding.warnings)
    except (McpClientError, McpCredentialError) as exc:
        return ProbeResult(False, str(exc), error=exc)
