"""Fetch public Play Store reviews into a local export file (resolves O1).

**This is deliberately not a `ReviewSource`.** It is not in the adapter registry, and the
pipeline cannot reach it. Registering it would have made network access an *opt-in* for
stage 1; keeping it out makes stage 1 structurally incapable of it, which is stronger and
costs nothing. Three further reasons:

* The download is archived as a file, so re-running the pipeline never re-fetches. That is
  politer to Google and it keeps stage-1 artifacts reproducible.
* The test suite stays network-free with no mocking inside the pipeline.
* It produces, once, the thing the brief actually asks the pipeline to read: a public
  review export. The pipeline then reads a file like any other.

## The ToS position, stated rather than buried

problemStatement.md §6 requires "public review exports only -- no scraping behind store
logins, no ToS-violating automation", and architecture.md O1 has tracked the problem since
the first draft: **there is no official public export of Play Store reviews for someone who
does not own the app.** Play Console exports are owner-only and Groww is not ours.

This module reads data any visitor can see, with no account, no login, and no
authentication bypass. It also calls Google's undocumented endpoints, and Google's terms
broadly prohibit automated access — so it sits in tension with the second half of that
constraint. That trade was made knowingly on 2026-10-12 for a learning project, and is
recorded in architecture.md §19 and O1 rather than papered over.

For anything commercial, the honest path is a licensed provider (SerpApi, Appbot,
AppFollow, DataForSEO). That would be a second module beside this one; nothing downstream
changes, because both just produce an export file.

## Politeness

Newest-first, stopping as soon as reviews fall outside the configured window — never "fetch
everything". Paced between pages, with a hard page cap so a bug cannot turn into a flood.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pulse import logging as pulse_logging

log = pulse_logging.get_logger("ingest.play_store")

GROWW_APP_ID = "com.nextbillion.groww"

#: Per-request page size. The endpoint caps out around here in practice.
PAGE_SIZE = 200

#: Seconds between requests. Not tuned for speed -- this is a weekly job reading a public
#: page, and there is no reason to be in a hurry about it.
PAGE_DELAY_SECONDS = 1.0

#: Safety valve. At 200 per page this is 40,000 reviews, far beyond any sane window, so
#: hitting it means a bug in the stop condition rather than a genuinely huge week.
MAX_PAGES = 200

#: Keys the Play endpoint returns that identify the reviewer. Dropped here, at the edge,
#: so they are absent from the export file itself and not merely from stage 2 onward.
IDENTITY_KEYS = frozenset({"userName", "userImage"})


class FetchError(Exception):
    """Raised when reviews cannot be fetched."""


@dataclass
class FetchReport:
    """Counts only -- never review text."""

    app_id: str = ""
    pages: int = 0
    reviews_seen: int = 0
    reviews_kept: int = 0
    skipped_no_text: int = 0
    skipped_out_of_window: int = 0
    oldest_kept: str | None = None
    newest_kept: str | None = None
    stopped_because: str = ""
    identity_keys_dropped: set[str] = field(default_factory=set)

    def summary(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "pages": self.pages,
            "reviews_seen": self.reviews_seen,
            "reviews_kept": self.reviews_kept,
            "skipped_no_text": self.skipped_no_text,
            "skipped_out_of_window": self.skipped_out_of_window,
            "oldest_kept": self.oldest_kept,
            "newest_kept": self.newest_kept,
            "stopped_because": self.stopped_because,
            "identity_keys_dropped": sorted(self.identity_keys_dropped),
        }


#: A page fetcher: (continuation_token) -> (rows, next_token). Injected so the tests can
#: exercise pagination, the window stop, and identity dropping without a network call.
PageFetcher = Callable[[Any], tuple[Sequence[dict[str, Any]], Any]]


def _default_fetcher(app_id: str, lang: str, country: str) -> PageFetcher:
    try:
        from google_play_scraper import Sort, reviews
    except ImportError as exc:
        raise FetchError(
            "google-play-scraper is not installed. It is an optional dependency, kept out "
            "of the default install so the pipeline has no network-capable code in it:\n"
            "    pip install -e '.[scraper]'"
        ) from exc

    def fetch(token: Any) -> tuple[Sequence[dict[str, Any]], Any]:
        # The library is untyped, so the boundary is narrowed here rather than letting
        # `Any` leak into the rest of the module.
        batch, next_token = reviews(
            app_id,
            lang=lang,
            country=country,
            sort=Sort.NEWEST,
            count=PAGE_SIZE,
            continuation_token=token,
        )
        return list(batch), next_token

    return fetch


def _to_export_row(row: dict[str, Any], report: FetchReport) -> dict[str, Any] | None:
    """Map one endpoint row onto the canonical export format (docs/input-format.md)."""
    report.identity_keys_dropped.update(key for key in row if key in IDENTITY_KEYS)

    text = (row.get("content") or "").strip()
    if not text:
        # A rating with no words carries no theme and can never be quoted.
        report.skipped_no_text += 1
        return None

    at = row.get("at")
    if isinstance(at, datetime):
        submitted = at.date().isoformat()
    elif isinstance(at, str) and at:
        submitted = at[:10]
    else:
        return None

    return {
        # Only these keys. Identity keys are never copied across, so the export file --
        # the one artifact a human might open -- has no reviewer in it at all.
        "external_id": row.get("reviewId"),
        "rating": row.get("score"),
        "text": text,
        "date": submitted,
        "source": "play_store",
        "app_version": row.get("appVersion"),
    }


def fetch_window(
    *,
    window_start: date,
    app_id: str = GROWW_APP_ID,
    lang: str = "en",
    country: str = "in",
    max_reviews: int | None = None,
    fetcher: PageFetcher | None = None,
    delay_seconds: float = PAGE_DELAY_SECONDS,
    max_pages: int = MAX_PAGES,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], FetchReport]:
    """Fetch newest-first until reviews predate `window_start`, then stop.

    Bounded by the window rather than by a review count, because the window is what the
    brief asks for and what the rolling-baseline design (D5) needs. `max_reviews` is a
    second ceiling, not the primary stop condition.
    """
    page = fetcher or _default_fetcher(app_id, lang, country)
    report = FetchReport(app_id=app_id)

    rows: list[dict[str, Any]] = []
    token: Any = None

    while True:
        if report.pages >= max_pages:
            report.stopped_because = f"page cap ({max_pages}) reached"
            break

        try:
            batch, token = page(token)
        except Exception as exc:
            if rows:
                # Partial data is still useful, and re-fetching from scratch would be
                # both slower and less polite than keeping what arrived.
                report.stopped_because = f"request failed after {report.pages} page(s): {exc}"
                log.warning("fetch stopped early", extra={"reason": str(exc)[:200]})
                break
            raise FetchError(f"Could not fetch reviews for {app_id}: {exc}") from exc

        report.pages += 1
        report.reviews_seen += len(batch)

        reached_window_start = False
        for row in batch:
            at = row.get("at")
            when = at.date() if isinstance(at, datetime) else None
            if when is not None and when < window_start:
                # Newest-first, so the first out-of-window review means every later one is
                # too. This is the stop condition that keeps the fetch bounded.
                report.skipped_out_of_window += 1
                reached_window_start = True
                continue

            mapped = _to_export_row(row, report)
            if mapped is not None:
                rows.append(mapped)
                report.reviews_kept += 1

        log.info(
            "fetched page",
            extra={
                "page": report.pages,
                "kept_total": report.reviews_kept,
                "oldest_in_page": str(batch[-1].get("at")) if batch else None,
            },
        )

        if reached_window_start:
            report.stopped_because = f"reached the window start ({window_start.isoformat()})"
            break
        if not token or not batch:
            report.stopped_because = "the store returned no further reviews"
            break
        if max_reviews is not None and report.reviews_kept >= max_reviews:
            report.stopped_because = f"review cap ({max_reviews}) reached"
            break

        sleep(delay_seconds)

    dates = sorted(row["date"] for row in rows)
    report.oldest_kept = dates[0] if dates else None
    report.newest_kept = dates[-1] if dates else None

    if report.identity_keys_dropped:
        log.info(
            "reviewer identity fields dropped at the edge; they are not in the export",
            extra={"keys": sorted(report.identity_keys_dropped)},
        )

    log.info("fetch complete", extra=report.summary())
    return rows, report


def write_export(rows: list[dict[str, Any]], path: Path) -> Path:
    """Write rows as a canonical JSON export (docs/input-format.md)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def default_export_path(root: Path, fetched_on: date, app_id: str = GROWW_APP_ID) -> Path:
    """`data/raw/<app>-<date>.json` — dated, so fetches accumulate rather than overwrite."""
    slug = app_id.replace(".", "-")
    return root / f"{slug}-{fetched_on.isoformat()}.json"
