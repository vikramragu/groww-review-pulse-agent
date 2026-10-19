# Groww Review Pulse — agent

Turns public **Google Play Store** reviews of the Groww app into a **weekly one-page note**
(top 3 themes, 3 verbatim quotes, 3 action ideas, at most 250 words). It appends the note
to a **Google Doc** and creates a **Gmail draft**, never a sent email, through an
**MCP server**.

This repository is the **agent**: the analysis pipeline and the MCP client. It holds no
Google credential and has no Google SDK (a test enforces both). Google Docs and Gmail are
reached through a separate server,
[google-workspace-mcp](https://github.com/vikramragu/Mail-Docs-MCP-Server), which owns
OAuth and the Google API calls.

```text
Play Store export ──▶ agent (this repo) ──MCP over HTTPS + bearer token──▶ google-workspace-mcp ──▶ Google Docs / Gmail
                      stages 1-8: analysis                                   (Railway)
                      stages 9-10: publish
```

## Contents

- [Install](#install)
- [Configure](#configure)
- [Get review data](#get-review-data)
- [Run](#run)
- [Read the output](#read-the-output)
- [Troubleshoot](#troubleshoot)
- [Development](#development)
- [Documentation](#documentation)

## Install

Requires Python 3.11 or later.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,scraper to use `pulse fetch`
pulse init-env                    # creates .env from .env.example
```

## Configure

### Secrets: `.env`

`.env` is gitignored. Real environment variables take precedence over it.

| Variable | What it is |
| --- | --- |
| `GROQ_API_KEY` | For stages 4 (classify) and 6 (draft). From [console.groq.com](https://console.groq.com) |
| `PULSE_MCP_TOKEN` | Bearer token for the MCP server. Must equal the server's `MCP_AUTH_TOKEN` |
| `PULSE_RECIPIENT` | Who the Gmail draft is addressed to |
| `PULSE_DOC_ID` | Optional. Overrides the doc in `config/pulse.toml`, e.g. to test against a scratch doc |

To make a token, run `openssl rand -hex 32` and set the same value as `MCP_AUTH_TOKEN` on
the server and `PULSE_MCP_TOKEN` here. Redeploy the server after changing it. Never paste a
token into a commit, a chat or a doc.

### The MCP server (Railway)

The agent needs `ENABLE_DOCS_APPEND` and `ENABLE_GMAIL_DRAFTS` on (the defaults). Set
`ENABLE_GMAIL_SEND=false`: the agent refuses to call a send tool anyway, and with it off a
leaked token cannot send mail as you. `ALLOWED_RECIPIENT_DOMAINS` and
`ALLOWED_DOCUMENT_IDS` narrow what a leaked token could do further.

### The Google Doc

The server cannot create or find documents, so the doc is created once by hand, in the
Google account the server is logged into. Each week is **appended** to it as a section
headed `Groww Review Pulse — <week>`. Put its ID (the part of the URL between `/d/` and
`/edit`) in `config/pulse.toml`:

```toml
[docs]
document_id = "1z2t…"
```

### Configuration files

| File | Purpose |
| --- | --- |
| `config/pulse.toml` | Window, thresholds, model, draft attempts, the doc, raw-data retention |
| `config/themes.toml` | The closed 5-theme taxonomy and its version |
| `config/mcp.toml` | The server URL, tool names, and the in-repo stub |

Then check everything:

```bash
pulse doctor            # every check; warnings do not fail it, problems do
pulse contract check    # the live server's tools still accept what the agent sends
```

## Get review data

```bash
pulse fetch --dry-run   # one page, report only
pulse fetch             # the configured window into data/raw/
```

`pulse fetch` downloads public Play Store reviews, newest first, and drops reviewer
identity before writing. It is deliberately not a pipeline stage, so the pipeline itself
never touches the network for data. There is no official review export for a non-owner of
the app, so it reads Google's public but undocumented endpoints;
[architecture.md O1](docs/architecture.md) explains that trade-off. Any file matching
[the export format](docs/input-format.md) works instead.

After writing a new export, `pulse fetch` deletes its own exports fetched before the window.
Files you dropped in by hand are never touched.

## Run

```bash
pulse run                       # the full weekly run: last complete week, publish live
pulse run --dry-run             # stages 1-8 only: writes pulse.md, touches no server
pulse run --week 2026-W41       # a specific ISO week
pulse run --mcp stub            # publish to the in-repo stub server instead
```

Before stage 1, a live run connects to the server, checks its name, version and tools, and
reads the configured doc. A wrong token, a missing doc or a server that is down therefore
stops the run **before** any model call costs money.

Each stage writes an artifact, so any stage can be replayed:

```bash
pulse run --from 6 --run-dir runs/2026-W41-02     # redraft from stored stats
pulse run --from 9 --run-dir runs/2026-W41-02     # publish an existing note
pulse run --from 10 --run-dir runs/2026-W41-02    # retry only the Gmail draft
pulse validate                                     # re-check the latest note
```

**Re-runs don't duplicate.** A week already appended to the doc is skipped, and so is a
week already drafted for that doc. `--republish` overrides both on purpose. This record is
kept locally in `runs/`, so a fresh checkout does not know which weeks were published.

### What it costs

Classifying the real 12-week corpus (about 1,700 reviews) is about 43 model calls, and
labels are cached in `.cache/classify.json`. A weekly run over an overlapping window
therefore classifies only new reviews. Drafting is one call, more if the validation gate
rejects a draft. Each run ends with a line of calls, tokens and cost.

## Read the output

Each run lives in `runs/<week>-<nn>/`:

| File | Contents |
| --- | --- |
| `01_raw.json` | Rows as exported, **unscrubbed**. Deleted once the run succeeds; kept if it fails |
| `02_normalized.json` | Typed, deduplicated, in-window, substantive (English, more than 8 words) |
| `03_scrubbed.json` | Personal details removed. The first artifact safe to share |
| `04_classified.json` | A theme (or none) per review id. No review text |
| `05_stats.json` | Every theme ranked, with figures and quote candidates |
| `06_note.json` | The note as structured data |
| `07_validation.json` | Every draft attempt and why it was rejected |
| `pulse.md` | The note as published |
| `09_published.json` / `10_draft.json` | Where it went: doc URL, draft id, appended or skipped |
| `run.json` | The ledger: per-stage timings and outcomes, counts, model usage per stage. Never review text |

What the note's numbers mean:

- **"1,708 substantive reviews of 8,812 in window"**: themes are measured on reviews of
  more than 8 English words. Short reviews are mostly praise and long ones mostly
  complaints, so these figures describe what people complain about, not overall sentiment.
- **"(37 reviews, 25.7% of week, +5.6 pts vs prior 11 wks, 2.6★)"**: printed by code from
  the data, never written by the model. "+5.6 pts" is the change in the theme's share of
  the week against the previous weeks, in percentage points. ▲ / ▼ / → means up, down, or
  within one point.
- **Quotes** are checked character for character against real (scrubbed) reviews. Their
  star rating comes from that review, not from the model.
- **Themes are ranked**, not only counted: how negative a theme is and how fast it is
  rising both count, so the biggest theme is not always first.

## Troubleshoot

`pulse doctor` names most problems directly. Common ones:

| Symptom | Fix |
| --- | --- |
| `rejected our token (HTTP 401)` | `PULSE_MCP_TOKEN` ≠ the server's `MCP_AUTH_TOKEN`, or the server was not redeployed after the change |
| `could not be reached` | Server down or asleep: open `<server>/healthz`, check Railway |
| `Document not found or not shared` / `No edit access` | Wrong doc ID, or the doc isn't in or shared with the server's Google account, or it isn't on `ALLOWED_DOCUMENT_IDS` |
| `Google authorization required` | The server's Google login expired. Re-authorize it ([runbook](docs/runbook.md#when-a-run-fails)); if it happens weekly, its OAuth app is in Testing status |
| `Recipient domain not allowed` | `PULSE_RECIPIENT` is outside the server's `ALLOWED_RECIPIENT_DOMAINS`. The doc is already published; fix it and run `--from 10` |
| `Only N review(s) in <week>` | That week is too thin to report on. Check `--week`, or fetch fresh data |
| `No compliant note after 3 attempt(s)` | See `07_validation.json` for what the model kept getting wrong. Nothing was published |
| Warning that the server exposes `gmail_send_email` | `ENABLE_GMAIL_SEND` is on again on Railway. The agent will not call it, but turn it off |
| Week shows `skipped` | Already published to this doc. That's intended; use `--republish` to add it again |

The weekly routine and how to recover from each failure are in
[docs/runbook.md](docs/runbook.md).

## Development

```bash
pytest -q                       # offline: no network, no real server, no tokens
ruff check . && ruff format --check . && mypy src
UPDATE_GOLDEN=1 pytest tests/test_golden.py    # after an intended change to the note
```

The tests run the MCP layer against an in-repo stub of the real server
(`tests/stubs/stub_mcp_server.py`), over both stdio and HTTP. `tests/test_golden.py` runs
the whole pipeline end to end, offline, against `tests/fixtures/pulse_golden.md`.

Test fixtures are synthetic. Never commit real reviews: `data/raw/` and `runs/` are
gitignored.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/problemStatement.md](docs/problemStatement.md) | Requirements, constraints, acceptance checklist |
| [docs/architecture.md](docs/architecture.md) | Design and decisions; §11 the MCP layer, §15 failure modes |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Phase-by-phase build log |
| [docs/project-context.md](docs/project-context.md) | Handoff summary: status, decisions, what's pending |
| [docs/runbook.md](docs/runbook.md) | Weekly operation and recovery |
| [docs/input-format.md](docs/input-format.md) | The review export format |
