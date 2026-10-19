"""Stage 9: append the week's section to the standing Google Doc (P4-T9, P5-T6).

The server can describe and append to an existing doc, but not find, create, or read its
body (implementation-plan.md §8.1). So:

* the doc is configured, and `get_info` proves it exists and is reachable;
* each week is appended as a section headed `Groww Review Pulse — <week>`;
* idempotency is the agent's own record: a week already appended to this doc, according
  to any run's `09_published.json`, is skipped unless `--republish` is given.

That record is local state. A clean checkout forgets it, and a re-run would then append a
duplicate section -- a known, recorded limitation (plan §11), not an oversight.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import anyio

from pulse.publish.ports import Block, DocsPort
from pulse.publish.retry import Sleep, with_unavailable_retry

PUBLISHED_ARTIFACT = "09_published.json"


@dataclass(frozen=True)
class PublishedSection:
    document_id: str
    url: str
    iso_week: str
    heading: str
    status: Literal["appended", "skipped", "dry_run"]
    blocks_appended: int
    #: run that already published this week, when skipped
    previously_published_in: str | None = None
    republished: bool = False

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def previous_publication(runs_root: Path, document_id: str, iso_week: str) -> str | None:
    """The run that already appended `iso_week` to `document_id`, if any."""
    for path in sorted(runs_root.glob(f"*/{PUBLISHED_ARTIFACT}")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            record.get("document_id") == document_id
            and record.get("iso_week") == iso_week
            and record.get("status") == "appended"
        ):
            return path.parent.name
    return None


async def publish_section(
    docs: DocsPort,
    *,
    document_id: str,
    iso_week: str,
    heading: str,
    blocks: list[Block],
    previously_published_in: str | None,
    republish: bool = False,
    sleep: Sleep = anyio.sleep,
) -> PublishedSection:
    info = await with_unavailable_retry(
        lambda: docs.get_info(document_id), what="get_info", sleep=sleep
    )

    if previously_published_in and not republish:
        return PublishedSection(
            document_id=info.document_id,
            url=info.url,
            iso_week=iso_week,
            heading=heading,
            status="skipped",
            blocks_appended=0,
            previously_published_in=previously_published_in,
        )

    result = await with_unavailable_retry(
        lambda: docs.append(document_id, blocks), what="append", sleep=sleep
    )
    return PublishedSection(
        document_id=result.document_id,
        url=result.url,
        iso_week=iso_week,
        heading=heading,
        status="dry_run" if result.dry_run else "appended",
        blocks_appended=result.blocks_appended,
        previously_published_in=previously_published_in,
        republished=bool(previously_published_in),
    )
