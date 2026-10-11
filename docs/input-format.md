# Canonical review export format (P1-T2)

What the file-drop adapter reads. This is the **internal contract**, deliberately defined
before the concrete export source is settled (open decision O1), so the rest of the
pipeline could be built and tested without waiting on it.

When the real export is chosen, the work is either a column rename in `FIELD_ALIASES` or a
new adapter registered beside `file_drop` — not a change to anything downstream.

---

## Where files go

```
data/raw/                 # default, gitignored
```

Or pass `--input <file-or-directory>`. A directory is read in **sorted filename order** so
a multi-file corpus is deterministic; that is what keeps run artifacts byte-stable and the
Phase 6 golden test meaningful.

Accepted suffixes: `.json`, `.csv`, `.tsv`.

---

## Fields

| Canonical | Required | Type | Notes |
| --- | --- | --- | --- |
| `text` | yes¹ | string | The review body. Becomes `Review.text` verbatim after whitespace collapsing. |
| `rating` | yes | 1–5 | Coerced from string or float. Out-of-range values are **clamped** and counted. |
| `date` | yes | date or datetime | See accepted formats below. |
| `title` | no | string | Used **only** when `text` is absent. Never concatenated — see below. |
| `source` | no | `play_store` | Defaults to `play_store`. Any other value is skipped as out of scope (D10). |
| `external_id` | no | string | Carried for traceability. Not used for identity or dedupe. |

¹ A row with no `text` but a non-empty `title` is accepted, and the title becomes the text.
A row with neither is skipped.

### Accepted column aliases

Case-insensitive. The canonical name wins if both are present.

| Canonical | Also accepted |
| --- | --- |
| `rating` | `score`, `stars`, `star_rating` |
| `text` | `content`, `review`, `review_text`, `body`, `comment` |
| `title` | `subject`, `headline` |
| `date` | `submitted_at`, `at`, `review_date`, `created_at`, `timestamp`, `posted_at` |
| `external_id` | `review_id`, `reviewid`, `id` |

### Accepted date formats

ISO 8601 first (including a trailing `Z` and UTC offsets), then:
`YYYY-MM-DD HH:MM:SS`, `YYYY/MM/DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `Mon DD, YYYY`,
`DD Mon YYYY`, `Month DD, YYYY`.

An unparseable date means the row is skipped and counted — never silently dated "today",
which would misfile it into the current week and distort the pulse.

---

## Shapes

**JSON** — a top-level array, or an object wrapping one under `reviews`, `data`, `results`,
or `items`:

```json
[
  { "rating": 1, "text": "Withdrawal pending for four days.", "date": "2026-10-07" },
  { "rating": 5, "text": "Charts are excellent.", "date": "2026-10-06" }
]
```

**CSV / TSV** — a header row using canonical names or aliases. A UTF-8 BOM is tolerated,
since spreadsheet exports routinely add one:

```csv
rating,text,date
1,"Withdrawal pending for four days.",2026-10-07
5,"Charts are excellent.",2026-10-06
```

---

## Columns that are dropped

These are recognised as reviewer identity and **discarded at ingest** — they are not mapped
onto any field and are not kept as extras, so they never reach even `01_raw.json`:

`author`, `author_name`, `avatar`, `device`, `device_id`, `email`, `profile`,
`profile_name`, `reviewer`, `reviewer_name`, `user`, `user_image`, `user_name`, `username`

Their presence is logged once per run so an operator knows the export carried identity data
at all.

This is stricter than architecture.md §18 requires, and it is free: nothing downstream wants
these columns. It does **not** make the raw artifact safe — PII written inside a review body
survives until stage 3 — so `01_raw.json` is still the one artifact to treat as sensitive.

Any other unrecognised column is preserved in `RawReview.model_extra` and appears in
`01_raw.json` only. Normalization reads a fixed set of keys, so an unknown column cannot
reach stage 2 (architecture.md §10, layer 1).

---

## What happens to each row

```
01_raw.json        every row that had text-or-title and a date
02_normalized.json typed, deduplicated, window-filtered; identity gone
03_scrubbed.json   PII removed from the body; the first artifact safe to read freely
```

Rows are dropped for exactly these reasons, each counted in the run ledger:

| Stage | Reason |
| --- | --- |
| 1 | no text or title; no date; out-of-scope `source` |
| 2 | empty text; missing date; unparseable date; missing rating; duplicate |
| 2 | outside the configured window |
| 3 | text was entirely PII, leaving nothing quotable |

### Deduplication

Two rows are the same review when `sha256(source | whitespace-collapsed text | date)`
matches. Deliberately **excludes** rating, title, and every reviewer field, so:

- an export that corrects a rating does not fork the id (which would miss the Phase 2
  classification cache on every corrected row), and
- the id can never be identity-derived.

### Why title and text are never concatenated

Stage 7 verifies every published quote is an exact substring of `Review.text`
(architecture.md §9). If the text were `title + ". " + body`, a "verbatim" quote could
straddle punctuation nobody wrote. Keeping `Review.text` a contiguous span of real reviewer
prose is what makes the verbatim guarantee mean anything.

---

## Fixture

`tests/fixtures/reviews_sample.json` is a 263-row synthetic corpus spanning 12 ISO weeks,
regenerated deterministically by:

```bash
python scripts/make_fixture.py
```

Every review in it is invented, and its PII is unusable by construction (reserved example
domains, the `90000 00000` phone block, checksum-valid but unowned Aadhaar numbers).
Committing real Play Store reviews would put third-party text and plausibly real PII into
the repository — the exact thing this project promises not to do. It also makes the
scrubber's negative tests honest, because the ground truth for every row is known.
