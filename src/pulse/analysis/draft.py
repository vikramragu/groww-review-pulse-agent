"""Stage 6 + 7: draft the note and gate it, regenerating on failure (P3-T8, P3-T9, P3-T16).

The one place in the pipeline where a model writes prose that gets published, and
therefore the one place that needs a loop rather than a call. The loop is bounded at
`max_attempts` and every retry carries the **specific** failures: "try again" produces a
differently-wrong note, while naming the fault produces a better one.

Three outcomes, and the difference between them matters:

* **pass** — the note cleared every check and may be published.
* **exhausted** — `max_attempts` attempts all failed validation. The run aborts without
  publishing; a non-compliant note is worse than no note.
* **fatal** — PII reached the rendered note. Aborts *immediately*, without retrying,
  because that is a stage 3 defect and regenerating would be treating a scrubber bug as a
  drafting problem (architecture.md §9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pulse import logging as pulse_logging
from pulse.config import Settings
from pulse.llm import LlmPort, LlmResponseInvalid, json_schema_for, parse_as
from pulse.models import PulseNote, QuoteCandidate, Theme, ThemeStats
from pulse.prompts.draft import (
    DRAFT_SYSTEM_PROMPT,
    render_draft_input,
    render_retry_feedback,
)
from pulse.rendering.render import render_note
from pulse.rendering.validate import ValidationResult, validate_note

log = pulse_logging.get_logger("analysis.draft")

SCHEMA_NAME = "weekly_pulse_note"


class DraftError(Exception):
    """Raised when no compliant note could be produced."""


@dataclass
class DraftReport:
    """Counts and failure reasons — never the rejected note text."""

    attempts: int = 0
    accepted_on_attempt: int | None = None
    word_count: int = 0
    provenance_corrections: int = 0
    failures_by_check: dict[str, int] = field(default_factory=dict)
    parse_failures: int = 0
    fatal: bool = False

    def record(self, result: ValidationResult) -> None:
        for failure in result.failures:
            self.failures_by_check[failure.check] = self.failures_by_check.get(failure.check, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "accepted_on_attempt": self.accepted_on_attempt,
            "word_count": self.word_count,
            "provenance_corrections": self.provenance_corrections,
            "parse_failures": self.parse_failures,
            "failures_by_check": dict(sorted(self.failures_by_check.items())),
            "fatal": self.fatal,
        }


@dataclass
class DraftOutcome:
    note: PulseNote
    markdown: str
    result: ValidationResult
    report: DraftReport
    history: list[ValidationResult] = field(default_factory=list)


def draft_note(
    stats: list[ThemeStats],
    *,
    llm: LlmPort,
    settings: Settings,
    iso_week: str,
    corpus_size: int,
    corpus_total: int | None = None,
    generated_on: date | None = None,
) -> DraftOutcome:
    """Generate a note and regenerate until it validates or the budget runs out."""
    llm_config = settings.pulse.llm
    max_attempts = settings.pulse.draft.max_attempts

    allowed_themes = [theme.theme for theme in stats]
    candidates_by_theme: dict[Theme, list[QuoteCandidate]] = {
        theme.theme: list(theme.quote_candidates) for theme in stats
    }

    base_input = render_draft_input(stats, iso_week=iso_week, corpus_size=corpus_size)
    schema = json_schema_for(PulseNote)

    report = DraftReport()
    history: list[ValidationResult] = []
    feedback = ""

    for attempt in range(1, max_attempts + 1):
        report.attempts = attempt

        try:
            response = llm.generate_json(
                system=DRAFT_SYSTEM_PROMPT,
                user=base_input + feedback,
                schema=schema,
                schema_name=SCHEMA_NAME,
                max_tokens=llm_config.draft_max_tokens,
                reasoning_effort=llm_config.draft_reasoning_effort,
            )
            note = parse_as(response, PulseNote)
        except LlmResponseInvalid as exc:
            # A schema violation is a validation failure like any other: the cardinality
            # constraints live in PulseNote, so "four quotes" arrives here rather than as
            # a passed note with the wrong shape.
            report.parse_failures += 1
            log.warning(
                "draft did not parse; retrying",
                extra={"attempt": attempt, "reason": str(exc)[:200]},
            )
            feedback = render_retry_feedback([f"the reply was rejected: {exc}"], attempt)
            continue

        markdown = render_note(
            note,
            corpus_size=corpus_size,
            corpus_total=corpus_total,
            generated_on=generated_on,
        )
        result = validate_note(
            note,
            markdown,
            allowed_themes=allowed_themes,
            candidates_by_theme=candidates_by_theme,
            attempt=attempt,
        )
        history.append(result)
        report.record(result)
        report.word_count = result.word_count

        if result.fatal:
            # Not retried: see the module docstring. Stage 3 is the thing to fix.
            report.fatal = True
            log.error("draft rejected with a fatal failure", extra=result.summary())
            raise DraftError("Aborting: " + "; ".join(result.messages()))

        if result.ok:
            # The grounded note, not the model's: its quotes carry the real review_id and
            # rating from the source review rather than whatever the model wrote there.
            note = result.grounded_note or note
            markdown = render_note(note, corpus_size=corpus_size, generated_on=generated_on)
            report.provenance_corrections = result.provenance_corrections
            report.accepted_on_attempt = attempt
            log.info(
                "draft accepted",
                extra={"attempt": attempt, "word_count": result.word_count},
            )
            return DraftOutcome(
                note=note,
                markdown=markdown,
                result=result,
                report=report,
                history=history,
            )

        log.warning(
            "draft failed validation; regenerating",
            extra={
                "attempt": attempt,
                "checks": sorted({f.check for f in result.failures}),
            },
        )
        feedback = render_retry_feedback(result.messages(), attempt)

    failed_checks = sorted(report.failures_by_check)
    raise DraftError(
        f"No compliant note after {max_attempts} attempt(s). "
        f"Failing checks: {', '.join(failed_checks) or 'response never parsed'}. "
        f"A non-compliant note is not published; see 07_validation.json for every "
        f"attempt's failures."
    )
