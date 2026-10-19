"""The Play Store fetcher (resolves O1). Entirely offline.

Every test injects a fake page fetcher, so the suite never touches the network — which is
the same property the pipeline has, and for the same reason.

Two behaviours carry most of the weight:

* **The window is the stop condition.** Newest-first, stopping at the first review older
  than the window start. A fetcher bounded by a review count instead would either
  over-fetch (impolite) or hand the pipeline a corpus narrower than `window.min_weeks`,
  which stage 2 then correctly refuses.
* **Identity is dropped at the edge.** `userName` and `userImage` never reach the export
  file, so the one artifact a human might open by hand has no reviewer in it at all.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from pulse.ingest.play_store import (
    GROWW_APP_ID,
    IDENTITY_KEYS,
    FetchError,
    default_export_path,
    fetch_window,
    prune_old_exports,
    write_export,
)

WINDOW_START = date(2026, 7, 20)


def a_row(
    *,
    day: str = "2026-10-10",
    content: str = "Withdrawal has been pending for four days with no update.",
    score: int = 2,
    review_id: str = "abc-123",
    with_identity: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "reviewId": review_id,
        "content": content,
        "score": score,
        "at": datetime.fromisoformat(f"{day}T09:15:00"),
        "appVersion": "18.18.1",
        "thumbsUpCount": 0,
        "replyContent": None,
        "repliedAt": None,
        "reviewCreatedVersion": "18.18.1",
    }
    if with_identity:
        row["userName"] = "A Real Person"
        row["userImage"] = "https://example.invalid/avatar.png"
    return row


def pages(*batches: list[dict[str, Any]]):
    """A fake page fetcher serving `batches` in order, then exhausting its token."""
    remaining = list(batches)
    calls: list[Any] = []

    def fetch(token: Any) -> tuple[list[dict[str, Any]], Any]:
        calls.append(token)
        if not remaining:
            return [], None
        batch = remaining.pop(0)
        return batch, f"token-{len(calls)}" if remaining else None

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------------------------------
# Mapping to the canonical export format
# --------------------------------------------------------------------------------------


def test_rows_are_mapped_to_the_canonical_format() -> None:
    rows, report = fetch_window(window_start=WINDOW_START, fetcher=pages([a_row()]), sleep=no_sleep)
    (row,) = rows

    assert row["rating"] == 2
    assert row["date"] == "2026-10-10"
    assert row["source"] == "play_store"
    assert row["external_id"] == "abc-123"
    assert report.reviews_kept == 1


def test_the_export_has_only_the_canonical_keys() -> None:
    """A fixed key set, so an endpoint change cannot quietly widen what is written."""
    rows, _ = fetch_window(window_start=WINDOW_START, fetcher=pages([a_row()]), sleep=no_sleep)
    assert set(rows[0]) == {
        "external_id",
        "rating",
        "text",
        "date",
        "source",
        "app_version",
    }


def test_a_rating_with_no_text_is_skipped() -> None:
    """It carries no theme and could never be quoted."""
    rows, report = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(content=""), a_row(content="   "), a_row()]),
        sleep=no_sleep,
    )
    assert len(rows) == 1
    assert report.skipped_no_text == 2


def test_a_row_with_no_usable_date_is_skipped() -> None:
    broken = a_row()
    broken["at"] = None
    rows, _ = fetch_window(window_start=WINDOW_START, fetcher=pages([broken]), sleep=no_sleep)
    assert rows == []


def test_a_string_date_is_accepted() -> None:
    row = a_row()
    row["at"] = "2026-10-10T09:15:00"
    rows, _ = fetch_window(window_start=WINDOW_START, fetcher=pages([row]), sleep=no_sleep)
    assert rows[0]["date"] == "2026-10-10"


# --------------------------------------------------------------------------------------
# Identity is dropped at the edge
# --------------------------------------------------------------------------------------


def test_identity_fields_never_reach_the_export() -> None:
    """Stricter than the pipeline needs: the export file itself has no reviewer in it."""
    rows, report = fetch_window(window_start=WINDOW_START, fetcher=pages([a_row()]), sleep=no_sleep)

    serialized = json.dumps(rows)
    assert "A Real Person" not in serialized
    assert "avatar.png" not in serialized
    assert "userName" not in serialized
    assert report.identity_keys_dropped == {"userName", "userImage"}


def test_the_dropped_keys_are_reported_not_silently_discarded() -> None:
    """An operator should know the endpoint returns identity at all."""
    _, report = fetch_window(window_start=WINDOW_START, fetcher=pages([a_row()]), sleep=no_sleep)
    assert report.identity_keys_dropped
    assert report.identity_keys_dropped <= set(IDENTITY_KEYS)


def test_nothing_is_reported_dropped_when_nothing_was_sent() -> None:
    _, report = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(with_identity=False)]),
        sleep=no_sleep,
    )
    assert report.identity_keys_dropped == set()


# --------------------------------------------------------------------------------------
# The window is the stop condition
# --------------------------------------------------------------------------------------


def test_fetching_stops_at_the_window_start() -> None:
    """Newest-first, so the first out-of-window review means every later one is too."""
    in_window = [a_row(day="2026-10-10", review_id="new")]
    straddling = [
        a_row(day="2026-07-21", review_id="edge"),
        a_row(day="2026-07-19", review_id="old"),
    ]
    never_requested = [a_row(day="2026-01-01", review_id="ancient")]

    fetcher = pages(in_window, straddling, never_requested)
    rows, report = fetch_window(window_start=WINDOW_START, fetcher=fetcher, sleep=no_sleep)

    ids = {row["external_id"] for row in rows}
    assert ids == {"new", "edge"}
    assert report.skipped_out_of_window == 1
    assert "reached the window start" in report.stopped_because
    # The third page was never requested.
    assert len(fetcher.calls) == 2  # type: ignore[attr-defined]


def test_a_review_exactly_on_the_window_start_is_kept() -> None:
    rows, _ = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(day="2026-07-20", review_id="boundary")]),
        sleep=no_sleep,
    )
    assert [row["external_id"] for row in rows] == ["boundary"]


def test_pagination_follows_the_continuation_token() -> None:
    fetcher = pages(
        [a_row(day="2026-10-10", review_id="p1")],
        [a_row(day="2026-10-09", review_id="p2")],
        [a_row(day="2026-10-08", review_id="p3")],
    )
    rows, report = fetch_window(window_start=WINDOW_START, fetcher=fetcher, sleep=no_sleep)

    assert [row["external_id"] for row in rows] == ["p1", "p2", "p3"]
    assert report.pages == 3
    # First call gets no token; later calls carry the previous page's.
    assert fetcher.calls[0] is None  # type: ignore[attr-defined]
    assert fetcher.calls[1] is not None  # type: ignore[attr-defined]


def test_fetching_stops_when_the_store_runs_out() -> None:
    _, report = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(day="2026-10-10")]),
        sleep=no_sleep,
    )
    assert "no further reviews" in report.stopped_because


# --------------------------------------------------------------------------------------
# Safety valves
# --------------------------------------------------------------------------------------


def test_the_review_cap_stops_the_fetch() -> None:
    """A second ceiling, not the primary stop condition."""
    batch = [a_row(day="2026-10-10", review_id=f"r{n}") for n in range(5)]
    _, report = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages(batch, batch, batch),
        max_reviews=5,
        sleep=no_sleep,
    )
    assert "review cap" in report.stopped_because
    assert report.reviews_kept == 5


def test_the_page_cap_stops_the_fetch() -> None:
    """Hitting it means the stop condition is broken, so it must not run forever."""
    batch = [a_row(day="2026-10-10")]
    fetcher = pages(*[batch] * 10)
    _, report = fetch_window(
        window_start=WINDOW_START, fetcher=fetcher, max_pages=3, sleep=no_sleep
    )

    assert report.pages == 3
    assert "page cap" in report.stopped_because


def test_requests_are_paced() -> None:
    """Politeness: a weekly job reading a public page has no reason to hurry."""
    slept: list[float] = []
    batch = [a_row(day="2026-10-10")]
    fetch_window(
        window_start=WINDOW_START,
        fetcher=pages(batch, batch, batch),
        delay_seconds=1.5,
        sleep=slept.append,
    )
    # One sleep between each pair of pages, none after the last.
    assert slept == [1.5, 1.5]


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------


def test_a_failure_on_the_first_page_raises() -> None:
    def explode(_token: Any) -> tuple[list[dict[str, Any]], Any]:
        raise RuntimeError("connection reset")

    with pytest.raises(FetchError, match="connection reset"):
        fetch_window(window_start=WINDOW_START, fetcher=explode, sleep=no_sleep)


def test_a_later_failure_keeps_what_arrived() -> None:
    """Partial data is still useful, and refetching from scratch is slower and ruder."""
    calls = {"n": 0}

    def flaky(_token: Any) -> tuple[list[dict[str, Any]], Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [a_row(day="2026-10-10", review_id="kept")], "token-1"
        raise RuntimeError("connection reset")

    rows, report = fetch_window(window_start=WINDOW_START, fetcher=flaky, sleep=no_sleep)

    assert [row["external_id"] for row in rows] == ["kept"]
    assert "request failed" in report.stopped_because


# --------------------------------------------------------------------------------------
# Writing the export
# --------------------------------------------------------------------------------------


def test_the_export_is_valid_canonical_json(tmp_path: Path) -> None:
    rows, _ = fetch_window(window_start=WINDOW_START, fetcher=pages([a_row()]), sleep=no_sleep)
    path = write_export(rows, tmp_path / "export.json")

    assert json.loads(path.read_text(encoding="utf-8")) == rows


def test_the_export_is_readable_by_the_file_drop_adapter(tmp_path: Path) -> None:
    """The point of the whole design: fetch produces a file, the pipeline reads a file."""
    from pulse.ingest.file_drop import FileDropSource

    rows, _ = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(review_id="a"), a_row(review_id="b", day="2026-10-09")]),
        sleep=no_sleep,
    )
    path = write_export(rows, tmp_path / "export.json")

    loaded, report = FileDropSource(path).load()
    assert len(loaded) == 2
    assert report.identity_columns_seen == set()


def test_the_export_path_is_dated(tmp_path: Path) -> None:
    """Dated so repeated fetches accumulate rather than overwrite each other."""
    path = default_export_path(tmp_path, date(2026, 10, 12), GROWW_APP_ID)
    assert path.name == "com-nextbillion-groww-2026-10-12.json"


def test_the_report_contains_no_review_text() -> None:
    _, report = fetch_window(
        window_start=WINDOW_START,
        fetcher=pages([a_row(content="Mail me at notareal@example.com about this.")]),
        sleep=no_sleep,
    )
    assert "notareal" not in str(report.summary())


# --------------------------------------------------------------------------------------
# Retention (O6, P6-T7) -- exports fetched before the window are pruned
# --------------------------------------------------------------------------------------


def test_exports_fetched_before_the_window_are_deleted(tmp_path: Path) -> None:
    old = default_export_path(tmp_path, date(2026, 7, 1))
    edge = default_export_path(tmp_path, WINDOW_START)
    new = default_export_path(tmp_path, date(2026, 10, 11))
    for path in (old, edge, new):
        path.write_text("[]", encoding="utf-8")

    removed = prune_old_exports(tmp_path, WINDOW_START)

    assert removed == [old]
    assert not old.exists()
    assert edge.exists() and new.exists()


def test_files_not_named_by_fetch_are_never_deleted(tmp_path: Path) -> None:
    """A hand-dropped export, another app's export, or a CSV is the owner's, not ours."""
    keep = [
        tmp_path / "my-export-2020-01-01.json",
        tmp_path / "com-other-app-2020-01-01.json",
        tmp_path / "com-nextbillion-groww-2020-01-01.csv",
    ]
    for path in keep:
        path.write_text("[]", encoding="utf-8")

    assert prune_old_exports(tmp_path, WINDOW_START, GROWW_APP_ID) == []
    assert all(path.exists() for path in keep)


def test_pruning_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert prune_old_exports(tmp_path / "absent", WINDOW_START) == []
