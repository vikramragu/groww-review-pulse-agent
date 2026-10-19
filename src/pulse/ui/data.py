"""What the dashboard shows, from a local run or from a public snapshot.

**Two sources, one view.** A local run reads `runs/<id>/` directly. The public dashboard
cannot: `runs/` holds review text and is gitignored. So `export_snapshot` writes one JSON
file with what a reader needs and nothing more -- the published note, per-theme figures,
weekly counts, the filter funnel -- and both sources load into the same `PulseView`.

**What a snapshot leaves out, on purpose:** every review id, every quote candidate (those
are other reviewers' text), all review bodies except the three already published, and the
draft id. Export refuses if the PII scanner finds anything.

**Checks are recomputed, not trusted.** A snapshot carries a SHA-256 of its content, and
the checks are re-run on load. The verbatim check is the exception: it needs the candidate
pools the snapshot withholds, so its result is recorded at export and marked as such.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulse.analysis.scrub import scan_for_pii
from pulse.models import (
    ClassifiedReview,
    PulseNote,
    QuoteCandidate,
    Review,
    RunLedger,
    Theme,
    ThemeStats,
    direction_of,
)
from pulse.orchestrator import LEDGER_FILENAME, NOTE_FILENAME, STAGE_ARTIFACTS
from pulse.rendering.render import theme_label
from pulse.rendering.validate import WORD_LIMIT, count_note_words, validate_note

SNAPSHOT_FORMAT = "pulse-snapshot/1"
WITHHELD = "withheld"
UNCLASSIFIED = "unclassified"


class SnapshotError(Exception):
    """A snapshot that cannot be exported safely, or does not verify."""


@dataclass(frozen=True)
class Check:
    """One line of the dashboard's checklist."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ThemeRow:
    theme: str
    label: str
    review_count: int
    share_of_week: float
    delta_vs_baseline: float
    mean_rating: float
    on_page: bool

    @property
    def direction(self) -> str:
        return direction_of(self.delta_vs_baseline)


@dataclass
class PulseView:
    """Everything one dashboard page needs, whichever source it came from."""

    source: str  # "local" or "snapshot"
    run_id: str
    iso_week: str
    note: PulseNote
    markdown: str
    themes: list[ThemeRow]
    week_review_count: int
    baseline_weeks: int
    corpus_size: int | None
    corpus_total: int | None
    window: str | None
    #: One row per (week, theme): {"iso_week", "theme", "count", "share"}.
    weekly: list[dict[str, Any]] = field(default_factory=list)
    #: {"week": {"1": n, ...}, "window": {...}} star counts of substantive reviews.
    ratings: dict[str, dict[str, int]] = field(default_factory=dict)
    funnel: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    exported_at: str | None = None
    run_dir: Path | None = None

    @property
    def word_count(self) -> int:
        return count_note_words(self.markdown)


# --------------------------------------------------------------------------------------
# Checks -- the same gate the pipeline runs, phrased for a reader
# --------------------------------------------------------------------------------------


def run_checks(
    note: PulseNote,
    markdown: str,
    themes: list[ThemeRow],
    candidates_by_theme: dict[Theme, list[QuoteCandidate]] | None,
    *,
    verbatim_at_export: bool | None = None,
) -> list[Check]:
    """Six checks, in the order a reader cares about them.

    With candidate pools (a local run) every check is computed here. Without them (a
    snapshot) the verbatim check reports what export recorded, and says so.
    """
    on_page = [t.theme for t in note.top_themes]
    pools = candidates_by_theme or {
        # Without pools, give each quote a pool of itself, so the gate's other checks run;
        # the verbatim verdict is then taken from export, never from this.
        q.theme: [QuoteCandidate(review_id=f"q{i}", text=q.text, rating=q.rating)]
        for i, q in enumerate(note.quotes)
    }
    result = validate_note(note, markdown, allowed_themes=on_page, candidates_by_theme=pools)
    failed = {f.check for f in result.failures}
    words = result.word_count

    if candidates_by_theme is not None:
        verbatim_ok = not failed & {"verbatim", "quote_distinctness", "quote_theme"}
        verbatim_detail = "3 quotes match real reviews character for character"
    else:
        verbatim_ok = bool(verbatim_at_export)
        verbatim_detail = (
            "checked against the source reviews at export; the pools are withheld here"
        )

    stats = {t.theme: t for t in themes}
    figures_ok = "figures" not in failed and all(
        s.figures is not None
        and s.theme.value in stats
        and s.figures.review_count == stats[s.theme.value].review_count
        and s.figures.share_of_week == stats[s.theme.value].share_of_week
        and s.figures.delta_vs_baseline == stats[s.theme.value].delta_vs_baseline
        for s in note.top_themes
    )

    return [
        Check("word_limit", words <= WORD_LIMIT, f"{words} words (limit {WORD_LIMIT})"),
        Check(
            "three_three_three",
            len(note.top_themes) == len(note.quotes) == len(note.actions) == 3,
            f"{len(note.top_themes)} themes, {len(note.quotes)} quotes, "
            f"{len(note.actions)} actions",
        ),
        Check("quotes_verbatim", verbatim_ok, verbatim_detail),
        Check(
            "no_pii",
            "pii" not in failed,
            "no emails, phone numbers, IDs or names in the note",
        ),
        Check(
            "figures_from_data",
            figures_ok,
            "every number is printed by code from the stats, none written by the model",
        ),
        Check(
            "grounded_in_themes",
            not failed & {"theme_validity", "theme_distinctness", "action_grounding"},
            "3 distinct top themes; every action names one of them",
        ),
    ]


# --------------------------------------------------------------------------------------
# Local runs
# --------------------------------------------------------------------------------------


def list_runs(runs_root: Path) -> list[Path]:
    """Run directories that hold a finished note, most recently updated first."""
    if not runs_root.is_dir():
        return []
    runs = [
        child
        for child in runs_root.iterdir()
        if child.is_dir()
        and (child / NOTE_FILENAME).exists()
        and (child / STAGE_ARTIFACTS[6]).exists()
        and (child / LEDGER_FILENAME).exists()
    ]
    # Most recently updated first: a replay or a publish makes a run current again.
    return sorted(runs, key=lambda p: (p / LEDGER_FILENAME).stat().st_mtime, reverse=True)


def _read(run_dir: Path, stage: int) -> Any:
    path = run_dir / STAGE_ARTIFACTS[stage]
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _theme_rows(stats_payload: dict[str, Any]) -> list[ThemeRow]:
    selected = {s["theme"] for s in stats_payload.get("selected", [])}
    rows = []
    for item in stats_payload.get("ranked", []):
        stats = ThemeStats.model_validate(item)
        rows.append(
            ThemeRow(
                theme=stats.theme.value,
                label=theme_label(stats.theme),
                review_count=stats.review_count,
                share_of_week=stats.share_of_week,
                delta_vs_baseline=stats.delta_vs_baseline,
                mean_rating=stats.mean_rating,
                on_page=stats.theme.value in selected,
            )
        )
    return rows


def _weekly_and_ratings(
    run_dir: Path, iso_week: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Per-week theme counts and star mixes. Counts only: no text leaves this function."""
    reviews_raw, labels_raw = _read(run_dir, 3), _read(run_dir, 4)
    if reviews_raw is None:
        return [], {}
    reviews = [Review.model_validate(r) for r in reviews_raw]
    theme_of: dict[str, str] = {}
    if labels_raw is not None:
        for item in labels_raw:
            label = ClassifiedReview.model_validate(item)
            theme_of[label.review_id] = label.theme.value if label.theme else UNCLASSIFIED

    per_week: dict[str, Counter[str]] = {}
    for review in reviews:
        per_week.setdefault(review.iso_week, Counter())[
            theme_of.get(review.review_id, UNCLASSIFIED)
        ] += 1

    weekly = []
    for week in sorted(per_week):
        total = sum(per_week[week].values())
        for theme in [*(t.value for t in Theme), UNCLASSIFIED]:
            count = per_week[week].get(theme, 0)
            weekly.append(
                {"iso_week": week, "theme": theme, "count": count, "share": count / total}
            )

    def stars(subset: list[Review]) -> dict[str, int]:
        counted = Counter(r.rating for r in subset)
        return {str(star): counted.get(star, 0) for star in range(1, 6)}

    ratings = {
        "week": stars([r for r in reviews if r.iso_week == iso_week]),
        "window": stars(reviews),
    }
    return weekly, ratings


def _funnel(ledger: RunLedger) -> dict[str, Any]:
    """From the ledger's latest stage 2 and 3 records: how 12,800 rows became 1,708."""
    latest = {record.stage: record.detail for record in ledger.stages}
    normalize, scrub = latest.get(2, {}), latest.get(3, {})
    raw_substance = normalize.get("substance")
    substance: dict[str, Any] = raw_substance if isinstance(raw_substance, dict) else {}
    return {
        "rows_exported": normalize.get("rows_in"),
        "duplicates_removed": normalize.get("duplicates_removed"),
        "in_window": substance.get("in_window", normalize.get("reviews_in_window")),
        "substantive": substance.get("kept"),
        "dropped": dict(substance.get("dropped") or {}),
        #: Reviews that had something redacted (and were kept), not a count after stage 3.
        "pii_redacted": scrub.get("reviews_scrubbed"),
        "analysed": ledger.corpus_size,
    }


def _delivery(run_dir: Path) -> dict[str, Any]:
    published, drafted = _read(run_dir, 9) or {}, _read(run_dir, 10) or {}
    return {
        "doc_status": published.get("status"),
        "document_url": published.get("url") or drafted.get("document_url"),
        "draft_status": drafted.get("status"),
        "draft_id": drafted.get("draft_id"),
    }


def _usage(ledger: RunLedger) -> dict[str, Any]:
    usage = ledger.usage
    return {
        # Runs from before per-stage accounting recorded only the last model stage.
        "complete": "classify" in ledger.usage_by_stage or not ledger.theme_counts,
        "model": usage.model,
        "calls": usage.calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "cache_hit_rate": ledger.classification_cache_hit_rate,
    }


def load_run(run_dir: Path) -> PulseView:
    """A local run, with every check computed against its candidate pools."""
    ledger = RunLedger.model_validate_json((run_dir / LEDGER_FILENAME).read_text(encoding="utf-8"))
    note = PulseNote.model_validate(_read(run_dir, 6))
    markdown = (run_dir / NOTE_FILENAME).read_text(encoding="utf-8")
    stats_payload = _read(run_dir, 5) or {}
    report = stats_payload.get("report", {})
    themes = _theme_rows(stats_payload)
    pools = {
        ThemeStats.model_validate(s).theme: ThemeStats.model_validate(s).quote_candidates
        for s in stats_payload.get("selected", [])
    }
    weekly, ratings = _weekly_and_ratings(run_dir, ledger.iso_week)
    return PulseView(
        source="local",
        run_id=ledger.run_id,
        iso_week=ledger.iso_week,
        note=note,
        markdown=markdown,
        themes=themes,
        week_review_count=int(report.get("week_review_count", 0)),
        baseline_weeks=int(report.get("baseline_weeks", 0)),
        corpus_size=ledger.corpus_size,
        corpus_total=ledger.corpus_total,
        window=_funnel_window(ledger),
        weekly=weekly,
        ratings=ratings,
        funnel=_funnel(ledger),
        delivery=_delivery(run_dir),
        usage=_usage(ledger),
        checks=run_checks(note, markdown, themes, pools),
        run_dir=run_dir,
    )


def _funnel_window(ledger: RunLedger) -> str | None:
    latest = {record.stage: record.detail for record in ledger.stages}
    window = latest.get(2, {}).get("window")
    return str(window) if window else None


# --------------------------------------------------------------------------------------
# Snapshots -- the public, read-only form
# --------------------------------------------------------------------------------------


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k not in {"sha256", "exported_at"}}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export_snapshot(run_dir: Path) -> dict[str, Any]:
    """The public snapshot of a run. Raises `SnapshotError` rather than export anything
    unsafe or anything that failed a check."""
    view = load_run(run_dir)
    failing = [c.name for c in view.checks if not c.ok]
    if failing:
        raise SnapshotError(f"Refusing to export a note that fails: {', '.join(failing)}.")

    note = view.note.model_dump(mode="json")
    for quote in note["quotes"]:
        quote["review_id"] = WITHHELD

    payload: dict[str, Any] = {
        "format": SNAPSHOT_FORMAT,
        "run_id": view.run_id,
        "iso_week": view.iso_week,
        "note": note,
        "markdown": view.markdown,
        "themes": [row.__dict__ for row in view.themes],
        "week_review_count": view.week_review_count,
        "baseline_weeks": view.baseline_weeks,
        "corpus_size": view.corpus_size,
        "corpus_total": view.corpus_total,
        "window": view.window,
        "weekly": view.weekly,
        "ratings": view.ratings,
        "funnel": view.funnel,
        "delivery": {
            "doc_status": view.delivery.get("doc_status"),
            # The doc is private to the team; a public page links nowhere useful.
            "document_url": None,
            "draft_status": view.delivery.get("draft_status"),
            "draft_id": WITHHELD if view.delivery.get("draft_id") else None,
        },
        "usage": view.usage,
        "verbatim_at_export": True,
    }

    if hits := scan_for_pii(_public_text(payload)):
        raise SnapshotError(
            "Refusing to export: the PII scanner flagged "
            + "; ".join(hit.describe() for hit in hits)
        )

    payload["exported_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    payload["sha256"] = _digest(payload)
    return payload


def _public_text(payload: dict[str, Any]) -> str:
    """The prose a reader sees, for the PII scan. Numbers are not scanned: the scanner is
    built for review text, and a share like 0.2569444444444444 reads to it as an account
    number."""
    note = payload["note"]
    parts = [payload["markdown"], note["headline"]]
    parts += [s["summary"] for s in note["top_themes"]]
    parts += [q["text"] for q in note["quotes"]]
    parts += [f"{a['action']} {a['rationale']}" for a in note["actions"]]
    parts += [row["label"] for row in payload["themes"]]
    return "\n".join(parts)


def write_snapshot(payload: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{payload['iso_week']}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def latest_snapshot(snapshot_dir: Path) -> Path | None:
    if not snapshot_dir.is_dir():
        return None
    snapshots = sorted(snapshot_dir.glob("*-W*.json"))
    return snapshots[-1] if snapshots else None


def load_snapshot(path: Path) -> PulseView:
    """A snapshot, verified: its digest must match and its checks are recomputed."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError(f"{path} is not a {SNAPSHOT_FORMAT} snapshot.")
    if payload.get("sha256") != _digest(payload):
        raise SnapshotError(f"{path} was modified after export: its SHA-256 does not match.")

    note = PulseNote.model_validate(payload["note"])
    themes = [ThemeRow(**row) for row in payload["themes"]]
    checks = run_checks(
        note,
        payload["markdown"],
        themes,
        None,
        verbatim_at_export=payload.get("verbatim_at_export"),
    )
    return PulseView(
        source="snapshot",
        run_id=payload["run_id"],
        iso_week=payload["iso_week"],
        note=note,
        markdown=payload["markdown"],
        themes=themes,
        week_review_count=payload["week_review_count"],
        baseline_weeks=payload["baseline_weeks"],
        corpus_size=payload["corpus_size"],
        corpus_total=payload["corpus_total"],
        window=payload["window"],
        weekly=payload["weekly"],
        ratings=payload["ratings"],
        funnel=payload["funnel"],
        delivery=payload["delivery"],
        usage=payload["usage"],
        checks=checks,
        exported_at=payload.get("exported_at"),
    )
