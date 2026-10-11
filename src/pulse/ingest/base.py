"""Review source protocol and the adapter registry (P1-T3).

The registry is **explicit**: adapters are listed in `_REGISTRY` by hand, with no entry-point
scanning or module auto-discovery. That is the mechanism behind the "public exports only"
constraint in architecture.md §3 — a network-fetching adapter cannot appear in a build just
by being importable, so the default path is structurally incapable of scraping. Adding one
is a visible, reviewable edit to this file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pulse.models import RawReview


class IngestError(Exception):
    """Raised when a source cannot produce reviews at all.

    Row-level problems are *not* this — those are counted in `LoadReport` so one malformed
    row never discards an otherwise good export.
    """


@dataclass
class LoadReport:
    """What a source did, including everything it chose not to load.

    Skipped rows are counted by reason rather than silently dropped. A corpus that is
    quietly 30% smaller than the export produces a plausible-looking but wrong pulse, and
    nothing downstream would reveal it.
    """

    files_read: list[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_loaded: int = 0
    rows_skipped: dict[str, int] = field(default_factory=dict)
    identity_columns_seen: set[str] = field(default_factory=set)

    def skip(self, reason: str) -> None:
        self.rows_skipped[reason] = self.rows_skipped.get(reason, 0) + 1

    @property
    def total_skipped(self) -> int:
        return sum(self.rows_skipped.values())

    def summary(self) -> dict[str, Any]:
        """Ledger-safe summary. Contains counts and column names only, never row content."""
        return {
            "files_read": sorted(self.files_read),
            "rows_seen": self.rows_seen,
            "rows_loaded": self.rows_loaded,
            "rows_skipped": dict(sorted(self.rows_skipped.items())),
            "identity_columns_seen": sorted(self.identity_columns_seen),
        }


@runtime_checkable
class ReviewSource(Protocol):
    """Produces raw reviews from somewhere. Stage 1's only interface."""

    #: Registry key, also what `--source` accepts.
    name: str

    def load(self) -> tuple[list[RawReview], LoadReport]:
        """Return every review the source can offer, plus an account of what it skipped."""
        ...


#: Explicit adapter registry. See the module docstring before adding to it.
_REGISTRY: dict[str, Callable[..., ReviewSource]] = {}


def register_source(name: str, factory: Callable[..., ReviewSource]) -> None:
    """Register an adapter factory. Called at import time by each adapter module."""
    if name in _REGISTRY:
        raise IngestError(f"Review source {name!r} is already registered.")
    _REGISTRY[name] = factory


def registered_sources() -> list[str]:
    return sorted(_REGISTRY)


def build_source(name: str, **kwargs: Any) -> ReviewSource:
    """Instantiate a registered adapter by name."""
    try:
        factory = _REGISTRY[name]
    except KeyError:
        known = ", ".join(registered_sources()) or "none"
        raise IngestError(f"Unknown review source {name!r}. Registered: {known}") from None
    return factory(**kwargs)
