"""The two ports stages 9 and 10 depend on (architecture.md §11.3).

The pipeline talks to these Protocols, never to MCP, so publishing logic -- idempotency,
retries, dry-run handling -- is testable with an in-memory fake and no server.
`McpDocsAdapter` / `McpGmailAdapter` implement them over a real session.

Shaped by the deployed server (implementation-plan.md §8.1): Docs can only describe and
append to an existing document, and content goes over as typed blocks, not markdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

BlockType = Literal["paragraph", "heading1", "heading2", "heading3", "bullet", "numbered"]


@dataclass(frozen=True)
class Block:
    type: BlockType
    text: str
    bold: bool = False


@dataclass(frozen=True)
class DocInfo:
    document_id: str
    title: str
    url: str


@dataclass(frozen=True)
class AppendResult:
    document_id: str
    url: str
    blocks_appended: int
    #: True when the server is in DRY_RUN mode: nothing was written.
    dry_run: bool


@dataclass(frozen=True)
class DraftRef:
    draft_id: str | None
    #: True when the server is in DRY_RUN mode: no draft exists.
    dry_run: bool


class DocsPort(Protocol):
    async def get_info(self, document: str) -> DocInfo: ...

    async def append(self, document: str, blocks: list[Block]) -> AppendResult: ...


class GmailPort(Protocol):
    async def create_draft(self, to: list[str], subject: str, body: str) -> DraftRef: ...
