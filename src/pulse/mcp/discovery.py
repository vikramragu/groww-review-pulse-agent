"""Startup discovery: server identity, version, tool binding, compatibility (P4-T6, P5-T2).

Runs before any LLM spend, so a missing, outdated, or misconfigured server is reported
at the start of a run rather than as a confusing failure in stage 9.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from mcp import types

from pulse import logging as pulse_logging
from pulse.mcp.contract import Contract, ContractError, ServerVersion, request_schema_problems
from pulse.mcp.errors import ContractMismatch, MissingTool
from pulse.mcp.session import Connection

log = pulse_logging.get_logger("mcp.discovery")


@dataclass(frozen=True)
class Binding:
    """What discovery established about one server."""

    label: str
    server_version: ServerVersion
    #: role (e.g. "append") -> the tool name this server actually advertises
    tools: dict[str, str]
    advertised: dict[str, types.Tool]
    warnings: list[str] = field(default_factory=list)


def check_server(name: str, version: str, contract: Contract, *, label: str) -> str | None:
    """Return a warning for a tolerable version difference; raise on anything worse."""
    if name != contract.server:
        raise ContractMismatch(
            f"The {label} server is {name!r}, but this agent was built against "
            f"{contract.server!r} (contract file). Point config/mcp.toml at the right server."
        )
    try:
        actual = ServerVersion.parse(version)
    except ContractError:
        raise ContractMismatch(
            f"The {label} server reports version {version!r}, which is not semver."
        ) from None

    expected = contract.version
    if actual.major != expected.major or (expected.major == 0 and actual.minor != expected.minor):
        raise ContractMismatch(
            f"The {label} server is {name} {actual}, but this agent was built against "
            f"{expected}. That difference may break the tools it uses; re-run "
            f"`pulse contract check` and update the contract file deliberately."
        )
    if actual.minor != expected.minor:
        return f"The {label} server is {name} {actual}; the contract is for {expected}. Continuing."
    return None


def resolve_tools(
    advertised: Iterable[types.Tool],
    configured: dict[str, str],
    required_roles: Iterable[str],
    contract: Contract,
    *,
    label: str,
) -> dict[str, str]:
    by_name = {tool.name: tool for tool in advertised}
    resolved: dict[str, str] = {}
    problems: list[str] = []

    for role in required_roles:
        name = configured.get(role)
        if name is None:
            problems.append(f"config/mcp.toml has no `{role}` entry under the server's tools")
        elif contract.forbids(name):
            # Defence in depth (implementation-plan.md D-1): even if config is edited to
            # point a role at a send tool, the agent will not bind it.
            raise MissingTool(
                f"Refusing to bind `{name}` for role `{role}`: this agent never binds a tool "
                f"matching {contract.never_bind}. The pulse is a draft, never sent."
            )
        elif name not in by_name:
            problems.append(f"`{name}` (role `{role}`) is not advertised")
        else:
            resolved[role] = name

    if problems:
        listing = ", ".join(sorted(by_name)) or "(none)"
        raise MissingTool(
            f"The {label} server cannot be used: {'; '.join(problems)}. It advertises: {listing}."
        )
    return resolved


def compatibility_problems(binding: Binding, contract: Contract, roles: Iterable[str]) -> list[str]:
    """Where the server's advertised schemas would reject what we send or omit what we read.

    Consumer-driven rather than equality: the server owns its schemas and may add optional
    fields freely. What matters is that our example request validates against its input
    schema, and that every response field we read is declared in its output schema.
    """
    problems: list[str] = []
    for role in roles:
        expected = contract.tool_for_role(role)
        tool = binding.advertised[binding.tools[role]]

        if rejected := request_schema_problems(dict(tool.input_schema), expected.example):
            problems.append(f"{tool.name} rejects our request: {'; '.join(rejected[:3])}")

        declared = set((tool.output_schema or {}).get("properties", {}))
        if tool.output_schema is None:
            problems.append(f"{tool.name} declares no outputSchema")
        elif missing := sorted(set(expected.response.get("required", [])) - declared):
            problems.append(
                f"{tool.name} no longer declares field(s) we read: {', '.join(missing)}"
            )
    return problems


async def discover(
    connection: Connection,
    *,
    configured_tools: dict[str, str],
    required_roles: Iterable[str],
    contract: Contract,
) -> Binding:
    roles = list(required_roles)
    warning = check_server(
        connection.server_info.name,
        connection.server_info.version,
        contract,
        label=connection.label,
    )
    warnings = [warning] if warning else []

    advertised = await list_all_tools(connection)
    tools = resolve_tools(advertised, configured_tools, roles, contract, label=connection.label)

    if exposed := sorted(t.name for t in advertised if contract.forbids(t.name)):
        warnings.append(
            f"The {connection.label} server also exposes {', '.join(exposed)}; this agent will "
            f"never call it (implementation-plan.md D-1)."
        )
    for message in warnings:
        log.warning("mcp discovery warning", extra={"detail": message})

    return Binding(
        label=connection.label,
        server_version=ServerVersion.parse(connection.server_info.version),
        tools=tools,
        advertised={tool.name: tool for tool in advertised},
        warnings=warnings,
    )


async def list_all_tools(connection: Connection) -> list[types.Tool]:
    """Every advertised tool, following pagination."""
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await connection.session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
