"""Planner: decomposes the normalised requirement into a task DAG.

Planning is *staged*: the first planning pass decides the shape of the work;
brownfield work gets a second pass (`plan_changes`) after codebase analysis,
because the concrete change set depends on what the code actually looks like.

The planner also performs conflict analysis: change tasks that touch the
same files are serialised (ordered by recipe priority, bug fixes first) while
independent tasks are left parallel.
"""
# Store annotations as strings.
from __future__ import annotations

# GateRequest: what the human sees when approving a plan.
from ..approval import GateRequest
# get_pack returns None for unknown domains; require_pack raises instead.
from ..knowledge import get_pack, require_pack
# Plan = list of tasks + rationale; TaskSpec = one DAG node; ScenarioKind = greenfield/brownfield/...
from ..models import NormalizedRequirement, Plan, ScenarioKind, TaskSpec
# Agent base class and result container.
from .base import Agent, AgentResult


# Agent that turns the requirement into an executable task DAG.
class Planner(Agent):
    # Registry name used by TaskSpec.agent.
    name = "planner"

    # Entry point: choose a plan shape and spawn its tasks.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Analysed requirement.
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Planning stage: "initial" (first pass) or "changes" (brownfield second pass).
        stage = task.params.get("stage", "initial")
        # Second pass: concrete brownfield change tasks from the impact analysis.
        if stage == "changes":
            plan = self._brownfield_changes(ctx, req)
        # Brownfield with a repository: analyse first, then plan changes.
        elif req.kind is ScenarioKind.BROWNFIELD and req.target_repo:
            plan = self._brownfield_initial(req)
        # Brownfield without a repository: ask a human for it.
        elif req.kind is ScenarioKind.BROWNFIELD:
            # A reporter task in "needs_input" mode that raises NeedsHuman.
            plan = Plan([TaskSpec("need_repo", "Locate target repository", "reporter",
                                  params={"mode": "needs_input",
                                          "message": "Brownfield requirement but no --repo given"})],
                        "Brownfield work cannot start without the code base.")
            # Still append the risk review + report tail so the run ends with a summary.
            plan.tasks.extend(self._tail(["need_repo"], docs=None))
        # Unrecognised domain: skeleton plan with escalated generation.
        elif req.domain == "unknown":
            plan = self._unknown(req)
        # Recognised greenfield domain: full contract-first plan.
        else:
            plan = self._greenfield(req)
        # Blackboard key depends on the stage so both plans are kept.
        key = "plan" if stage == "initial" else "change_plan"
        # Append this plan to the history of all planning stages.
        history = ctx.board.get("plans", []) + [plan]
        # Publish the plan and spawn its tasks into the running DAG.
        return AgentResult(outputs={key: plan, "plans": history}, spawn=plan.tasks,
                           notes=[f"{len(plan.tasks)} tasks planned ({stage})"])

    # --------------------------------------------------------------- shapes
    # Greenfield plan: architecture -> contract -> parallel codegen/testgen/docs -> validate -> tail.
    def _greenfield(self, req: NormalizedRequirement) -> Plan:
        # Domain pack listing components and test suites.
        pack = get_pack(req.domain)
        # Only called for known domains.
        assert pack is not None
        # Fixed design tasks that come first.
        tasks = [
            # Architecture document (no dependencies).
            TaskSpec("design_architecture", "Design architecture, scaling plan and trade-offs",
                     "architect", params={"part": "architecture"},
                     rationale="Architecture constrains every downstream artefact."),
            # Contract depends on the architecture.
            TaskSpec("design_contract", "Define API contract (OpenAPI) and data schema",
                     "architect", ["design_architecture"], params={"part": "contract"},
                     rationale="Contract-first: code, tests and docs all consume the same contract."),
        ]
        # One codegen task per component; dependencies come from the pack (import order).
        for comp in pack.components:
            tasks.append(TaskSpec(comp.task_id, comp.title, "codegen", list(comp.depends_on),
                                  params={"files": comp.files}, rationale=comp.rationale))
        # One testgen task per test suite (runs in parallel with code generation).
        for suite in pack.test_suites:
            tasks.append(TaskSpec(suite.task_id, suite.title, "testgen", list(suite.depends_on),
                                  params={"files": suite.files}, rationale=suite.rationale))
        # Documentation task: non-critical, so its failure only degrades the run.
        tasks.append(TaskSpec("gen_docs", "Generate README, API reference, ADRs, runbook", "docgen",
                              ["design_contract"], critical=False,
                              rationale="Non-critical: docs failure degrades, never blocks code."))
        # Ids of every generator task; validation waits for all of them.
        gen_ids = [c.task_id for c in pack.components] + [s.task_id for s in pack.test_suites]
        # Validation quality gate.
        tasks.append(TaskSpec("validate", "Validate: syntax, safety scan, lint, contract, tests, "
                              "traceability", "validator", gen_ids,
                              rationale="Quality gate; failures trigger targeted repair cycles."))
        # Risk review + report (report also waits for docs).
        tasks.extend(self._tail(["validate"], docs="gen_docs"))
        # The plan with a human-readable rationale.
        return Plan(tasks, "Contract-first decomposition: architecture -> contract -> parallel "
                           "component generation (DAG by import dependencies) || contract-driven "
                           "test generation || docs -> validation with repair loop -> risk review "
                           "-> engineering summary + release gate.")

    # Brownfield first pass: analyse the codebase, then plan the changes (gated).
    def _brownfield_initial(self, req: NormalizedRequirement) -> Plan:
        tasks = [
            # Copy the repo, model it, run baseline tests, compute impact.
            TaskSpec("analyze_codebase", "Snapshot repo, map modules/routes/data model, baseline tests, "
                     "impact analysis", "codebase", rationale="Never change code you have not mapped."),
            # Re-invoke the Planner at stage "changes"; a human approves the result at gate "change_plan".
            TaskSpec("plan_changes", "Plan concrete change set from impact analysis", "planner",
                     ["analyze_codebase"], params={"stage": "changes"}, gate="change_plan",
                     rationale="Second planning stage: the change set depends on the code."),
        ]
        # Return the two-task plan.
        return Plan(tasks, "Brownfield: analyse first, then plan concrete edits against the real "
                           "code, with a human gate on the change plan.")

    # Brownfield second pass: one codegen task per recipe, serialising those that share files.
    def _brownfield_changes(self, ctx, req: NormalizedRequirement) -> Plan:
        # Impact analysis from the CodebaseAnalyst.
        impact = ctx.board.require("impact")
        # Domain pack with brownfield recipes.
        pack = require_pack(req.domain)
        # Matched recipes ordered by priority (bug fixes first).
        recipes = sorted((pack.brownfield_recipes[r] for r in impact["recipes"]),
                         key=lambda r: r.priority)
        # Tasks being built.
        tasks: list[TaskSpec] = []
        # Tracks the most recent task that edits each file (for conflict serialisation).
        touched: dict[str, str] = {}   # file -> last task that edits it
        # Ids of all change tasks.
        change_ids = []
        # Create a task per recipe.
        for recipe in recipes:
            # Files this recipe will modify.
            files = impact["files_by_recipe"][recipe.id]
            # Depend on earlier tasks that touched any of the same files.
            deps = sorted({touched[f] for f in files if f in touched})
            # Task id derived from the recipe id.
            tid = f"change_{recipe.id}"
            # Codegen task in brownfield mode; rationale explains any serialisation.
            tasks.append(TaskSpec(tid, recipe.title, "codegen", ["plan_changes", *deps],
                                  params={"mode": "brownfield", "recipe": recipe.id},
                                  rationale=("serialised after " + ", ".join(deps) +
                                             " (overlapping files)") if deps else "independent"))
            # This task is now the latest editor of these files.
            for f in files:
                touched[f] = tid
            # Remember the id.
            change_ids.append(tid)
        # Regression + feature tests, in parallel with the edits.
        tasks.append(TaskSpec("gen_regression_tests", "Generate regression + feature tests", "testgen",
                              ["plan_changes"], params={"mode": "brownfield"},
                              rationale="Tests derive from acceptance criteria; parallel to edits."))
        # Changelog / change notes after all edits (non-critical).
        tasks.append(TaskSpec("gen_docs", "Changelog, change notes, impact report", "docgen",
                              change_ids, params={"mode": "brownfield"}, critical=False))
        # Validation after all edits and the new tests.
        tasks.append(TaskSpec("validate", "Validate: regression vs baseline, new tests, safety, scope",
                              "validator", [*change_ids, "gen_regression_tests"],
                              params={"mode": "brownfield"}))
        # Risk review + report.
        tasks.extend(self._tail(["validate"], docs="gen_docs"))
        # The change plan.
        return Plan(tasks, f"{len(recipes)} change(s) ordered by priority; edits to shared files are "
                           "serialised to avoid conflicting anchors, tests generated in parallel.")

    # Unknown domain: generic architecture, freeform (LLM/human) generation, validation, tail.
    def _unknown(self, req: NormalizedRequirement) -> Plan:
        tasks = [
            # Generic layered outline.
            TaskSpec("design_architecture", "Outline generic layered architecture", "architect",
                     params={"part": "architecture"}),
            # Freeform generation (LLM, or NeedsHuman without one).
            TaskSpec("gen_implementation", "Generate implementation (LLM or human required)", "codegen",
                     ["design_architecture"], params={"mode": "freeform"}),
            # Validate whatever was produced.
            TaskSpec("validate", "Validate generated output", "validator", ["gen_implementation"]),
        ]
        # Risk review + report (no docs task).
        tasks.extend(self._tail(["validate"], docs=None))
        # The skeleton plan.
        return Plan(tasks, "Domain not recognised: plan the skeleton and escalate generation.")

    # Common ending for every plan: risk review then the report/release gate.
    @staticmethod
    def _tail(after: list[str], docs: str | None) -> list[TaskSpec]:
        return [
            # Risk review always runs, even if upstream tasks failed.
            TaskSpec("risk_review", "Risk register, trade-offs, failure scenarios", "risk", after,
                     always_run=True),
            # Report always runs; also waits for docs when present; gated by "release".
            TaskSpec("report", "Engineering summary and release decision", "reporter",
                     ["risk_review"] + ([docs] if docs else []), always_run=True, gate="release"),
        ]

    # ----------------------------------------------------------------- gate
    # Build the text a human reviews when approving a plan.
    def gate_request(self, ctx, task: TaskSpec) -> GateRequest:
        # Which plan to show depends on the stage of the gated task.
        key = "plan" if task.params.get("stage", "initial") == "initial" else "change_plan"
        # Fetch that plan.
        plan = ctx.board.require(key)
        # Start with the rationale and a task list heading.
        lines = [f"Rationale: {plan.rationale}", "", "Tasks:"]
        # One aligned line per task.
        for t in plan.tasks:
            # Dependency suffix like " <- a, b".
            deps = f" <- {', '.join(t.depends_on)}" if t.depends_on else ""
            # Flags for gated and non-critical tasks.
            flags = "".join([" [gate]" if t.gate else "", " [non-critical]" if not t.critical else ""])
            # Fixed-width columns for readability.
            lines.append(f"  {t.id:<24} {t.agent:<11}{deps}{flags}")
        # For change plans, also list the files that will be edited.
        if key == "change_plan":
            impact = ctx.board.require("impact")
            lines += ["", "Files to change:"] + [f"  {f}" for f in impact["files"]]
        # Gate name and summary text.
        return GateRequest(task.gate or key, "\n".join(lines))
