"""Reporter: the structured engineering summary + the release gate.

The summary is assembled only from blackboard records and the execution
trace (nothing is re-derived), so every claim in it is traceable to the step
that produced it.
"""
# Store annotations as strings.
from __future__ import annotations

# re: sanitise ids for Mermaid.
import re
# Any: loosely-typed summary dictionary.
from typing import Any

# GateRequest: what the human sees at the release gate.
from ..approval import GateRequest
# NeedsHuman: escalation used in "needs_input" mode.
from ..errors import NeedsHuman
# Data models plus dumps (JSON serialiser) and sha256 (content hash helper).
from ..models import (FileChange, GateDecision, NormalizedRequirement, ScenarioKind, TaskSpec,
                      dumps, sha256)
# Agent base class and result container.
from .base import Agent, AgentResult

# Task status -> Mermaid CSS class used to colour the plan graph.
STATUS_CLASS = {"succeeded": "ok", "degraded": "warn", "failed": "bad", "skipped": "skip",
                "pending": "skip", "running": "warn"}


# Agent that writes ENGINEERING_SUMMARY.md / summary.json and owns the release gate.
class Reporter(Agent):
    # Registry name used by TaskSpec.agent.
    name = "reporter"

    # Entry point; behaviour depends on task.params["mode"].
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # "needs_input": stop and ask a human (e.g. missing --repo).
        if task.params.get("mode") == "needs_input":
            raise NeedsHuman(task.params["message"])
        # "escalation": write a partial summary when the run stopped before planning.
        if task.params.get("mode") == "escalation":
            # If planning actually succeeded, the normal report will follow, so skip.
            if (ctx.board.get("task_status") or {}).get("plan") == "succeeded":
                return AgentResult(notes=["not needed: plan approved, full report will follow"])
            summary = build_summary(ctx)
            write(ctx, summary, task.id)
            return AgentResult(outputs={"summary": summary},
                               notes=["run stopped before planning; escalation summary written"])
        # Normal mode: build and write the full summary.
        summary = build_summary(ctx)
        write(ctx, summary, task.id)
        return AgentResult(outputs={"summary": summary},
                           notes=[f"summary written; status={summary['status']}"])

    # Build the release-gate request.
    def gate_request(self, ctx, task: TaskSpec) -> GateRequest:
        # The summary published by run().
        s = ctx.board.require("summary")
        # Reasons that prevent approval.
        blocking = []
        # Only a "ready_for_review" run can be released.
        if s["status"] != "ready_for_review":
            blocking.append(f"run status is '{s['status']}'")
        # Validation report (may be None).
        v = s.get("validation") or {}
        # Every blocking validation finding also blocks release.
        blocking += [f"blocking finding: {f.message}" for f in v.get("blocking", [])]
        # Short text summary for the approver.
        text = (f"Status: {s['status']}\nTests: {v.get('tests', {}).get('passed', 0)}/"
                f"{v.get('tests', {}).get('total', 0)} passed\nArtifacts: {len(s['artifacts'])}\n"
                f"Risks: {len(s['risks'])}\nSummary: {s['path']}")
        return GateRequest("release", text, blocking_reasons=blocking)

    # After the release decision, re-render the summary to include it.
    def on_gate(self, ctx, task: TaskSpec, decision: GateDecision) -> None:
        summary = build_summary(ctx)   # re-render including the release decision
        summary["release_decision"] = decision
        write(ctx, summary, task.id)
        # Republish with the gate as producer.
        ctx.board.put("summary", summary, producer="gate:release")


# --------------------------------------------------------------------------- #
# Assemble the summary dict purely from blackboard state and the trace.
def build_summary(ctx) -> dict[str, Any]:
    # Short alias for the blackboard.
    b = ctx.board
    # The requirement, or a placeholder if analysis never completed.
    req: NormalizedRequirement = b.get("requirement") or NormalizedRequirement(
        raw=b.get("requirement_text", ""), title="(requirement not analysed)", kind=ScenarioKind.GREENFIELD,
        domain="unknown", summary="Requirement analysis did not complete.", capabilities=[], nfrs={},
        acceptance_criteria=[], ambiguities=[], out_of_scope=[])
    # task id -> execution record.
    records = {r.id: r for r in b.get("task_records", [])}
    # All task specs that were planned.
    specs = b.get("task_specs", [])
    # Validation report (None if it never ran).
    validation = b.get("validation")
    # task id -> status string.
    status_map = b.get("task_status", {})
    # Critical tasks that failed or were skipped (excluding the reporting tasks themselves).
    failed_critical = [s.id for s in specs if s.critical and status_map.get(s.id) in ("failed", "skipped")
                       and s.id not in ("report", "escalate")]
    # Overall outcome: ready only if no critical failures and validation passed.
    status = "ready_for_review" if not failed_critical and validation and validation.get("passed") \
        else ("escalated" if failed_critical else "validation_failed")
    # Latest FileChange per path (later writes override earlier ones).
    latest: dict[str, FileChange] = {}
    for ch in b.get("artifacts", []):
        latest[ch.path] = ch
    # Workspace root on disk.
    root = ctx.workspace
    # Artifact manifest rows.
    artifacts = []
    for path, ch in sorted(latest.items()):
        p = root / path
        # Only list files that still exist; record line count and content hash.
        if p.exists():
            text = p.read_text(encoding="utf-8")
            artifacts.append({"path": path, "task": ch.produced_by, "lines": text.count("\n"),
                              "sha": sha256(text), "op": ch.op})
    # The summary dictionary consumed by render_markdown() and summary.json.
    return {
        "run_id": ctx.run_id, "status": status, "failed_critical": failed_critical,
        "requirement": req, "plans": b.get("plans", []), "specs": specs, "records": records,
        "validation": validation, "risks": b.get("risks", []), "tradeoffs": b.get("tradeoffs", []),
        "failure_scenarios": b.get("failure_scenarios", []),
        "strategy": b.get("validation_strategy", {}), "gates": b.get("gate_decisions", []),
        "artifacts": artifacts, "impact": b.get("impact"), "baseline": b.get("baseline"),
        "repair_cycles": b.get("repair_cycles", 0), "repair_history": b.get("repair_history", []),
        "events": list(ctx.tracer.events),
        "rejected_answers": b.get("rejected_answers", []), "release_decision": None,
        # LLM provider name, or a note that agents ran deterministically.
        "llm": getattr(ctx.llm, "name", "none (deterministic agents)"),
        "path": "ENGINEERING_SUMMARY.md",
    }


# Write the markdown summary and a JSON copy (without the bulky event list).
def write(ctx, summary: dict[str, Any], task_id: str) -> None:
    ctx.guard.apply(FileChange("ENGINEERING_SUMMARY.md", "create", render_markdown(summary),
                               produced_by=task_id))
    # Drop "events" from the JSON (the full trace lives in trace.jsonl).
    slim = {k: v for k, v in summary.items() if k not in ("events",)}
    ctx.guard.apply(FileChange("summary.json", "create", dumps(slim), produced_by=task_id))


# Mermaid-safe node id (non-word characters -> underscore).
def _mermaid_id(s: str) -> str:
    return re.sub(r"\W", "_", s)


# Render the 10-section engineering summary as markdown.
def render_markdown(s: dict[str, Any]) -> str:
    req: NormalizedRequirement = s["requirement"]
    # Validation report or empty dict.
    v = s["validation"] or {}
    # Output lines.
    L: list[str] = []
    # Human-readable outcome label.
    badge = {"ready_for_review": "READY FOR REVIEW", "validation_failed": "VALIDATION FAILED",
             "escalated": "ESCALATED TO HUMAN"}[s["status"]]
    # Title and one-line run header.
    L += [f"# Engineering summary - {req.title}", "",
          f"**Run:** `{s['run_id']}`  |  **Outcome:** {badge}  |  **Scenario:** {req.kind.value}  |  "
          f"**Domain:** {req.domain}  |  **LLM:** {s['llm']}", ""]
    # Release decision, if the gate has been decided.
    if s["release_decision"]:
        d = s["release_decision"]
        L += [f"**Release gate:** {'APPROVED' if d.approved else 'REJECTED'} by `{d.decided_by}`"
              + (f" - {d.comment}" if d.comment else ""), ""]
    # Escalations: first error line of each failed critical task.
    if s["failed_critical"]:
        L += ["**Escalations:** " + ", ".join(f"`{t}`: {s['records'][t].error.splitlines()[0]}"
                                               for t in s["failed_critical"] if t in s["records"]
                                               and s["records"][t].error), ""]

    # 1 requirement
    # Raw requirement, normalised summary and capabilities.
    L += ["## 1. Requirement understanding", "", f"> {req.raw}", "", f"**Normalised:** {req.summary}", "",
          "**Capabilities**"] + [f"- {c}" for c in req.capabilities]
    if req.out_of_scope:
        L += ["", "**Out of scope (v1):** " + "; ".join(req.out_of_scope)]
    # Acceptance criteria table.
    L += ["", "**Acceptance criteria**", "", "| ID | Type | Criterion |", "|---|---|---|"]
    L += [f"| {c.id} | {c.kind} | {c.text} |" for c in req.acceptance_criteria]
    # Ambiguities table with resolution source.
    L += ["", "### Ambiguities and resolutions", "",
          "| ID | Severity | Trigger | Question | Resolution | Source |", "|---|---|---|---|---|---|"]
    for a in req.ambiguities:
        L.append(f"| {a.id} | {a.severity.value} | {a.term} | {a.question} | {a.resolution} | "
                 f"{'human answer' if a.answer else 'default assumption'} |")
    # Answers rejected at the gate.
    for r in s["rejected_answers"]:
        L.append(f"\n> Guardrail: answer {r}")
    # Settings fed into code generation.
    if req.settings:
        L += ["", "Resolved settings fed to generation: " +
              ", ".join(f"`{k}={v}`" for k, v in sorted(req.settings.items()))]

    # 2 plan
    L += ["", "## 2. Implementation plan and rationale", ""]
    # Rationale of each planning stage.
    for i, p in enumerate(s["plans"], 1):
        L.append(f"- **Planning stage {i}:** {p.rationale}")
    # Mermaid DAG of tasks coloured by status.
    L += ["", "```mermaid", "graph TD"]
    for spec in s["specs"]:
        # Status of the task, or "pending" if it never ran.
        st = s["records"].get(spec.id).status.value if spec.id in s["records"] else "pending"
        # Node label: id, agent, and a gate marker.
        label = f"{spec.id}<br/><i>{spec.agent}</i>" + ("<br/>⏸ gate" if spec.gate else "")
        L.append(f"  {_mermaid_id(spec.id)}[\"{label}\"]:::{STATUS_CLASS.get(st, 'skip')}")
        # Edges from dependencies.
        for d in spec.depends_on:
            L.append(f"  {_mermaid_id(d)} --> {_mermaid_id(spec.id)}")
    # Colour classes, then the task table header.
    L += ["  classDef ok fill:#d4edda,stroke:#2e7d32", "  classDef warn fill:#fff3cd,stroke:#b8860b",
          "  classDef bad fill:#f8d7da,stroke:#c62828", "  classDef skip fill:#eeeeee,stroke:#9e9e9e",
          "```", "", "| Task | Agent | Depends on | Status | Attempts | ms | Notes |",
          "|---|---|---|---|---|---|---|"]
    # One row per executed task.
    for spec in s["specs"]:
        r = s["records"].get(spec.id)
        if not r:
            continue
        # Notes plus the first error line, if any.
        notes = "; ".join(r.notes + ([r.error.splitlines()[0]] if r.error else []))
        # The report task itself is still running while this is rendered.
        st = "running (this report)" if r.status.value == "running" else r.status.value
        # Pipes in notes are replaced so they don't break the table; truncated to 220 chars.
        L.append(f"| `{spec.id}` | {spec.agent} | {', '.join(spec.depends_on) or '-'} | {st} | "
                 f"{r.attempts} | {r.duration_ms} | {notes.replace('|', '/')[:220]} |")

    # 3 orchestration
    ev = s["events"]
    # Task start events.
    starts = [e for e in ev if e["event"] == "task_started"]
    # Aggregate orchestration stats.
    L += ["", "## 3. Orchestration trace", "",
          f"- {len(starts)} task executions, max parallelism {max_parallel(ev)}, "
          f"{sum(e['event'] == 'task_retry' for e in ev)} transient retries, "
          f"{s['repair_cycles']} repair cycle(s), "
          f"{sum(e['event'] == 'gate_decided' for e in ev)} human/policy gate decisions", ""]
    # Events worth showing individually.
    notable = [e for e in ev if e["event"] in ("task_retry", "repair_cycle", "gate_decided", "task_failed",
                                               "task_degraded", "guardrail", "task_skipped", "plan_extended",
                                               "fault_injected", "checkpoint")]
    if notable:
        L += ["| t (s) | Event | Detail |", "|---|---|---|"]
        for e in notable:
            # All fields except time and event name, as key=value pairs.
            detail = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "event"))
            L.append(f"| {e['t']} | {e['event']} | {detail.replace('|', '/')[:240]} |")
    # Each repair cycle: trigger, localised targets, re-run tasks, and up to 8 findings.
    for rep in s["repair_history"]:
        L += ["", f"**Repair cycle {rep['cycle']}** (triggered by `{rep['trigger']}`): fault localised to "
              + ", ".join(f"`{t}`" for t in rep["targets"]) + "; re-ran "
              + ", ".join(f"`{t}`" for t in rep["rerun"]), ""]
        L += [f"- {f.severity.value} [{f.source}] {f.message[:160]} -> `{f.task_hint}`"
              for f in rep["findings"][:8]]
    L += ["", "Full machine-readable trace: `trace.jsonl`."]

    # 4 codebase (brownfield)
    # Only present when impact analysis ran.
    if s["impact"]:
        im = s["impact"]
        L += ["", "## 4. Codebase reasoning (brownfield)", "",
              "Recovered roles: " + ", ".join(f"{k} = `{v}`" for k, v in sorted(im["roles"].items())),
              "", "Impacted APIs: " + ", ".join(f"`{a}`" for a in im["apis"]),
              "", "Directly impacted symbols: " + ", ".join(f"`{x}`" for x in im["direct_symbols"]),
              "", "Ripple (callers): " + (", ".join(f"`{x}`" for x in im["ripple_symbols"]) or "none"),
              "", f"Schema change: {'yes (online, idempotent migration)' if im['schema_change'] else 'no'}",
              "", "Details: `review/impact_analysis.md`; diff: `review/changes.patch`."]

    # 5 artifacts
    # Manifest of generated files with producer, size and hash.
    L += ["", "## 5. Generated artifacts", "", "| Path | Produced by | Lines | sha256 |", "|---|---|---|---|"]
    L += [f"| `{a['path']}` | {a['task']} | {a['lines']} | `{a['sha']}` |" for a in s["artifacts"]]

    # 6 validation
    L += ["", "## 6. Validation results", ""]
    if v:
        t = v["tests"]
        # Overall verdict, test counts and the checks that ran.
        L += [f"- **Overall:** {'PASSED' if v['passed'] else 'FAILED'} "
              f"({len(v['blocking'])} blocking, {len(v['findings'])} total findings)",
              f"- **Tests:** {t['passed']}/{t['total']} passed, {t['failed']} failed, {t['errors']} errors "
              f"({t['duration_s']}s)", f"- **Checks run:** {', '.join(v['checks'])}"]
        # External tool status (missing / clean / N issues).
        for name, tool in (v.get("tools") or {}).items():
            L.append(f"- **{name}:** " + ("not installed (gap)" if not tool["available"] else
                                           ("clean" if tool["ok"] else f"{len(tool['issues'])} issue(s)")))
        # Brownfield extras: baseline vs now, and diff size.
        if v.get("mode") == "brownfield":
            b = v["baseline"]
            L += [f"- **Baseline:** {b['passed']}/{b['total']} before change; regressions: "
                  f"{len(v['regressions'])}", f"- **Diff:** {len(v['changed_files'])} files, "
                  f"+{v['diff_stats']['added']} / -{v['diff_stats']['removed']} lines"]
        # Traceability table: up to 4 test names per criterion.
        L += ["", "**Traceability (acceptance criterion -> tests)**", "", "| AC | Tests |", "|---|---|"]
        for ac, row in v["traceability"].items():
            tests = row["tests"]
            L.append(f"| {ac} | {len(tests)}: " + ", ".join(f"`{x.split('::')[-1]}`" for x in tests[:4])
                     + (" ..." if len(tests) > 4 else "") + " |")
        # Findings table, most severe first, capped at 40.
        if v["findings"]:
            L += ["", "**Findings**", "", "| Severity | Check | Message | Location |", "|---|---|---|---|"]
            for f in sorted(v["findings"], key=lambda f: -f.severity.rank)[:40]:
                L.append(f"| {f.severity.value} | {f.source} | {f.message.replace('|', '/')[:160]} | "
                         f"{f.location} |")
    else:
        L.append("Validation did not complete (see escalations).")

    # 7 risks
    # Risk register table.
    L += ["", "## 7. Risks, trade-offs and failure scenarios", "",
          "| ID | Risk | Likelihood | Impact | Mitigation |", "|---|---|---|---|---|"]
    L += [f"| {r.id} | {r.title} | {r.likelihood} | {r.impact} | {r.mitigation} |" for r in s["risks"]]
    # Trade-offs table.
    if s["tradeoffs"]:
        L += ["", "| Decision | Chosen | Alternatives | Rationale |", "|---|---|---|---|"]
        L += [f"| {t.decision} | {t.chosen} | {t.alternatives} | {t.rationale} |" for t in s["tradeoffs"]]
    # Failure scenarios table.
    if s["failure_scenarios"]:
        L += ["", "| Failure scenario | Designed behaviour |", "|---|---|"]
        L += [f"| {a} | {b} |" for a, b in s["failure_scenarios"]]

    # 8 strategy
    # Validation strategy by stage.
    L += ["", "## 8. Validation approach", ""]
    for stage, items in s["strategy"].items():
        L += [f"**{stage}**"] + [f"- {i}" for i in items] + [""]

    # 9 assumptions
    L += ["## 9. Assumptions and limitations", ""]
    # Defaults used vs answers confirmed by a human.
    L += [f"- Assumed ({a.id}): {a.resolution}" for a in req.ambiguities if not a.answer]
    L += [f"- Confirmed by human ({a.id}): {a.answer}" for a in req.ambiguities if a.answer]
    # Whether an LLM was involved.
    L += ["- Generation ran with deterministic agents and a curated domain pack; "
          "an LLM provider (SDLC_LLM) is only needed for domains without a pack."
          if s["llm"].startswith("none") else f"- LLM provider used: {s['llm']}"]
    # Greenfield prototype limitation note.
    if req.kind.value == "greenfield":
        L += ["- Prototype adapters are single-node (SQLite, in-process cache and rate limiter); "
              "production adapters are specified in docs/architecture.md, not implemented."]

    # 10 approvals
    # Log of every gate decision.
    L += ["", "## 10. Human oversight log", "", "| Gate | Decision | By | Comment |", "|---|---|---|---|"]
    for g in s["gates"]:
        L.append(f"| {g.gate} | {'approved' if g.approved else 'rejected'} | {g.decided_by} | "
                 f"{g.comment.replace('|', '/')} |")
    return "\n".join(L) + "\n"


# Compute the peak number of concurrently running tasks from the event stream.
def max_parallel(events: list[dict[str, Any]]) -> int:
    # Current in-flight count and the maximum observed.
    running, best = 0, 0
    for e in events:
        # Resuming from a checkpoint resets the in-flight count.
        if e["event"] == "checkpoint" and e.get("resumed"):
            running = 0          # work in flight at a crash never completed
        # A task started.
        elif e["event"] == "task_started":
            running += 1
            best = max(best, running)
        # A task finished one way or another (never go below zero).
        elif e["event"] in ("task_succeeded", "task_failed", "task_retry", "task_degraded",
                            "repair_cycle"):
            running = max(0, running - 1)
    return best
