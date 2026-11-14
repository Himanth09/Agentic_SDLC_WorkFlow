"""DAG orchestrator with controlled autonomy.

Execution model
---------------
* The plan is a DAG of TaskSpecs. A bootstrap plan (understand -> plan) runs
  first; the planner *spawns* the rest of the DAG at runtime (dynamic planning).
* Ready tasks (all deps succeeded) run concurrently in a worker pool, so
  independent branches (e.g. code generation and contract-first test
  generation) execute in parallel waves.
* Agents coordinate through a typed blackboard, never by calling each other.

Recovery policy (keyed on exception type)
-----------------------------------------
* TransientError     -> retry with exponential backoff up to task.max_retries
* ValidationFailed   -> repair cycle: the findings are published as feedback,
                        the generator tasks named in finding.task_hint plus all
                        of their downstream tasks are invalidated and re-run.
                        Bounded by Limits.max_repair_cycles.
* GuardrailViolation /
  AgentError         -> task FAILED (critical: dependants SKIPPED) or DEGRADED
                        (non-critical: run continues, flagged in the summary)
* always_run tasks (risk analysis, reporting) execute even after failures, so
  a failed run still produces a reviewable partial outcome.

Human oversight
---------------
A task may declare `gate=`. After it succeeds the run pauses at that gate;
rejection fails the task (and therefore its dependants).

Durability
----------
After every state change the run is checkpointed; `resume()` continues an
interrupted run, skipping tasks that already succeeded.
"""
# Store annotations as strings.
from __future__ import annotations

# pickle: serialise the full run state for checkpoint/resume.
import pickle
# time: timestamps and retry backoff sleeps.
import time
# traceback: short stack traces for unexpected agent bugs.
import traceback
# Thread pool primitives for running ready tasks in parallel.
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
# dataclass/field: the RunContext container.
from dataclasses import dataclass, field
# Path: run directory paths.
from pathlib import Path
# Any: loosely-typed options.
from typing import Any

# Agent interface and its result type.
from .agents.base import Agent, AgentResult
# Approval gate used for human/policy decisions.
from .approval import ApprovalGate
# Shared state, fault injection and tracing.
from .context import Blackboard, FaultInjector, Tracer
# Exception types that drive the recovery policy.
from .errors import AgentError, BudgetExceeded, TransientError, ValidationFailed
# Sandboxed file writer and limits.
from .guardrails import Guardrails
# Optional LLM provider type.
from .llm import LLMProvider
# Task/record models and JSON serialiser.
from .models import Finding, GateDecision, TaskRecord, TaskSpec, TaskStatus, dumps

# Statuses that count as "finished well enough" for dependants to proceed.
DONE_OK = (TaskStatus.SUCCEEDED, TaskStatus.DEGRADED)


# Exit raised by the "crash:<task>" fault to simulate the process dying.
class SimulatedCrash(SystemExit):
    """Raised by fault injection `crash:<task>` to demonstrate checkpoint/resume."""

    # Exit code 75 (EX_TEMPFAIL: "try again later"); remember which task triggered it.
    def __init__(self, task_id: str) -> None:
        super().__init__(75)
        self.task_id = task_id
# Statuses that count as "finished badly".
DONE_BAD = (TaskStatus.FAILED, TaskStatus.SKIPPED)


# Everything an agent needs while running; passed to every Agent.run().
@dataclass
class RunContext:
    # Run identifier (the run directory's name).
    run_id: str
    # Directory holding the trace, checkpoint and workspace.
    run_dir: Path
    # Shared key/value store.
    board: Blackboard
    # Execution trace / audit log.
    tracer: Tracer
    # Approval gate (human, policy or file).
    gate: ApprovalGate
    # Sandboxed file writer and limits.
    guard: Guardrails
    # Demo fault injector.
    faults: FaultInjector
    # Optional LLM provider (None = offline).
    llm: LLMProvider | None = None
    # Extra options such as target_repo.
    options: dict[str, Any] = field(default_factory=dict)

    # The workspace root (where all files are written).
    @property
    def workspace(self) -> Path:
        return self.guard.root


# Schedules and runs the task DAG, applying the recovery policy.
class Orchestrator:
    # Set up empty plan/state.
    def __init__(self, ctx: RunContext, agents: dict[str, Agent], max_workers: int = 4,
                 backoff_s: float = 0.1) -> None:
        # Shared run context.
        self.ctx = ctx
        # Agent name -> agent instance.
        self.agents = agents
        # Size of the worker thread pool.
        self.max_workers = max_workers
        # Base delay for exponential retry backoff.
        self.backoff_s = backoff_s
        # Task id -> spec.
        self.specs: dict[str, TaskSpec] = {}
        # Task ids in insertion order (scheduling order).
        self.order: list[str] = []
        # Task id -> execution record.
        self.records: dict[str, TaskRecord] = {}
        # Repair cycles performed so far.
        self.repair_cycles = 0
        # Total task executions so far (budget).
        self.executions = 0
        # Task id -> number of transient failures (drives retries/backoff).
        self._transient: dict[str, int] = {}

    # ------------------------------------------------------------------ plan
    # Add tasks to the DAG (bootstrap or spawned by an agent) and validate the graph.
    def add_tasks(self, specs: list[TaskSpec], source: str = "bootstrap") -> None:
        # Spawned tasks with no dependencies are attached under the task that spawned them.
        if source != "bootstrap":
            for spec in specs:
                if not spec.depends_on:
                    spec.depends_on = [source]   # spawned roots hang off the spawning task
        # Register each task.
        for spec in specs:
            # Ids must be unique.
            if spec.id in self.specs:
                raise AgentError(f"duplicate task id {spec.id}")
            # The agent must exist.
            if spec.agent not in self.agents:
                raise AgentError(f"task {spec.id} references unknown agent {spec.agent}")
            self.specs[spec.id] = spec
            self.order.append(spec.id)
            self.records[spec.id] = TaskRecord(spec.id)
        # All dependencies must refer to known tasks.
        for spec in specs:
            missing = [d for d in spec.depends_on if d not in self.specs]
            if missing:
                raise AgentError(f"task {spec.id} depends on unknown task(s) {missing}")
        # The graph must stay acyclic.
        self._assert_acyclic()
        # Trace dynamic plan extensions.
        if source != "bootstrap":
            self.ctx.tracer.emit("plan_extended", by=source, added=[s.id for s in specs])

    # Depth-first search cycle check (1 = on the current path, 2 = fully explored).
    def _assert_acyclic(self) -> None:
        state: dict[str, int] = {}

        # Visit node n; `stack` is the current path (for the error message).
        def visit(n: str, stack: list[str]) -> None:
            # Seen again while still on the path -> cycle.
            if state.get(n) == 1:
                raise AgentError(f"dependency cycle: {' -> '.join(stack + [n])}")
            # Already fully explored.
            if state.get(n) == 2:
                return
            state[n] = 1
            for d in self.specs[n].depends_on:
                visit(d, stack + [n])
            state[n] = 2

        # Start from every node.
        for n in self.specs:
            visit(n, [])

    # The given tasks plus every task that (transitively) depends on them.
    def downstream(self, roots: set[str]) -> set[str]:
        out = set(roots)
        changed = True
        # Fixed-point iteration: keep adding dependants until nothing changes.
        while changed:
            changed = False
            for tid, spec in self.specs.items():
                if tid not in out and any(d in out for d in spec.depends_on):
                    out.add(tid)
                    changed = True
        return out

    # ------------------------------------------------------------------- run
    # Main loop: schedule ready tasks, handle completions, until nothing is running.
    def run(self) -> bool:
        self.ctx.tracer.emit("run_started", run_id=self.ctx.run_id, tasks=len(self.specs))
        # Future -> task id for tasks currently executing.
        running: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while True:
                # Make current task statuses visible to agents.
                self.publish_status()
                # Start every task that is ready.
                self._schedule(pool, running)
                # Nothing running and nothing startable -> done.
                if not running:
                    break
                # Block until at least one task finishes.
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    tid = running.pop(fut)
                    # Apply the result (success, retry, repair, failure).
                    self._on_complete(tid, fut)
                    # Persist state after every change.
                    self.checkpoint()
                    if self.ctx.faults.take("crash", tid):
                        # Simulated process death right after a durable checkpoint.
                        self.ctx.tracer.emit("checkpoint", crashed_after=tid)
                        # Cancel work that hasn't started yet.
                        for f in running:
                            f.cancel()
                        raise SimulatedCrash(tid)
        # The run succeeds only if every critical task succeeded.
        ok = all(self.records[t].status is TaskStatus.SUCCEEDED
                 for t, s in self.specs.items() if s.critical)
        self.ctx.board.put("run_status", "succeeded" if ok else "failed", "orchestrator")
        self.ctx.tracer.emit("run_finished", ok=ok, executions=self.executions,
                             repair_cycles=self.repair_cycles)
        # Final checkpoint.
        self.checkpoint()
        return ok

    # Start ready tasks and skip blocked ones, repeating until no more progress.
    def _schedule(self, pool: ThreadPoolExecutor, running: dict[Future, str]) -> None:
        progressed = True
        # Repeat because skipping one task can make its dependants skippable/runnable.
        while progressed:
            progressed = False
            for tid in self.order:
                rec, spec = self.records[tid], self.specs[tid]
                # Only pending tasks that aren't already submitted.
                if rec.status is not TaskStatus.PENDING or tid in running.values():
                    continue
                verdict = self._readiness(spec)
                # A dependency failed: skip this task.
                if verdict == "skip":
                    rec.status = TaskStatus.SKIPPED
                    rec.error = "upstream failure"
                    self.ctx.tracer.emit("task_skipped", task=tid, reason="upstream failure")
                    progressed = True
                elif verdict == "run":
                    # Over budget: skip unless the task must always run (risk/report).
                    if self.executions >= self.ctx.guard.limits.max_task_executions \
                            and not spec.always_run:
                        rec.status = TaskStatus.SKIPPED
                        rec.error = "execution budget exhausted"
                        self.ctx.tracer.emit("guardrail", blocked=f"budget: skip {tid}")
                        progressed = True
                        continue
                    # Count the execution and mark the task running.
                    self.executions += 1
                    rec.status = TaskStatus.RUNNING
                    rec.attempts += 1
                    rec.started_at = time.time()
                    self.ctx.tracer.emit("task_started", task=tid, agent=spec.agent,
                                         attempt=rec.attempts)
                    # Submit to the thread pool.
                    running[pool.submit(self._execute, spec)] = tid

    # "run" if all deps are done OK, "wait" if any are still pending/running, else "skip".
    def _readiness(self, spec: TaskSpec) -> str:
        states = [self.records[d].status for d in spec.depends_on]
        if all(s in DONE_OK for s in states):
            return "run"
        if any(s in (TaskStatus.PENDING, TaskStatus.RUNNING) for s in states):
            return "wait"
        # Some dependency failed: always_run tasks still run.
        return "run" if spec.always_run else "skip"

    # Runs in a worker thread: backoff, optional fault injection, then the agent.
    def _execute(self, spec: TaskSpec) -> AgentResult:
        # Exponential backoff before a retry: backoff_s * 2^(n-1).
        n = self._transient.get(spec.id, 0)
        if n:
            time.sleep(self.backoff_s * (2 ** (n - 1)))
        # Demo: simulate a transient failure once.
        if self.ctx.faults.take("transient", spec.id):
            raise TransientError(f"injected transient fault in {spec.id}")
        return self.agents[spec.agent].run(self.ctx, spec)

    # ------------------------------------------------------- result handling
    # Apply a finished task's outcome according to the recovery policy.
    def _on_complete(self, tid: str, fut: Future) -> None:
        spec, rec = self.specs[tid], self.records[tid]
        rec.finished_at = time.time()
        try:
            # Re-raises any exception from the worker thread.
            result: AgentResult = fut.result()
        except TransientError as exc:
            # Count transient failures for this task.
            n = self._transient.get(tid, 0) + 1
            self._transient[tid] = n
            # Retries left: put the task back to PENDING.
            if n <= spec.max_retries:
                rec.status = TaskStatus.PENDING
                rec.notes.append(f"transient failure #{n}: {exc}")
                self.ctx.tracer.emit("task_retry", task=tid, retry=n, error=str(exc))
                return
            return self._fail(spec, rec, f"retries exhausted: {exc}")
        except ValidationFailed as exc:
            # Start a targeted repair cycle.
            return self._repair(spec, rec, exc.findings)
        except (AgentError, BudgetExceeded) as exc:
            # Permanent failure.
            return self._fail(spec, rec, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - unexpected bug in an agent
            tb = traceback.format_exc(limit=3)
            return self._fail(spec, rec, f"unexpected {type(exc).__name__}: {exc}\n{tb}")

        # Success: publish outputs to the blackboard.
        for key, value in result.outputs.items():
            self.ctx.board.put(key, value, producer=tid)
        rec.notes.extend(result.notes)
        rec.status = TaskStatus.SUCCEEDED
        self.ctx.tracer.emit("task_succeeded", task=tid, ms=rec.duration_ms,
                             outputs=sorted(result.outputs))
        # Pause at the task's approval gate, if any (may fail the task).
        if spec.gate:
            self._gate(spec, rec)
        # Add spawned tasks only if the task is still succeeded (i.e. gate approved).
        if result.spawn and rec.status is TaskStatus.SUCCEEDED:
            self.add_tasks(result.spawn, source=tid)

    # Mark a task FAILED (critical) or DEGRADED (non-critical).
    def _fail(self, spec: TaskSpec, rec: TaskRecord, error: str) -> None:
        rec.error = error
        if spec.critical:
            rec.status = TaskStatus.FAILED
            self.ctx.tracer.emit("task_failed", task=spec.id, error=error.splitlines()[0])
        else:
            rec.status = TaskStatus.DEGRADED
            self.ctx.tracer.emit("task_degraded", task=spec.id, error=error.splitlines()[0])

    # Repair cycle: re-run the generator tasks blamed by the findings plus everything downstream.
    def _repair(self, spec: TaskSpec, rec: TaskRecord, findings: list[Finding]) -> None:
        self.ctx.board.put("validation_findings", findings, producer=spec.id)
        # Task hints that refer to real tasks.
        hints = {f.task_hint for f in findings if f.task_hint in self.specs}
        limit = self.ctx.guard.limits.max_repair_cycles
        # Can't repair: no target, or the repair budget is used up.
        if not hints or self.repair_cycles >= limit:
            why = "no repairable task identified" if not hints else f"repair budget ({limit}) spent"
            return self._fail(spec, rec, f"validation failed, {why}: "
                              + "; ".join(f.message for f in findings[:5]))
        self.repair_cycles += 1
        # Targets plus all dependants (this includes the validator itself).
        rerun = self.downstream(hints)
        # Feedback that generators read via CodeGenerator._feedback_for().
        self.ctx.board.put("repair_feedback",
                           {"cycle": self.repair_cycles, "findings": findings,
                            "targets": sorted(hints)}, producer=spec.id)
        # History entry for the engineering summary.
        self.ctx.board.append("repair_history", [{
            "cycle": self.repair_cycles, "trigger": spec.id, "targets": sorted(hints),
            "rerun": sorted(rerun), "findings": findings}], producer="orchestrator")
        self.ctx.tracer.emit("repair_cycle", cycle=self.repair_cycles, trigger=spec.id,
                             targets=sorted(hints), rerun=sorted(rerun))
        # Reset every affected task to PENDING so the scheduler runs it again.
        for t in rerun:
            r = self.records[t]
            if r.status is not TaskStatus.PENDING:
                r.notes.append(f"invalidated by repair cycle {self.repair_cycles}")
            r.status = TaskStatus.PENDING
            # Fresh retry budget.
            self._transient.pop(t, None)

    # Ask the gate to decide; a rejection fails the task.
    def _gate(self, spec: TaskSpec, rec: TaskRecord) -> None:
        agent = self.agents[spec.agent]
        # The agent describes what needs approval.
        req = agent.gate_request(self.ctx, spec)
        self.ctx.tracer.emit("gate_requested", gate=req.gate, task=spec.id,
                             questions=len(req.questions), blocking=len(req.blocking_reasons))
        # The gate (not the agent) decides.
        decision: GateDecision = self.ctx.gate.decide(req)
        self.ctx.board.append("gate_decisions", [decision], producer=spec.id)
        self.ctx.tracer.emit("gate_decided", gate=req.gate, approved=decision.approved,
                             by=decision.decided_by, comment=decision.comment[:120])
        # Let the agent apply answers (e.g. requirement settings).
        agent.on_gate(self.ctx, spec, decision)
        if not decision.approved:
            self._fail(spec, rec, f"rejected at gate '{req.gate}' by {decision.decided_by}: "
                                  f"{decision.comment}")

    # ------------------------------------------------------------ durability
    # Publish task statuses, records, specs and repair count to the blackboard.
    def publish_status(self) -> None:
        self.ctx.board.put("task_status", {t: self.records[t].status.value for t in self.order},
                           "orchestrator")
        self.ctx.board.put("task_records", [self.records[t] for t in self.order], "orchestrator")
        self.ctx.board.put("task_specs", [self.specs[t] for t in self.order], "orchestrator")
        self.ctx.board.put("repair_cycles", self.repair_cycles, "orchestrator")

    # Write checkpoint.pkl atomically, plus a human-readable state.json.
    def checkpoint(self) -> None:
        self.publish_status()
        # Everything needed to resume.
        state = {"specs": self.specs, "order": self.order, "records": self.records,
                 "repair_cycles": self.repair_cycles, "executions": self.executions,
                 "board": self.ctx.board.snapshot()}
        # Write to a temp file, then rename (atomic replace) so a crash never leaves a partial file.
        tmp = self.ctx.run_dir / "checkpoint.pkl.tmp"
        # Trusted local file written and read only by this process' owner.
        tmp.write_bytes(pickle.dumps(state))
        tmp.replace(self.ctx.run_dir / "checkpoint.pkl")
        # Readable status snapshot.
        (self.ctx.run_dir / "state.json").write_text(dumps(
            {"run_id": self.ctx.run_id, "repair_cycles": self.repair_cycles,
             "executions": self.executions,
             "tasks": [self.records[t] for t in self.order]}), encoding="utf-8")

    # Restore state from checkpoint.pkl so run() can continue.
    def resume(self) -> None:
        state = pickle.loads((self.ctx.run_dir / "checkpoint.pkl").read_bytes())  # trusted
        self.specs, self.order = state["specs"], state["order"]
        self.records = state["records"]
        self.repair_cycles, self.executions = state["repair_cycles"], state["executions"]
        self.ctx.board.restore(state["board"])
        # Re-queue anything interrupted, failed or skipped; succeeded tasks are kept.
        for rec in self.records.values():
            if rec.status in (TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.SKIPPED):
                rec.status = TaskStatus.PENDING   # re-attempt interrupted / failed work
        self.ctx.tracer.emit("checkpoint", resumed=True,
                             done=sum(r.status in DONE_OK for r in self.records.values()))
