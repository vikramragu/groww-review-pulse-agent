# Runbook

How to run the weekly pulse and recover when something fails. For setup, see
the [README](../README.md). For the full failure table, see
[architecture.md §15](architecture.md).

## Every week

A week runs Monday to Sunday (ISO weeks). Run on Monday or later, so the target week, by
default the last complete one, is finished.

A person starts every run, nothing is scheduled. There are three ways to do it, and all
call the same `pulse` commands.

**From GitHub** (no laptop needed): repository → **Actions** → **Weekly pulse** → **Run
workflow**. Leave the week empty for the last complete week; tick **publish** to append it
to the Doc and create the Gmail draft. It fetches, analyses, publishes, then commits
`snapshots/<week>.json`, and Streamlit Cloud redeploys. If publishing fails (an expired
Google login, say) the dashboard is still updated and the run is marked failed. A week whose
snapshot already says it was published is not published again unless **republish** is
ticked. It needs three repository secrets (Settings → Secrets and variables → Actions):
`GROQ_API_KEY`, `PULSE_MCP_TOKEN`, `PULSE_RECIPIENT`. The logs are public, so the workflow
prints only counts and outcomes.

**From the dashboard** (`streamlit run frontend/streamlit_app.py`):

1. Sidebar → **Fetch & analyse**: fetches reviews and writes the note. Publishes nothing.
2. Read the note and its six checks on the **Weekly pulse** tab.
3. **Delivery** tab → tick the confirmation → **Publish to Doc + create draft**. On
   success it also writes `snapshots/<week>.json` and shows the `git push` command for the
   public dashboard.

**From the terminal:**

```bash
cd /path/to/agent-mcp && source .venv/bin/activate
pulse fetch          # fresh export into data/raw/
pulse run            # analyse the last complete week, append it to the doc, draft the email
```

The run prints where the note went:

```text
Document: appended  https://docs.google.com/document/d/…/edit
Gmail draft: created  r-…
LLM: 44 call(s) · 120,300 in / 65,400 out tokens · …
```

Then, before anything reaches anyone:

1. **Read the new section in the doc.** The checks guarantee the format, the word limit,
   verbatim quotes and no personal details. They can't judge tone: a headline can still
   overstate a small change. The figures in brackets are the facts; the headline is the
   model's reading of them.
2. **Open the Gmail draft, edit it if needed, and send it yourself.** The agent never
   sends anything.
3. **Update the public dashboard:** `pulse snapshot`, then commit and push
   `snapshots/<week>.json`. Streamlit Cloud redeploys on push.

If the week shows `skipped`, it was already published to this doc. That's intended.

## Why there is no schedule

Runs are started by hand on purpose: someone decides when a week goes out. The GitHub
workflow can be made weekly by adding a `schedule:` trigger to
`.github/workflows/weekly-pulse.yml`. While the Google OAuth app stays in Testing status its
login expires every 7 days, so an unattended weekly publish would fail most weeks.

## Checks worth doing now and then

| When | Run | Why |
| --- | --- | --- |
| After the server is redeployed or upgraded | `pulse contract check` | Confirms the live tools still accept what the agent sends. A 0.x minor version change stops runs on purpose |
| After changing a Railway variable | `pulse doctor` | Confirms the token, the doc and Google authorization in one go |
| If a token may have leaked | Rotate (below) | The token is the only thing standing between the public URL and your Docs and Drafts |
| Before changing `config/themes.toml` | Bump `version` | Labels are cached per taxonomy version; the next run re-classifies (~$0.06) |

### Rotating the token

1. `openssl rand -hex 32`
2. Railway → server → Variables → `MCP_AUTH_TOKEN` = the new value → **redeploy**.
3. `.env` → `PULSE_MCP_TOKEN` = the same value.
4. `pulse doctor`. `mcp-live` should be `OK`.

## When a run fails

Every run writes `runs/<week>-<nn>/run.json`, recording which stage failed and why. Nothing
is published unless stages 1–8 all passed. After a fix, replay from the failed stage
rather than starting over:

```bash
pulse run --from <stage> --run-dir runs/<week>-<nn>
```

| Message | Stage | What to do |
| --- | --- | --- |
| `rejected our token (HTTP 401)` | before 1 | The tokens differ, or the server wasn't redeployed. See rotating above |
| `could not be reached` / `did not finish initialize in time` | before 1 | Open `https://<server>/healthz`. If it isn't 200, check the Railway deployment and logs |
| `Google authorization required` | before 1 | The server's Google login has lapsed. In the server repo: run `npm run auth` locally, base64 the new token file into Railway's `GOOGLE_TOKEN_JSON_B64`, and redeploy (its `docs/deployment-plan.md` §0.2). If this happens **every week**, the Google OAuth app is probably still in *Testing* status, where refresh tokens expire after 7 days: publish the app, or re-authorize weekly |
| `Document not found or not shared` / `No edit access` | before 1 | Check the doc ID in `config/pulse.toml` (or `PULSE_DOC_ID` in `.env`, which overrides it). The doc must be in, or shared with edit rights to, the server's Google account |
| `No review exports in data/raw/` | 1 | Run `pulse fetch`, or pass `--input <file>` |
| `Only N review(s) in <week>` | 5 | The week is too thin to report on (minimum 20 substantive reviews). Check `--week`; a fresh `pulse fetch` may help |
| `No compliant note after 3 attempt(s)` | 6 | Nothing was published. `07_validation.json` lists what the model kept getting wrong. Usually a re-run (`--from 6`) succeeds |
| `PII detected in the rendered note` | 7 | **Stop.** Personal details got past the scrubber, which is a bug in stage 3, not in drafting. Don't republish until it's fixed |
| `Recipient domain not allowed` | 10 | The doc is already published. Fix `PULSE_RECIPIENT` or the server's `ALLOWED_RECIPIENT_DOMAINS`, then `--from 10` |
| `Google API temporarily unavailable` after 3 attempts | 9 or 10 | Google is having problems. Wait, then replay from the same stage |
| Run marked `partial` | 10 | The doc was published but the draft failed. `--from 10` creates just the draft |

### Publishing a week again on purpose

```bash
pulse run --from 9 --run-dir runs/<week>-<nn> --republish
```

This appends a **second** section for that week and creates a second draft. The first
section stays: the server can append to a doc but not edit it, so remove the old section by
hand if needed.

## Data and privacy

- `runs/`, `data/raw/` and `.env` are gitignored. Never commit them.
- `01_raw.json`, the only run file that may contain personal details, is deleted once a run
  succeeds and kept when one fails, for debugging. To keep it while investigating an
  ingestion problem, set `[retention] keep_raw_artifact = true`, and set it back afterwards.
- `pulse fetch` deletes its own exports fetched before the window. Exports in the window
  stay, since they are the source data.
- Published notes contain three short public review quotes, with personal details removed
  and no reviewer names.
