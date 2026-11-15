"""Orchestration semantics: ordering, parallelism, recovery, gates, durability.

Every test here uses ``FnAgent`` fakes (from conftest) instead of real agents, so each one
checks a single orchestrator rule in isolation and runs in milliseconds. The rules covered:

- dependency order and publishing outputs to the blackboard
- independent tasks running in parallel
- the recovery policy: retry transient errors, degrade non-critical failures, run targeted
  repair cycles on validation failures, never retry guardrail violations, contain crashes
- bounded budgets (repair cycles, total executions)
- approval gates stopping dependants
- plans that grow while running, and rejection of invalid plans
- checkpoint and resume after a process crash
"""
# threading: a barrier to prove tasks really run at the same time.
import threading
# time: a short sleep so the crash test has work in flight.
import time

# pytest: raises.
import pytest

# Agent result type.
from sdlc_agent.agents.base import AgentResult
# Request an agent submits at its approval gate.
from sdlc_agent.approval import GateRequest
# The exception types that drive the recovery policy.
from sdlc_agent.errors import AgentError, GuardrailViolation, TransientError, ValidationFailed
# Budget limits.
from sdlc_agent.guardrails import Limits
# Data models.
from sdlc_agent.models import Finding, Severity, TaskSpec, TaskStatus
# The scheduler under test, and the exception the "crash:<task>" fault raises.
from sdlc_agent.orchestrator import Orchestrator, SimulatedCrash

# Scriptable fake agent and context builder.
from .conftest import FnAgent, make_ctx


# Build an orchestrator over the given fake agents; backoff_s=0 makes retries instant.
def orch(ctx, *agents, workers=4):
    return Orchestrator(ctx, {a.name: a for a in agents}, max_workers=workers, backoff_s=0.0)


# Final status of every task, keyed by task id.
def status(o):
    return {t: r.status for t, r in o.records.items()}


def test_dependencies_respected_and_outputs_published(ctx):
    """A chain x -> y -> z runs in order, and outputs land on the blackboard with provenance."""
    # Record the order tasks actually run in. (The tuple trick runs append, then returns the result.)
    order = []
    a = FnAgent("a", lambda c, t, n: (order.append(t.id), AgentResult(outputs={t.id: 1}))[1])
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("x", "", "a"), TaskSpec("y", "", "a", ["x"]), TaskSpec("z", "", "a", ["y"])])
    assert o.run()
    assert order == ["x", "y", "z"]
    # z's output is published, and the blackboard records which task produced it.
    assert ctx.board.get("z") == 1 and ctx.board.provenance()["z"] == ["z"]


def test_independent_tasks_run_in_parallel(ctx):
    """Tasks with no dependencies between them execute at the same time."""
    # A barrier for 3 threads: it only opens once all 3 tasks are waiting on it together.
    barrier = threading.Barrier(3, timeout=5)

    def fn(c, t, n):
        barrier.wait()          # deadlocks (timeout -> BrokenBarrierError) unless all 3 run concurrently
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec(f"p{i}", "", "a") for i in range(3)])
    assert o.run()


def test_transient_failure_is_retried_then_succeeds(ctx):
    """A TransientError is retried up to max_retries times."""
    # Fail on attempts 1 and 2, succeed on attempt 3.
    def fn(c, t, n):
        if n < 3:
            raise TransientError("flaky")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("x", "", "a", max_retries=2)])
    assert o.run()
    # 1 original attempt + 2 retries.
    assert o.records["x"].attempts == 3


def test_transient_retries_exhausted_fails_and_skips_dependants(ctx):
    """When retries run out, the task fails, its dependants are skipped, and always_run tasks still run."""
    # x always fails; everything else succeeds.
    def fn(c, t, n):
        if t.id == "x":
            raise TransientError("down")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    # y depends on x; r depends on y but is always_run (like the final report).
    o.add_tasks([TaskSpec("x", "", "a", max_retries=1), TaskSpec("y", "", "a", ["x"]),
                 TaskSpec("r", "", "a", ["y"], always_run=True)])
    assert not o.run()
    s = status(o)
    assert s["x"] is TaskStatus.FAILED and s["y"] is TaskStatus.SKIPPED
    assert s["r"] is TaskStatus.SUCCEEDED, "always_run tasks still execute"


def test_non_critical_failure_degrades_without_blocking(ctx):
    """A failing non-critical task (e.g. docs) is marked DEGRADED and doesn't stop the run."""
    def fn(c, t, n):
        if t.id == "docs":
            raise AgentError("docs broke")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("docs", "", "a", critical=False), TaskSpec("after", "", "a", ["docs"])])
    # The run still succeeds, and the dependant still runs.
    assert o.run()
    assert status(o)["docs"] is TaskStatus.DEGRADED and status(o)["after"] is TaskStatus.SUCCEEDED


def test_repair_cycle_reruns_hinted_generator_and_downstream_only(ctx):
    """A validation failure blamed on g2 re-runs g2 and its dependants, but not unrelated g1."""
    gen = FnAgent("gen")

    # The validator fails the first time, blaming generator g2 via task_hint; then it passes.
    def validate(c, t, n):
        if n == 1:
            raise ValidationFailed([Finding("tests", Severity.BLOCKER, "broken", task_hint="g2")])
        return AgentResult()

    val = FnAgent("val", validate)
    o = orch(ctx, gen, val)
    # Graph: g1 and g2 are independent; g3 depends on g2; v validates g1 and g3.
    o.add_tasks([TaskSpec("g1", "", "gen"), TaskSpec("g2", "", "gen"), TaskSpec("g3", "", "gen", ["g2"]),
                 TaskSpec("v", "", "val", ["g1", "g3"])])
    assert o.run()
    assert gen.calls.count("g1") == 1, "unrelated generator not re-run"
    assert gen.calls.count("g2") == 2 and gen.calls.count("g3") == 2, "target + downstream re-run"
    assert o.repair_cycles == 1
    # The generators are told what to fix through the blackboard.
    assert ctx.board.get("repair_feedback")["targets"] == ["g2"]


def test_repair_budget_is_bounded(tmp_path):
    """A defect that is never fixed stops after max_repair_cycles instead of looping forever."""
    ctx = make_ctx(tmp_path, limits=Limits(max_repair_cycles=2))

    # The validator always fails.
    def validate(c, t, n):
        raise ValidationFailed([Finding("tests", Severity.BLOCKER, "always broken", task_hint="g")])

    o = orch(ctx, FnAgent("gen"), FnAgent("val", validate))
    o.add_tasks([TaskSpec("g", "", "gen"), TaskSpec("v", "", "val", ["g"])])
    assert not o.run()
    assert o.repair_cycles == 2 and status(o)["v"] is TaskStatus.FAILED
    # The failure message explains why the run stopped.
    assert "repair budget" in o.records["v"].error


def test_unattributable_validation_failure_fails_without_repair(ctx):
    """If no generator can be blamed, the orchestrator fails instead of re-running things at random."""
    # The finding has no task_hint.
    def validate(c, t, n):
        raise ValidationFailed([Finding("tests", Severity.BLOCKER, "?", task_hint="")])

    o = orch(ctx, FnAgent("val", validate))
    o.add_tasks([TaskSpec("v", "", "val")])
    assert not o.run() and o.repair_cycles == 0


def test_guardrail_violation_is_never_retried(ctx):
    """A safety violation is final: even with retries allowed, the task runs only once."""
    def fn(c, t, n):
        raise GuardrailViolation("escape attempt")

    a = FnAgent("a", fn)
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("x", "", "a", max_retries=5)])
    assert not o.run() and a.calls == ["x"]


def test_unexpected_exception_is_contained(ctx):
    """A bug inside an agent fails that task with a useful error instead of crashing the orchestrator."""
    def fn(c, t, n):
        raise ZeroDivisionError

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("x", "", "a")])
    assert not o.run() and "ZeroDivisionError" in o.records["x"].error


class Gated(FnAgent):
    """Fake agent with an approval gate; params["block"] supplies blocking reasons."""

    def gate_request(self, ctx, task):
        return GateRequest(task.gate, "review", blocking_reasons=task.params.get("block", []))


def test_gate_rejection_stops_dependants(ctx):
    """A rejected gate fails its task and skips everything downstream."""
    o = orch(ctx, Gated("a"))
    # x has a gate with a blocking reason, so the auto policy rejects it; y depends on x.
    o.add_tasks([TaskSpec("x", "", "a", gate="g", params={"block": ["not safe"]}),
                 TaskSpec("y", "", "a", ["x"])])
    assert not o.run()
    assert status(o)["x"] is TaskStatus.FAILED and status(o)["y"] is TaskStatus.SKIPPED
    # The decision is recorded for the human oversight log.
    decision = ctx.board.get("gate_decisions")[0]
    assert not decision.approved and decision.decided_by == "policy:auto"


def test_dynamic_plan_extension(ctx):
    """A task can add new tasks to the plan while the run is in progress (as the Planner does)."""
    # "plan" spawns child, and grandchild depending on child; the spawned tasks just succeed.
    def planner(c, t, n):
        if t.id == "plan":
            return AgentResult(spawn=[TaskSpec("child", "", "a"), TaskSpec("grandchild", "", "a", ["child"])])
        return AgentResult()

    a = FnAgent("a", planner)
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("plan", "", "a")])
    assert o.run()
    assert a.calls == ["plan", "child", "grandchild"]
    # A spawned task with no dependencies is attached under the task that spawned it.
    assert o.specs["child"].depends_on == ["plan"], "spawned roots hang off the spawner"


def test_cycles_and_unknown_dependencies_rejected(ctx):
    """Invalid plans are rejected up front: cycles, unknown dependencies, unknown agents."""
    o = orch(ctx, FnAgent("a"))
    # x depends on y and y depends on x.
    with pytest.raises(AgentError, match="cycle"):
        o.add_tasks([TaskSpec("x", "", "a", ["y"]), TaskSpec("y", "", "a", ["x"])])
    o2 = orch(ctx, FnAgent("a"))
    # Dependency on a task that doesn't exist.
    with pytest.raises(AgentError, match="unknown"):
        o2.add_tasks([TaskSpec("x", "", "a", ["nope"])])
    # Task assigned to an agent that isn't registered.
    with pytest.raises(AgentError, match="unknown agent"):
        o2.add_tasks([TaskSpec("z", "", "ghost")])


def test_execution_budget_guardrail(tmp_path):
    """The total-executions budget stops new work, but always_run tasks (the report) still run."""
    ctx = make_ctx(tmp_path, limits=Limits(max_task_executions=2))
    # One worker so execution order is predictable.
    o = orch(ctx, FnAgent("a"), workers=1)
    # Four independent tasks, plus an always_run report.
    o.add_tasks([TaskSpec(f"t{i}", "", "a") for i in range(4)] +
                [TaskSpec("report", "", "a", ["t3"], always_run=True)])
    assert not o.run()
    # Two tasks ran within the budget; the other two were skipped.
    assert sum(r.status is TaskStatus.SKIPPED for r in o.records.values()) == 2
    assert status(o)["report"] is TaskStatus.SUCCEEDED


def test_checkpoint_and_resume_after_crash(tmp_path):
    """After a crash, resume restores state and runs only the work that hadn't finished."""
    # First process: the "crash:b" fault kills the run right after task b finishes.
    ctx = make_ctx(tmp_path, faults=["crash:b"])
    a = FnAgent("a", lambda c, t, n: (time.sleep(0.01), AgentResult(outputs={t.id: "done"}))[1])
    o = orch(ctx, a, workers=1)
    # Chain a1 -> b -> c.
    o.add_tasks([TaskSpec("a1", "", "a"), TaskSpec("b", "", "a", ["a1"]), TaskSpec("c", "", "a", ["b"])])
    with pytest.raises(SimulatedCrash):
        o.run()
    # c never started before the crash.
    assert "c" not in a.calls

    # Second process: a fresh context and agent over the same directory, loaded from the checkpoint.
    ctx2 = make_ctx(tmp_path)
    a2 = FnAgent("a")
    o2 = orch(ctx2, a2)
    o2.resume()
    # b's output survived the crash.
    assert ctx2.board.get("b") == "done", "blackboard restored"
    assert o2.run()
    # Only the unfinished task ran.
    assert a2.calls == ["c"], "completed work is not repeated"
