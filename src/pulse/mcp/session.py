"""`ClientSession` lifecycle over stdio or Streamable HTTP, with timeouts (P4-T5, P5-T1).

One session per server per stage. For stdio the agent spawns the server as a subprocess;
for HTTP it connects to a deployed endpoint with a bearer token. Everything above this
module is transport-agnostic -- the X2 decision lands here and nowhere else.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import anyio
import httpx2
from mcp import ClientSession, MCPError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from pulse.config import ConfigError, McpServerConfig, resolve_token
from pulse.mcp.errors import (
    McpClientError,
    McpCredentialError,
    McpProtocolError,
    McpTokenMissing,
    McpUnavailable,
)

CONNECT_TIMEOUT_SECONDS = 20.0
CALL_TIMEOUT_SECONDS = 120.0
_STDERR_TAIL_CHARS = 800


@dataclass
class Connection:
    label: str
    #: the command or URL, for messages; never includes the token
    target: str
    session: ClientSession
    server_info: types.Implementation


def _resolve_command(command: str) -> str:
    """`python` in config means the interpreter running pulse, not whatever is on PATH.

    Otherwise a stub configured as `python -m tests.stubs...` would run under a system
    Python that cannot import `pulse`, and fail with a confusing "connection closed".
    """
    return sys.executable if command in {"python", "python3"} else command


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    first = group.exceptions[0]
    return _first_leaf(first) if isinstance(first, BaseExceptionGroup) else first


def _stderr_tail(log: TextIO) -> str:
    try:
        log.seek(0)
        text = log.read().strip()
    except (OSError, ValueError):
        return ""
    return text[-_STDERR_TAIL_CHARS:]


def _transport(
    server: McpServerConfig, errlog: TextIO, rejected: list[int]
) -> AbstractAsyncContextManager[tuple[Any, Any]]:
    if server.transport == "http":
        return _http_transport(server, rejected)
    params = StdioServerParameters(
        command=_resolve_command(server.command),
        args=server.args,
        env=server.env or None,
        cwd=Path.cwd(),
    )
    return stdio_client(params, errlog=errlog)


@asynccontextmanager
async def _http_transport(
    server: McpServerConfig, rejected: list[int]
) -> AsyncIterator[tuple[Any, Any]]:
    try:
        token = resolve_token(server)
    except ConfigError as exc:
        raise McpTokenMissing(str(exc)) from exc
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx2.Timeout(30.0, read=CALL_TIMEOUT_SECONDS)

    # The SDK turns a 401 into a generic "Server returned an error response", losing the
    # status. Recording it here is what lets a wrong token be reported as a wrong token.
    async def note_rejection(response: httpx2.Response) -> None:
        if response.status_code in (401, 403):
            rejected.append(response.status_code)

    async with (
        httpx2.AsyncClient(
            headers=headers, timeout=timeout, event_hooks={"response": [note_rejection]}
        ) as client,
        # The server is stateless: there is no session to DELETE on close.
        streamable_http_client(server.url, http_client=client, terminate_on_close=False) as (
            read,
            write,
        ),
    ):
        yield read, write


@asynccontextmanager
async def open_session(
    server: McpServerConfig,
    *,
    label: str,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    call_timeout: float = CALL_TIMEOUT_SECONDS,
) -> AsyncIterator[Connection]:
    """Connect to `server`, initialize, and yield a live connection.

    The SDK's task groups wrap every error -- including ones raised by the caller's own
    code inside this block -- in nested `ExceptionGroup`s. They are unwrapped here so the
    pipeline sees one clear exception rather than a group of groups.
    """
    target = server.describe()
    initialized = False
    rejected: list[int] = []

    with tempfile.TemporaryFile("w+", encoding="utf-8") as errlog:

        def unavailable(reason: str) -> McpUnavailable:
            tail = _stderr_tail(errlog)
            return McpUnavailable(
                f"The {label} MCP server (`{target}`) {reason}."
                + (f"\nIts stderr ended with:\n{tail}" if tail else "")
                + "\nCheck config/mcp.toml, or use `--mcp stub` / `--dry-run` to proceed "
                "without it."
            )

        try:
            async with (
                _transport(server, errlog, rejected) as (read, write),
                ClientSession(read, write, read_timeout_seconds=call_timeout) as session,
            ):
                with anyio.fail_after(connect_timeout):
                    result = await session.initialize()
                initialized = True
                yield Connection(label, target, session, result.server_info)
        except BaseExceptionGroup as group:
            leaf = _first_leaf(group)
            if not isinstance(leaf, Exception):
                raise
            translated = _translate(
                leaf, initialized, unavailable, label=label, server=server, rejected=rejected
            )
            if translated is leaf:
                raise leaf from None
            raise translated from leaf
        except McpClientError:
            raise
        except TimeoutError as exc:
            if initialized:
                raise McpProtocolError(f"The {label} MCP server timed out mid-call.") from exc
            raise unavailable(f"did not finish `initialize` within {connect_timeout:.0f}s") from exc
        except (OSError, httpx2.HTTPError) as exc:
            if initialized:
                raise McpProtocolError(f"Lost the {label} MCP server mid-call: {exc}") from exc
            raise _translate(
                exc, initialized, unavailable, label=label, server=server, rejected=rejected
            ) from exc


_TRANSPORT_ERRORS = (
    MCPError,
    TimeoutError,
    OSError,
    httpx2.HTTPError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)


def _translate(
    leaf: Exception,
    initialized: bool,
    unavailable: Callable[[str], McpUnavailable],
    *,
    label: str,
    server: McpServerConfig,
    rejected: list[int],
) -> Exception:
    """Name SDK and transport failures; pass everything else (the caller's own) through."""
    if isinstance(leaf, McpClientError):
        return leaf
    if rejected:
        return McpCredentialError(
            f"The {label} MCP server rejected our token (HTTP {rejected[-1]}). Check that "
            f"{server.token_env or 'the token'} matches the server's MCP_AUTH_TOKEN."
        )
    if not initialized:
        if isinstance(leaf, TimeoutError):
            return unavailable("did not finish `initialize` in time")
        if isinstance(leaf, OSError) and server.transport == "stdio":
            return unavailable(f"could not be started ({leaf.strerror or leaf})")
        verb = "could not be reached" if server.transport == "http" else "exited during startup"
        return unavailable(f"{verb} ({type(leaf).__name__}: {leaf})")
    if isinstance(leaf, _TRANSPORT_ERRORS):
        return McpProtocolError(
            f"Lost the {label} MCP server mid-call: {type(leaf).__name__}: {leaf}"
        )
    return leaf
