"""File-drop adapter: reads public review exports from disk (P1-T4).

The only adapter registered by default, and the reason the "public exports only" constraint
holds structurally: this module performs **no network I/O of any kind**. There is no HTTP
client imported here, so the default ingest path cannot scrape a store even by accident.

Accepts a single file or a directory, in JSON or CSV. The canonical schema and its accepted
aliases are documented in `docs/input-format.md`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pulse import logging as pulse_logging
from pulse.ingest.base import IngestError, LoadReport, register_source
from pulse.models import RawReview, Source

log = pulse_logging.get_logger("ingest.file_drop")

JSON_SUFFIXES = {".json"}
CSV_SUFFIXES = {".csv", ".tsv"}
SUPPORTED_SUFFIXES = JSON_SUFFIXES | CSV_SUFFIXES

#: Keys a JSON export may wrap its array in.
ENVELOPE_KEYS = ("reviews", "data", "results", "items")

#: Canonical field -> accepted source column names, lowercased.
#:
#: Aliases exist because O1 is unresolved: we do not yet know which concrete export this
#: will read. Rather than guess one shape and rewrite the adapter later, accept the
#: spellings the common Play Store export tools emit. Order within each tuple is priority.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "rating": ("rating", "score", "stars", "star_rating"),
    "text": ("text", "content", "review", "review_text", "body", "comment"),
    "title": ("title", "subject", "headline"),
    "submitted_at": (
        "submitted_at",
        "date",
        "at",
        "review_date",
        "created_at",
        "timestamp",
        "posted_at",
    ),
    "external_id": ("external_id", "review_id", "reviewid", "id"),
}

#: Columns known to carry reviewer identity.
#:
#: These are *not* mapped onto `RawReview`'s declared fields. They may still land in
#: `model_extra` and therefore in `01_raw.json`, which architecture.md §18 already treats
#: as the one sensitive artifact. What matters is that normalization never reads them, so
#: nothing past stage 2 can carry identity (architecture.md §10, layer 1). We log their
#: presence so an operator knows the export contains identity data at all.
IDENTITY_COLUMNS = frozenset(
    {
        "author",
        "author_name",
        "avatar",
        "device",
        "device_id",
        "email",
        "profile",
        "profile_name",
        "reviewer",
        "reviewer_name",
        "user",
        "user_image",
        "user_name",
        "username",
    }
)


def _resolve(row: dict[str, Any], field: str) -> Any:
    """Pick the first present alias for a canonical field."""
    for alias in FIELD_ALIASES[field]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key).strip().lower(): value for key, value in row.items()}


def _unmapped(row: dict[str, Any]) -> dict[str, Any]:
    """Columns the canonical schema does not claim, minus identity columns."""
    claimed = {alias for aliases in FIELD_ALIASES.values() for alias in aliases}
    claimed.add("source")
    return {
        key: value
        for key, value in row.items()
        if key not in claimed and key not in IDENTITY_COLUMNS
    }


def _read_json(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IngestError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ENVELOPE_KEYS:
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            raise IngestError(
                f"{path} is a JSON object with no review array. Expected a top-level list, "
                f"or one of these keys: {', '.join(ENVELOPE_KEYS)}."
            )
    else:
        raise IngestError(f"{path} must contain a JSON array of review objects.")

    non_objects = sum(1 for row in rows if not isinstance(row, dict))
    if non_objects:
        raise IngestError(f"{path} contains {non_objects} entries that are not JSON objects.")
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise IngestError(f"{path} is empty — a header row is required.")
        return [dict(row) for row in reader]


class FileDropSource:
    """Reads review exports from a file or directory. No network access."""

    name = "file_drop"

    def __init__(self, path: Path, default_source: Source = Source.PLAY_STORE) -> None:
        self.path = Path(path)
        self.default_source = default_source

    def _files(self) -> list[Path]:
        if not self.path.exists():
            raise IngestError(
                f"Review export not found: {self.path}\n"
                f"Drop a public Play Store export there, or pass --input. "
                f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}."
            )

        if self.path.is_file():
            if self.path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise IngestError(
                    f"Unsupported export type {self.path.suffix!r} for {self.path}. "
                    f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}."
                )
            return [self.path]

        # Sorted so a multi-file directory produces a deterministic corpus order, which
        # keeps run artifacts byte-stable across runs.
        files = sorted(
            child
            for child in self.path.iterdir()
            if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise IngestError(
                f"No review exports in {self.path}/ — expected at least one file with a "
                f"{' or '.join(sorted(SUPPORTED_SUFFIXES))} suffix."
            )
        return files

    def load(self) -> tuple[list[RawReview], LoadReport]:
        report = LoadReport()
        reviews: list[RawReview] = []

        for path in self._files():
            rows = _read_json(path) if path.suffix.lower() in JSON_SUFFIXES else _read_csv(path)
            report.files_read.append(str(path))

            for row in rows:
                report.rows_seen += 1
                normalized = _lower_keys(row)
                report.identity_columns_seen.update(
                    key for key in normalized if key in IDENTITY_COLUMNS
                )

                raw = self._to_raw_review(normalized, report)
                if raw is not None:
                    reviews.append(raw)
                    report.rows_loaded += 1

        if report.identity_columns_seen:
            # Worth saying out loud: it tells the operator the export carries identity, and
            # that only 01_raw.json can contain it.
            log.warning(
                "export contains reviewer identity columns; they stop at stage 2",
                extra={"columns": sorted(report.identity_columns_seen)},
            )

        if not reviews:
            raise IngestError(
                f"Read {report.rows_seen} row(s) from {len(report.files_read)} file(s) but "
                f"loaded none. Skipped: {report.rows_skipped or 'nothing'}. "
                f"Check the export matches docs/input-format.md."
            )

        log.info("ingest complete", extra=report.summary())
        return reviews, report

    def _to_raw_review(self, row: dict[str, Any], report: LoadReport) -> RawReview | None:
        """Map one export row onto `RawReview`, or count why it was skipped.

        Stage 1 is deliberately permissive: `RawReview` requires only a source, so almost
        nothing is rejected here. Typing, clamping, and dropping happen at stage 2, where
        the rules are in one place and testable.
        """
        raw_source = row.get("source")
        if raw_source in (None, ""):
            source = self.default_source
        else:
            try:
                source = Source(str(raw_source).strip().lower())
            except ValueError:
                # D10: Play Store only. An App Store row is out of scope, not a bad row.
                report.skip(f"out-of-scope source {str(raw_source).strip().lower()!r}")
                return None

        rating = _resolve(row, "rating")
        payload: dict[str, Any] = {
            "source": source,
            "external_id": _coerce_str(_resolve(row, "external_id")),
            "rating": _coerce_int(rating),
            "title": _coerce_str(_resolve(row, "title")),
            "text": _coerce_str(_resolve(row, "text")),
            "submitted_at": _coerce_str(_resolve(row, "submitted_at")),
            **_unmapped(row),
        }

        if payload["text"] is None and payload["title"] is None:
            report.skip("no text or title")
            return None
        if payload["submitted_at"] is None:
            report.skip("no date")
            return None

        return RawReview.model_validate(payload)


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any) -> int | None:
    """Best-effort int for stage 1. Range enforcement belongs to stage 2."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return round(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


register_source(FileDropSource.name, FileDropSource)
