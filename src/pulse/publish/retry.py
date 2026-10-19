"""Retrying a transiently unavailable Google API for stages 9 and 10 (P4-T11, P5-T4).

The server already retries 429/5xx itself (3 times, sub-second backoff) before reporting
"Google API temporarily unavailable". The agent adds a slower outer retry on top, so a
blip lasting a few seconds does not abort a weekly run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio

from pulse import logging as pulse_logging
from pulse.mcp.contract import ErrorCode
from pulse.mcp.errors import McpToolError

T = TypeVar("T")

MAX_ATTEMPTS = 3
WAIT_SECONDS = 5.0

Sleep = Callable[[float], Awaitable[None]]

log = pulse_logging.get_logger("publish.retry")


async def with_unavailable_retry(
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    sleep: Sleep = anyio.sleep,
    attempts: int = MAX_ATTEMPTS,
) -> T:
    """Run `call`, retrying only on UNAVAILABLE.

    Every other code is raised at once: retrying AUTH_REQUIRED, PERMISSION_DENIED or
    INVALID_INPUT cannot succeed, and a retry would only delay a clear failure.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except McpToolError as exc:
            if exc.code is not ErrorCode.UNAVAILABLE or attempt == attempts:
                raise
            wait = WAIT_SECONDS * attempt
            log.warning(
                "google unavailable; retrying",
                extra={"what": what, "attempt": attempt, "wait_seconds": wait},
            )
            await sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
