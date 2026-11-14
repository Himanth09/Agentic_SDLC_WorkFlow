# Testing approach

There are two separate questions:

1. **Is the agentic system correct?** Does it orchestrate, recover, guard and decide as
   designed? This is answered by the test suite in `tests/`.
2. **Is each output it produces correct and of good quality?** This is answered at run time by
   the Validator, which runs on every run and attaches its evidence to the engineering summary.

## 1. Tests for the agentic system (`python -m pytest`, 56 tests, ~13 s)

| File | What it proves |
|---|---|
| `test_orchestrator.py` (15) | dependency order; **true parallelism** (a 3-party barrier only passes if three tasks run at the same time); transient retry then success; retries exhausted → dependants skipped while `always_run` still executes; non-critical → degraded; **repair re-runs only the hinted task + downstream**; repair budget bound; unattributable failure is not "repaired"; guardrail violations never retried; unexpected exceptions contained; gate rejection stops dependants; dynamic plan extension; cycle / unknown-dependency / unknown-agent rejection; execution budget; **checkpoint + resume after a simulated crash** (completed work is not repeated, blackboard restored) |
| `test_guardrails_and_gates.py` (21) | sandbox: absolute paths, `..`, symlink escape; anchored edits (0 or 2 matches rejected); file-size and change-set caps; AST safety scan catches `eval`, `os.system`, `shell=True`, `pickle.loads`, unsafe `yaml.load`, SQL built by f-string or `%`, hard-coded secrets, syntax errors, and accepts safe equivalents; gate modes (policy, file with fallback, **interactive with scripted input**); **LLM output validation**: invalid JSON is retried with the error fed back, then falls back |
| `test_agents.py` (12) | the mandatory requirement is understood (stated vs assumed capabilities, acceptance criteria, "scalable" flagged); vague requirement → high-severity domain question that blocks the auto policy; unknown domain is a blocker; brownfield classification; **gate answers become settings, bad answers are rejected**; codebase model recovers roles/routes/tables/flows of `legacy_links`; symbol-level impact analysis; **recipes compose in either order and keep the suite green**; a recipe raises `NeedsHuman` on unfamiliar code instead of guessing; fault localisation (traceback frame, **captured-log frame**, entry point, widened closure) |
| `test_end_to_end.py` (8, marked `e2e`) | full CLI runs: greenfield is ready for review with every acceptance criterion covered; transient fault + schema drift recovered in one targeted repair; API defect localised to `gen_api`; ambiguous + auto policy escalates; ambiguous + recorded answers changes the generated config; brownfield has 0 regressions, serialised edits, and an untouched source repo; unknown domain escalates generation; brownfield without `--repo` asks for it |

Test doubles: `FnAgent` (behaviour per call number) isolates orchestration semantics from real
agents; `FakeLLM` checks the LLM contract without network access. The end-to-end tests go
through `cli.run` (the same path a user takes) and assert on the persisted checkpoint and trace,
not on in-memory objects.

The agent code itself is clean under `ruff check --select E,F,W,B` and `mypy`.

## 2. Validating each run's outputs (the Validator)

Validation is layered so cheap, precise checks run first and their findings point at a specific
generator:

| Layer | Greenfield | Brownfield |
|---|---|---|
| Safety (blocking) | AST scan of every generated file (see guardrails) | scan of changed files |
| Lint / types | ruff (undefined names block), mypy; a missing tool is reported as a validation gap and as a risk | ruff on changed files |
| Cross-artifact consistency (blocking) | SQL columns vs data schema; routes vs OpenAPI contract (both directions); config defaults vs settings approved at the gate | change scope vs approved impact analysis |
| Behaviour (blocking) | full generated suite: unit, integration over real SQLite, contract, durability, concurrency | full suite; **every baseline-passing test must still pass**; new tests pass |
| Requirements (blocking) | every acceptance criterion has at least one covering test (`Covers:` tags → traceability matrix) | same, plus `AC-REGRESSION` satisfied by the baseline comparison |
| Review aids | `validation_report.json`, traceability matrix in the summary | `changes.patch`, `impact_analysis.md`, `change_notes.md` |

Blocking findings trigger a bounded repair loop (see Architecture §3). The release gate cannot
be passed by the auto policy while blocking findings remain or the run is escalated.

**Validation beyond this run**, listed in each summary (§8) because it can't be done honestly
inside a prototype run: load test at 2× target peak (p99 < 50 ms), 24-hour soak, chaos (analytics
store down, link store throttled), DAST, canary with SLO-based rollback, synthetic probes.

## Known limitations and trade-offs

| Limitation | Impact | Mitigation / path forward |
|---|---|---|
| Offline code generation needs a **domain pack** (only URL shorteners ship) | Other domains get analysis, planning and architecture outline, then an LLM or a human | LLM freeform mode behind the same guardrails and validator; packs are data, so new domains are added without changing agents |
| Brownfield recipes recognise **known code shapes** | Unfamiliar structure → `NeedsHuman` (by design: no fuzzy edits) | LLM-proposed edits in the same anchored `FileChange` format, validated identically |
| Offline "repair" regenerates from the blueprint | Shows the loop and the localisation, not reasoning about the fix itself | With an LLM, findings for the task are included in the prompt; the loop is unchanged |
| Faults are injected | Recovery is shown on reproducible defects rather than organic ones | Same code paths as real failures; the guardrail and secret-scanner findings during development were organic |
| Fault localisation is heuristic (frames → entry point → import closure) | A masked or cross-cutting fault may need a second cycle or human help | Bounded by the repair budget; findings and targets are recorded in the summary |
| Static analysis is Python-only, name-based call graph | Dynamic dispatch and other languages are not modelled | Language-server / tree-sitter adapters behind the same model |
| No line-coverage measurement (`coverage` not installed in this environment) | Coverage is argued through the traceability matrix, not a percentage | Add `coverage run` in the Validator when available |
| Generated tests share a blueprint with the code | Not independent like a separate QA author | Tests are written from contract and criteria, contract tests check both directions, and properties (concurrency, durability) are tested behaviourally |
| Prototype service is single-node (SQLite, in-process cache and limiter) | Not deployable at the stated scale as-is | Ports and adapters; the production adapters and capacity plan are in the generated architecture doc |
| `checkpoint.pkl` uses pickle | Must only be loaded from trusted local storage | Production: JSON/DynamoDB checkpointer |
| Interactive gate requires a TTY | Under CI it falls back to the auto policy (logged) | Use `--approval file` for recorded decisions |
