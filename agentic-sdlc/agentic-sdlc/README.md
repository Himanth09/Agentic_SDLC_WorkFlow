# Agentic SDLC: from a requirement to a reviewable engineering outcome

A working prototype of an agentic system that takes a software requirement and produces a
**reviewable engineering outcome**: a normalised requirement with explicit assumptions, a task DAG,
architecture and API contract, production-quality code (or a code change), tests, documentation,
a validation report, a risk register and a structured engineering summary. Agents work on their
own across many steps; humans approve at gates.

```
requirement ──► understand ──⏸──► plan ──⏸──► [ design ► contract ► codegen ‖ testgen ‖ docgen ] ──► validate ⟲ repair ──► risk ──► report ──⏸ release
                  (gate: requirements)  (gate: plan)            parallel DAG waves                  targeted re-runs                     (gate: release)
```

| Scenario | Input | Outcome (from `runs/`) |
|---|---|---|
| **Greenfield (mandatory)** | "Build a scalable URL shortener service with APIs, persistence, and analytics." | 17-task DAG, max parallelism 4-5; 38 artifacts; **90/90 generated tests pass**; all 12 acceptance criteria traced to tests; ruff + mypy clean |
| Greenfield + injected faults | same, with a transient fault plus a defect in generated code | transient fault retried with backoff; defect **localised to the responsible generator**; one repair cycle re-runs only that task and the tasks downstream of it |
| **Brownfield** | add link expiry + fix an alias-overwrite bug in `examples/legacy_links` | code base mapped (roles, routes, data flows, symbol-level impact); edits that overlap are run one after another; **5/5 baseline tests still pass + 12 new**; schema migration; reviewable `changes.patch` |
| **Ambiguous** | "We need a link thing… fast and secure… track stuff…" | 14 clarifying questions; under the autonomous policy the run **escalates to a human** instead of guessing; with recorded human answers the answers change the generated code (8-char codes, no IP storage, 180-day TTL) |
| Crash + resume | crash injected after `gen_service` | resumes from the checkpoint; only the remaining 4 tasks execute |

## Quick start

Requirements: Python ≥ 3.10 and `pyyaml`. `pytest` is used to validate generated code; `ruff`
and `mypy` are used if installed (if they're missing, the run reports a validation gap). No LLM
or API key is needed.

```bash
pip install pyyaml pytest            # ruff and mypy optional
./scripts/run_demo.sh                # runs every scenario (~15 s) into runs/
python -m pytest                     # 56 tests for the agentic system itself (~13 s)
```

Then open `runs/01-greenfield/workspace/ENGINEERING_SUMMARY.md`.

### Run your own requirement

```bash
# greenfield, autonomous policy at the gates
python -m sdlc_agent run -r "Build a scalable URL shortener service with APIs, persistence, and analytics."

# a human answers clarifying questions and approves each gate in the terminal
python -m sdlc_agent run -f examples/requirements/ambiguous_campaign_links.md --approval interactive

# recorded decisions (reproducible reviews / CI)
python -m sdlc_agent run -f examples/requirements/ambiguous_campaign_links.md \
       --approval file --answers examples/answers/ambiguous_campaign_links.yaml

# brownfield: change an existing repository (the source repo is never modified; work happens on a copy)
python -m sdlc_agent run -f examples/requirements/brownfield_expiry_and_alias_bug.md --repo examples/legacy_links

# demonstrate recovery paths
python -m sdlc_agent run -f examples/requirements/greenfield_url_shortener.md \
       --inject-fault transient:design_contract --inject-fault bug:gen_persistence
python -m sdlc_agent run -f examples/requirements/greenfield_url_shortener.md --inject-fault crash:gen_service
python -m sdlc_agent resume runs/<run-id>
```

Options: `--workers N` (parallelism), `--max-repairs N` (repair budget), `--llm anthropic|bedrock`
(optional LLM assistance, see [Architecture](docs/ARCHITECTURE.md#llm-integration)), `--quiet`.

### What a run produces

```
runs/<run-id>/
  requirement.md                 input
  trace.jsonl                    append-only audit log of every scheduling, retry, repair, gate and guardrail event
  state.json / checkpoint.pkl    durable run state (resume)
  workspace/
    ENGINEERING_SUMMARY.md       the reviewable outcome (sections listed below)
    summary.json                 same content, machine-readable
    project/                     generated service or patched copy of the repo: code, tests, openapi.yaml, docs/
    review/                      validation_report.json, impact_analysis.md, changes.patch, change_notes.md
```

`ENGINEERING_SUMMARY.md` contains: 1 requirement understanding (capabilities, acceptance
criteria, ambiguities and how each was resolved) · 2 implementation plan and rationale (DAG
diagram plus a per-task table) · 3 orchestration trace (parallelism, retries, repair cycles with
localised findings) · 4 codebase reasoning (brownfield) · 5 generated artifacts with hashes ·
6 validation results and traceability matrix · 7 risks, trade-offs, failure scenarios ·
8 validation approach · 9 assumptions and limitations · 10 human oversight log.

## Repository layout

```
sdlc_agent/
  orchestrator.py        DAG engine: parallel waves, retries, repair cycles, gates, budget, checkpoint/resume
  context.py             blackboard (typed shared state with provenance), tracer, fault injector
  approval.py            human gates: interactive | auto policy | recorded file
  guardrails.py          workspace sandbox, anchored edits, AST safety scan, execution limits
  llm.py                 optional provider layer (Anthropic / Bedrock), schema-validated with fallback
  tools.py               pytest / ruff / mypy adapters (subprocess, timeout, scrubbed env)
  knowledge.py           domain-pack interfaces (capabilities, NFR dimensions, components, risks…)
  agents/                requirements, planner, codebase, architect, codegen, testgen, docgen,
                         validator, risk, reporter
  blueprints/url_shortener/
                         domain pack: knowledge, OpenAPI builder, code blueprint, brownfield recipes
examples/                requirements, recorded answers, legacy_links (brownfield target repo)
runs/                    outputs of scripts/run_demo.sh (committed so reviewers can read them without running)
tests/                   tests for the agentic system (orchestration, guardrails, agents, end-to-end)
docs/                    FLOW.md, ARCHITECTURE.md, SCENARIOS.md, TESTING.md
```

## Documentation

- [docs/FLOW.md](docs/FLOW.md): how the four layers (control plane, agents, domain pack,
  examples/tests/outputs) connect, and one run traced step by step through the code
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, execution model, control flow,
  design decisions, and the path to production on AWS
- [docs/SCENARIOS.md](docs/SCENARIOS.md): greenfield, brownfield and ambiguous examples with
  their decomposition, orchestration and validation
- [docs/TESTING.md](docs/TESTING.md): how correctness and output quality are validated; known
  limitations and trade-offs

## The honest boundary

Agents that understand requirements, plan, reason about code bases, orchestrate, validate and
report are **generic**. Generating code offline relies on a curated **domain pack** (here: URL
shorteners, greenfield blueprint plus brownfield change recipes). Generated code is never trusted
because of where it came from: every artifact, whether from a pack or an LLM, goes through the
same sandbox, safety scan, cross-artifact checks and executed tests. Requirements outside every
pack are still analysed, decomposed and planned. Generation is then handed to an LLM provider if
one is configured, or **escalated to a human**. See [Testing: limitations](docs/TESTING.md#known-limitations-and-trade-offs).
