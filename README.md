# Groww Review Pulse — agent

Turns public **Google Play Store** reviews of the Groww app into a weekly one-page pulse,
publishes it to Google Docs, and creates a Gmail draft — reaching both Google surfaces
through MCP servers it does not own.

This repository is the **agent** (the MCP client and analytical pipeline). The Docs and
Gmail MCP servers live in a separate project and own all Google OAuth, tokens, and REST
calls. See [docs/architecture.md §1.1](docs/architecture.md).

## Documentation

| Document | Contents |
| --- | --- |
| [docs/problemStatement.md](docs/problemStatement.md) | Requirements, constraints, acceptance checklist |
| [docs/architecture.md](docs/architecture.md) | Design, decisions, and the `pulse-mcp/1` tool contract |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Phase-by-phase build plan |
| [docs/input-format.md](docs/input-format.md) | The review export format the file-drop adapter reads |

## Status

**Phases 0–3 complete.** Stages 1–8 run end to end: a review export is ingested,
normalized, deduplicated, window-filtered, scrubbed of PII, classified into the
five-theme taxonomy, ranked, drafted, gated, and rendered as a compliant one-page note.

`pulse run --dry-run` produces `pulse.md`: ≤250 words, 3 themes, 3 verbatim quotes with
verified provenance, 3 action ideas, no PII.

MCP publishing (stages 9–10) arrives in Phase 4. Asking for a later stage exits with a
pointer to the phase that implements it.

## Install

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
pulse doctor            # check environment, config, credentials, input, MCP servers
pulse themes            # show the configured theme taxonomy
pulse models --probe    # list provider models and test their JSON-schema support
pulse version
```

Run `pulse doctor` first — it reports what is not yet ready rather than letting a run
discover it halfway through. Warnings (no review export yet, no draft recipient) do not
fail the command; genuine problems do.

### Running the pipeline

Drop a public Play Store export into `data/raw/` (format:
[docs/input-format.md](docs/input-format.md)), then:

```bash
pulse run --dry-run                           # the whole analysis: stages 1-8
pulse run --to 3                              # stop before anything costs money
pulse run --dry-run --input path/to/export.json
pulse run --dry-run --week 2026-W41           # a specific target week
pulse run --from 6 --to 8                     # redraft from stored artifacts
pulse run --dry-run --no-cache                # re-label every review
pulse validate                                # re-gate the latest run's note
```

Stage 4 is the first stage that calls a model. `pulse run --to 3` never does, so ingestion
and scrubbing work with no credential at all.

Each stage writes a JSON artifact under `runs/<iso-week>-<nn>/`, so stages are
independently replayable and every published quote can later be traced back to the review
it came from:

| Artifact | Contents |
| --- | --- |
| `01_raw.json` | Rows as exported. **The one sensitive artifact** — review bodies are unscrubbed until stage 3. |
| `02_normalized.json` | Typed, deduplicated, window-filtered. No reviewer identity. |
| `03_scrubbed.json` | PII removed from bodies. The first artifact safe to read freely. |
| `04_classified.json` | One theme (or `null`) per review id. No review text. |
| `05_stats.json` | Every theme ranked with its score, plus the three selected and their quote pools. |
| `06_note.json` | The structured note, with quote provenance derived from the source reviews. |
| `07_validation.json` | Every draft attempt and why it was rejected. |
| `pulse.md` | The rendered note — the single source for both the doc and the email bodies. |
| `run.json` | Ledger: per-stage timings, counts, token usage, outcomes. Never review text. |

To try it without an export of your own, use the synthetic fixture:

```bash
pulse run --to 3 --input tests/fixtures/reviews_sample.json --week 2026-W41
```

### Not yet implemented

| Command | Arrives in |
| --- | --- |
| `pulse run` stages 9–10 | Phase 4 |
| `pulse contract check` | Phase 4 |

## Configuration

| File | Purpose |
| --- | --- |
| `config/pulse.toml` | Window, thresholds, draft attempts, doc title convention |
| `config/themes.toml` | The closed 5-theme taxonomy and its version |
| `config/mcp.toml` | MCP server bindings and the contract version |

Environment overrides: `PULSE_WINDOW_WEEKS`, `PULSE_MIN_WINDOW_WEEKS`, `PULSE_INPUT`,
`PULSE_MAX_DRAFT_ATTEMPTS`, `PULSE_RECIPIENT`, `PULSE_CONFIG_DIR`.
Precedence is CLI > environment > TOML > default.

### Language model

Stages 4 (classification) and 6 (drafting) call a model; nothing else does. The provider is
configuration, not architecture — every hard constraint from the brief is enforced in code,
so a different model changes how often the validation gate retries, never whether a
non-compliant note can be published.

Currently **Groq**. Set `GROQ_API_KEY`, then pick a model:

```bash
export GROQ_API_KEY=...
pulse models --probe     # which models enforce a JSON schema varies; this checks
```

Set the result as `[llm] model` in `config/pulse.toml` or `PULSE_LLM_MODEL`. Override the
provider per-run with `--llm groq|fake|none`.

Classification labels are cached on disk in `.cache/classify.json`, keyed by review id,
taxonomy version, model, **and a fingerprint of the prompt** — so a weekly re-run over an
overlapping corpus costs almost nothing, while changing the taxonomy, the model, or a
disambiguation rule invalidates the cache automatically rather than serving labels from a
judge the run is no longer using.

Classifying the 241-review fixture costs about **$0.008** and takes ~2 minutes.

This codebase never handles a Google credential at all.

## Development

```bash
pytest -q          # no network, no MCP servers needed
ruff check .
mypy src
```

## Privacy

Review text is scrubbed of PII before it reaches any model, and the rendered note is
rescanned before publication. `data/raw/` and `runs/` are gitignored; test fixtures are
synthetic. See [docs/architecture.md §10 and §18](docs/architecture.md).
