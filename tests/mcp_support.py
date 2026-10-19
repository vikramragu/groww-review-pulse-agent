"""Shared helpers for the MCP tests: settings aimed at the stub, a publishable run, and the
stub served over HTTP the way the Railway deployment is."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from pulse.config import McpServerConfig, Settings
from pulse.orchestrator import RunContext, start_run
from pulse.rendering.render import render_note
from tests.stubs.stub_mcp_server import STUB_DOCUMENT_ID
from tests.test_render import a_note

WEEK = "2026-W41"
RECIPIENT = "pulse-test@example.com"
TOKEN_ENV = "PULSE_TEST_MCP_TOKEN"


def stub_settings(
    settings: Settings,
    state: Path,
    *,
    faults: str = "",
    recipient: str | None = RECIPIENT,
    document_id: str = STUB_DOCUMENT_ID,
    **extra_env: str,
) -> Settings:
    """`settings` with the stub persisting to `state`, the given faults armed, and the
    stub's built-in document configured as the standing doc."""
    assert settings.mcp.stub is not None
    env = {"PULSE_STUB_STATE": str(state), "PULSE_STUB_FAULTS": faults, **extra_env}
    stub = settings.mcp.stub.model_copy(update={"env": env})
    docs = settings.pulse.docs.model_copy(update={"document_id": document_id})
    return settings.model_copy(
        update={
            "mcp": settings.mcp.model_copy(update={"stub": stub}),
            "pulse": settings.pulse.model_copy(update={"docs": docs}),
            "recipient": recipient,
        }
    )


def live_settings(settings: Settings, url: str, *, document_id: str = STUB_DOCUMENT_ID) -> Settings:
    """`settings` whose live server is the given URL, with its token in TOKEN_ENV."""
    live = McpServerConfig(
        transport="http", url=url, token_env=TOKEN_ENV, tools=dict(settings.mcp.live.tools)
    )
    docs = settings.pulse.docs.model_copy(update={"document_id": document_id})
    return settings.model_copy(
        update={
            "mcp": settings.mcp.model_copy(update={"live": live}),
            "pulse": settings.pulse.model_copy(update={"docs": docs}),
            "recipient": RECIPIENT,
        }
    )


def publishable_run(runs_root: Path) -> RunContext:
    """A run holding a validated note and its `pulse.md`, as stages 3-8 would leave it."""
    context = start_run(WEEK, runs_root)
    note = a_note()
    context.write_artifact(3, [])
    context.write_artifact(6, note)
    context.write_note(render_note(note, corpus_size=120, generated_on=date(2026, 10, 12)))
    return context


def stub_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def appended_sections(path: Path) -> list[list[dict[str, Any]]]:
    sections: list[list[dict[str, Any]]] = stub_state(path)["documents"][STUB_DOCUMENT_ID][
        "sections"
    ]
    return sections


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def http_stub(token: str = "good-token", **env: str) -> Iterator[str]:
    """Serve the stub over stateless HTTP with bearer auth; yields the /mcp URL."""
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.stubs.stub_mcp_server",
            "--http",
            str(port),
            "--token",
            token,
        ],
        env={**_inherited_env(), **env},
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError("the HTTP stub did not start") from None
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


def _inherited_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("PULSE_STUB_")}
