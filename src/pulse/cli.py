"""Command-line entrypoint. Commands match architecture.md §14."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import anyio
import typer

from pulse import __version__
from pulse import logging as pulse_logging
from pulse.analysis.scrub import PiiLeakError
from pulse.config import (
    DEFAULT_ENV_FILE,
    ENV_EXAMPLE_FILE,
    ConfigError,
    LlmProvider,
    Settings,
    load_settings,
)
from pulse.doctor import CheckStatus, run_checks
from pulse.ingest.base import IngestError
from pulse.ingest.normalize import iso_week_start, weeks_in_window
from pulse.ingest.play_store import (
    MAX_PAGES,
    PAGE_DELAY_SECONDS,
    FetchError,
    default_export_path,
    fetch_window,
    prune_old_exports,
    write_export,
)
from pulse.llm import LlmError, list_groq_models, probe_schema_support
from pulse.mcp.adapters import ConformanceResult, check_conformance
from pulse.models import Theme
from pulse.orchestrator import (
    STAGE_NAMES,
    RunContext,
    RunError,
    last_complete_iso_week,
    latest_run_dir,
    open_run,
    start_run,
)
from pulse.pipeline import (
    DOC_STAGE,
    EMAIL_STAGE,
    FIRST_STAGE,
    LAST_STAGE,
    PipelineError,
    PipelineOptions,
    run_stages,
)

ISO_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")

app = typer.Typer(
    name="pulse",
    help="Turn public Play Store reviews of the Groww app into a weekly one-page pulse.",
    no_args_is_help=True,
    add_completion=False,
)

contract_app = typer.Typer(name="contract", help="Inspect the pulse-mcp tool contract.")
app.add_typer(contract_app)


class McpMode(StrEnum):
    """Which MCP servers to talk to."""

    LIVE = "live"
    STUB = "stub"


def _load_or_exit(directory: Path | None = None) -> Settings:
    try:
        return load_settings(directory)
    except ConfigError as exc:
        typer.secho(f"Configuration error:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Emit debug-level logs.")
    ] = False,
    json_logs: Annotated[
        bool, typer.Option("--json-logs", help="Emit logs as JSON lines on stderr.")
    ] = False,
) -> None:
    """Configure logging before any subcommand runs."""
    pulse_logging.configure(
        json_output=json_logs,
        level=logging.DEBUG if verbose else logging.INFO,
    )


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("init-env")
def init_env(
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing .env. Destroys any key in it."),
    ] = False,
) -> None:
    """Create a `.env` from `.env.example` for you to paste your API key into.

    Refuses to overwrite an existing `.env` unless `--force` is passed: silently clobbering
    a file that holds a working credential would be a genuinely bad day, and the recovery
    is "go and generate a new key".
    """
    template = Path(ENV_EXAMPLE_FILE)
    target = Path(DEFAULT_ENV_FILE)

    if not template.is_file():
        typer.secho(
            f"{template} is missing — cannot create {target} from it.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if target.exists() and not force:
        typer.secho(f"{target} already exists; leaving it alone.", fg=typer.colors.YELLOW)
        has_placeholder = "gsk_your_key_here" in target.read_text(encoding="utf-8")
        if has_placeholder:
            typer.echo("It still contains the placeholder key. Edit it and paste your own.")
        else:
            typer.echo("Run `pulse doctor` to check whether it is picked up.")
        return

    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    typer.secho(f"Created {target.resolve()}", fg=typer.colors.GREEN, bold=True)
    typer.echo("")
    typer.echo("Next: open it and replace gsk_your_key_here with your Groq key.")
    typer.echo("Get one at https://console.groq.com -> API Keys")
    typer.echo("")
    typer.secho(
        "It is gitignored, so it will not be committed. Do not paste it into a chat or "
        "a commit message.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def fetch(
    week: Annotated[
        str | None,
        typer.Option("--week", help="Target ISO week. Default: last complete week."),
    ] = None,
    weeks: Annotated[
        int | None,
        typer.Option(
            "--weeks", help="How many weeks back to fetch. Default: the configured window."
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Where to write the export. Default: data/raw/<app>-<today>.json"
        ),
    ] = None,
    max_reviews: Annotated[
        int | None, typer.Option("--max-reviews", help="Second ceiling on the fetch.")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch one page and report, without writing a file."),
    ] = False,
) -> None:
    """Download public Play Store reviews into a local export file.

    Separate from `pulse run` on purpose: the pipeline reads files and never the network
    (see src/pulse/ingest/play_store.py). Fetching once and archiving the result means
    re-running the pipeline costs nothing and stays reproducible.

    Reads data any visitor can see, with no account and no login. It does use Google's
    undocumented endpoints -- the trade-off is recorded in architecture.md O1 and §19
    rather than hidden. For commercial use, prefer a licensed provider.
    """
    settings = _load_or_exit()
    play = settings.pulse.play_store

    try:
        target_week = _resolve_week(week)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    span = weeks or settings.pulse.window.weeks
    window = weeks_in_window(target_week, span)
    window_start = iso_week_start(window[0])

    typer.secho(
        f"fetching {play.app_id}  weeks {window[0]}..{window[-1]}  "
        f"(from {window_start.isoformat()})",
        fg=typer.colors.CYAN,
        bold=True,
    )
    typer.secho(
        f"newest-first, stopping at the window start; paced "
        f"{PAGE_DELAY_SECONDS:.0f}s between pages",
        fg=typer.colors.BRIGHT_BLACK,
    )

    try:
        rows, report = fetch_window(
            window_start=window_start,
            app_id=play.app_id,
            lang=play.lang,
            country=play.country,
            max_reviews=1 if dry_run else (max_reviews or play.max_reviews),
            max_pages=1 if dry_run else MAX_PAGES,
        )
    except FetchError as exc:
        typer.secho(f"\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("")
    typer.echo(f"  pages fetched     {report.pages}")
    typer.echo(f"  reviews seen      {report.reviews_seen}")
    typer.echo(f"  reviews kept      {report.reviews_kept}")
    typer.echo(f"  skipped, no text  {report.skipped_no_text}")
    typer.echo(f"  date range        {report.oldest_kept} .. {report.newest_kept}")
    typer.echo(f"  stopped because   {report.stopped_because}")
    if report.identity_keys_dropped:
        typer.secho(
            f"  dropped at source {', '.join(sorted(report.identity_keys_dropped))} "
            f"(never written to the export)",
            fg=typer.colors.BRIGHT_BLACK,
        )

    if dry_run:
        typer.secho("\nDry run: nothing written.", fg=typer.colors.YELLOW)
        return

    if not rows:
        typer.secho(
            "\nNo reviews in the window; nothing written.", fg=typer.colors.YELLOW, err=True
        )
        raise typer.Exit(code=1)

    target = out or default_export_path(
        settings.pulse.input.path, datetime.now(UTC).date(), play.app_id
    )
    written = write_export(rows, target)

    typer.secho(f"\nWrote {len(rows)} reviews to {written}", fg=typer.colors.GREEN, bold=True)
    # O6: exports fetched before the window can no longer feed a run, only hold raw text.
    for pruned in prune_old_exports(written.parent, window_start, play.app_id):
        typer.secho(f"Deleted {pruned} (fetched before the window)", fg=typer.colors.BRIGHT_BLACK)
    typer.echo("")
    typer.echo("Next:")
    typer.echo(f"  pulse run --dry-run --input {written} --week {target_week}")


@app.command()
def doctor(
    input_path: Annotated[
        Path | None,
        typer.Option("--input", help="Review export path to check instead of the configured one."),
    ] = None,
) -> None:
    """Check the environment: Python, config, credentials, input, MCP servers.

    Run this first. It reports what is not yet ready rather than letting a run discover it
    halfway through. Exits non-zero if any check fails; warnings do not fail the command.
    """
    results = run_checks(input_override=input_path)

    width = max(len(result.name) for result in results)
    for result in results:
        colour = {
            CheckStatus.OK: typer.colors.GREEN,
            CheckStatus.WARN: typer.colors.YELLOW,
            CheckStatus.FAIL: typer.colors.RED,
            CheckStatus.SKIP: typer.colors.BLUE,
        }[result.status]
        typer.secho(f"{result.status.value:<5}", fg=colour, nl=False, bold=True)
        typer.echo(f" {result.name.ljust(width)}  {result.detail}")
        if result.hint:
            typer.secho(f"      -> {result.hint}", fg=typer.colors.BRIGHT_BLACK)

    failures = [r for r in results if r.status is CheckStatus.FAIL]
    warnings = [r for r in results if r.status is CheckStatus.WARN]

    typer.echo("")
    if failures:
        typer.secho(
            f"{len(failures)} check(s) failed, {len(warnings)} warning(s).",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"All checks passed ({len(warnings)} warning(s)).", fg=typer.colors.GREEN, bold=True
    )


@app.command()
def themes() -> None:
    """List the configured theme taxonomy."""
    settings = _load_or_exit()
    typer.secho(f"Taxonomy version {settings.taxonomy_version}", bold=True)
    for theme in Theme:
        definition = settings.themes.definition(theme)
        typer.secho(f"\n  {theme.value}", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"    {definition.label}")
        typer.echo(f"    {definition.covers}")


@app.command()
def models(
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Also send one tiny request per model to test json_schema support.",
        ),
    ] = False,
) -> None:
    """List the models your provider account offers, and optionally test schema support.

    Which Groq models accept a `json_schema` response format is model-dependent, and the
    published accounts of it disagree — so the model is chosen by asking the account rather
    than by assuming (architecture.md D6a). `--probe` spends a few tokens per model to find
    out for certain.
    """
    settings = _load_or_exit()
    provider = settings.pulse.llm.provider

    if provider is not LlmProvider.GROQ:
        typer.secho(
            f"Provider is {provider.value!r}; nothing to list. "
            f'Set `[llm] provider = "groq"` to use this command.',
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        listed = list_groq_models()
    except LlmError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not listed:
        typer.secho("The account returned no models.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"{len(listed)} model(s) available:\n", bold=True)
    for name in listed:
        if not probe:
            typer.echo(f"  {name}")
            continue

        outcome = probe_schema_support(name)
        colour = typer.colors.GREEN if outcome.supported else typer.colors.YELLOW
        typer.secho(f"  {'schema' if outcome.supported else 'json  '}", fg=colour, nl=False)
        typer.echo(f"  {name}" + (f"  ({outcome.detail})" if outcome.detail else ""))

    typer.echo("")
    typer.secho(
        "Set the chosen model in config/pulse.toml under [llm] model, or export PULSE_LLM_MODEL.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def run(
    week: Annotated[
        str | None,
        typer.Option("--week", help="Target ISO week, e.g. 2026-W41. Default: last complete week."),
    ] = None,
    input_path: Annotated[
        Path | None, typer.Option("--input", help="Review export file or directory.")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Stages 1-8 only; writes pulse.md and never touches MCP."),
    ] = False,
    mcp: Annotated[
        McpMode, typer.Option("--mcp", help="Which MCP servers to publish through.")
    ] = McpMode.LIVE,
    from_stage: Annotated[
        int | None,
        typer.Option("--from", help="Replay from this stage using stored artifacts (1-10)."),
    ] = None,
    to_stage: Annotated[
        int | None, typer.Option("--to", help="Stop after this stage (1-10).")
    ] = None,
    run_dir: Annotated[
        Path | None,
        typer.Option(
            "--run-dir",
            help="Run directory to replay into. With --from >1, defaults to the latest run.",
        ),
    ] = None,
    llm: Annotated[
        str | None,
        typer.Option("--llm", help="Override the configured LLM provider (groq|fake|none)."),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache", help="Ignore the classification cache and re-label every review."
        ),
    ] = False,
    republish: Annotated[
        bool,
        typer.Option(
            "--republish",
            help="Append the week to the doc and draft the email even if already published.",
        ),
    ] = False,
) -> None:
    """Run the pipeline: ingest, theme, draft, validate, publish, draft the email."""
    for label, value in (("--from", from_stage), ("--to", to_stage)):
        if value is not None and value not in STAGE_NAMES:
            typer.secho(
                f"{label} must be a stage between 1 and 10, got {value}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
    if from_stage is not None and to_stage is not None and from_stage > to_stage:
        typer.secho(
            f"--from ({from_stage}) is after --to ({to_stage}).", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    settings = _load_or_exit()

    first = from_stage or FIRST_STAGE
    # --dry-run stops before the two MCP stages and never touches a server.
    last = to_stage or (8 if dry_run else LAST_STAGE)
    mcp_mode: Literal["none", "stub", "live"] = (
        "none" if dry_run else ("stub" if mcp is McpMode.STUB else "live")
    )

    try:
        target_week = _resolve_week(week)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    resolved_input = input_path or settings.pulse.input.path
    options = PipelineOptions(
        settings=settings,
        input_path=resolved_input,
        target_week=target_week,
        from_stage=first,
        to_stage=last,
        llm_override=llm,
        use_cache=not no_cache,
        mcp_mode=mcp_mode,
        republish=republish,
    )

    # Replaying a later stage must reuse the run that produced the earlier artifacts;
    # starting a fresh run would find an empty directory and fail confusingly.
    if first > FIRST_STAGE or run_dir is not None:
        context = _open_run_for_replay(run_dir)
        if last >= DOC_STAGE:
            context.ledger.mcp_mode = mcp_mode
    else:
        context = start_run(target_week, mcp_mode=mcp_mode)

    typer.secho(
        f"run {context.run_id}  week {target_week}  stages {first}-{last}",
        fg=typer.colors.CYAN,
        bold=True,
    )

    try:
        run_stages(context, options)
    # ConfigError belongs in this list: a misconfiguration discovered *during* a stage
    # (an unchosen model, say) must still finalize the ledger. Without it the run is left
    # recorded as "running" forever, indistinguishable from a crashed process — and
    # architecture.md §15's partial-failure handling depends on the ledger being truthful
    # about what completed.
    except (PipelineError, IngestError, PiiLeakError, LlmError, ConfigError, RunError) as exc:
        if _published_but_draft_failed(context):
            context.finish("partial", abort_reason=str(exc))
            typer.secho(
                f"\nPartial success. Document published: {context.ledger.document_url}"
                f"\nThe Gmail draft failed: {exc}"
                f"\nRetry just the draft:  pulse run --from {EMAIL_STAGE} --mcp {mcp.value} "
                f"--run-dir {context.run_dir}",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1) from exc
        context.finish("aborted", abort_reason=str(exc))
        typer.secho(f"\nRun aborted: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    context.finish("ok")
    typer.secho(f"\nRun {context.run_id} complete.", fg=typer.colors.GREEN, bold=True)
    # What *this* run did, from the ledger -- not the stage artifact, which a skip leaves
    # holding the earlier run's outcome on purpose.
    outcomes = {record.stage: record.detail for record in context.ledger.stages}
    for stage, label in ((DOC_STAGE, "Document"), (EMAIL_STAGE, "Gmail draft")):
        if (record := outcomes.get(stage)) is not None:
            where = record.get("url") or record.get("draft_id") or record.get("document_url")
            typer.echo(f"{label}: {record.get('status', 'done')}  {where or ''}".rstrip())
    _print_artifacts(context)


def _published_but_draft_failed(context: RunContext) -> bool:
    """Stage 9 succeeded and stage 10 did not: keep the doc, report partial (§15)."""
    stages = context.ledger.stages
    return bool(
        context.ledger.document_url
        and stages
        and stages[-1].stage == EMAIL_STAGE
        and stages[-1].status == "failed"
    )


def _resolve_week(week: str | None) -> str:
    """Validate an explicit --week, or fall back to the last complete ISO week."""
    if week is None:
        return last_complete_iso_week()
    candidate = week.strip().upper()
    if not ISO_WEEK_RE.match(candidate):
        raise ValueError(f"--week must look like 2026-W41, got {week!r}.")
    return candidate


def _open_run_for_replay(run_dir: Path | None) -> RunContext:
    target = run_dir or latest_run_dir()
    if target is None:
        typer.secho(
            "No existing run to replay into. Run the earlier stages first, or drop --from.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        return open_run(target)
    except RunError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _print_artifacts(context: RunContext) -> None:
    stages = context.completed_stages()
    if not stages:
        return
    typer.echo(f"Artifacts in {context.run_dir}/")
    for stage in stages:
        path = context.artifact_path(stage)
        typer.echo(f"  {path.name}  ({path.stat().st_size / 1024:.1f} KiB)")


@app.command()
def validate(
    run_dir: Annotated[
        Path | None,
        typer.Argument(help="Run directory to re-validate. Defaults to the most recent."),
    ] = None,
) -> None:
    """Re-run the validation gate against a stored note."""
    settings = _load_or_exit()
    context = _open_run_for_replay(run_dir)

    options = PipelineOptions(
        settings=settings,
        input_path=settings.pulse.input.path,
        target_week=context.iso_week,
        from_stage=7,
        to_stage=7,
    )

    typer.secho(f"re-validating {context.run_id}", fg=typer.colors.CYAN, bold=True)
    try:
        run_stages(context, options)
    except (PipelineError, RunError) as exc:
        typer.secho(f"\nValidation failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    record = context.ledger.stages[-1]
    typer.secho(
        f"The stored note still passes every check ({record.detail.get('word_count')} words).",
        fg=typer.colors.GREEN,
        bold=True,
    )


@contract_app.command("check")
def contract_check() -> None:
    """Check the stub and the live server against the contract file.

    For each server: its name and version, that every tool the agent uses is advertised,
    that each accepts our example request, and that each still declares the response
    fields we read. An unreachable server is reported, not failed. A rejected token,
    a mismatch, or an incompatibility fails the command.
    """
    settings = _load_or_exit()

    targets = [("live", settings.mcp.live)]
    if settings.mcp.stub is not None:
        targets.insert(0, ("stub", settings.mcp.stub))

    results: list[ConformanceResult] = []
    for label, config in targets:
        result = anyio.run(partial(check_conformance, settings, config, label=label))
        results.append(result)
        colour, word = {
            "ok": (typer.colors.GREEN, "OK"),
            "fail": (typer.colors.RED, "FAIL"),
            "unavailable": (typer.colors.YELLOW, "SKIP"),
        }[result.status]
        typer.secho(f"{word:<5}", fg=colour, bold=True, nl=False)
        typer.echo(f" {label}")
        for line in result.lines:
            typer.echo(f"      {line}")

    if any(r.status == "fail" for r in results):
        raise typer.Exit(code=1)


def entrypoint() -> None:
    """Console-script wrapper that keeps tracebacks out of normal failure paths."""
    try:
        app()
    except ConfigError as exc:  # pragma: no cover - defensive
        typer.secho(f"Configuration error:\n{exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("COLUMNS", "100")
    entrypoint()
