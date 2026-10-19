"""Stage 10: create the Gmail draft (P4-T10). A draft only -- the agent never binds a send tool."""

from __future__ import annotations

import anyio

from pulse.publish.ports import DraftRef, GmailPort
from pulse.publish.retry import Sleep, with_unavailable_retry


async def create_pulse_draft(
    gmail: GmailPort,
    *,
    to: str,
    subject: str,
    body: str,
    sleep: Sleep = anyio.sleep,
) -> DraftRef:
    return await with_unavailable_retry(
        lambda: gmail.create_draft([to], subject, body), what="create_draft", sleep=sleep
    )
