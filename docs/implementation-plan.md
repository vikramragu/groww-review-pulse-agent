# Groww Review Pulse — Implementation Plan

> Phase-by-phase build plan for the agent described in [architecture.md](architecture.md),
> delivering the requirements in [problemStatement.md](problemStatement.md).
> Status: **Phases 0–4 complete. Phase 5 (option A, adapt to the deployed
> `google-workspace-mcp`) is built and green against the stub over stdio and HTTP (§8.5). The
> live run waits on the MCP token and doc IDs. Open product decision: `onboarding_kyc` below
> the 5% floor (§5.1).** Last updated: 2026-10-16.

---

## 0. How to use this plan

| Document | Answers |
| --- | --- |
| [problemStatement.md](problemStatement.md) | *What* must be true when we're done, and *why* |
| [architecture.md](architecture.md) | *How* the system is shaped, and which decisions are settled |
| **this file** | *In what order* we build it, task by task, and *how we know each step works* |

Every task carries an ID (`P2-T4`) so it can be referenced in commits and reviews. Tasks are
written to be completable in one sitting and verifiable by a command, not by inspection.

**Conventions used below:**

- `✅ Exit:` a command whose success closes the phase. If it can't be run, the phase isn't done.
- `⚠️` a known trap worth reading before writing the code.
- `🔗` a dependency on a decision or another phase.
- Estimates assume one developer fluent in Python, working from this plan. They are effort, not
  calendar time.

---

## 1. Before Phase 0: decisions that gate work

Three open items from architecture.md §20. Only one of them actually blocks starting.

| Item | Blocks | Can we start without it? |
| --- | --- | --- |
| ~~**O1** — which concrete Play Store export~~ | — | **Resolved 2026-10-12.** `pulse fetch` creates the export via `google-play-scraper`, window-bounded and outside the pipeline. The ToS trade-off is recorded in architecture.md O1 rather than hidden. |
| ~~**X1** — contract `pulse-mcp/1` sign-off~~ | — | **Superseded 2026-10-15.** The servers project shipped its own interface (`google-workspace-mcp` 0.1.0). The agent adapts to it in Phase 5 (§8.1, option A). |
| **O3** — Gmail draft recipient | Phase 5 only | Yes. Config value. |

**The one thing to do today, in parallel with Phase 0:** send architecture.md §11.2 to whoever owns
the servers project and get X1 agreed. It is the only cross-project dependency, it costs them a
read, and it has four phases of lead time. Leaving it until Phase 4 is the most likely way this
plan slips.

### Environment prerequisites

```bash
python --version                  # need 3.11+
ant auth status                   # or export ANTHROPIC_API_KEY
```

If `ant auth status` reports an active profile, no API key env var is needed — the SDK picks the
profile up from a bare `anthropic.Anthropic()`.

---

## 2. Phase dependency graph

```mermaid
graph LR
    P0["P0 Skeleton"] --> P1["P1 Ingest + scrub"]
    P1 --> P2["P2 Classification"]
    P2 --> P3["P3 Aggregate → note"]
    P0 --> P4["P4 Contract + stub + publish"]
    P3 --> P5["P5 Live integration"]
    P4 --> P5
    P3 --> P6["P6 Hardening"]
    P4 --> P6
    X1(["X1 contract agreed"]) -.-> P4
    SRV(["servers project ready"]) -.-> P5
```

Note **P4 depends only on P0**, not on P3. The MCP half and the analysis half are independent after
the skeleton exists — if two people are available, that's the split. The only true serialization is
P1 → P2 → P3.

| Phase | Deliverable | Est. | Blocked externally? |
| --- | --- | --- | --- |
| P0 | Skeleton, models, config, CLI, `doctor` | 0.5–1 d | No |
| P1 | File-drop ingest, normalize, scrub | 1.5–2 d | No (synthetic fixture) |
| P2 | Classification + caching | 1.5 d | No |
| P3 | Aggregate, draft, validate, render, `--dry-run` | 2.5–3 d | No |
| P4 | Contract, stub server, MCP client, publish | 2–2.5 d | Needs X1 agreed |
| P5 | Live integration — adapt to the deployed server (replanned, §8) | ~~0.5–1 d~~ **2–3 d** | No longer blocked; needs D-1–D-5 and an MCP token |
| P6 | Hardening, golden tests, ledger, docs | 1–1.5 d | No |
| | **Total** | **~10–13 d** | |

---

## 3. Phase 0 — Skeleton ✅ COMPLETE (2026-10-11)

**Goal:** a runnable, installed CLI with the complete data model and config loading, so every later
phase is filling in behaviour rather than inventing structure.

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P0-T1 | `git init`; `.gitignore` covering `data/raw/`, `runs/`, `.cache/`, `__pycache__`, `.venv` | `.gitignore` | ✅ |
| P0-T2 | `pyproject.toml`: src layout, `pulse` package, `pulse` console script, pinned deps | `pyproject.toml` | ✅ |
| P0-T3 | Transcribe the full data model from architecture.md §7 verbatim | `src/pulse/models.py` | ✅ |
| P0-T4 | Config loading: TOML + env, with a typed `Settings` model and clear errors on missing required values | `src/pulse/config.py`, `config/*.toml` | ✅ |
| P0-T5 | CLI skeleton (Typer): `run`, `validate`, `doctor`, `contract check` — all wired, most raising `NotImplementedError` | `src/pulse/cli.py` | ✅ |
| P0-T6 | Run directory + ledger helpers: allocate `runs/<week>-<nn>/`, write/read stage artifacts, append to `run.json` | `src/pulse/orchestrator.py` | ✅ |
| P0-T7 | Structured logging setup (stdlib `logging` + JSON formatter), `run_id` on every record | `src/pulse/logging.py` | ✅ |
| P0-T8 | `pulse doctor`: checks Python version, Anthropic credential resolution, input path, config parse. MCP probes stubbed as "not implemented (P4)" | `src/pulse/doctor.py`, `src/pulse/cli.py` | ✅ |
| P0-T9 | Model round-trip tests: each model accepts a valid payload and rejects an invalid one | `tests/test_models.py` | ✅ |
| P0-T10 | Dev tooling: `ruff`, `mypy`, `pytest` config; one CI workflow running all three | `pyproject.toml`, `.github/workflows/ci.yml` | ✅ |

### What landed beyond the task list — Phase 0

| Addition | Why |
| --- | --- |
| `src/pulse/doctor.py` as its own module | Checks are unit-testable without invoking Typer, and detection stays separate from presentation. P0-T8 named `cli.py`; the command lives there, the checks do not. |
| `README.md` | `pyproject.toml`'s `readme =` field makes it an install-time requirement — the package will not build without it. Expanded further in P1-T2 and P6-T5. |
| `RunLedger`, `StageRecord`, `LlmUsage` in `models.py` | P0-T6 needs a typed ledger; architecture.md §16 specifies its contents. |
| `IsoWeek` constrained type | Turns the ISO-year boundary trap into a validation error at the stage boundary that caused it. |
| `tests/test_config.py`, `test_orchestrator.py`, `test_doctor.py` | The phase exit criterion is `pytest -q`, which is only meaningful if config loading and run-dir allocation are actually covered. |
| `config/themes.toml` ↔ `Theme` enum consistency check | The enum enforces the ≤5 constraint; the config builds the classifier prompt. Drift between them surfaces as confusing classification failures rather than as the config error it is. |
| `ruff format` + `extend-exclude = ["docs"]` | Formatting is in CI. `docs/` is excluded because ruff reformats the Python snippets inside the design documents. |

### Deviations from architecture.md §7 (document updated to match)

| Change | Reason |
| --- | --- |
| `StrEnum` instead of `(str, Enum)` | `f"{Theme.APP_RELIABILITY}"` renders as `"Theme.APP_RELIABILITY"` under `(str, Enum)`. §8.1 renders theme names into the classifier prompt, so that would degrade classification silently. Pydantic JSON and schema output are identical either way. Pinned by a test. |
| `extra="forbid"` on `Review` | Makes the structural PII defence loud. The default would silently drop an `author=` an adapter passed in — safe, but it hides the bug. |
| `iso_week: IsoWeek` instead of `str` | As above. |

**Dependencies to pin** (P0-T2):

```toml
dependencies = [
  "pydantic>=2.9",
  "anthropic>=1.0",
  "mcp>=1.2",
  "typer>=0.12",
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6", "mypy>=1.11"]
```

⚠️ **Do not add any `google-*` package, ever.** P6-T1 enforces this with a test, but the habit
starts here. If something seems to need one, the need belongs in the servers project.

⚠️ `tomllib` is in the 3.11 stdlib — no `tomli` dependency needed.

**Cardinality note for P0-T3:** architecture.md §7 writes the list constraints as
`Field(min_length=3, max_length=3)`. In Pydantic v2 those are the correct keyword names for
sequence length (`min_items`/`max_items` are the v1 spelling and are rejected). Keep v2 spelling.

```bash
# ✅ Exit — all verified 2026-10-11
pip install -e ".[dev]"      # installs clean
pulse doctor                 # reports env status truthfully, exits 0 or 1 with reasons
pytest -q                    # 140 passed
ruff check . && ruff format --check . && mypy src
```

On a machine with no Anthropic credential, `pulse doctor` exits 1 with
`FAIL anthropic-auth` plus warnings for the absent review export and draft recipient.
That is the correct result, not a setup problem — the credential is first needed in
Phase 2, and the warnings track open decisions O1 and O3.

---

## 4. Phase 1 — Ingest, normalize, scrub ✅ COMPLETE (2026-10-11)

**Goal:** a corpus of normalized, PII-free `Review` objects on disk, from a file the user drops in.

This is where the privacy constraint is actually won or lost, so the scrubber gets disproportionate
test attention.

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P1-T1 | **Synthetic fixture corpus**: ~250 reviews across 12 ISO weeks, realistic Groww-flavoured text, deliberate PII samples, rating skew, a few duplicates and a few empty-text rows | `tests/fixtures/reviews_sample.json` | ✅ 263 rows |
| P1-T2 | Declare the canonical input format (the JSON/CSV shape the file-drop adapter reads) and document it in the README | `docs/input-format.md`, `README.md` | ✅ |
| P1-T3 | `ReviewSource` protocol + adapter registry; registry is explicit, no auto-discovery | `src/pulse/ingest/base.py` | ✅ |
| P1-T4 | File-drop adapter: JSON and CSV, directory or single file, emits `RawReview[]` | `src/pulse/ingest/file_drop.py` | ✅ +TSV |
| P1-T5 | Normalization: rating coercion/clamping, date parsing → `submitted_on` + `iso_week`, `review_id` content hash, drop empty-text rows, dedupe | `src/pulse/ingest/normalize.py` | ✅ |
| P1-T6 | Window filter + window validation (abort if span < `PULSE_MIN_WINDOW_WEEKS`) | `src/pulse/ingest/normalize.py` | ✅ |
| P1-T7 | Scrubber layers 1–3 from architecture.md §10, each pattern a named, individually testable rule | `src/pulse/analysis/scrub.py` | ✅ 13 rules |
| P1-T8 | `scan_for_pii(text) -> list[PiiHit]` — the detector used both by the scrubber and by the §9 pre-publish rescan. One implementation, two callers | `src/pulse/analysis/scrub.py` | ✅ |
| P1-T9 | Wire stages 1–3 into the orchestrator, writing `01_raw.json`, `02_normalized.json`, `03_scrubbed.json` | `src/pulse/pipeline.py`, `src/pulse/cli.py` | ✅ |
| P1-T10 | `test_scrub.py` — positive table (every PII class redacted) **and** negative table | `tests/test_scrub.py` | ✅ 124 tests |
| P1-T11 | `test_normalize.py` — dedupe, week assignment incl. year boundary, window abort, rating clamp | `tests/test_normalize.py` | ✅ 70 tests |

### What landed beyond the task list — Phase 1

| Addition | Why |
| --- | --- |
| `src/pulse/pipeline.py` as its own module | P1-T9 named `orchestrator.py`, but putting stage dispatch there would break the layering rule in architecture.md §5 — `orchestrator` owns run dirs and the ledger and must not import `ingest/` or `analysis/`. Composition lives here instead. |
| `scripts/make_fixture.py` | The fixture is generated from a committed, seeded script, so it can be regenerated and diffed rather than hand-edited. Verified byte-identical on re-run. |
| `docs/input-format.md` | P1-T2 said "document it in the README". The format needs a full page (aliases, date formats, drop reasons, dedupe rule); the README links to it. |
| `tests/test_ingest.py`, `tests/test_pipeline.py` | P1-T10/11 cover scrub and normalize. The adapter and the wired pipeline needed their own, including artifact-level privacy assertions. |
| Scrub **idempotence** as a tested property | Found a real bug: `self_contact` re-matched its own `[contact]` placeholder, which would have made stage 7's pre-publish rescan abort good runs. Now no rule may claim an already-redacted span. |
| `assert_clean()` + `PiiHit.masked()` | Stage 7 (P3-T15) needs to report a leak without echoing the leak into logs. Tested. |
| TSV support, UTF-8 BOM tolerance | One line each; spreadsheet exports routinely carry a BOM, which would otherwise corrupt the first column header. |

### Deviations and discoveries

| Item | Detail |
| --- | --- |
| **Identity columns dropped at ingest, not stage 2** | Architecture.md §18 accepted PII in `01_raw.json`. The adapter is stricter: recognised identity columns never reach any artifact. Free, since nothing downstream wants them. §18 and §10 updated. The raw artifact is still sensitive — PII *inside review bodies* survives to stage 3 — so O6 still matters. |
| **Title is never concatenated with text** | `Review.text` must be a contiguous span of real reviewer prose, because stage 7 verifies quotes as exact substrings of it. A synthetic `title + ". " + body` join would let a "verbatim" quote straddle punctuation nobody wrote. Title is used only when text is absent. |
| **`self_name` split into two rules** | The stopword guard was wrongly applied to "my name is", where whatever follows *is* a name — it silently refused to redact "My name is Not A Realperson". Explicit triggers now redact unconditionally; weak triggers ("I am") require capitalization *and* pass a stopword list, because neither guard alone holds ("I am giving 2 stars" vs "I am Unable to login"). |
| **Plan's ISO-week example was wrong** | This document claimed 2026-12-31 is `2027-W01`; it is `2026-W53`. Corrected above, with verified boundary cases. |

### Implementation notes — Phase 1

**The fixture must be synthetic.** Committing real Play Store reviews would put third-party text —
and plausibly real PII — into the repo, which is the exact thing this project promises not to do.
Write the fixture by hand or generate it; make the PII in it obviously fake (`+91 90000 00000`,
`notareal@example.com`). This also makes the negative tests honest, because we know the ground
truth.

**`review_id` definition** (P1-T5): `sha256(source + "|" + normalized_text + "|" + submitted_on)`,
first 16 hex chars. Deliberately excludes rating and title so an edited rating doesn't fork the id,
and excludes any reviewer field so the id is never identity-derived. Dedupe on this hash.

⚠️ **ISO week boundaries.** Use `date.isocalendar()` and format as `f"{year}-W{week:02d}"`, taking
the ISO year from `isocalendar()[0]`, **not** `date.year`. Getting this wrong silently misfiles
reviews at the year boundary and corrupts the baseline. Verified cases where the ISO year differs
from the calendar year: 2027-01-01 → `2026-W53`, 2025-12-29 → `2026-W01`, 2021-01-01 → `2020-W53`.
(An earlier draft of this plan claimed 2026-12-31 is `2027-W01`; it is `2026-W53` — corrected in
P0 after checking against `isocalendar()` rather than by hand.)

⚠️ **Scrubber ordering matters.** Run specific patterns before the catch-all digit-run rule, or
`[number]` will swallow phone numbers and PANs before their own rules see them. Order: email → URL
→ UPI handle → PAN → Aadhaar → IFSC → phone → account → `@handle` → long-digit catch-all.

⚠️ **Negative cases are not optional.** A scrubber that redacts `₹5000`, `T+1`, `24x7`, `2 stars`,
or `SBI` destroys legitimate review text and will quietly degrade every quote. P1-T10's negative
table is as important as its positive table.

```bash
# ✅ Exit — all verified 2026-10-11
pytest -q                 # 399 passed (124 scrub, 70 normalize, 44 ingest, 21 pipeline)
ruff check . && ruff format --check . && mypy src
pulse run --dry-run --input tests/fixtures/reviews_sample.json --week 2026-W41 --from 01 --to 03
# → runs/2026-W41-01/{01_raw,02_normalized,03_scrubbed}.json
```

**Privacy verification on the artifact**, not just in unit tests: grepping
`03_scrubbed.json` for email shapes, bare `@handles`, and every value the fixture planted
returns nothing. Long digit runs do appear — all 17 are digit substrings of the 16-char hex
`review_id` content hashes, which exclude every reviewer field by construction; zero occur
in review text. `tests/test_pipeline.py` asserts both halves of this, plus that the fixture
really does still contain the PII the scrubber is claimed to remove (without which the
leak test could pass against an empty fixture).

Corpus over the fixture: 263 rows → 261 ingested → 242 normalized (17 duplicates, 2 ratings
clamped, 2 rows dropped) → 241 scrubbed, spanning 13 weeks, 22 PII hits across 10 rules.

---

## 5. Phase 2 — Classification 🔄 T1–T10 complete (2026-10-12); §5.1 in progress

**Goal:** every review in the corpus labelled with one of the five themes or `None`, cheaply and
repeatably.

**Provider changed to Groq** (architecture.md D6a), decided 2026-10-11, to build without
per-token spend. `LlmPort` keeps a switch back a config change.

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P2-T1 | **`LlmPort` protocol** + provider implementation + `FakeLlm` for tests. Usage/cost accounting lives here | `src/pulse/llm.py` | ✅ `GroqLlm`, `FakeLlm`, `NullLlm` |
| P2-T2 | Taxonomy prompt: the five themes with inclusion/exclusion guidance and a worked example each. Stable text — it's the cache prefix | `src/pulse/prompts/taxonomy.py` | ✅ |
| P2-T3 | `ClassificationBatch` wrapper model; batched classification | `src/pulse/analysis/classify.py` | ✅ |
| P2-T4 | Prompt caching, where the provider supports it | `src/pulse/analysis/classify.py` | ✅ conditional — Groq has none |
| P2-T5 | Classification cache keyed by `(review_id, taxonomy_version)` in `.cache/classify.json` | `src/pulse/analysis/classify.py` | ✅ + model in the key |
| P2-T6 | Confidence floor (< 0.5 → `theme = None`); per-batch validation retry then mark unclassified | `src/pulse/analysis/classify.py` | ✅ |
| P2-T7 | Record tokens, cost, and cache hit rate into the run ledger | `src/pulse/pipeline.py` | ✅ |
| P2-T8 | Wire stage 4; write `04_classified.json` | `src/pulse/pipeline.py` | ✅ |
| P2-T9 | Tests with `FakeLlm`: batching splits correctly, cache hit avoids a second call, low confidence → `None`, invalid theme → retry path | `tests/test_classify.py` | ✅ 35 + 17 prompt tests |
| P2-T10 | **One-time**: run real classification over the fixture corpus and commit the result as a golden artifact for P3/P6 | `tests/fixtures/classified_golden.json` | ✅ 241 labels, $0.0083 |

### What landed beyond the task list — Phase 2

| Addition | Why |
| --- | --- |
| Adaptive `json_schema` → `json_object` fallback | Which Groq models accept a `json_schema` response format is model-dependent and the published accounts disagree. The client tries schema mode, falls back permanently for the run on a 400, and logs once. Client-side Pydantic validation makes the difference retry count, never correctness. |
| **Index-based reconciliation** instead of `review_id` echo | Deviation from architecture.md §8.1, documented there. A 1-based batch index is far harder for a model to corrupt than a 16-char hex hash, an out-of-range index is detectable where a plausible wrong hash is not, and it costs fewer output tokens. Missing / duplicate / spurious indices each become `None` and are counted. |
| Model in the cache key | The plan keyed on `(review_id, taxonomy_version)`. Adding the model prevents serving labels from one judge while a run uses another — which would quietly undermine the O5 comparison. |
| `pulse models [--probe]` | Picks the model by asking the account instead of assuming. `--probe` spends ~30 tokens per model testing real schema support. |
| `--llm` / `--no-cache` flags, `NullLlm` | Offline development and explicit failure when no provider is set. |
| Provider-aware `doctor` | Was Anthropic-specific; a hardcoded check would make `doctor` lie about a correctly configured run. |

### Bugs the tests caught

| Bug | Impact if shipped |
| --- | --- |
| `cache = cache or ClassificationCache(...)` | `ClassificationCache` defines `__len__`, so an **empty cache is falsy** — the caller's cache, including `--no-cache`, was silently discarded and replaced with one at the default path. Fixed to `is None`; the same hazard fixed for the injected `LlmPort`. |
| `ConfigError` escaped the run handler | A misconfiguration found *during* a stage left the ledger at `status: "running"` forever, indistinguishable from a crashed process, breaking §15's partial-failure handling. |

### Implementation notes — Phase 2

**Why `LlmPort` (P2-T1) even though the architecture doesn't name it.** Stages 4 and 6 are the only
non-deterministic steps, and both need to be testable in CI without spending tokens or flaking.
A narrow port with a `FakeLlm` makes the regeneration loop in P3 testable at all — you can script
"return a paraphrased quote, then a verbatim one" and assert the loop behaves. This is a plan-level
addition, consistent with the architecture's deterministic-spine stance.

**Settings for the classification call** (from architecture.md §8.1): `model="claude-opus-5-5"`,
`output_config={"effort": "low"}`, `max_tokens=8000`, batch ~40 reviews.

⚠️ **Verify caching actually works.** After the first batch, assert
`usage.cache_read_input_tokens > 0`. If it's zero on every batch, something volatile has leaked into
the prefix — a timestamp, an unsorted `json.dumps`, a per-batch count in the system text. The cache
is what makes weekly re-runs nearly free (the 8–12 week window overlaps ~90% between runs), so a
silent cache miss is a real cost regression, not a cosmetic one.

⚠️ **Don't let the cache outlive the taxonomy.** The cache key includes `taxonomy_version`; bump
that value in `config/themes.toml` whenever theme definitions change, or you'll serve labels from a
taxonomy that no longer exists.

🔗 **O5** (cheaper model for bulk classification) becomes measurable here. Don't pre-empt it: get
correct labels from `claude-opus-5-5` first, then — if cost matters — compare a cheaper model
against this phase's output on the same corpus and decide with numbers.

```bash
# ✅ Exit — P2-T1..T9 verified 2026-10-11, entirely offline
pytest -q                 # 465 passed (35 classify, 17 prompt, 28 pipeline)
ruff check . && ruff format --check . && mypy src
```

Stages 1-4 verified end to end over the fixture with a scripted provider: 241 reviews,
7 batches, zero missing/duplicate/spurious indices, theme counts populated in the ledger.
A replay of stage 4 makes **no** API call (cache hit rate 1.0) — the property that makes
weekly re-runs nearly free.

**P2-T10 complete** — real classification over the 241-review fixture on
`openai/gpt-oss-120b`, committed as `tests/fixtures/classified_golden.json` and pinned by
`tests/test_golden_classification.py`:

| Measure | Result |
| --- | --- |
| Batches / API calls | 7 / 7, **zero** retries, **zero** batch failures |
| Reconciliation anomalies | 0 missing, 0 spurious, 0 duplicate indices |
| Unclassified | 5 of 241 (2.1%) — **all five are the fixture's adapter-test rows**, which genuinely have no theme. The model declined rather than guessing. |
| Theme spread | 16–25% each; the taxonomy fits the corpus |
| Confidence | cleanly bimodal — 0.1–0.6 for the five declines, 0.8–1.0 for the rest |
| Cost | **$0.0083** (13.6k in / 8.3k out) |
| Wall time | ~120s |

The cache-read assertion from the original plan does not apply on Groq, which has no cache
breakpoint; the report records `prompt_cache_supported: false` rather than implying a miss.

### Model selection — probing beat assuming

`pulse models` found the account offers **neither Kimi K2 nor Llama 4**, the two families
secondary sources named as the schema-capable ones. Of the five usable chat models,
`openai/gpt-oss-120b` was chosen: strongest available, strict `json_schema` once the schema
is reshaped. `qwen/qwen3.8-27b` is the alternative — also strict, and with no reasoning
overhead.

### Quality caveat, reported rather than tuned away

3 of 64 distinct review texts received **different labels in different batches**, all at
genuine taxonomy seams ("OTP never arrives" sits between `app_reliability` and
`onboarding_kyc`). That is ~5% label noise flowing into Phase 3's theme counts. Left
unfixed deliberately: the fixture is synthetic, so sharpening the prompt against it would
be optimising for fiction. Revisit when real reviews are available (O1). Real reviews are
now available, so this is P2-T17 in §5.1.

### 5.1 Phase 2 reopened — filtering the real corpus 🔄 IN PROGRESS (2026-10-15)

**Why reopened.** Everything above was built and measured against the 263-row synthetic
fixture. `pulse fetch` has since produced a real export, and most of it is not classifiable
text. The rule, decided 2026-10-15: **classify only English reviews of more than 8 words.
Emoji-only and non-English reviews are removed before stage 4.**

**Where the filter lives.** The filter runs in stage 2, after windowing (`filter_substantive` in
`normalize.py`), not in stage 4. It is listed under Phase 2 because its job is to decide what
the classifier sees. Running it at stage 2 means stages 3–4 never scrub or pay for text that
will be thrown away, and the ledger records both `corpus_total` (the window) and `corpus_size`
(the analysable subset).

#### Is the download sound? Audit of `data/raw/com-nextbillion-groww-2026-10-11.json`

| Check | Result | Verdict |
| --- | --- | --- |
| Rows / schema | 12,800 rows. All have exactly `app_version, date, external_id, rating, source, text` | ✅ |
| Identity fields | No reviewer name, avatar, or reply fields. The fetcher dropped them at source | ✅ |
| Window coverage | 2026-07-20 (Mon) → 2026-10-11 (Sun): **exactly 12 complete ISO weeks**, W30–W41, 847–1,358 rows/week | ✅ |
| Duplicate `external_id` | 0 | ✅ |
| Empty text | 0 | ✅ |
| After normalize (dedupe on text + date) | 12,800 → **8,812**. The drops are mostly same-day repeats of "good" / "nice" | ✅ expected |
| Rating skew | 77% are 5★ (9,884). 1★ is 10% | ⚠️ see below |
| Language | `lang=en` was requested, but 145 rows are mostly non-Latin script (Devanagari 84, Bengali, Gujarati, Kannada, Tamil, Telugu) and ~200+ are romanized Hindi | ⚠️ the `lang` hint is not a filter |

The download is complete and correctly shaped. **`lang=en` cannot be relied on as a language
filter**, so the pipeline has to apply its own.

#### What survives the filter (measured on the real file with the project's own code)

8,812 normalized reviews in the window:

| Removed by | ≥8 words (current code) | **>8 words (decided rule)** |
| --- | --- | --- |
| Emoji / punctuation only | 231 | 231 |
| Non-Latin script (>30% non-Latin letters) | 145 | 145 |
| Too short | 6,276 | 6,491 |
| **Kept** | **2,160 (24.5%)** | **1,945 (22.1%)** |
| Kept per week (min / median / max) | 109 / 180 / 282 | 97 / 161 / 259 |
| Target week 2026-W41 | 173 | 160 |

Every week stays far above the 20-review floor (P3-T5), so the stricter rule costs no weeks.

**Gaps found in the code on 2026-10-15** (each one became a task below; all now fixed):

1. **Off-by-one.** `word_count(text) < min_words` with `min_words = 8` *keeps* 8-word reviews,
   which is "at least 8", not "more than 8". 215 reviews depend on this.
2. **Emoji count as words.** `word_count` splits on whitespace, so `"if some wonderful app
   application and thank you 🙏"` has 9 tokens and passes, though it has only 8 words. 191 kept
   reviews contain emoji.
3. **Romanized Hindi is kept on purpose.** `MAX_NON_LATIN_RATIO` catches Devanagari but not
   `"Isme bond ka monthly payout correct ni show karta hai"`. About **149 of the 1,945** kept
   reviews look like Hinglish. The code comment argues for keeping them. The 2026-10-15
   decision overrides that comment: they are not English and they are removed.

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P2-T11 | Emoji-only / punctuation-only reviews dropped (`has_letters`) | `src/pulse/ingest/normalize.py` | ✅ |
| P2-T12 | Non-Latin-script reviews dropped (`non_latin_ratio > 0.3`) | `src/pulse/ingest/normalize.py` | ✅ |
| P2-T13 | **"More than 8 words."** Change the rule to `word_count > 8` and rename the setting to `thresholds.min_review_words_exclusive = 8`, or set `min_review_words = 9`. Pick one, and make the config comment, the drop reason string, and the test boundary say the same thing. Test that 8 words are dropped and 9 are kept | `normalize.py`, `config/pulse.toml`, `tests/test_normalize.py` | ✅ `min_review_words = 9` (inclusive minimum); reason `"fewer than 9 words"` |
| P2-T14 | **Count only lexical words.** `word_count` counts only tokens that contain at least one letter, so emoji, `!!!`, and `₹500` don't count toward the floor. Emoji *inside* a kept review are left in `Review.text`, because P3-T13 requires quotes to be exact substrings of it | `normalize.py`, `tests/test_normalize.py` | ✅ |
| P2-T15 | **Romanized-Hindi (Hinglish) filter**, deterministic with no new dependency: drop a review when ≥20% of its tokens are in a curated Hindi function-word list (`hai, nahi, nhi, ka, ki, ke, se, mein, mujhe, kya, kar, raha, bahut, paisa, …`) **and** <15% are English stopwords. The list is committed and versioned, like `taxonomy_version`. New drop reason: `"non-English (romanized)"` | `normalize.py`, `tests/test_normalize.py` | ✅ + a second clause: ≥40% Hindi markers alone (see below) |
| P2-T16 | Hinglish precision check: hand-label a 100-review sample from the real corpus (kept as a local scratch file, **not committed**, since it is third-party text) and record the precision/recall of P2-T15 here. Target: <5% of genuinely English reviews dropped. Write tests for the false positives: `"bad luck for me"`, `"ok ka"` | — | ✅ 0 English dropped in 59; ~4% residual (below) |
| P2-T17 | Re-run stage 4 on the filtered real corpus (~1,800 reviews ≈ 45 batches; ~$0.06 extrapolated from P2-T10). Report the unclassified rate, the theme spread, and the cross-batch label disagreement from the quality caveat above. If one theme is >40% or <5%, revisit the taxonomy (bump `taxonomy_version`, do not add a sixth theme) | — | ✅ run; ⚠️ `onboarding_kyc` at 3.9% trips the <5% rule — **decision pending** (below) |
| P2-T18 | Update the synthetic fixture (`scripts/make_fixture.py`) with rows for each new drop reason: exactly 8 words, 9 words, emoji-padded 8 words, Hinglish, and English with one Hindi word. Then regenerate `classified_golden.json` | `scripts/make_fixture.py`, `tests/fixtures/` | ✅ 270 rows; golden now covers the filtered corpus (238) |

#### What the real corpus taught — §5.1 results

**Filter, final numbers** (8,812 in window): 231 emoji/punctuation-only, 145 non-Latin
script, 471 romanized Hindi, 6,257 fewer than 9 words. **1,708 kept (19.4%)**; at least 86
in every week, 144 in 2026-W41. Ratings in the kept corpus: 1★ 705, 2★ 101, 3★ 147, 4★ 149,
5★ 606.

**P2-T16, measured rather than assumed.** Reviews were labelled by reading them (by the
assistant during implementation, not by a second human, so treat as one rater). The samples
stayed in scratch and were not committed.

| Sample | Result |
| --- | --- |
| 50 reviews the first lexicon dropped | 50/50 genuinely Hindi/Hinglish: **no English review dropped** |
| 50 reviews the first lexicon kept | 3 clearly non-English, e.g. `"do ki jyada … ke liye the"`, where `do` and `the` read as English markers |
| 9 reviews added by the widened lexicon | all Hinglish or code-mixed (plus one gibberish string); none English |
| 50 kept after widening | 2 non-English (~4%), both spelling `hai` as `he`, which cannot be listed because `he` is English |

The fix for the 3 misses was a second clause (`HINDI_DOMINANT_RATIO = 0.4`: drop when Hindi
markers alone reach 40%) plus ~60 more common words. Residual ~4% non-English is accepted:
those reviews reach the classifier, which can usually label them, and closing the gap would
start costing English reviews.

**Fixture survives cleanly**: 248 in window → 238 kept, 28 in the target week. One row per
drop reason, plus two that must survive (9 words; English borrowing `paisa`).

**Golden artifact redefined.** It now covers what stage 4 actually classifies (the
substance-filtered corpus), not the whole window. The 236 surviving labels from 2026-10-11
are **byte-identical**; the 2 new kept rows were labelled by the same model on 2026-10-15
(`payments_withdrawals` 0.99, `app_reliability` 0.98). The 5 labels that fell away were the
short adapter-test rows and one 6-word title-only row.

**Bug found on the first real run.** `classify_reviews` saved the cache only after the last
batch. On 43 real batches, Groq dropped the connection after 94s (past the SDK's own
retries) and **every completed batch was lost**, because the transport error correctly
aborts the stage. The 7-batch fixture could never have shown this. Now `cache.save()` runs
in `finally`, so a re-run sends only the batches still missing; pinned by
`test_batches_finished_before_a_transport_failure_are_kept`.

**P2-T17 — real classification, 2026-10-15** (`openai/gpt-oss-120b`, 1,708 reviews, 43
batches over three attempts because Groq dropped the connection twice; the cache fix above
meant no batch was paid for twice):

| Theme | Reviews | Share | Mean ★ | 2026-W41 |
| --- | --- | --- | --- | --- |
| `orders_execution` | 470 | 27.5% | 3.47 | 44 |
| `app_reliability` | 352 | 20.6% | 2.33 | 37 |
| `charges_support` | 344 | 20.1% | 2.03 | 32 |
| `payments_withdrawals` | 108 | 6.3% | 1.99 | 10 |
| `onboarding_kyc` | 66 | **3.9%** | 2.05 | **3** |
| unclassified | 368 | 21.5% | 4.01 | 18 |

- **Zero** batch retries, failures, or reconciliation anomalies across all 43 batches.
- **Unclassified 21.5% is genuine, not model failure**: mean rating 4.01, and a sample is
  generic praise with no topic ("very nice app", "Trusted app for real ones"). The word
  floor keeps long reviews, not on-topic ones.
- **Cross-batch disagreement is unmeasurable here**: deduplication left no repeated text,
  so the ~5% noise estimate from P2-T10 stands unconfirmed.
- 144 of 1,340 labels (11%) have confidence below 0.8.
- Tokens ~118k in / ~64k out; the ledger reports cost as unknown for this model, and the
  P2-T10 rate extrapolates to roughly $0.06.

✅ **Decided 2026-10-19: keep `onboarding_kyc` as is (option b).** Widening it to login/OTP would move only ~25 reviews (to ~5.3%) and split login failures from the rest of App Reliability; splitting Orders (~115 of 470 mention charts/IPO/price data) is a reasonable later product call. Ranking weighs growth, so a KYC spike still surfaces. No re-classification, `taxonomy_version` unchanged.

~~**Open decision — `onboarding_kyc` is below the 5% floor.**~~ The plan's rule says revisit the
taxonomy. Options: (a) merge it into a neighbour and split `orders_execution` (the largest,
27.5%) into trading vs. IPO/market data — keeps five themes; (b) keep it, accepting that
in the target week it has only 3 reviews and will essentially never rank top-3; (c) widen
its definition (account access, login, profile changes). Any change bumps
`taxonomy_version` and re-classifies everything. Not decided in Phase 4 because it changes
what the note says, which is a product call.

⚠️ **The filter changes what the note's numbers mean.** In the raw corpus 77% of reviews are 5★
and 10% are 1★. After the >8-word filter, 1★ (808) *outnumbers* 5★ (698): short reviews are
mostly "good 👍", and long reviews are mostly complaints. That is the right corpus for finding
themes. It is the wrong corpus for describing sentiment. The note must keep disclosing
`corpus_size of corpus_total` (already wired via `RunLedger.corpus_total`), and mean ratings in
`ThemeStats` describe *substantive* reviews only. Say so in the rendered footer.

⚠️ **Don't put a language-ID library in the pipeline to fix this.** The heuristic in P2-T15 is
auditable, deterministic, and testable against fixed cases. A model-based detector is none of
those, and it would make `02_normalized.json` depend on a library version. Revisit only if
P2-T16 measures the heuristic as inadequate.

```bash
# ✅ Exit — §5.1
pytest -q
ruff check . && ruff format --check . && mypy src
pulse run --dry-run --input data/raw/com-nextbillion-groww-2026-10-11.json --week 2026-W41 --from 01 --to 02
# → ledger: corpus_total 8812, corpus_size ≈ 1,800, drop reasons for emoji / non-Latin /
#   non-English (romanized) / "≤8 words", no week below 20
```

---

## 6. Phase 3 — Aggregate, draft, validate, render ✅ COMPLETE (2026-10-12)

**Goal:** `pulse run --dry-run` produces a compliant one-page note on disk. This is the phase that
satisfies most of the acceptance checklist.

Largest phase; build it in three ordered slices.

### 3a — Aggregation (deterministic)

| ID | Task | Files |
| --- | --- | --- |
| P3-T1 | Per-theme weekly stats: count, share, mean rating, negative volume | `src/pulse/analysis/aggregate.py` |
| P3-T2 | Trailing baseline + `delta_vs_baseline` from the rolling window | `src/pulse/analysis/aggregate.py` |
| P3-T3 | Ranking score from architecture.md §8.2, with documented tie-breaks | `src/pulse/analysis/aggregate.py` |
| P3-T4 | Quote candidate pool: 4-step filter, top 8 per theme | `src/pulse/analysis/aggregate.py` |
| P3-T5 | Thin-week abort (< 20 reviews) and the no-quotable-candidate fallback to the 4th theme | `src/pulse/analysis/aggregate.py` |
| P3-T6 | `test_aggregate.py`: ranking determinism, tie-breaks, delta can't promote a 3-review theme over a 100-review one, candidate filters | `tests/test_aggregate.py` |

### 3b — Draft and render

| ID | Task | Files |
| --- | --- | --- |
| P3-T7 | Draft system prompt: word budget, verbatim-copy instruction, action grounding | `src/pulse/prompts/draft.py` |
| P3-T8 | Draft input renderer — top 3 themes, stats, candidate pools only. **Never the full corpus** | `src/pulse/analysis/draft.py` |
| P3-T9 | `PulseNote` generation via `messages.parse()`, `effort: "high"` | `src/pulse/analysis/draft.py` |
| P3-T10 | Markdown renderer — one template, the single source for both doc and email bodies | `src/pulse/rendering/render.py` |
| P3-T11 | `test_render.py`: stable output for a fixed `PulseNote`, CommonMark only (no tables/HTML — §11.2 limits the servers to basic marks) | `tests/test_render.py` |

### 3c — The validation gate

| ID | Task | Files |
| --- | --- | --- |
| P3-T12 | `count_note_words()` implementing architecture.md §9's definition exactly, with its own tests | `src/pulse/rendering/validate.py` |
| P3-T13 | Verbatim check: whitespace-normalize both sides, require an exact substring match in exactly one candidate | `src/pulse/rendering/validate.py` |
| P3-T14 | Remaining checks: provenance, distinctness, theme validity, action grounding | `src/pulse/rendering/validate.py` |
| P3-T15 | PII rescan via P1-T8's detector → **abort**, not retry | `src/pulse/rendering/validate.py` |
| P3-T16 | Regeneration loop: ≤3 attempts, each retry carrying the specific failures as feedback; write `07_validation.json` | `src/pulse/orchestrator.py` |
| P3-T17 | `test_validate.py`: paraphrase rejected, verbatim accepted, 251 words rejected / 250 accepted, wrong cardinality rejected, duplicate-source quotes rejected | `tests/test_validate.py` |
| P3-T18 | Loop test with `FakeLlm`: scripted fail-then-pass succeeds; three failures abort without publishing | `tests/test_draft_loop.py` |

### Implementation notes — Phase 3

⚠️ **The verbatim check is the highest-value test in the repo.** Write P3-T17 before P3-T13 and
watch it fail. A model asked for "verbatim" quotes will tidy punctuation, fix spelling, and merge
clauses — all of which look fine to a human reviewer and all of which violate a hard requirement.
Normalize only whitespace before comparing; do **not** normalize case or punctuation, or the check
stops checking the thing that matters.

⚠️ **Word count must be one function with one definition.** If the prompt's budget and the
validator's count disagree even slightly, the loop will fail in ways that look like model
disobedience. Implement `count_note_words()` first, state its rule in the draft prompt, and test
the boundary at exactly 250 and 251.

⚠️ **Feedback, not retries.** `"Try again"` produces a differently-wrong note. The retry message
must name what failed: which quote wasn't verbatim, what the measured word count was, which theme
was out of range.

**Render target is deliberately plain.** §11.2 commits the servers only to headings, bold/italic,
bullets, block quotes, and links. Rendering a markdown table here would produce something the Docs
server isn't contracted to handle.

```bash
# ✅ Exit — all verified 2026-10-12
pytest -q          # 607 passed (25 aggregate, 54 validate, 15 render, 14 draft loop)
ruff check . && ruff format --check . && mypy src
pulse run --dry-run --input tests/fixtures/reviews_sample.json --week 2026-W41
pulse validate     # re-gates the stored note: passes
```

The produced note, measured rather than asserted: **238 words**, 3 themes, 3 quotes, 3
actions, no PII, every quote verbatim with correct provenance. Accepted on the **first**
attempt — the regeneration loop was not needed, though `test_draft_loop.py` scripts it.

### What landed beyond the task list — Phase 3

| Addition | Why |
| --- | --- |
| `QuoteCandidate` model | §7 typed `quote_candidates` as `list[str]`, which made §9's provenance check unimplementable. See the bug below. |
| Stage 7 runnable standalone | Fused into stage 6 for the retry loop, but also registered on its own so `pulse validate` can re-gate a stored artifact. Makes a published note auditable after the fact. |
| `05_stats.json` stores the **full** ranking | Not just the top three, so "why wasn't payments on the page?" is answerable from the artifact rather than by re-running. |
| `generated_on` as a parameter | Rendering must be a pure function of its inputs, or the P6 golden test could never compare two renders. |
| `readability_score()` | The candidate pool needs a deterministic order; the model picks from a pre-vetted set, so what gets into that set is a code decision. |
| `render_email_body()`, `email_subject()` | Stage 10 needs them in Phase 4; cheap to write beside the renderer they share a template with. |

### The bug this phase found

**The model fabricated quote provenance.** The verbatim check protected the quote *text*
and nothing else — so `Quote.review_id` and `Quote.rating` were fields the model filled in
freely. In a real run it invented ids (`r001`, `r002`, `r003`) that existed nowhere in the
corpus, and guessed star ratings: "Onboarding is clean and fast" was published as **2★**
when the source review was **5★**, and an unanswered-ticket complaint as **4★** when it was
**2★**.

A verbatim quote under a fabricated rating is still a misleading artifact. Root cause was an
inconsistency in the architecture itself: §9 specified a provenance check that §7's
text-only candidate type made impossible to write. Candidates now carry provenance, stage 7
**derives** both fields from the matched review rather than trusting them, and the number of
corrections is recorded in the ledger — a model that invents ids is worth knowing about even
when the correction is automatic.

At this point problemStatement.md §8 is satisfied **except** the two MCP delivery lines.

---

## 7. Phase 4 — Contract, stub server, MCP client, publish ✅ COMPLETE against the stub (2026-10-15)

🔗 **X1 not yet signed off.** Built ahead of it on purpose: the contract file (P4-T1) *is* the
thing to review, and the stub is where any change is cheapest to absorb. P4-T2 is the one task
that cannot be closed from this repo.

**Goal:** stages 9–10 fully working against an in-repo stub, so live integration is later a config
change.

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P4-T1 | Write `pulse-mcp/1` as JSON Schema — four tools, inputs and outputs, error-code enum | `contract/pulse-mcp-1.json` | ✅ + error payload schema, version format |
| P4-T2 | Share it with the servers project; confirm it is the mirrored canonical copy (🔗 X3) | — | ⊘ superseded — the server shipped its own interface; see §8.1 |
| P4-T3 | Stub MCP server: in-memory docs store, find/create/replace with real revision semantics, `create_draft`; advertises contract version in `initialize` | `tests/stubs/stub_mcp_server.py` | ✅ + optional state file |
| P4-T4 | Stub fault injection: force any error code (`AUTH_REQUIRED`, `REVISION_MISMATCH`, `RATE_LIMITED`, …) via env var or tool arg | `tests/stubs/stub_mcp_server.py` | ✅ env only (`PULSE_STUB_FAULTS`); a tool arg would violate the contract's `additionalProperties: false` |
| P4-T5 | `ClientSession` lifecycle over stdio, with timeouts | `src/pulse/mcp/session.py` | ✅ |
| P4-T6 | Discovery: `list_tools()`, contract-version check (warn on minor, abort on major), configured-name resolution with a helpful failure listing actual names | `src/pulse/mcp/discovery.py` | ✅ + pagination |
| P4-T7 | `DocsPort` / `GmailPort` protocols + `McpDocsAdapter` / `McpGmailAdapter`. **Only file in the repo that knows a tool name** | `src/pulse/mcp/adapters.py` | ✅ ports in `publish/ports.py` |
| P4-T8 | Validate every tool response against the contract schema before use (architecture.md §18 trust boundary) | `src/pulse/mcp/adapters.py` | ✅ outputs, errors, and our own inputs |
| P4-T9 | Stage 9: find-then-create/replace, title convention `Groww Review Pulse — <ISO week>`; `REVISION_MISMATCH` reports the conflict rather than overwriting | `src/pulse/publish/docs.py` | ✅ |
| P4-T10 | Stage 10: draft only, body = full note + doc link | `src/pulse/publish/gmail.py` | ✅ |
| P4-T11 | `RATE_LIMITED` honoured via `retry_after_seconds`, ≤3 attempts | `src/pulse/publish/` | ✅ `retry.py`; 2s×attempt without a hint, capped at 60s |
| P4-T12 | Partial-failure handling: doc succeeded + draft failed is recorded and resumable with `--from 10` | `src/pulse/orchestrator.py` | ✅ ledger `partial`; CLI prints the exact resume command |
| P4-T13 | `--mcp stub` / `--mcp live` switch; startup probe runs **before** any LLM call | `src/pulse/cli.py` | ✅ probe in `pipeline.preflight`, before stage 1 |
| P4-T14 | `pulse contract check` | `src/pulse/cli.py` | ✅ |
| P4-T15 | Real `doctor` MCP probes, replacing the P0 stubs | `src/pulse/cli.py` | ✅ in `doctor.py` |
| P4-T16 | `test_mcp_contract.py` — stub's advertised schemas match the contract file | `tests/test_mcp_contract.py` | ✅ 15 tests |
| P4-T17 | `test_publish.py` — happy path, idempotent re-run, and each error code's handling | `tests/test_publish.py` | ✅ 23 tests, 8 through the real stub |
| P4-T18 | `test_mcp_discovery.py` — missing tool, version mismatch (minor and major) | `tests/test_mcp_discovery.py` | ✅ 19 tests |

### What landed beyond the task list — Phase 4

| Addition | Why |
| --- | --- |
| `mcp/contract.py` | One loader for the contract file, used by the client, the stub, and `contract check`, so the three cannot disagree about it. |
| Stub state file (`PULSE_STUB_STATE`) | Each stage spawns its own server process, so purely in-memory state could never show the exit criterion's "same doc URL on a second run". Set in `[stub] env` in `config/mcp.toml`. |
| `python` in `mcp.toml` means `sys.executable` | Otherwise the stub runs under whatever Python is first on PATH, cannot import `pulse`, and fails as a bare "connection closed". |
| Stderr tail in "server won't start" errors | The servers project not being installed shows up as `No module named pulse_mcp_servers` in the message, rather than "connection closed". |
| Stage 9 re-scans `pulse.md` for PII | It is a file on disk between stages 8 and 9 and can be edited; publishing re-checks the exact bytes it sends. |
| `09_published.json`, `10_draft.json` | `--from 10` needs the published URL, and the run directory should show what was published without opening the ledger. |
| `mcp>=2.3,<3`, explicit `jsonschema` | The code targets the 2.x SDK (snake_case fields, handler-based server); `mcp>=1.2` would have admitted an incompatible install. |

### Bugs this phase found

| Bug | Impact if shipped |
| --- | --- |
| **SDK errors arrive as nested `ExceptionGroup`s** — including the caller's own exceptions | Every MCP failure would have surfaced as an unreadable group-of-groups traceback, and the pipeline's `except McpClientError` would never match. `session.py` unwraps to the real error; a test raises inside a session and asserts it arrives intact. |
| **The PII digit backstop redacted ISO dates.** `\d[\d\s\-]{7,}\d` counted *characters*, so `2026-10-01` (8 digits) matched | Caught when a note rendered with a date failed its own pre-publish scan. The same rule would have redacted "stuck since 2026-10-01" in review text, degrading quotes. Now counts ≥9 digits; pinned in the scrub negative table. |

### Contract findings — for the X1 review

1. **Gmail authorization cannot be probed.** `list_tools()` works without credentials by
   contract, and Gmail's only tool creates a draft. So an unauthorized Gmail server is found
   at stage 10, *after* LLM spend — exactly what §11.6 set out to prevent. Docs is probed with
   the read-only `find_document`. Proposal for `pulse-mcp/1.1`: a read-only
   `gmail.auth_status`, or `AUTH_REQUIRED` surfaced at `initialize`.
2. **Version location and error shape were unspecified** in §11.2's prose. The contract file
   now fixes both (`serverInfo.version = "pulse-mcp/1.0"`; error object in
   `structuredContent`). The servers project should confirm.
3. **Drafts are not idempotent.** Re-running a week creates a second draft (seen in the exit
   run: `draft-0002`, then `draft-0003`). Docs are idempotent by title; Gmail has no
   find-draft tool. Acceptable for a weekly job; a `gmail.find_draft` would close it if needed.
4. **X4 is handled without a decision:** `find_document` may return `revision: null`, and
   the agent then sends `if_revision: null`, which is last-write-wins by contract.

```bash
# ✅ Exit — all verified 2026-10-15
pytest -q                                   # 729 passed (incl. 57 Phase 4 tests)
ruff check . && ruff format --check . && mypy src
pulse contract check                        # OK stub; SKIP docs/gmail (live) — not built yet
pulse run --mcp stub --input tests/fixtures/reviews_sample.json --week 2026-W41
#   → Document: https://docs.stub.invalid/d/doc-0001   Gmail draft: draft-0002 (not sent)
pulse run --mcp stub --input tests/fixtures/reviews_sample.json --week 2026-W41
#   → Document: https://docs.stub.invalid/d/doc-0001   (same URL; stub holds 1 doc, revision 2)
```

Both runs set `PULSE_RECIPIENT`; without it the preflight aborts before stage 1, as it should.

### Implementation notes — Phase 4

**Build the stub before the client.** It is the executable form of the contract; writing it first
surfaces ambiguities in §11.2 while they're still cheap to fix — and any ambiguity you find is one
the servers project would otherwise hit later.

⚠️ **Probe ordering is a cost control, not a nicety.** The startup probe must run before stage 4.
Discovering an unauthorized server after classifying 1,200 reviews wastes the whole run's spend.
Test this explicitly: configure a failing server and assert zero LLM calls happened.

⚠️ **Idempotency is ours.** The servers are permitted to be dumb about it. The re-run test (P4-T17)
must assert the same document URL comes back, not merely that a doc exists — otherwise a weekly job
scatters duplicates across Drive and nobody notices for a month.

🔗 **X4**: if the Docs server can't supply a stable `revision`, drop `if_revision` and accept
last-write-wins. Decide with the servers project rather than silently sending `null`.

```bash
# ✅ Exit
pytest -q                                              # incl. contract, publish, discovery
pulse contract check
pulse run --mcp stub --input tests/fixtures/reviews_sample.json
pulse run --mcp stub --input tests/fixtures/reviews_sample.json   # same doc URL, no duplicate
```

---

## 8. Phase 5 — Live integration against `google-workspace-mcp` ✅ live (2026-10-19); §15 docs pending

**Unblocked:** the servers project exists and is deployed.

| | |
| --- | --- |
| Endpoint | `https://mail-docs-mcp-server-production.up.railway.app/mcp` |
| Source | [vikramragu/Mail-Docs-MCP-Server](https://github.com/vikramragu/Mail-Docs-MCP-Server), read at commit `75adcf6` |

**Goal (unchanged):** a real Google Doc and a real Gmail draft.

**What changed:** the deployed server does **not** implement `pulse-mcp/1`. It is a general-purpose
Google Workspace server with its own interface. Below, everything was read from the server's
README and source; none of it has been checked against the live endpoint yet (P5-T1 does that).

### 8.1 Gap analysis — `pulse-mcp/1` (what P4 built against) vs. the deployed server

| Area | `pulse-mcp/1` (our contract and stub) | Deployed server | Impact on the agent |
| --- | --- | --- | --- |
| **Transport** | stdio subprocess | Streamable HTTP, **stateless**, `POST /mcp`; `GET /healthz` unauthenticated | `session.py` needs an HTTP transport (X2 resolved: remote HTTP) |
| **Auth to the server** | none (local process) | `Authorization: Bearer <MCP_AUTH_TOKEN>` | Token from env (`PULSE_MCP_TOKEN`), never in config or the repo |
| **Servers** | two (docs, gmail) | **one** server, both tool groups (X5 resolved) | One `[live]` entry instead of `[docs]` and `[gmail]` |
| **Version** | `serverInfo.version = "pulse-mcp/1.0"` | `serverInfo = {name: "google-workspace-mcp", version: "0.1.0"}` | Current discovery **aborts** ("not a contract version"). Must check name + semver instead. |
| **Tool names** | `docs.find_document`, … | `ping`, `docs_get_info`, `docs_append_content`, `gmail_create_draft`, `gmail_send_email` | Names are config-driven, but the *semantics* differ (next rows) |
| **Find doc by title** | `find_document(title)` | ✗ none. `docs_get_info(document)` takes an ID or URL; returns title, tabs, URL | Agent can't discover the doc; **the doc ID must be configured** |
| **Create doc** | `create_document` | ✗ none (a stated non-goal: "no auto-create") | The doc is **created once by hand** and shared with the authorized account |
| **Replace body** | `replace_document_body(if_revision)` | ✗ none. `docs_append_content(document, blocks[], tab_id?, add_separator)` only appends | **Publishing model changes:** one standing doc, a new section per week (see 8.2) |
| **Content format** | CommonMark string | `blocks[]`: `paragraph`, `heading1`-`3`, `bullet`, `numbered`; whole-block `bold` only; ≤200 blocks, ≤10k chars each | New **blocks renderer**. Block quotes, inline bold/italic and links have no equivalent. Quotes must still be verbatim. |
| **Revision conflicts** | `REVISION_MISMATCH` to the agent | Handled inside the server (`writeControl` + one retry) | Agent-side conflict handling becomes unreachable; keep the code, drop it from the live path |
| **Gmail draft** | `to: str, body_text, body_html` → `{draft_id, thread_id}` | `to: str[], subject, body, body_format: text\|html, cc, bcc, …` → `{status, draft_id?, message_id?, to[]}` | Adapter argument and result mapping |
| **Send tool** | deliberately absent | **`gmail_send_email` present**; `ENABLE_GMAIL_SEND=true` on the live deployment | The agent must never bind it. Ask for it to be disabled for this agent (8.3, D-1) |
| **Errors** | `structuredContent.code`, one of 6 codes | **Prose only:** `isError: true` + text, no `code`, no `structuredContent` | Current adapter rejects every error as a protocol violation. Needs a message → code map. |
| **Rate limits** | `RATE_LIMITED` + `retry_after_seconds` | Server retries 429/5xx itself (3×), then "Google API temporarily unavailable." | Agent's retry layer rarely fires. Map that message to `UPSTREAM_ERROR`. |
| **Auth probe** | `find_document` (Docs only) | `docs_get_info(<configured doc>)` is read-only, and **one OAuth token covers both APIs** | Better than P4: probing Docs also proves the shared token. Only a missing Gmail *scope* stays unprobeable. |
| **Dry run** | — | `DRY_RUN=false` live: real writes | First live run goes to a **scratch doc** (P5-T8) |

**X1 is superseded rather than signed off.** The servers project defined its own interface, and
its README lists create/replace as non-goals. The two ways forward:

- **A. Adapt the agent to the server** — **recommended, and planned below.** The server is
  deliberately client-agnostic and already tested and deployed. Every change on our side lands
  in the modules built to absorb it: `mcp/`, `publish/`, `render.py`, config.
- **B. Add `pulse-mcp/1` tools to the server** (`find_document`, `create_document`,
  `replace_document_body`, structured error codes). That reopens the server's non-goals, and
  the server team carries the work. Choose B only if per-week documents are a hard requirement.

### 8.2 Publishing model under option A

- **One standing doc**, e.g. "Groww Review Pulse". It is created by hand and its ID is set as
  `[docs] document_id` (or `PULSE_DOC_ID`).
- Each week's note is **appended as a section**: a `heading1` reading
  `Groww Review Pulse — <ISO week>`, then the note's blocks.
- **Idempotency moves to the agent's own record.** The server can't read the doc body, so the
  agent can't check whether a week's section already exists. Before appending, stage 9 checks a
  local publish record keyed by `(document_id, iso_week)`, built from previous runs'
  `09_published.json`. If the week is already there, it skips with a message.
  `--republish` appends a second, marked section deliberately.
  This is weaker than P4's find-by-title: it is local state, so a clean checkout forgets it.
  Recorded as a risk.
- The doc URL in the draft points at the standing doc, plus the week's tab or heading where
  possible.

### 8.3 Decisions needed before or at the start of Phase 5

| # | Decision | Recommendation |
| --- | --- | --- |
| D-1 | `gmail_send_email` is live and enabled on a public URL | **Decided 2026-10-15: `ENABLE_GMAIL_SEND=false` for now; revisit later.** Set in Railway by the owner. The agent only needs `ENABLE_GMAIL_DRAFTS` and `ENABLE_DOCS_APPEND`, and also refuses to bind any `send` tool (P5-T6). If the agent should later *send* rather than draft, that is a requirements change: the problem statement asks for a draft. It would need a deliberate decision and a change to the P5-T6 guard, not just flipping this switch. |
| D-2 | Option A (adapt agent) vs B (extend server) | **Decided 2026-10-16: A.** Implemented in 8.4. |
| D-3 | Standing doc vs per-week docs | **Standing doc** (follows from A) |
| D-4 | Which Google account and doc | Create the doc in the account the server's OAuth token belongs to, and record its ID |
| D-5 | `ALLOWED_RECIPIENT_DOMAINS` on the server | Set it to the pulse recipient's domain, so a misconfigured agent can't draft to anyone |

### 8.4 Tasks

| ID | Task | Files | Done |
| --- | --- | --- | --- |
| P5-T1 | **HTTP transport + live smoke test.** Streamable HTTP in `session.py`, selected by `transport = "http"` + `url` in `config/mcp.toml`. Bearer token from `PULSE_MCP_TOKEN` (env / `.env` only). Then `ping` and `list_tools()` against the live URL, saving the advertised schemas for P5-T2 | `src/pulse/mcp/session.py`, `config/mcp.toml`, `config.py` | ✅ code; ☐ live smoke test waits on `PULSE_MCP_TOKEN` |
| P5-T2 | **Contract for the real server.** `contract/google-workspace-mcp-0.1.json` holds the three tools the agent uses (`docs_get_info`, `docs_append_content`, `gmail_create_draft`) exactly as served. Discovery checks `serverInfo.name` plus semver (0.x: a minor change counts as breaking). Retire `pulse-mcp-1.json`, keeping it in git history | `contract/`, `mcp/contract.py`, `mcp/discovery.py` | ✅ consumer-driven (below); `pulse-mcp-1.json` moved to `contract/archive/` (it was never committed) |
| P5-T3 | **Rewrite the stub to the real server's surface**: same tool names, schemas, prose-only errors, and `DRY_RUN`-style behaviour. Every Phase 4 test keeps exercising the interface that actually exists | `tests/stubs/stub_mcp_server.py` | ✅ + HTTP mode with bearer auth |
| P5-T4 | **Ports and adapters.** `DocsPort`: `get_info(document)`, `append(document, blocks)`. `GmailPort`: `create_draft(to, subject, body, body_format)`. Error **message → code map** (auth / not found / no access / unavailable / allowlist / feature disabled / validation). An unrecognised message maps to `UPSTREAM_ERROR` with the text preserved | `publish/ports.py`, `mcp/adapters.py` | ✅ |
| P5-T5 | **Blocks renderer** from `PulseNote`: heading → `heading1`/`2`, themes and actions → `bullet`, quotes → `paragraph` holding the exact quote text in quotation marks, headline → bold paragraph. Tests: quote text survives byte-for-byte; no markdown syntax leaks; the block count and text sizes stay within server limits; the word count matches `pulse.md` | `rendering/render.py`, `tests/test_render.py` | ✅ |
| P5-T6 | **Stage 9/10 rework.** Stage 9 runs get-info then append under the week heading, with the publish-record idempotency from 8.2 and `--republish`. Stage 10 calls `gmail_create_draft` with `to=[recipient]`, `body_format="text"`. **Never bind `gmail_send_email`**: binding refuses any tool with `send` in its name, and `contract check` warns if one is advertised | `publish/docs.py`, `publish/gmail.py`, `pipeline.py`, `cli.py` | ✅ + stage 10 skips a draft the week already has |
| P5-T7 | **Preflight for the real server:** `ping`, then `docs_get_info(configured doc)`. This proves the token, the doc's existence and access, and Docs auth before any LLM spend. Missing config (`PULSE_MCP_TOKEN`, doc ID, recipient) aborts with what to set | `pipeline.py`, `doctor.py` | ✅ (`ping` not needed: `initialize` proves reachability) |
| P5-T8 | **First live run against a scratch doc** (`PULSE_DOC_ID` = a throwaway doc), because the server's `DRY_RUN` is off. Check the doc's formatting by eye and that the draft is in **Drafts, not Sent** | — | ✅ 2026-10-19: 18 blocks appended to the scratch doc; draft in Drafts, nothing sent |
| P5-T9 | **Real run** against the standing doc. Then re-run the same week: confirm no duplicate section, and record the second draft (drafts are still not idempotent) | — | ✅ 2026-10-19: appended to the standing doc, new draft linking to it; the re-run skipped both |
| P5-T10 | **Contract tests against live:** `pulse contract check` compares the live `list_tools()` with `contract/google-workspace-mcp-0.1.json`; record any drift | `tests/test_mcp_contract.py` (opt-in live marker) | ✅ 2026-10-19: `OK live`, no drift |
| P5-T11 | **Docs:** rewrite architecture.md §11 (contract, publishing semantics, auth), §15 failure modes, X1–X5 outcomes; record the deviations here | `docs/architecture.md` | ◐ architecture.md §11 updated; §15 pending |

⚠️ **Quotes stay verbatim through the block conversion.** Stage 7 proves quotes are exact
substrings of reviews in `pulse.md`. The blocks renderer is a second render path, so P5-T5 must
prove the same quote bytes reach the doc. Otherwise the guarantee holds for the file and not
for the published artifact.

⚠️ **Live writes are real** (`DRY_RUN=false`). Use a scratch doc, and `ALLOWED_RECIPIENT_DOMAINS`
if possible, until P5-T8 has passed by eye.

⚠️ **Verify the draft was not sent.** Check the Drafts folder, not Sent. Unlike under
`pulse-mcp/1`, a send tool now *exists* on the server, so this is no longer structurally
impossible. That is why D-1 and the P5-T6 guard matter.

### 8.5 Results so far (2026-10-16)

Option A is built and green against the rewritten stub, over **both** stdio and HTTP with
bearer auth. The live server has only been checked at `/healthz` (200), because no token is
configured yet. Everything below runs offline.

| Measure | Result |
| --- | --- |
| Test suite | **771 passed**; ruff and mypy clean |
| Contract check | `OK stub` (3 tools accept our requests and declare the fields we read); `SKIP live` (no `PULSE_MCP_TOKEN`) |
| `pulse doctor` | `OK mcp-stub`; `WARN mcp-live` (no token); `WARN mcp-doc` (no `PULSE_DOC_ID`) |
| CLI stages 9-10 vs stub, replayed 3× into one run | 1 section appended, 1 draft, **0 sends**; runs 2-3 report `skipped` |

**The contract is consumer-driven, not a copy of the server's schemas.** The server owns its
schemas and may add optional fields without telling us. `contract/google-workspace-mcp-0.1.json`
records only what the agent relies on:

- an example request per tool, which must validate against the server's *advertised* input
  schema (draft-07 aware);
- the response fields the agent reads, which must stay declared in its output schema;
- the server's name and semver;
- the ordered message → error-code rules.

`pulse contract check` asserts all of these against the stub and the live server.

#### Bugs this phase found

| Bug | Impact if shipped |
| --- | --- |
| **`record_stage(..., **detail)` crashed when a stage's detail contained `status`** | Every stage 9/10 completion would have raised `TypeError` inside the ledger. Detail is now passed as a dict. |
| **Replaying into the run that published overwrote its `appended` record with `skipped`** | The only evidence a week was in the doc was erased, so the *next* replay appended it again. Skips never overwrite a real record now; pinned by `test_replaying_into_the_same_run_never_erases_the_record`. |
| **The SDK reports HTTP 401 as a generic "Server returned an error response"** | A wrong `PULSE_MCP_TOKEN` would have read as "server could not be reached". The session records 401/403 itself, so the error names the token. |
| **Stage 8 rendered `pulse.md` without `corpus_total`** | The "N substantive reviews of M in window" disclosure required by §5.1 never reached the published note. Fixed in both renderers. |
| **The Phase 4 stub's state file crashed the Phase 5 stub** | A stale `.cache/stub-mcp-state.json` made `--mcp stub` fail as "exited during startup". The state file is versioned; an old one is ignored. |

#### First real-data note (2026-10-19) — figures moved from the model to the code

The live server now passes `pulse doctor` and `pulse contract check` (token, doc access,
three tools, and no send tool advertised: D-1 applied). Stages 5–8 then ran on the real
corpus for the first time. The note passed every gate, but a read against `05_stats.json`
found three faults the gate could not see:

| Fault | Cause | Fix |
| --- | --- | --- |
| "up 5.6% week-over-week" | The model restated `delta_vs_baseline`, a 5.6-*point* change against the pooled prior 11 weeks | Figures are printed by `render.figures_text` from `ThemeFigures`, attached by `ground_theme_summaries` (like quote provenance) and hidden from the model's schema. New `figures` check rejects any digit in the headline or theme summaries |
| "execution improves" | The model read a higher mean rating as a trend; the theme actually rose 3.3 points | Prompt: "improving" only when direction is down; rating is not a trend. `direction` and `review_count` are now overwritten from stats (`direction_of`, shared by prompt and gate) |
| A failed re-draft left the previous `06_note.json` and `pulse.md` on disk | `--from 9` would have published a note the run had just rejected | Stage 6 deletes both on abort; pinned by `test_a_failed_redraft_removes_the_previous_note` |

The figures cost ~36 words. With only a total budget the model overshot three times
(288 → 286 → 272) and the run aborted; per-field word limits in the prompt fixed it (223
words, first attempt). Also fixed: the draft loop's final re-render dropped `corpus_total`.

**Waiting on the owner before P5-T8:**

- `PULSE_MCP_TOKEN` in `.env`;
- the scratch and standing doc IDs (D-4);
- `PULSE_RECIPIENT`;
- `ENABLE_GMAIL_SEND=false` on Railway (D-1).

```bash
# ✅ Exit
pulse doctor                         # live server reachable, token OK, doc accessible
pulse contract check                 # live tools match contract/google-workspace-mcp-0.1.json
pulse run --mcp live                 # week section appended to the standing doc; draft id in ledger
pulse run --mcp live                 # same week: skipped, no duplicate section
pytest -q                            # offline, against the rewritten stub
```

---

## 9. Phase 6 — Hardening

**Goal:** green from a clean checkout; every acceptance-checklist line verifiable.

| ID | Task | Files |
| --- | --- | --- |
| P6-T1 | `test_no_google_sdk.py` — fail if `googleapiclient`, `google-auth`, `google-api-python-client`, or `oauth2client` resolve in the dependency tree | `tests/test_no_google_sdk.py` |
| P6-T2 | Golden end-to-end test: fixture corpus + committed classification → note render, fully offline | `tests/test_golden.py` |
| P6-T3 | Complete the run ledger: corpus size, per-theme counts, tokens, cost, cache rate, attempts, doc URL, draft id, wall time | `src/pulse/orchestrator.py` |
| P6-T4 | End-of-run cost summary line, with cache hit rate called out | `src/pulse/cli.py` |
| P6-T5 | README: install, configure, drop an export, run, interpret output, troubleshoot | `README.md` |
| P6-T6 | Runbook: weekly operation, scheduling (cron/CI), what to do on each failure mode in architecture.md §15 | `docs/runbook.md` |
| P6-T7 | ✅ 2026-10-19. O6 decided and implemented: `01_raw.json` deleted after a run succeeds through stage 3 (ledger `raw_artifact_deleted`), kept on failure; `pulse fetch` prunes its exports fetched before the window (`prune_old_exports`) | `pipeline.py`, `config.py`, `ingest/play_store.py`, `cli.py` |
| P6-T8 | Walk problemStatement.md §8 line by line, tick each with the command that proves it | `docs/problemStatement.md` |
| P6-T9 | Capture one real pulse as a reviewed sample output (PII-checked before committing) | `docs/sample-pulse.md` |

⚠️ **P6-T7 is the last privacy loose end.** `01_raw.json` is the one artifact that may contain PII
as exported. Either prune it after a successful run or document explicitly why it's retained and
for how long. Don't leave it undecided.

```bash
# ✅ Exit — from a clean clone
git clone <repo> && cd agent-mcp
pip install -e ".[dev]"
pytest -q                        # everything green, no network, no servers project
pulse run --dry-run --input tests/fixtures/reviews_sample.json
```

---

## 10. Traceability: acceptance criteria → phase

Every line of problemStatement.md §8, mapped to where it gets satisfied and what proves it.

| Acceptance criterion | Phase | Proof |
| --- | --- | --- |
| Play Store reviews, last 8–12 weeks, public export | P1 | `test_normalize.py` window tests; `Source` enum |
| Clustered into ≤5 themes | P2 | `Theme` enum; `test_models.py` |
| Note ≤250 words | P3 | `count_note_words()`; `test_validate.py` boundary at 250/251 |
| Top 3 themes | P3 | `test_aggregate.py` ranking; `PulseNote` cardinality |
| 3 verbatim quotes, no PII | P3 | `test_validate.py` paraphrase rejection + PII rescan |
| 3 action ideas tied to themes | P3 | `PulseNote` cardinality + action-grounding check |
| Published to a Google Doc via MCP | P4 (stub) → P5 (live) | `test_publish.py`; live run |
| Gmail draft via MCP with note or link | P4 (stub) → P5 (live) | `test_publish.py`; Drafts folder |
| No PII in any artifact | P1 + P3 | `test_scrub.py` both tables; pre-publish rescan |
| No direct Google REST/OAuth path | P4 | All Google access via `pulse-mcp/1` |
| No Google SDK in the dependency tree | P6 | `test_no_google_sdk.py` |

---

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| ~~**O1 never resolves**~~ — no compliant public Play export is found | **Retired 2026-10-12** | — | Resolved by `pulse fetch`, outside the pipeline so the analysis stays file-driven. **The residual risk is not technical:** the fetch reads Google's undocumented endpoints, which sits in tension with §6's "no ToS-violating automation". Accepted knowingly for a learning project and documented in architecture.md O1; a licensed provider is the swap for anything commercial, and costs one new module. A second caveat: `lang=en` makes the corpus an English-speaking subset, not a representative sample. |
| **X1 stalls** — contract not agreed by Phase 4 | Medium | Medium | Send §11.2 for review on day 1. P4 has four phases of lead time; nothing else depends on it. |
| Servers project late | Medium | Low | Only P5 blocks. P6 can run before P5. |
| Model won't produce verbatim quotes reliably | **High** | Medium | Expected, not exceptional. Candidate pool + substring check + feedback loop; if 3 attempts prove insufficient in practice, narrow the pool and sharpen the prompt rather than loosening the check. |
| Prompt cache silently missing | Medium | Medium (cost) | Assert `cache_read_input_tokens > 0` in P2; surface the rate in every run summary. |
| Scrubber over-redacts, degrading quotes | Medium | Medium | Negative test table (P1-T10); candidate filter drops placeholder-containing reviews. |
| Thin target week produces a meaningless note | Low | Low | < 20 reviews aborts (P3-T5). |
| Theme taxonomy doesn't fit the real corpus | Medium | Medium | Visible at P2 on real data (P2-T17). Change enum members and bump `taxonomy_version`; do not add a sixth theme. |
| Substance filter misread as population sample | **High** | Medium | Measured 2026-10-15: the >8-word English subset is 22% of the window and is complaint-skewed (1★ > 5★, versus 77% 5★ raw). The note discloses `corpus_size of corpus_total`, and ratings are labelled as describing substantive reviews (§5.1). |
| Hinglish heuristic drops real English | Medium | Low | Precision measured on a hand-labelled sample (P2-T16), with false-positive cases pinned as tests. |
| Contract gaps found only at P5 | **Happened** (2026-10-15) | Medium | Larger than expected: the server has a different interface altogether, not small schema drift (§8.1). Contained as designed: the analysis half is untouched, and the changes land in `mcp/`, `publish/` and the renderer. P5 grew from ~1 day to 2–3. |
| A send tool is reachable from the agent's server | Medium | **High** | `gmail_send_email` is enabled on the live deployment. Disable it for this agent (D-1), and have the agent refuse to bind any `send` tool (P5-T6). |
| Week idempotency lives in local state | Medium | Low | The server can't read doc bodies, so a clean checkout forgets which weeks were published, and a re-run duplicates a section. The publish record makes it visible; `--republish` makes duplication deliberate. |

The two rows worth re-reading are the first and the fourth. The first is the only risk that can
make the project undeliverable, and it is not something code can fix. The fourth is near-certain but
fully handled — it's the reason the validation gate exists.

---

## 12. Suggested commit sequence

One commit per task where practical, prefixed with the task ID:

```text
P0-T2: add pyproject with pinned deps and src layout
P0-T3: add Pydantic data model from architecture §7
P1-T7: add PII scrubber layers 1-3
P1-T10: add scrub test tables, positive and negative
P3-T13: enforce verbatim quotes by substring verification
P4-T3: add stub MCP server implementing pulse-mcp/1
```

**Definition of done for any task:** code written, tests written and passing, `ruff` and `mypy`
clean, and — if the task touched a documented decision — architecture.md updated in the same commit.

Keep the architecture document current as you build. Where implementation proves a design decision
wrong, change the decision in architecture.md and note why; a plan that silently diverges from its
architecture leaves neither document trustworthy.
