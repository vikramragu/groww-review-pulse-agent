"""File-drop adapter and the source registry (P1-T3, P1-T4).

The registry test matters more than it looks: the "public exports only" constraint holds
because the only registered adapter reads from disk and imports no HTTP client. A test that
asserts the default registry contains exactly one file-based source is what stops a
network-fetching adapter from arriving unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.ingest.base import (
    IngestError,
    LoadReport,
    build_source,
    register_source,
    registered_sources,
)
from pulse.ingest.file_drop import FIELD_ALIASES, IDENTITY_COLUMNS, FileDropSource
from pulse.models import Source

ROWS = [
    {"rating": 1, "text": "Withdrawal is stuck again.", "date": "2026-10-05"},
    {"rating": 5, "text": "Charts are excellent.", "date": "2026-10-06"},
]


def write_json(path: Path, rows: object) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def load(path: Path) -> tuple[list[object], LoadReport]:
    reviews, report = FileDropSource(path).load()
    return list(reviews), report


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


def test_only_the_file_drop_source_is_registered() -> None:
    """Guards the "public exports only" constraint (architecture.md §3).

    The default ingest path must be structurally incapable of fetching from a store. If
    this test fails because an adapter was added, that adapter needs an explicit review
    against the constraint, not a test update.
    """
    assert registered_sources() == ["file_drop"]


def test_build_source_returns_the_adapter() -> None:
    source = build_source("file_drop", path=Path("data/raw"))
    assert isinstance(source, FileDropSource)
    assert source.name == "file_drop"


def test_unknown_source_names_what_is_available() -> None:
    with pytest.raises(IngestError, match="file_drop"):
        build_source("play_store_scraper", path=Path("."))


def test_registering_a_duplicate_name_fails() -> None:
    with pytest.raises(IngestError, match="already registered"):
        register_source("file_drop", FileDropSource)


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------


def test_reads_a_json_array(tmp_path: Path) -> None:
    reviews, report = load(write_json(tmp_path / "reviews.json", ROWS))

    assert len(reviews) == 2
    assert report.rows_seen == 2
    assert report.rows_loaded == 2


@pytest.mark.parametrize("key", ["reviews", "data", "results", "items"])
def test_reads_a_wrapped_json_array(tmp_path: Path, key: str) -> None:
    reviews, _ = load(write_json(tmp_path / "reviews.json", {key: ROWS}))
    assert len(reviews) == 2


def test_rejects_json_that_is_not_reviews(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="no review array"):
        load(write_json(tmp_path / "reviews.json", {"unexpected": 1}))


def test_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(IngestError, match="not valid JSON"):
        load(path)


def test_rejects_an_array_of_non_objects(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not JSON objects"):
        load(write_json(tmp_path / "reviews.json", [1, 2, 3]))


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------


def test_reads_a_csv(tmp_path: Path) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text(
        "rating,text,date\n1,Withdrawal is stuck.,2026-10-05\n5,Charts are good.,2026-10-06\n",
        encoding="utf-8",
    )
    reviews, report = load(path)

    assert len(reviews) == 2
    assert report.rows_loaded == 2


def test_reads_a_csv_with_a_bom(tmp_path: Path) -> None:
    """Spreadsheet exports routinely carry a UTF-8 BOM, which would corrupt the first header."""
    path = tmp_path / "reviews.csv"
    path.write_text("﻿rating,text,date\n2,Deposit failed.,2026-10-05\n", encoding="utf-8")
    reviews, _ = load(path)
    assert len(reviews) == 1


def test_reads_a_tsv(tmp_path: Path) -> None:
    path = tmp_path / "reviews.tsv"
    path.write_text("rating\ttext\tdate\n3\tSlow app.\t2026-10-05\n", encoding="utf-8")
    assert len(load(path)[0]) == 1


def test_rejects_an_empty_csv(tmp_path: Path) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(IngestError, match="header row"):
        load(path)


# --------------------------------------------------------------------------------------
# Column aliases
# --------------------------------------------------------------------------------------


def test_alias_columns_are_mapped(tmp_path: Path) -> None:
    """O1 is unresolved, so the adapter accepts the spellings common exporters emit."""
    rows = [{"score": 4, "content": "Alias columns map cleanly.", "at": "2026-10-06"}]
    reviews, _ = load(write_json(tmp_path / "reviews.json", rows))

    (review,) = reviews
    assert review.rating == 4  # type: ignore[attr-defined]
    assert review.text == "Alias columns map cleanly."  # type: ignore[attr-defined]


def test_column_names_are_case_insensitive(tmp_path: Path) -> None:
    rows = [{"Rating": 4, "Text": "Mixed case headers.", "Date": "2026-10-06"}]
    assert len(load(write_json(tmp_path / "reviews.json", rows))[0]) == 1


def test_canonical_names_win_over_aliases(tmp_path: Path) -> None:
    rows = [
        {
            "rating": 1,
            "score": 5,
            "text": "Canonical wins.",
            "content": "Alias loses.",
            "date": "2026-10-06",
        }
    ]
    (review,), _ = load(write_json(tmp_path / "reviews.json", rows))
    assert review.rating == 1  # type: ignore[attr-defined]
    assert review.text == "Canonical wins."  # type: ignore[attr-defined]


def test_no_alias_is_claimed_by_two_fields() -> None:
    """An ambiguous alias would silently map a column to the wrong field."""
    seen: set[str] = set()
    for aliases in FIELD_ALIASES.values():
        for alias in aliases:
            assert alias not in seen, f"{alias!r} is claimed by two fields"
            seen.add(alias)


# --------------------------------------------------------------------------------------
# Type coercion at stage 1
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("4", 4), (4, 4), (4.0, 4), ("4.0", 4), (" 4 ", 4), ("", None), (None, None)],
)
def test_rating_coercion(tmp_path: Path, raw: object, expected: int | None) -> None:
    rows = [{"rating": raw, "text": "Some review text.", "date": "2026-10-06"}]
    (review,), _ = load(write_json(tmp_path / "reviews.json", rows))
    assert review.rating == expected  # type: ignore[attr-defined]


def test_out_of_range_ratings_pass_through_stage_one(tmp_path: Path) -> None:
    """Clamping is stage 2's job, so the rules live in one place."""
    rows = [{"rating": 9, "text": "Out of range rating.", "date": "2026-10-06"}]
    (review,), _ = load(write_json(tmp_path / "reviews.json", rows))
    assert review.rating == 9  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Skipped rows are counted, never silently dropped
# --------------------------------------------------------------------------------------


def test_rows_without_text_or_title_are_counted(tmp_path: Path) -> None:
    rows = [{"rating": 3, "date": "2026-10-06"}, *ROWS]
    reviews, report = load(write_json(tmp_path / "reviews.json", rows))

    assert len(reviews) == 2
    assert report.rows_skipped == {"no text or title": 1}


def test_rows_without_a_date_are_counted(tmp_path: Path) -> None:
    rows = [{"rating": 3, "text": "No date on this row."}, *ROWS]
    reviews, report = load(write_json(tmp_path / "reviews.json", rows))

    assert len(reviews) == 2
    assert report.rows_skipped == {"no date": 1}


def test_out_of_scope_sources_are_skipped(tmp_path: Path) -> None:
    """D10: Play Store only. An App Store row is out of scope, not malformed."""
    rows = [
        {"source": "app_store", "rating": 4, "text": "An iOS review.", "date": "2026-10-06"},
        *ROWS,
    ]
    reviews, report = load(write_json(tmp_path / "reviews.json", rows))

    assert len(reviews) == 2
    assert report.rows_skipped == {"out-of-scope source 'app_store'": 1}


def test_source_defaults_to_play_store(tmp_path: Path) -> None:
    (review,), _ = load(write_json(tmp_path / "reviews.json", [ROWS[0]]))
    assert review.source is Source.PLAY_STORE  # type: ignore[attr-defined]


def test_loading_nothing_at_all_is_an_error(tmp_path: Path) -> None:
    """A corpus of zero is never a valid run; say so rather than proceeding."""
    rows = [{"rating": 3}, {"text": "No date."}]
    with pytest.raises(IngestError, match="loaded none"):
        load(write_json(tmp_path / "reviews.json", rows))


# --------------------------------------------------------------------------------------
# Identity columns
# --------------------------------------------------------------------------------------


def test_identity_columns_are_reported(tmp_path: Path) -> None:
    """The operator should know the export carries identity, even though it stops at stage 2."""
    rows = [
        {
            "rating": 1,
            "text": "Identity columns present.",
            "date": "2026-10-06",
            "userName": "Not A Realperson",
            "device_id": "fake-0001",
        }
    ]
    _, report = load(write_json(tmp_path / "reviews.json", rows))

    assert report.identity_columns_seen == {"username", "device_id"}


def test_identity_columns_are_not_mapped_onto_declared_fields(tmp_path: Path) -> None:
    rows = [
        {
            "rating": 1,
            "text": "Real body text.",
            "date": "2026-10-06",
            "author": "Not A Realperson",
        }
    ]
    (review,), _ = load(write_json(tmp_path / "reviews.json", rows))

    assert review.text == "Real body text."  # type: ignore[attr-defined]
    assert "author" not in (review.model_extra or {})  # type: ignore[attr-defined]


def test_identity_column_list_is_lowercase() -> None:
    """Matching happens on lowercased keys, so the list must be too."""
    assert all(column == column.lower() for column in IDENTITY_COLUMNS)


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


def test_reads_every_export_in_a_directory(tmp_path: Path) -> None:
    write_json(tmp_path / "a.json", [ROWS[0]])
    write_json(tmp_path / "b.json", [ROWS[1]])
    reviews, report = load(tmp_path)

    assert len(reviews) == 2
    assert len(report.files_read) == 2


def test_directory_files_are_read_in_sorted_order(tmp_path: Path) -> None:
    """Deterministic corpus order keeps run artifacts byte-stable across runs."""
    write_json(tmp_path / "b.json", [ROWS[1]])
    write_json(tmp_path / "a.json", [ROWS[0]])
    _, report = load(tmp_path)

    assert [Path(name).name for name in report.files_read] == ["a.json", "b.json"]


def test_non_export_files_in_a_directory_are_ignored(tmp_path: Path) -> None:
    write_json(tmp_path / "reviews.json", ROWS)
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "archive.zip").write_bytes(b"PK\x03\x04")

    _, report = load(tmp_path)
    assert len(report.files_read) == 1


def test_missing_path_names_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="--input"):
        load(tmp_path / "absent.json")


def test_empty_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="No review exports"):
        load(tmp_path)


def test_unsupported_suffix_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "reviews.xlsx"
    path.write_text("not a spreadsheet", encoding="utf-8")
    with pytest.raises(IngestError, match="Unsupported export type"):
        load(path)


# --------------------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------------------


def test_load_report_summary_contains_no_review_text(tmp_path: Path) -> None:
    rows = [{"rating": 1, "text": "Mail notareal@example.com now.", "date": "2026-10-06"}]
    _, report = load(write_json(tmp_path / "reviews.json", rows))

    assert "notareal" not in str(report.summary())


def test_load_report_counts_reconcile(tmp_path: Path) -> None:
    rows = [*ROWS, {"rating": 3, "date": "2026-10-06"}]
    _, report = load(write_json(tmp_path / "reviews.json", rows))

    assert report.rows_seen == report.rows_loaded + report.total_skipped
