"""Structured JSON logging with `run_id` on every record (architecture.md §16).

The run id is held in a `ContextVar` rather than passed through every call site, so a
stage deep in the pipeline cannot accidentally emit an unattributable log line.

Note on the module name: this shadows the stdlib `logging` *within the package namespace*
only. Python 3 uses absolute imports, so `import logging` below resolves to the stdlib as
normal. The filename follows the plan (P0-T7).
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

_run_id: ContextVar[str | None] = ContextVar("pulse_run_id", default=None)

# Attributes present on every LogRecord; anything else a caller attaches via `extra=`
# is treated as structured context and included in the JSON output.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def set_run_id(run_id: str | None) -> None:
    """Bind a run id to the current context. Every later log record carries it."""
    _run_id.set(run_id)


def get_run_id() -> str | None:
    return _run_id.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, run_id, message, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if (run_id := _run_id.get()) is not None:
            payload["run_id"] = run_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, sort_keys=True)


class HumanFormatter(logging.Formatter):
    """Terse console output for interactive use."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        suffix = "  " + " ".join(f"{k}={v}" for k, v in sorted(extras.items())) if extras else ""
        return f"{record.levelname:<7} {record.getMessage()}{suffix}"


def configure(*, json_output: bool = False, level: int = logging.INFO) -> None:
    """Install a single stderr handler. Idempotent — safe to call more than once.

    Logs go to stderr so stdout stays clean for machine-readable command output.
    """
    root = logging.getLogger("pulse")
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `pulse` namespace."""
    return logging.getLogger(name if name.startswith("pulse") else f"pulse.{name}")
