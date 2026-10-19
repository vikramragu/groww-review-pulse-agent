"""Run directories, stage artifacts, and the run ledger (P0-T6).

Architecture.md §6 makes each stage write its output to `runs/<run_id>/` as a JSON
artifact, so stages are independently replayable: regenerating a note must not re-classify
1,200 reviews, and a publish retry must not re-run the LLM.

This module owns that filesystem contract. Stage *behaviour* arrives in later phases; what
exists here is the bookkeeping every stage will use.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pulse import logging as pulse_logging
from pulse.models import RunLedger, StageRecord

ModelT = TypeVar("ModelT", bound=BaseModel)

DEFAULT_RUNS_ROOT = Path("runs")
LEDGER_FILENAME = "run.json"
NOTE_FILENAME = "pulse.md"

#: Stage number -> artifact filename. Stage 8 produces `pulse.md` instead. Stages 9-10 also
#: set ledger fields; their artifacts record what was published, so `--from 10` can find it.
STAGE_ARTIFACTS: dict[int, str] = {
    1: "01_raw.json",
    2: "02_normalized.json",
    3: "03_scrubbed.json",
    4: "04_classified.json",
    5: "05_stats.json",
    6: "06_note.json",
    7: "07_validation.json",
    9: "09_published.json",
    10: "10_draft.json",
}

STAGE_NAMES: dict[int, str] = {
    1: "ingest",
    2: "normalize",
    3: "scrub",
    4: "classify",
    5: "aggregate",
    6: "draft",
    7: "validate",
    8: "render",
    9: "publish_doc",
    10: "draft_email",
}

_RUN_ID_RE = re.compile(r"^(?P<week>\d{4}-W\d{2})-(?P<seq>\d{2,})$")

log = pulse_logging.get_logger("orchestrator")


class RunError(Exception):
    """Raised when a run cannot proceed: missing artifact, unusable run directory."""


def iso_week_of(day: date) -> str:
    """Format a date as its ISO week label, e.g. `2026-W41`.

    Uses the **ISO** year from `isocalendar()`, not `date.year`. These differ at year
    boundaries — 2026-12-31 falls in ISO week 2027-W01 — and using the calendar year
    there would silently misfile reviews and corrupt the trailing baseline.
    """
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def current_iso_week() -> str:
    """The ISO week containing today. Usually *not* what a run should target."""
    return iso_week_of(datetime.now(UTC).date())


def last_complete_iso_week(today: date | None = None) -> str:
    """The most recent ISO week that has actually finished.

    The default target for a run, and deliberately not `current_iso_week()`. A weekly job
    firing on a Monday would otherwise aim at a week holding a few hours of reviews, which
    stage 5 then correctly refuses as a thin week -- so the obvious default would make the
    scheduled case fail every time. It also removes a timezone dependency: which week
    "today" falls in changes with the hour, while the last finished week does not.
    """
    reference = today or datetime.now(UTC).date()
    return iso_week_of(reference - timedelta(days=7))


class RunContext:
    """One run's directory, ledger, and artifact accessors."""

    def __init__(self, run_dir: Path, ledger: RunLedger) -> None:
        self.run_dir = run_dir
        self.ledger = ledger
        pulse_logging.set_run_id(ledger.run_id)

    # -- identity ----------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self.ledger.run_id

    @property
    def iso_week(self) -> str:
        return self.ledger.iso_week

    @property
    def note_path(self) -> Path:
        return self.run_dir / NOTE_FILENAME

    def artifact_path(self, stage: int) -> Path:
        try:
            return self.run_dir / STAGE_ARTIFACTS[stage]
        except KeyError:
            raise RunError(
                f"Stage {stage} has no JSON artifact. "
                f"Stages with artifacts: {sorted(STAGE_ARTIFACTS)}."
            ) from None

    # -- artifacts ---------------------------------------------------------------

    def write_artifact(self, stage: int, payload: Any) -> Path:
        """Write a stage's output. Pydantic models and lists of them are serialized natively."""
        path = self.artifact_path(stage)
        path.write_text(_to_json(payload), encoding="utf-8")
        log.debug("artifact written", extra={"stage": stage, "path": str(path)})
        return path

    def read_artifact(self, stage: int) -> Any:
        """Read a stage's raw JSON payload."""
        path = self.artifact_path(stage)
        if not path.exists():
            raise RunError(
                f"Stage {stage} artifact missing: {path}\n"
                f"Run the earlier stages first, or use --from to replay from a stage that exists."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def read_models(self, stage: int, model: type[ModelT]) -> list[ModelT]:
        """Read a stage artifact as a validated list of models."""
        data = self.read_artifact(stage)
        if not isinstance(data, list):
            raise RunError(
                f"Stage {stage} artifact is a {type(data).__name__}, expected a list "
                f"of {model.__name__}."
            )
        try:
            return [model.model_validate(item) for item in data]
        except ValidationError as exc:
            raise RunError(
                f"Stage {stage} artifact does not match {model.__name__}:\n{exc}"
            ) from exc

    def read_model(self, stage: int, model: type[ModelT]) -> ModelT:
        """Read a stage artifact as a single validated model."""
        try:
            return model.model_validate(self.read_artifact(stage))
        except ValidationError as exc:
            raise RunError(
                f"Stage {stage} artifact does not match {model.__name__}:\n{exc}"
            ) from exc

    def write_note(self, markdown: str) -> Path:
        self.note_path.write_text(markdown, encoding="utf-8")
        return self.note_path

    def completed_stages(self) -> list[int]:
        return [stage for stage in sorted(STAGE_ARTIFACTS) if self.artifact_path(stage).exists()]

    # -- ledger ------------------------------------------------------------------

    def save_ledger(self) -> Path:
        """Persist the ledger. Called after every stage so a crash still leaves evidence."""
        path = self.run_dir / LEDGER_FILENAME
        path.write_text(self.ledger.model_dump_json(indent=2), encoding="utf-8")
        return path

    def record_stage(
        self,
        stage: int,
        status: str,
        started_at: datetime,
        duration_ms: int,
        detail: dict[str, Any],
    ) -> None:
        # `detail` is a dict, not **kwargs: a stage's own detail may legitimately contain
        # keys like "status", which as keyword arguments would collide with the parameters.
        self.ledger.stages.append(
            StageRecord.model_validate(
                {
                    "stage": stage,
                    "name": STAGE_NAMES.get(stage, f"stage_{stage}"),
                    "status": status,
                    "started_at": started_at,
                    "duration_ms": duration_ms,
                    "detail": detail,
                }
            )
        )
        self.save_ledger()

    @contextmanager
    def stage(self, stage: int, **detail: Any) -> Iterator[dict[str, Any]]:
        """Time a stage, log both boundaries, and record the outcome either way.

        Yields a mutable dict the stage body can add detail to; it is merged into the
        ledger record. A raised exception is recorded as `failed` and re-raised.
        """
        name = STAGE_NAMES.get(stage, f"stage_{stage}")
        started_at = datetime.now(UTC)
        began = time.perf_counter()
        extra_detail: dict[str, Any] = dict(detail)

        log.info("stage start", extra={"stage": stage, "stage_name": name})
        try:
            yield extra_detail
        except Exception as exc:
            elapsed = int((time.perf_counter() - began) * 1000)
            extra_detail["error"] = f"{type(exc).__name__}: {exc}"
            self.record_stage(stage, "failed", started_at, elapsed, extra_detail)
            log.error(
                "stage failed",
                extra={"stage": stage, "stage_name": name, "duration_ms": elapsed},
            )
            raise
        else:
            elapsed = int((time.perf_counter() - began) * 1000)
            self.record_stage(stage, "ok", started_at, elapsed, extra_detail)
            log.info(
                "stage ok",
                extra={"stage": stage, "stage_name": name, "duration_ms": elapsed},
            )

    def finish(self, status: str, abort_reason: str | None = None) -> Path:
        self.ledger.finished_at = datetime.now(UTC)
        self.ledger.status = status  # type: ignore[assignment]
        self.ledger.abort_reason = abort_reason
        return self.save_ledger()


def _to_json(payload: Any) -> str:
    """Serialize models, lists of models, or plain data — deterministically.

    Keys are sorted so repeated runs over identical input produce byte-identical
    artifacts, which is what makes the golden tests in P6 meaningful.
    """
    if isinstance(payload, BaseModel):
        data: Any = payload.model_dump(mode="json")
    elif isinstance(payload, list) and payload and isinstance(payload[0], BaseModel):
        data = [item.model_dump(mode="json") for item in payload]
    else:
        data = payload
    return json.dumps(data, indent=2, sort_keys=True, default=str, ensure_ascii=False)


def allocate_run_dir(iso_week: str, runs_root: Path = DEFAULT_RUNS_ROOT) -> tuple[Path, str]:
    """Create the next run directory for a week: `runs/<iso_week>-<nn>/`.

    Sequence numbers increment rather than overwrite, so a re-run never destroys the
    evidence from the previous attempt — which matters when diagnosing why a run aborted.
    """
    runs_root.mkdir(parents=True, exist_ok=True)

    used = set()
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        if (match := _RUN_ID_RE.match(child.name)) and match.group("week") == iso_week:
            used.add(int(match.group("seq")))

    sequence = max(used) + 1 if used else 1
    run_id = f"{iso_week}-{sequence:02d}"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir, run_id


def start_run(
    iso_week: str | None = None,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    *,
    mcp_mode: str = "none",
) -> RunContext:
    """Allocate a run directory and open a fresh ledger."""
    week = iso_week or current_iso_week()
    run_dir, run_id = allocate_run_dir(week, runs_root)

    ledger = RunLedger.model_validate(
        {
            "run_id": run_id,
            "iso_week": week,
            "started_at": datetime.now(UTC),
            "status": "running",
            "mcp_mode": mcp_mode,
        }
    )
    context = RunContext(run_dir, ledger)
    context.save_ledger()
    log.info("run started", extra={"run_dir": str(run_dir), "iso_week": week})
    return context


def open_run(run_dir: Path) -> RunContext:
    """Reopen an existing run directory for replay (`--from`) or re-validation."""
    if not run_dir.is_dir():
        raise RunError(f"Not a run directory: {run_dir}")

    ledger_path = run_dir / LEDGER_FILENAME
    if not ledger_path.exists():
        raise RunError(f"No {LEDGER_FILENAME} in {run_dir}; this is not a run directory.")

    try:
        ledger = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise RunError(f"Corrupt ledger at {ledger_path}:\n{exc}") from exc

    return RunContext(run_dir, ledger)


def latest_run_dir(runs_root: Path = DEFAULT_RUNS_ROOT) -> Path | None:
    """Most recently allocated run directory, or None if there are none."""
    if not runs_root.is_dir():
        return None
    candidates = [
        child for child in runs_root.iterdir() if child.is_dir() and _RUN_ID_RE.match(child.name)
    ]
    return max(candidates, key=lambda path: path.name) if candidates else None
