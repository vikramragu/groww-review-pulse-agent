"""In-repo stand-in for the deployed `google-workspace-mcp` server (P4-T3, P5-T3).

Mirrors the real server as read from its source (vikramragu/Mail-Docs-MCP-Server at
75adcf6): the same five tools including `gmail_send_email`, draft-07 input schemas, prose
error messages copied verbatim from its src/errors.ts, and its DRY_RUN behaviour. Tests
and `pulse run --mcp stub` exercise the interface that actually exists.

    python -m tests.stubs.stub_mcp_server                       # stdio
    python -m tests.stubs.stub_mcp_server --http 8765 --token T # stateless HTTP, bearer auth

Environment (all optional):

* ``PULSE_STUB_STATE`` -- JSON file persisting documents, drafts, sends and faults, so
  separate processes (one per pipeline stage) share state.
* ``PULSE_STUB_FAULTS`` -- comma-separated ``tool=KIND[:count]``; ``tool`` may be ``*``.
  KIND is one of the keys of ``FAULT_MESSAGES``. Without ``:count`` it fires every call.
* ``PULSE_STUB_DRY_RUN`` -- ``true`` to behave like the server's DRY_RUN mode.
* ``PULSE_STUB_NAME`` / ``PULSE_STUB_VERSION`` -- the serverInfo to advertise.
* ``PULSE_STUB_HIDE`` -- comma-separated tool names to leave out of ``tools/list``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft7Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

STUB_DOCUMENT_ID = "stub-doc"
#: Bumped whenever the state file's shape changes; an older file is ignored, not fatal.
STATE_FORMAT = 2
STUB_DOCUMENT_TITLE = "Groww Review Pulse (stub)"

#: Verbatim from the real server's src/errors.ts.
FAULT_MESSAGES: dict[str, str] = {
    "AUTH": "Google authorization required. Run `npm run auth`.",
    "SCOPE": "Missing permission scope: gmail.compose. Re-authorize.",
    "NOT_FOUND": "Document not found or not shared with this account.",
    "NO_ACCESS": "No edit access to this document.",
    "UNAVAILABLE": "Google API temporarily unavailable.",
    "ALLOWLIST": "Document is not on the configured allowlist.",
    "DISABLED": "Feature disabled by server configuration: docs.",
    "BOOM": "Unexpected error: something nobody planned for",
}

_EMAIL = {"type": "string"}
_BLOCK_TYPES = ["paragraph", "heading1", "heading2", "heading3", "bullet", "numbered"]
_DRAFT7 = "http://json-schema.org/draft-07/schema#"

#: Input schemas as the TypeScript server publishes them (zod -> JSON Schema, draft-07).
SCHEMAS: dict[str, dict[str, Any]] = {
    "ping": {"type": "object", "properties": {}, "additionalProperties": False},
    "docs_get_info": {
        "type": "object",
        "properties": {"document": {"type": "string", "minLength": 1}},
        "required": ["document"],
        "additionalProperties": False,
    },
    "docs_append_content": {
        "type": "object",
        "properties": {
            "document": {"type": "string", "minLength": 1},
            "blocks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": _BLOCK_TYPES},
                        "text": {"type": "string", "minLength": 1, "maxLength": 10000},
                        "bold": {"type": "boolean", "default": False},
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False,
                },
            },
            "tab_id": {"type": "string"},
            "add_separator": {"type": "boolean", "default": True},
        },
        "required": ["document", "blocks"],
        "additionalProperties": False,
    },
    "gmail_create_draft": {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": _EMAIL, "minItems": 1, "maxItems": 50},
            "subject": {"type": "string", "minLength": 1, "maxLength": 998},
            "body": {"type": "string", "minLength": 1, "maxLength": 200000},
            "body_format": {"type": "string", "enum": ["text", "html"], "default": "text"},
            "cc": {"type": "array", "items": _EMAIL, "maxItems": 50},
            "bcc": {"type": "array", "items": _EMAIL, "maxItems": 50},
            "reply_to": _EMAIL,
            "thread_id": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
    },
}
SCHEMAS["gmail_send_email"] = SCHEMAS["gmail_create_draft"]

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "ping": {"type": "object", "properties": {"message": {"type": "string"}}},
    "docs_get_info": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "title": {"type": "string"},
            "url": {"type": "string"},
            "tabs": {"type": "array"},
        },
        "required": ["document_id", "title", "url", "tabs"],
    },
    "docs_append_content": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["appended", "dry_run"]},
            "document_id": {"type": "string"},
            "tab_id": {"type": "string"},
            "blocks_appended": {"type": "integer"},
            "characters_inserted": {"type": "integer"},
            "url": {"type": "string"},
        },
        "required": ["status", "document_id", "blocks_appended", "characters_inserted", "url"],
    },
    "gmail_create_draft": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["draft_created", "dry_run"]},
            "draft_id": {"type": "string"},
            "message_id": {"type": "string"},
            "to": {"type": "array", "items": {"type": "string"}},
            "preview": {"type": "string"},
        },
        "required": ["status", "to"],
    },
    "gmail_send_email": {
        "type": "object",
        "properties": {"status": {"type": "string"}, "message_id": {"type": "string"}},
        "required": ["status"],
    },
}

_EMAIL_RE = re.compile(r"^[^\s@<>()\[\]\\,;:\"]+@[^\s@]+\.[^\s@]+$")
_DOC_URL_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
_DOC_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class StubError(Exception):
    """A failure reported, like the real server, as prose only."""


@dataclass
class Fault:
    tool: str
    kind: str
    remaining: int | None

    @classmethod
    def parse(cls, spec: str) -> Fault:
        tool, _, rest = spec.strip().partition("=")
        kind, _, count = rest.partition(":")
        if kind.strip() not in FAULT_MESSAGES:
            raise ValueError(f"Unknown stub fault {kind!r}; use one of {sorted(FAULT_MESSAGES)}")
        return cls(tool.strip(), kind.strip(), int(count) if count else None)


@dataclass
class StubState:
    documents: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {STUB_DOCUMENT_ID: {"title": STUB_DOCUMENT_TITLE, "sections": []}}
    )
    drafts: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    next_id: int = 1
    path: Path | None = None

    @classmethod
    def from_environment(cls) -> StubState:
        raw_path = os.environ.get("PULSE_STUB_STATE")
        path = Path(raw_path) if raw_path else None
        env_faults = [
            Fault.parse(s) for s in os.environ.get("PULSE_STUB_FAULTS", "").split(",") if s.strip()
        ]
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None
        if data is not None and data.get("format") == STATE_FORMAT:
            state = cls(
                documents=data["documents"],
                drafts=data["drafts"],
                sent=data["sent"],
                faults=[Fault(**f) for f in data["faults"]],
                next_id=data["next_id"],
                path=path,
            )
            # Faults are armed once, by the first process; later ones share the counts.
            if not data.get("faults_seeded"):
                state.faults = env_faults
            return state
        return cls(faults=env_faults, path=path)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "format": STATE_FORMAT,
                    "documents": self.documents,
                    "drafts": self.drafts,
                    "sent": self.sent,
                    "faults": [f.__dict__ for f in self.faults],
                    "faults_seeded": True,
                    "next_id": self.next_id,
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def take_fault(self, tool: str) -> Fault | None:
        for fault in self.faults:
            if fault.tool not in (tool, "*"):
                continue
            if fault.remaining is None:
                return fault
            if fault.remaining > 0:
                fault.remaining -= 1
                self.save()
                return fault
        return None

    def new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self.next_id:04d}"
        self.next_id += 1
        return value


def _document_id(reference: str) -> str:
    trimmed = reference.strip()
    if match := _DOC_URL_RE.search(trimmed):
        return match[1]
    if _DOC_ID_RE.match(trimmed):
        return trimmed
    raise StubError(
        f'Unexpected error: Could not extract a document ID from "{reference}". '
        "Provide a Google Docs URL or raw document ID."
    )


def _doc_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _require_doc(state: StubState, document_id: str) -> dict[str, Any]:
    doc = state.documents.get(document_id)
    if doc is None:
        raise StubError(FAULT_MESSAGES["NOT_FOUND"])
    return doc


def handle(state: StubState, tool: str, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    """Tool semantics, separated from transport so they read like the real server."""
    if tool == "ping":
        return {"message": "pong"}

    if tool == "docs_get_info":
        document_id = _document_id(args["document"])
        doc = _require_doc(state, document_id)
        return {
            "document_id": document_id,
            "title": doc["title"],
            "url": _doc_url(document_id),
            "tabs": [{"tab_id": "t.0", "title": "Tab 1"}],
        }

    if tool == "docs_append_content":
        document_id = _document_id(args["document"])
        doc = _require_doc(state, document_id)
        blocks = [{"bold": False, **block} for block in args["blocks"]]
        characters = sum(len(block["text"]) + 1 for block in blocks)
        reply = {
            "status": "dry_run" if dry_run else "appended",
            "document_id": document_id,
            "tab_id": "t.0",
            "blocks_appended": len(blocks),
            "characters_inserted": characters,
            "url": _doc_url(document_id),
        }
        if not dry_run:
            doc["sections"].append(blocks)
            state.save()
        return reply

    if tool in ("gmail_create_draft", "gmail_send_email"):
        for address in args["to"]:
            if not _EMAIL_RE.match(address):
                raise StubError(f"Input validation error: Invalid email address: {address}")
        if "\n" in args["subject"] or "\r" in args["subject"]:
            raise StubError("Invalid subject: contains CR/LF")
        if dry_run:
            return {"status": "dry_run", "to": args["to"], "preview": f"Subject: {args['subject']}"}
        if tool == "gmail_send_email":
            message_id = state.new_id("msg")
            state.sent.append({"message_id": message_id, **args})
            state.save()
            return {"status": "sent", "message_id": message_id}
        draft_id = state.new_id("draft")
        state.drafts.append({"draft_id": draft_id, **args})
        state.save()
        return {
            "status": "draft_created",
            "draft_id": draft_id,
            "message_id": draft_id,
            "to": args["to"],
        }

    raise StubError(f"Tool {tool} not found")


def _result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
        structured_content=payload,
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def build_server(state: StubState) -> Server[Any]:
    name = os.environ.get("PULSE_STUB_NAME") or "google-workspace-mcp"
    version = os.environ.get("PULSE_STUB_VERSION") or "0.1.0"
    dry_run = os.environ.get("PULSE_STUB_DRY_RUN", "").lower() == "true"
    hidden = {t.strip() for t in os.environ.get("PULSE_STUB_HIDE", "").split(",") if t.strip()}
    tools = [t for t in SCHEMAS if t not in hidden]

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool,
                    description=f"Stub of {tool}.",
                    input_schema={"$schema": _DRAFT7, **SCHEMAS[tool]},
                    output_schema=OUTPUT_SCHEMAS[tool],
                )
                for tool in tools
            ]
        )

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = params.name
        args = dict(params.arguments or {})
        try:
            if tool not in tools:
                raise StubError(f"Tool {tool} not found")
            problems = [e.message for e in Draft7Validator(SCHEMAS[tool]).iter_errors(args)]
            if problems:
                raise StubError(f"Input validation error: {'; '.join(problems)}")
            if fault := state.take_fault(tool):
                raise StubError(FAULT_MESSAGES[fault.kind])
            return _result(handle(state, tool, args, dry_run=dry_run))
        except StubError as exc:
            return _error(str(exc))

    return Server(name, version=version, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# --------------------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------------------


async def serve_stdio() -> None:
    server = build_server(StubState.from_environment())
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def http_app(token: str) -> Any:
    """Stateless Streamable HTTP at /mcp behind a bearer token, like the Railway deploy."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(
        app=build_server(StubState.from_environment()), stateless=True, json_response=True
    )

    class McpEndpoint:
        """A raw ASGI endpoint, so `/mcp` is served directly rather than redirected."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                response = JSONResponse(
                    {"error": "Missing or invalid bearer token"}, status_code=401
                )
                await response(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

    async def healthz(_request: Any) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/healthz", healthz), Route("/mcp", McpEndpoint(), methods=["POST"])],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", type=int, metavar="PORT", help="serve HTTP instead of stdio")
    parser.add_argument("--token", default="stub-token", help="bearer token for --http")
    args = parser.parse_args()
    if args.http is None:
        anyio.run(serve_stdio)
        return
    import uvicorn

    uvicorn.run(http_app(args.token), host="127.0.0.1", port=args.http, log_level="warning")


if __name__ == "__main__":
    main()
