# Groww Review Pulse — Project Context

> Canonical context for this project. Source of truth: [problemStatement.txt](problemStatement.txt).
> Last updated: 2026-10-09

---

## 1. Problem Statement

Build a system for the **Groww** platform (Groww Stocks, Mutual Fund, IPO — Android app) that turns
raw mobile-store feedback into a **weekly pulse a team can scan in minutes**: what users care about,
what they actually said, and what to do next.

Reviews are already public. The job is to **aggregate, theme, summarize, and deliver** that insight
through familiar surfaces — **Google Docs** for the written pulse and **Gmail** for a draft the user
can send themselves — *without handling credentials or REST wiring directly*.

**Target app:**
`https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN`
(package: `com.nextbillion.groww`)

---

## 2. End-to-End Flow (what "done" looks like)

1. **Pull** recent App Store and Play Store reviews for the product (within the rules in §6).
   → **Scope decision: Play Store only.** See the scope note below.
2. **Cluster** them into a small set of themes.
3. **Distill** a one-page weekly note.
4. **Publish** that note where stakeholders can read it → **Google Docs**.
5. **Draft** an email to self (or an alias) containing or linking to the pulse → **Gmail**.

> **Scope note — Play Store only (decided 2026-10-09).**
> The source brief names both stores but supplies only a Play Store URL, and the product is
> Android-first. The Apple App Store is therefore **deferred**, not dropped: it is out of scope for
> the current build and reconsidered after the first working pulse. Step 1 above is kept in the
> brief's original wording so the requirement stays traceable to
> [problemStatement.txt](problemStatement.txt); everywhere this project says "reviews" it now means
> Play Store reviews. Recorded as D10 in [architecture.md](architecture.md).

---

## 3. Deliverables

The **weekly one-page pulse** must include:

| Element | Requirement |
| --- | --- |
| **Top themes** | What people are talking about most — top **3** themes in the written note |
| **Real user quotes** | **3** verbatim snippets from actual reviews — *no invented wording* |
| **Three action ideas** | **3** concrete next steps, grounded in the identified themes |

Final step: **a Gmail draft** addressed to self or an alias containing the weekly note, or a clear
pointer (link) to it.

---

## 4. Who This Helps

| Audience | Why |
| --- | --- |
| **Product / Growth** | Prioritize fixes and improvements from real signals |
| **Support** | Align messaging with what users are actually saying |
| **Leadership** | One-page health check without drowning in raw reviews |

---

## 5. What Must Be Built

- **Import** **Play Store** reviews from roughly the **last 8–12 weeks** (App Store deferred — see
  the §2 scope note). Fields as available in the export: `rating`, `title`, `text`, `date` —
  whatever the export provides.
- **Group** reviews into **at most 5 themes**.
  Examples given: onboarding, KYC, payments, statements, withdrawals — pick what fits the product.
- **Generate** the weekly one-page note containing:
  - Top 3 themes (an appropriate subset of the ≤5 clustered themes)
  - 3 user quotes
  - 3 action ideas
- **Draft** an email with the note to self or an alias.

---

## 6. Key Constraints

These are hard requirements, not preferences.

| Constraint | Rule |
| --- | --- |
| **Reviews** | Public review **exports only**. No scraping behind store logins, no ToS-violating automation. |
| **Themes** | Maximum **5** themes for clustering; the written pulse highlights the **top 3**. |
| **Length** | Keep the note scannable — **≤250 words** where applicable. |
| **Privacy** | **No PII** in any artifact — no usernames, emails, device IDs, or other identifiable reviewer data. Quotes must be anonymous / stripped as needed. |
| **Integration** | **MCP-first** (see §7). No bespoke OAuth + REST client as the primary integration path. |

---

## 7. Integrations: Google Docs & Gmail via MCP

Use **MCP (Model Context Protocol) servers** for Google Docs and Gmail — e.g. creating or updating
the pulse document and creating the draft message — **rather than integrating Google APIs directly**.

- No bespoke OAuth client + REST client code as the primary integration path.
- MCP servers expose tools the agent or app can call; lean on that pattern so Docs and Gmail stay
  consistent with the course tooling and avoid duplicating auth and HTTP plumbing.
- **The requirement is MCP-first, not "call Google APIs manually."**

### Project split: this repo is the agent

The system is delivered as **two separate projects**, each with its own problem statement,
architecture, and implementation:

| Project | Role | Owns |
| --- | --- | --- |
| **`agent-mcp`** (this repo) | **Agent / MCP client** | The pipeline: ingest → scrub → theme → note → constraint enforcement, and the MCP client calls that publish it |
| **MCP servers** (separate repo) | **MCP servers** | Google Docs and Gmail exposed as MCP tools; OAuth, token storage and refresh, Google REST calls |

Consequences for *this* repo's scope:

- Google OAuth, credential storage, and Google REST calls are **out of scope here** — they live in
  the servers project. This repo must contain no Google SDK dependency at all.
- The two projects meet at a versioned **MCP tool contract** (`pulse-mcp/1`), specified in
  [architecture.md §11.2](architecture.md). Agreeing that contract is the only cross-project
  dependency that blocks work here.
- This repo develops and tests against a contract-conforming **stub MCP server**, so neither
  project waits on the other. Live integration is a config change, not a code change.

---

## 8. Acceptance Checklist

- [ ] **Play Store** reviews imported from the last 8–12 weeks, from a public export source
- [ ] Reviews clustered into **≤5** themes
- [ ] One-page note written, **≤250 words**
- [ ] Note contains top **3** themes
- [ ] Note contains **3** verbatim user quotes (unaltered wording, no PII)
- [ ] Note contains **3** concrete action ideas tied to the themes
- [ ] Note published to a **Google Doc** via MCP
- [ ] **Gmail draft** created via MCP, containing the note or a link to it
- [ ] No PII anywhere in any artifact
- [ ] No direct Google REST/OAuth code used as the primary integration path
- [ ] No Google SDK in this repo's dependency tree (the servers project owns that)

---

## 9. Open Questions

### Resolved in [architecture.md](architecture.md)

| Question | Resolution |
| --- | --- |
| **Stack** | Python 3.11+; CLI driven, external scheduler (D1) |
| **Theme set** | Fixed closed taxonomy of 5: onboarding/KYC, orders/execution, payments/withdrawals, app reliability, charges/support (D4, §7) |
| **Cadence** | Repeatable weekly run over a rolling 8–12 week corpus; the note covers the latest ISO week with deltas against the baseline (D5) |
| **Orchestration** | Deterministic pipeline; LLM used only for classification and drafting (D3) |
| **MCP ownership** | Servers built in a separate project; this repo is the client (D8) |
| **Store scope** | Play Store only; Apple App Store deferred (D10, §2 scope note) |

### Still open

- **Export mechanism.** "Public review exports only" — which concrete Play Store export path
  satisfies this (e.g. a provided CSV/JSON dataset, Play Console export, a public review feed)?
  Note there is no official public export for a non-owner of the Groww app; this tension is real
  and unresolved. **This is now the main unresolved input question.**
- **Recipient.** Which address or alias receives the Gmail draft?
- **Contract sign-off.** Is `pulse-mcp/1` (architecture.md §11.2) accepted by the servers project?
  This is the only cross-project item that blocks work here.

Full lists: architecture.md §20 (agent-side O1–O6, cross-project X1–X5).

---

## 10. Current Project State

As of 2026-10-09, the repository contains only this documentation:

```text
agent-mcp/
└── docs/
    ├── problemStatement.txt      # original brief (source of truth)
    ├── problemStatement.md       # this file — context and requirements
    ├── architecture.md           # agent design, incl. the MCP tool contract
    └── implementation-plan.md    # phase-by-phase build plan with task IDs
```

No code, dependencies, or MCP configuration exist yet. Not yet a git repository.

The companion MCP servers project does not exist yet either. Per architecture.md §21, phases 0–4
and 6 of this repo proceed without it, developing against a stub server generated from the
contract; only phase 5 (live integration) requires it.
