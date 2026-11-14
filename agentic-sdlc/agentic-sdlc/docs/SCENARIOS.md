# Example scenarios

Every scenario below was produced by `./scripts/run_demo.sh`. The complete outputs are committed
under `runs/<id>/`, and each run's `workspace/ENGINEERING_SUMMARY.md` is the primary artifact to
review. Numbers quoted here come from those runs.

| Run | Scenario | Status | Executions | Repairs | Tests |
|---|---|---|---|---|---|
| `01-greenfield` | Mandatory URL shortener | ready for review | 17 | 0 | 90/90 |
| `02a-recovery-schema-drift` | + transient fault + SQL/schema drift | ready for review | 23 | 1 | 90/90 |
| `02b-recovery-api-defect` | + wrong redirect status | ready for review | 19 | 1 | 90/90 |
| `03-brownfield` | Feature + bug fix in an existing repo | ready for review | 12 | 0 | 17/17 (5 baseline + 12 new) |
| `04a-ambiguous-escalated` | Vague requirement, autonomous policy | **escalated** | 2 | 0 | — |
| `04b-ambiguous-answered` | Same, human answers recorded | ready for review | 17 | 0 | 90/90 |
| `05-crash-resume` | Crash after `gen_service`, then resume | ready for review | 13 + 4 | 0 | 90/90 |

---

## 1. Greenfield: the mandatory use case

**Input** (`examples/requirements/greenfield_url_shortener.md`)

> Build a scalable URL shortener service with APIs, persistence, and analytics.

### Requirement understanding
- Classified **greenfield**, domain `url_shortener` (confidence 1.0).
- **5 stated** capabilities (create, persistence, analytics, scalability, API) and **5 assumed**
  ones that production use needs but the requirement did not mention: redirect, URL safety,
  custom aliases, expiry, abuse protection. Assumed capabilities are labelled as such.
- **12 acceptance criteria**, e.g. `AC-CONCURRENCY` (collision-safe allocation under concurrent
  writers), `AC-PRIVACY` (no raw IPs stored), `AC-CONTRACT` (implementation matches OpenAPI exactly).
- **11 clarifying questions**, each with a default. "scalable" is flagged as unmeasurable (medium
  severity) with the default "10k redirects/s, 1B links over 5 years". Unspecified dimensions
  (latency, availability, privacy, retention, rate limits, code length…) are raised as
  low-severity questions with defaults.

### Decomposition and orchestration
The planner spawns a 14-task DAG after the plan gate:

```
design_architecture ─► design_contract ─┬─► gen_domain ─┬─► gen_persistence ─► gen_analytics ─┐
                                        │               └─► gen_edge ───────────────────────────┼─► gen_service ─► gen_api ─┐
                                        ├─► gen_unit_tests ────────────────────────────────────────────────────────────────┤
                                        ├─► gen_integration_tests ─────────────────────────────────────────────────────────┼─► validate ─► risk_review ─► report ⏸
                                        └─► gen_docs (non-critical) ───────────────────────────────────────────────────────────────────────────────────┘
```

Observed: max parallelism 4 (domain code, two test suites and docs generated concurrently), three
gate decisions, 17 executions in about 2 s.

### Generated engineering outputs (`runs/01-greenfield/workspace/project/`)

| Kind | Artifacts |
|---|---|
| Code (stdlib only, ~1,100 lines) | `shortener/`: `config` (settings from the gate), `domain` (URL safety: scheme allowlist, no credentials, no private/loopback/link-local hosts; alias rules), `codes` (CSPRNG base62), `repository` (port + SQLite/in-memory adapters, atomic insert-if-absent), `cache` (LRU+TTL, negative caching), `ratelimit` (token bucket), `analytics` (bounded queue, batched flush, daily aggregates, salted daily-rotating IP hash), `service` (business rules), `http` (WSGI, problem+json, body limits, security headers), `main` (composition root, graceful shutdown) |
| API contract / schema | `openapi.yaml` / `openapi.json` (OpenAPI 3.1, 6 operations), `shortener/schema.sql` (3 tables, indexes, production mapping comments) |
| Tests (53 functions → 90 cases) | unit: validation, codes, cache, rate limiter, service rules, analytics recorder (backpressure, store failure). Integration: HTTP API over real SQLite, **contract conformance**, durability across restart, **8 threads × 40 concurrent creates**, **alias race with exactly one winner**, retention purge |
| Docs | `README.md` (endpoint table rendered from the contract), `docs/architecture.md` (components, flows, AWS mapping, capacity estimate), `docs/API.md`, `docs/RUNBOOK.md`, `docs/adr/ADR-001…007` |

### Validation
90/90 tests, ruff and mypy clean, no safety findings, schema/contract/settings conformance
passed, all 12 acceptance criteria traced to passing tests. The service was also smoke-tested
over real HTTP (create → 302 redirect → stats).

### Trade-offs and risks (summary §7)
7 ADRs (random codes vs counter/KGS, 302 vs 301, async vs sync analytics, raw+aggregate storage,
KV store, IP hashing, stdlib WSGI vs FastAPI), 6 domain risks plus run-specific ones
(unconfirmed assumptions), and 7 failure scenarios with designed behaviour.

---

## 2. Recovery: faults injected into the greenfield run

`--inject-fault` makes the failure paths reproducible:

**2a: `transient:design_contract` + `bug:gen_persistence`.** The contract task fails once and is
retried with backoff. The persistence generator emits SQL that selects a column (`expiry`) that is
not in the data schema.
- The Validator's **schema-conformance** check flags it statically, and the failing tests flag it
  dynamically. Several failures surface as HTTP 500s in API tests; fault localisation reads the
  **captured server log traceback**, finds `shortener/repository.py`, and blames `gen_persistence`
  rather than the API layer.
- Repair cycle 1 re-runs `gen_persistence` and its downstream tasks (`gen_analytics`,
  `gen_service`, `gen_api`, `validate`) and nothing else. The test generators and `gen_domain`
  are untouched. Result: 90/90.

**2b: `bug:gen_api`.** The redirect returns 200 instead of 302. There is no stack trace (nothing
raises), so localisation blames the most downstream generated module the failing tests import:
`gen_api`. Repair re-runs exactly `gen_api` and `validate`.

---

## 3. Brownfield: change an existing code base

**Input** (`examples/requirements/brownfield_expiry_and_alias_bug.md`, `--repo examples/legacy_links`)

> In the existing legacy-links service, add link expiration support: callers can pass an optional
> TTL when creating a link, and expired links must stop redirecting. Also fix the bug where creating
> a link with a custom alias that already exists silently overwrites the original link.

`legacy_links` is a small, realistic service with a real bug (`INSERT OR REPLACE` lets an alias
overwrite an existing link) and 5 passing tests.

### Codebase reasoning (`review/impact_analysis.md`)
- **Recovered architecture:** roles `http=links/app.py`, `service=links/service.py`,
  `store=links/store.py`, `model=links/models.py`, plus a module dependency graph.
- **API surface** from three detection styles: a route table (`POST /links`) and if/path dispatch
  (`GET /links/{code}`, `GET /{code}`).
- **Data model:** table `links(code, target_url, created_at, clicks)`.
- **Data flows**, e.g. `GET /{code} → app::redirect → service::resolve → store::get [SELECT] → store::increment_clicks [UPDATE]`.
- **Impact:** 8 directly impacted symbols; ripple through callers to `LinkService.info`
  (and so `GET /links/{code}`); a schema change is required.
- **Baseline:** the existing suite runs on an untouched copy before any edit (5/5).

### Decomposition and orchestration (two planning stages, four gates)
```
understand ⏸ ─► plan ⏸ ─► analyze_codebase ─► plan_changes ⏸ ─┬─► change_fix_alias_overwrite ─► change_add_expiration ─┬─► validate ─► risk ─► report ⏸
                                                                └─► gen_regression_tests ────────────────────────────────┘
```
Both changes touch `store.py`, `service.py` and `app.py`, so the planner **runs them in
sequence, bug fix first**. Each recipe re-reads the file after the previous edit, finds symbols
with `ast`, and applies anchored edits that must match exactly once. Regression-test generation
runs in parallel with the edits.

### Outputs
- `review/changes.patch`: 7 files, +219 / −17. Highlights: `INSERT OR REPLACE` → `INSERT` plus
  `IntegrityError → DuplicateCode`; `AliasTaken` → **409**; retry on random-code collision;
  `expires_at` on the model; an **idempotent online migration** (`PRAGMA table_info` →
  `ALTER TABLE ADD COLUMN`); `Expired` → **410**; `ttl_seconds` validation.
- `project/tests/test_alias_regression.py`, `test_expiration.py` (12 new tests, including a
  migration test against an old-schema database).
- `CHANGELOG.md` and `review/change_notes.md` (API impact, behaviour change for clients that
  relied on overwrite, rollout and rollback).

### Validation
Baseline 5/5 → after the change 17/17, **0 regressions**; change-scope check (every modified file
is inside the approved impact analysis); safety scan and ruff on changed files; traceability for
`AC-BUG-ALIAS`, `AC-EXP-TTL`, `AC-EXP-COMPAT`, `AC-EXP-MIGRATION`, and `AC-REGRESSION` (via the
baseline comparison). The source repository is never modified.

---

## 4. Ambiguous requirement

**Input** (`examples/requirements/ambiguous_campaign_links.md`)

> We need a link thing for our marketing campaigns. It should be fast and secure, and it should
> track stuff so the team can see how campaigns perform. Lots of users will hit it, so keep it
> simple and reliable.

### Understanding
Domain confidence is only 0.33 ("link thing"), which raises a **high-severity** confirmation
question. 14 questions in total, each tied to the phrase that triggered it: *Lots of users* →
scale; *fast* → latency; *reliable* → availability; *secure* → auth, privacy, rate limits;
*track stuff* → analytics dimensions, retention, scope; *simple* → subjective.

### 4a. Autonomous policy → escalation (`04a-ambiguous-escalated`)
The auto policy refuses to approve while a high-severity question is unanswered. `understand`
is rejected at the gate, `plan` is skipped, and the always-run `escalate` task writes a
summary with status **ESCALATED TO HUMAN**. The questions and defaults are listed so a human can
answer them. 2 task executions; nothing was built on a guess.

### 4b. Human answers (`04b-ambiguous-answered`, `examples/answers/ambiguous_campaign_links.yaml`)
A product owner's answers are recorded at the gates. Answers are **parsed into settings** and
those settings change the generated system:

| Question | Answer | Effect |
|---|---|---|
| domain | "Yes - a URL shortener for campaign links." | unblocks the gate |
| privacy | "Do not store IP addresses at all." | `analytics_ip_mode=drop` → `config.py`, README, architecture doc |
| rate limit | "20 per minute per key" | `create_rate_per_min=20` |
| code length | "8 characters" | `code_length=8` |
| default expiry | "…expire after 180 days by default" | `default_ttl_days=180` |
| retention | "keep them forever" | **rejected** (not a number of days): default 90 kept, guardrail event logged, risk raised, release comment asks to confirm with legal |

The validator's settings-conformance check verifies that the generated config matches the
approved answers. 90/90 tests pass; the generated tests are written against the settings, so
they hold under either configuration.

---

## 5. Crash and resume (`05-crash-resume`)

`--inject-fault crash:gen_service` stops the process right after a durable checkpoint, with 13
tasks done. `python -m sdlc_agent resume runs/05-crash-resume` restores the blackboard and task
records, resets interrupted work, and executes only `gen_api`, `validate`, `risk_review` and
`report`. The trace is continuous across both processes.

---

## Running other requirements

- Any URL-shortener-family requirement uses the domain pack (greenfield) or its recipes
  (brownfield: expiry, alias-overwrite fix).
- A requirement outside every pack (e.g. *"Build a payroll engine"*) is recognised as an
  **unknown domain**: a blocker question at the requirements gate. If a human approves anyway,
  analysis, planning and the architecture outline run, then generation is handed to an LLM
  (`--llm anthropic|bedrock`) or escalated (`NeedsHuman`). This is covered by
  `tests/test_end_to_end.py::test_unknown_domain_escalates_generation`.
