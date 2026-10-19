"""P5-T4..T7: stages 9-10 against google-workspace-mcp's real tool surface.

Three layers:

* `publish_section` / `create_pulse_draft` against in-memory fakes -- where idempotency,
  dry-run and retry rules are pinned precisely.
* The MCP adapter's trust boundary, against a scripted session.
* The wired pipeline against the stub, over stdio and over HTTP.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import MCPError, types

from pulse.analysis.scrub import PiiLeakError
from pulse.config import Settings, load_settings
from pulse.llm import FakeLlm
from pulse.mcp.adapters import McpDocsAdapter
from pulse.mcp.contract import ErrorCode, ServerVersion, load_contract
from pulse.mcp.discovery import Binding
from pulse.mcp.errors import McpProtocolError, McpToolError
from pulse.orchestrator import RunContext
from pulse.pipeline import PipelineError, PipelineOptions, run_stages
from pulse.publish import retry
from pulse.publish.docs import PublishedSection, previous_publication, publish_section
from pulse.publish.gmail import create_pulse_draft
from pulse.publish.ports import AppendResult, Block, DocInfo, DraftRef
from tests.mcp_support import (
    RECIPIENT,
    TOKEN_ENV,
    WEEK,
    appended_sections,
    http_stub,
    live_settings,
    publishable_run,
    stub_settings,
    stub_state,
)

DOC = "doc-123"
BLOCKS = [Block("heading1", f"Groww Review Pulse — {WEEK}"), Block("paragraph", "Body")]


# --------------------------------------------------------------------------------------
# In-memory fakes
# --------------------------------------------------------------------------------------


@dataclass
class FakeDocs:
    dry_run: bool = False
    failures: dict[str, list[McpToolError]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    appended: list[list[Block]] = field(default_factory=list)

    def _maybe_fail(self, method: str) -> None:
        self.calls.append(method)
        if queue := self.failures.get(method):
            raise queue.pop(0)

    async def get_info(self, document: str) -> DocInfo:
        self._maybe_fail("get_info")
        return DocInfo(document, "Pulse", f"https://docs.fake/{document}")

    async def append(self, document: str, blocks: list[Block]) -> AppendResult:
        self._maybe_fail("append")
        if not self.dry_run:
            self.appended.append(blocks)
        return AppendResult(document, f"https://docs.fake/{document}", len(blocks), self.dry_run)


@dataclass
class FakeGmail:
    failures: list[McpToolError] = field(default_factory=list)
    drafts: list[tuple[list[str], str, str]] = field(default_factory=list)

    async def create_draft(self, to: list[str], subject: str, body: str) -> DraftRef:
        if self.failures:
            raise self.failures.pop(0)
        self.drafts.append((to, subject, body))
        return DraftRef(f"draft-{len(self.drafts)}", dry_run=False)


def tool_error(code: ErrorCode) -> McpToolError:
    return McpToolError("tool", code, "injected")


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def publish(
    docs: FakeDocs,
    *,
    previous: str | None = None,
    republish: bool = False,
    sleep: Sleeps | None = None,
) -> PublishedSection:
    async def go() -> PublishedSection:
        return await publish_section(
            docs,
            document_id=DOC,
            iso_week=WEEK,
            heading=BLOCKS[0].text,
            blocks=BLOCKS,
            previously_published_in=previous,
            republish=republish,
            sleep=sleep or Sleeps(),
        )

    return anyio.run(go)


# --------------------------------------------------------------------------------------
# Stage 9 semantics
# --------------------------------------------------------------------------------------


def test_the_week_is_appended_after_confirming_the_doc() -> None:
    docs = FakeDocs()
    published = publish(docs)
    assert published.status == "appended"
    assert docs.calls == ["get_info", "append"]
    assert docs.appended == [BLOCKS]


def test_a_week_already_published_is_skipped() -> None:
    """The server cannot read the doc body, so the agent's record is the only guard."""
    docs = FakeDocs()
    published = publish(docs, previous="2026-W41-01")
    assert published.status == "skipped"
    assert published.previously_published_in == "2026-W41-01"
    assert docs.calls == ["get_info"]
    assert published.url == f"https://docs.fake/{DOC}"


def test_republish_appends_again_deliberately() -> None:
    docs = FakeDocs()
    published = publish(docs, previous="2026-W41-01", republish=True)
    assert published.status == "appended"
    assert published.republished


def test_a_dry_run_server_is_not_recorded_as_published() -> None:
    published = publish(FakeDocs(dry_run=True))
    assert published.status == "dry_run"


def test_an_unavailable_google_is_retried_then_succeeds() -> None:
    docs = FakeDocs(failures={"append": [tool_error(ErrorCode.UNAVAILABLE)] * 2})
    sleeps = Sleeps()
    assert publish(docs, sleep=sleeps).status == "appended"
    assert sleeps.waits == [retry.WAIT_SECONDS, retry.WAIT_SECONDS * 2]


def test_retrying_gives_up_after_three_attempts() -> None:
    docs = FakeDocs(failures={"get_info": [tool_error(ErrorCode.UNAVAILABLE)] * 3})
    with pytest.raises(McpToolError) as caught:
        publish(docs)
    assert caught.value.code is ErrorCode.UNAVAILABLE
    assert docs.calls.count("get_info") == 3


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.NOT_FOUND,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.UPSTREAM_ERROR,
    ],
)
def test_other_errors_are_not_retried(code: ErrorCode) -> None:
    docs = FakeDocs(failures={"get_info": [tool_error(code)]})
    with pytest.raises(McpToolError):
        publish(docs)
    assert docs.calls == ["get_info"]


# --------------------------------------------------------------------------------------
# The publish record
# --------------------------------------------------------------------------------------


def record(runs: Path, run: str, **fields: object) -> None:
    (runs / run).mkdir(parents=True)
    (runs / run / "09_published.json").write_text(json.dumps(fields), encoding="utf-8")


def test_the_record_finds_an_appended_week(tmp_path: Path) -> None:
    record(tmp_path, "2026-W41-01", document_id=DOC, iso_week=WEEK, status="appended")
    assert previous_publication(tmp_path, DOC, WEEK) == "2026-W41-01"


@pytest.mark.parametrize(
    "fields",
    [
        {"document_id": DOC, "iso_week": WEEK, "status": "skipped"},
        {"document_id": DOC, "iso_week": WEEK, "status": "dry_run"},
        {"document_id": "other-doc", "iso_week": WEEK, "status": "appended"},
        {"document_id": DOC, "iso_week": "2026-W40", "status": "appended"},
    ],
)
def test_the_record_ignores_what_was_not_appended_here(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    record(tmp_path, "2026-W41-01", **fields)
    assert previous_publication(tmp_path, DOC, WEEK) is None


def test_a_corrupt_record_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "2026-W41-01").mkdir()
    (tmp_path / "2026-W41-01" / "09_published.json").write_text("{nope", encoding="utf-8")
    assert previous_publication(tmp_path, DOC, WEEK) is None


# --------------------------------------------------------------------------------------
# Stage 10 semantics
# --------------------------------------------------------------------------------------


def test_the_draft_goes_to_one_recipient_with_retry() -> None:
    gmail = FakeGmail(failures=[tool_error(ErrorCode.UNAVAILABLE)])

    async def go() -> DraftRef:
        return await create_pulse_draft(gmail, to=RECIPIENT, subject="s", body="b", sleep=Sleeps())

    assert anyio.run(go).draft_id == "draft-1"
    assert gmail.drafts == [([RECIPIENT], "s", "b")]


# --------------------------------------------------------------------------------------
# The adapter's trust boundary (architecture.md §18)
# --------------------------------------------------------------------------------------


class ScriptedSession:
    def __init__(self, result: types.CallToolResult | Exception) -> None:
        self.result = result

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def adapter_returning(result: types.CallToolResult | Exception) -> McpDocsAdapter:
    contract = load_contract()
    binding = Binding(
        label="live",
        server_version=ServerVersion(0, 1),
        tools={t.role: t.name for t in contract.tools.values()},
        advertised={},
    )
    connection = SimpleNamespace(session=ScriptedSession(result), target="fake")
    return McpDocsAdapter(connection, binding, contract)  # type: ignore[arg-type]


def reply(payload: Any, *, is_error: bool = False, structured: bool = True) -> types.CallToolResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=payload if structured and isinstance(payload, dict) else None,
        is_error=is_error,
    )


def test_a_prose_error_becomes_a_classified_tool_error() -> None:
    adapter = adapter_returning(
        reply("Document not found or not shared with this account.", is_error=True)
    )
    with pytest.raises(McpToolError) as caught:
        anyio.run(lambda: adapter.get_info(DOC))
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert caught.value.server_message.startswith("Document not found")


def test_an_sdk_level_error_is_classified_too() -> None:
    adapter = adapter_returning(MCPError(-32602, "Tool docs_get_info not found"))
    with pytest.raises(McpToolError) as caught:
        anyio.run(lambda: adapter.get_info(DOC))
    assert caught.value.code is ErrorCode.FEATURE_DISABLED


def test_a_reply_missing_a_field_we_read_is_rejected() -> None:
    adapter = adapter_returning(reply({"status": "appended", "document_id": DOC}))
    with pytest.raises(McpProtocolError, match="missing what we read"):
        anyio.run(lambda: adapter.append(DOC, BLOCKS))


def test_we_refuse_to_send_an_invalid_request() -> None:
    adapter = adapter_returning(reply({}))
    with pytest.raises(ValueError, match="Refusing to send"):
        anyio.run(lambda: adapter.append(DOC, []))


def test_json_text_without_structured_content_is_accepted() -> None:
    payload = {"document_id": DOC, "title": "T", "url": "u", "tabs": []}
    adapter = adapter_returning(reply(payload, structured=False))
    assert anyio.run(lambda: adapter.get_info(DOC)).title == "T"


# --------------------------------------------------------------------------------------
# The wired pipeline against the stub (what `pulse run --mcp stub` does)
# --------------------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return load_settings(Path("config"))


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "WAIT_SECONDS", 0.0)


def run_publish(
    context: RunContext,
    settings: Settings,
    *,
    first: int = 9,
    last: int = 10,
    mode: str = "stub",
    llm: FakeLlm | None = None,
    republish: bool = False,
) -> None:
    run_stages(
        context,
        PipelineOptions(
            settings=settings,
            input_path=Path("tests/fixtures/reviews_sample.json"),
            target_week=WEEK,
            from_stage=first,
            to_stage=last,
            mcp_mode=mode,  # type: ignore[arg-type]
            llm=llm,
            republish=republish,
        ),
    )


def test_stages_nine_and_ten_append_and_draft(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    context = publishable_run(tmp_path / "runs")

    run_publish(context, stub_settings(settings, state))

    (section,) = appended_sections(state)
    assert section[0] == {"type": "heading1", "text": f"Groww Review Pulse — {WEEK}", "bold": False}
    assert any(b["text"] == "Withdrawal delays dominate this week" and b["bold"] for b in section)
    assert context.ledger.document_url == "https://docs.google.com/document/d/stub-doc/edit"

    stored = stub_state(state)
    (draft,) = stored["drafts"]
    assert draft["to"] == [RECIPIENT]
    assert draft["body_format"] == "text"
    assert context.ledger.document_url in draft["body"]
    assert stored["sent"] == [], "the agent must never call gmail_send_email"
    assert context.read_artifact(9)["status"] == "appended"
    assert context.read_artifact(10)["status"] == "created"


def test_quotes_reach_the_doc_verbatim(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    context = publishable_run(tmp_path / "runs")
    run_publish(context, stub_settings(settings, state), last=9)

    texts = [b["text"] for b in appended_sections(state)[0]]
    note = context.read_artifact(6)
    for quote in note["quotes"]:
        assert any(quote["text"] in text for text in texts)


def test_rerunning_a_published_week_adds_nothing(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state)
    run_publish(publishable_run(tmp_path / "runs"), configured)

    second = publishable_run(tmp_path / "runs")
    run_publish(second, configured)

    assert len(appended_sections(state)) == 1
    assert len(stub_state(state)["drafts"]) == 1
    assert second.read_artifact(9)["status"] == "skipped"
    assert second.read_artifact(10)["status"] == "skipped"
    assert second.ledger.document_url  # still points the reader at the doc


def test_replaying_into_the_same_run_never_erases_the_record(
    settings: Settings, tmp_path: Path
) -> None:
    """Found for real: a second `--from 9` into the run that published overwrote its
    "appended" record with "skipped", so a third replay appended the week again."""
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state)
    context = publishable_run(tmp_path / "runs")

    for _ in range(3):
        run_publish(context, configured)

    assert len(appended_sections(state)) == 1
    assert len(stub_state(state)["drafts"]) == 1
    assert context.read_artifact(9)["status"] == "appended"
    assert context.read_artifact(10)["status"] == "created"


def test_moving_to_another_doc_gets_a_draft_that_links_to_it(
    settings: Settings, tmp_path: Path
) -> None:
    """Found on the first live run: after publishing to a scratch doc, publishing the same
    week to the standing doc skipped the draft, leaving only one that linked to scratch."""
    state = tmp_path / "stub.json"
    context = publishable_run(tmp_path / "runs")
    run_publish(context, stub_settings(settings, state))

    stored = stub_state(state)
    stored["documents"]["real-doc"] = {"title": "Groww Review Pulse", "sections": []}
    state.write_text(json.dumps(stored), encoding="utf-8")
    real = stub_settings(settings, state, document_id="real-doc")
    run_publish(context, real)

    drafts = stub_state(state)["drafts"]
    assert len(drafts) == 2
    assert "/d/real-doc/" in drafts[1]["body"]
    assert context.read_artifact(10)["status"] == "created"

    # And the standing doc's draft is then not repeated.
    run_publish(context, real)
    assert len(stub_state(state)["drafts"]) == 2


def test_republish_appends_and_drafts_again(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state)
    run_publish(publishable_run(tmp_path / "runs"), configured)
    run_publish(publishable_run(tmp_path / "runs"), configured, republish=True)

    assert len(appended_sections(state)) == 2
    assert len(stub_state(state)["drafts"]) == 2


def test_an_unauthorized_server_aborts_before_any_llm_spend(
    settings: Settings, tmp_path: Path
) -> None:
    """Probe ordering is a cost control (architecture.md §11.6): stage 4 never starts."""
    llm = FakeLlm(responses=[])
    context = publishable_run(tmp_path / "runs")
    configured = stub_settings(settings, tmp_path / "s.json", faults="docs_get_info=AUTH")

    with pytest.raises(PipelineError, match="npm run auth"):
        run_publish(context, configured, first=4, llm=llm)

    assert llm.calls == []
    assert context.ledger.stages == []


def test_a_doc_the_server_cannot_see_aborts_before_anything(
    settings: Settings, tmp_path: Path
) -> None:
    context = publishable_run(tmp_path / "runs")
    configured = stub_settings(settings, tmp_path / "s.json", document_id="not-shared-doc")
    with pytest.raises(PipelineError, match="PULSE_DOC_ID"):
        run_publish(context, configured)
    assert context.ledger.stages == []


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [({"document_id": ""}, "PULSE_DOC_ID"), ({"recipient": None}, "PULSE_RECIPIENT")],
)
def test_missing_configuration_aborts_before_anything(
    settings: Settings, tmp_path: Path, overrides: dict[str, Any], variable: str
) -> None:
    context = publishable_run(tmp_path / "runs")
    with pytest.raises(PipelineError, match=variable):
        run_publish(context, stub_settings(settings, tmp_path / "s.json", **overrides))
    assert context.ledger.stages == []


def test_doc_published_but_draft_failed_is_resumable_from_stage_ten(
    settings: Settings, tmp_path: Path
) -> None:
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state, faults="gmail_create_draft=BOOM:1")
    context = publishable_run(tmp_path / "runs")

    with pytest.raises(PipelineError, match="UPSTREAM_ERROR"):
        run_publish(context, configured)
    assert [(s.stage, s.status) for s in context.ledger.stages] == [(9, "ok"), (10, "failed")]

    run_publish(context, configured, first=10)

    assert context.ledger.draft_id
    assert len(appended_sections(state)) == 1
    assert len(stub_state(state)["drafts"]) == 1


def test_a_transient_google_outage_is_survived(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state, faults="docs_append_content=UNAVAILABLE:2")
    run_publish(publishable_run(tmp_path / "runs"), configured, last=9)
    assert len(appended_sections(state)) == 1


def test_a_dry_run_server_is_reported_and_not_recorded(settings: Settings, tmp_path: Path) -> None:
    state = tmp_path / "stub.json"
    configured = stub_settings(settings, state, PULSE_STUB_DRY_RUN="true")
    context = publishable_run(tmp_path / "runs")
    run_publish(context, configured)

    assert context.read_artifact(9)["status"] == "dry_run"
    assert context.read_artifact(10)["status"] == "dry_run"
    assert context.ledger.draft_id is None
    # Nothing was really published, so the next run is not told to skip.
    assert previous_publication(tmp_path / "runs", "stub-doc", WEEK) is None


def test_pii_in_pulse_md_is_never_published(settings: Settings, tmp_path: Path) -> None:
    """pulse.md is a file on disk; stage 9 re-scans rather than trusting stage 8."""
    state = tmp_path / "stub.json"
    context = publishable_run(tmp_path / "runs")
    context.note_path.write_text("# Pulse\n\nMail notareal@example.com\n", encoding="utf-8")

    with pytest.raises(PiiLeakError):
        run_publish(context, stub_settings(settings, state), last=9)
    assert not stub_state(state) or appended_sections(state) == []


def test_publishing_over_http_like_the_deployed_server(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "good-token")
    context = publishable_run(tmp_path / "runs")
    with http_stub() as url:
        run_publish(context, live_settings(settings, url), mode="live")

    assert context.read_artifact(9)["status"] == "appended"
    assert context.read_artifact(10)["status"] == "created"
