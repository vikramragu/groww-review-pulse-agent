"""Command-line entrypoint (P0-T5, P0-T8, P1-T9).

Commands match architecture.md §14. `doctor`, `themes`, `version`, and `run` (stages 1-3)
do real work. `validate` and `contract check` are wired with their full option surface and
fail with a message naming the phase that implements them, so later phases fill in
behaviour rather than renegotiating the interface.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

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
from pulse.llm import LlmError, list_groq_models, probe_schema_support
from pulse.models import Theme
from pulse.orchestrator import (
    DEFAULT_RUNS_ROOT,
    STAGE_NAMES,
    RunContext,
    RunError,
    current_iso_week,
    latest_run_dir,
    open_run,
    start_run,
)
from pulse.pipeline import (
    FIRST_STAGE,
    LAST_STAGE,
    PipelineError,
    PipelineOptions,
    StageNotImplemented,
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


def _not_implemented(what: str, phase: str) -> typer.Exit:
    """Fail clearly for a command whose phase has not landed yet."""
    typer.secho(f"{what} is not implemented yet.", fg=typer.colors.YELLOW, err=True)
    typer.secho(
        f"It arrives in {phase} — see docs/implementation-plan.md.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    return typer.Exit(code=2)


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
        typer.Option("--week", help="Target ISO week, e.g. 2026-W41. Default: current week."),
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
    # --dry-run stops before the two MCP stages. Until Phase 4 that is the only mode that
    # can succeed anyway, but making it explicit keeps the flag honest once they land.
    last = to_stage or (8 if dry_run else LAST_STAGE)

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
    )

    # Replaying a later stage must reuse the run that produced the earlier artifacts;
    # starting a fresh run would find an empty directory and fail confusingly.
    if first > FIRST_STAGE or run_dir is not None:
        context = _open_run_for_replay(run_dir)
    else:
        context = start_run(target_week, mcp_mode="none" if dry_run else mcp.value)

    typer.secho(
        f"run {context.run_id}  week {target_week}  stages {first}-{last}",
        fg=typer.colors.CYAN,
        bold=True,
    )

    try:
        run_stages(context, options)
    except StageNotImplemented as exc:
        context.finish("partial", abort_reason=str(exc))
        typer.secho(f"\n{exc}", fg=typer.colors.YELLOW, err=True)
        _print_artifacts(context)
        raise typer.Exit(code=2) from exc
    # ConfigError belongs in this list: a misconfiguration discovered *during* a stage
    # (an unchosen model, say) must still finalize the ledger. Without it the run is left
    # recorded as "running" forever, indistinguishable from a crashed process — and
    # architecture.md §15's partial-failure handling depends on the ledger being truthful
    # about what completed.
    except (PipelineError, IngestError, PiiLeakError, LlmError, ConfigError) as exc:
        context.finish("aborted", abort_reason=str(exc))
        typer.secho(f"\nRun aborted: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    context.finish("ok")
    typer.secho(f"\nRun {context.run_id} complete.", fg=typer.colors.GREEN, bold=True)
    _print_artifacts(context)


def _resolve_week(week: str | None) -> str:
    """Validate an explicit --week, or fall back to the current ISO week."""
    if week is None:
        return current_iso_week()
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
    target = run_dir or DEFAULT_RUNS_ROOT
    if run_dir is not None and not run_dir.is_dir():
        typer.secho(f"Not a directory: {target}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    raise _not_implemented("pulse validate", "Phase 3 (P3-T12 through P3-T17)")


@contract_app.command("check")
def contract_check() -> None:
    """Verify the MCP servers' tool schemas against contract/pulse-mcp-1.json."""
    _load_or_exit()
    raise _not_implemented("pulse contract check", "Phase 4 (P4-T14)")


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
