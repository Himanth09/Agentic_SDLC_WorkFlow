"""Orchestration semantics: ordering, parallelism, recovery, gates, durability."""
import threading
import time

import pytest

from sdlc_agent.agents.base import AgentResult
from sdlc_agent.approval import GateRequest
from sdlc_agent.errors import AgentError, GuardrailViolation, TransientError, ValidationFailed
from sdlc_agent.guardrails import Limits
from sdlc_agent.models import Finding, Severity, TaskSpec, TaskStatus
from sdlc_agent.orchestrator import Orchestrator, SimulatedCrash

from .conftest import FnAgent, make_ctx


def orch(ctx, *agents, workers=4):
    return Orchestrator(ctx, {a.name: a for a in agents}, max_workers=workers, backoff_s=0.0)


def status(o):
    return {t: r.status for t, r in o.records.items()}


def test_dependencies_respected_and_outputs_published(ctx):
    order = []
    a = FnAgent("a", lambda c, t, n: (order.append(t.id), AgentResult(outputs={t.id: 1}))[1])
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("x", "", "a"), TaskSpec("y", "", "a", ["x"]), TaskSpec("z", "", "a", ["y"])])
    assert o.run()
    assert order == ["x", "y", "z"]
    assert ctx.board.get("z") == 1 and ctx.board.provenance()["z"] == ["z"]


def test_independent_tasks_run_in_parallel(ctx):
    barrier = threading.Barrier(3, timeout=5)

    def fn(c, t, n):
        barrier.wait()          # deadlocks (timeout -> BrokenBarrierError) unless all 3 run concurrently
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec(f"p{i}", "", "a") for i in range(3)])
    assert o.run()


def test_transient_failure_is_retried_then_succeeds(ctx):
    def fn(c, t, n):
        if n < 3:
            raise TransientError("flaky")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("x", "", "a", max_retries=2)])
    assert o.run()
    assert o.records["x"].attempts == 3


def test_transient_retries_exhausted_fails_and_skips_dependants(ctx):
    def fn(c, t, n):
        if t.id == "x":
            raise TransientError("down")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("x", "", "a", max_retries=1), TaskSpec("y", "", "a", ["x"]),
                 TaskSpec("r", "", "a", ["y"], always_run=True)])
    assert not o.run()
    s = status(o)
    assert s["x"] is TaskStatus.FAILED and s["y"] is TaskStatus.SKIPPED
    assert s["r"] is TaskStatus.SUCCEEDED, "always_run tasks still execute"


def test_non_critical_failure_degrades_without_blocking(ctx):
    def fn(c, t, n):
        if t.id == "docs":
            raise AgentError("docs broke")
        return AgentResult()

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("docs", "", "a", critical=False), TaskSpec("after", "", "a", ["docs"])])
    assert o.run()
    assert status(o)["docs"] is TaskStatus.DEGRADED and status(o)["after"] is TaskStatus.SUCCEEDED


def test_repair_cycle_reruns_hinted_generator_and_downstream_only(ctx):
    gen = FnAgent("gen")

    def validate(c, t, n):
        if n == 1:
            raise ValidationFailed([Finding("tests", Severity.BLOCKER, "broken", task_hint="g2")])
        return AgentResult()

    val = FnAgent("val", validate)
    o = orch(ctx, gen, val)
    o.add_tasks([TaskSpec("g1", "", "gen"), TaskSpec("g2", "", "gen"), TaskSpec("g3", "", "gen", ["g2"]),
                 TaskSpec("v", "", "val", ["g1", "g3"])])
    assert o.run()
    assert gen.calls.count("g1") == 1, "unrelated generator not re-run"
    assert gen.calls.count("g2") == 2 and gen.calls.count("g3") == 2, "target + downstream re-run"
    assert o.repair_cycles == 1
    assert ctx.board.get("repair_feedback")["targets"] == ["g2"]


def test_repair_budget_is_bounded(tmp_path):
    ctx = make_ctx(tmp_path, limits=Limits(max_repair_cycles=2))

    def validate(c, t, n):
        raise ValidationFailed([Finding("tests", Severity.BLOCKER, "always broken", task_hint="g")])

    o = orch(ctx, FnAgent("gen"), FnAgent("val", validate))
    o.add_tasks([TaskSpec("g", "", "gen"), TaskSpec("v", "", "val", ["g"])])
    assert not o.run()
    assert o.repair_cycles == 2 and status(o)["v"] is TaskStatus.FAILED
    assert "repair budget" in o.records["v"].error


def test_unattributable_validation_failure_fails_without_repair(ctx):
    def validate(c, t, n):
        raise ValidationFailed([Finding("tests", Severity.BLOCKER, "?", task_hint="")])

    o = orch(ctx, FnAgent("val", validate))
    o.add_tasks([TaskSpec("v", "", "val")])
    assert not o.run() and o.repair_cycles == 0


def test_guardrail_violation_is_never_retried(ctx):
    def fn(c, t, n):
        raise GuardrailViolation("escape attempt")

    a = FnAgent("a", fn)
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("x", "", "a", max_retries=5)])
    assert not o.run() and a.calls == ["x"]


def test_unexpected_exception_is_contained(ctx):
    def fn(c, t, n):
        raise ZeroDivisionError

    o = orch(ctx, FnAgent("a", fn))
    o.add_tasks([TaskSpec("x", "", "a")])
    assert not o.run() and "ZeroDivisionError" in o.records["x"].error


class Gated(FnAgent):
    def gate_request(self, ctx, task):
        return GateRequest(task.gate, "review", blocking_reasons=task.params.get("block", []))


def test_gate_rejection_stops_dependants(ctx):
    o = orch(ctx, Gated("a"))
    o.add_tasks([TaskSpec("x", "", "a", gate="g", params={"block": ["not safe"]}),
                 TaskSpec("y", "", "a", ["x"])])
    assert not o.run()
    assert status(o)["x"] is TaskStatus.FAILED and status(o)["y"] is TaskStatus.SKIPPED
    decision = ctx.board.get("gate_decisions")[0]
    assert not decision.approved and decision.decided_by == "policy:auto"


def test_dynamic_plan_extension(ctx):
    def planner(c, t, n):
        if t.id == "plan":
            return AgentResult(spawn=[TaskSpec("child", "", "a"), TaskSpec("grandchild", "", "a", ["child"])])
        return AgentResult()

    a = FnAgent("a", planner)
    o = orch(ctx, a)
    o.add_tasks([TaskSpec("plan", "", "a")])
    assert o.run()
    assert a.calls == ["plan", "child", "grandchild"]
    assert o.specs["child"].depends_on == ["plan"], "spawned roots hang off the spawner"


def test_cycles_and_unknown_dependencies_rejected(ctx):
    o = orch(ctx, FnAgent("a"))
    with pytest.raises(AgentError, match="cycle"):
        o.add_tasks([TaskSpec("x", "", "a", ["y"]), TaskSpec("y", "", "a", ["x"])])
    o2 = orch(ctx, FnAgent("a"))
    with pytest.raises(AgentError, match="unknown"):
        o2.add_tasks([TaskSpec("x", "", "a", ["nope"])])
    with pytest.raises(AgentError, match="unknown agent"):
        o2.add_tasks([TaskSpec("z", "", "ghost")])


def test_execution_budget_guardrail(tmp_path):
    ctx = make_ctx(tmp_path, limits=Limits(max_task_executions=2))
    o = orch(ctx, FnAgent("a"), workers=1)
    o.add_tasks([TaskSpec(f"t{i}", "", "a") for i in range(4)] +
                [TaskSpec("report", "", "a", ["t3"], always_run=True)])
    assert not o.run()
    assert sum(r.status is TaskStatus.SKIPPED for r in o.records.values()) == 2
    assert status(o)["report"] is TaskStatus.SUCCEEDED


def test_checkpoint_and_resume_after_crash(tmp_path):
    ctx = make_ctx(tmp_path, faults=["crash:b"])
    a = FnAgent("a", lambda c, t, n: (time.sleep(0.01), AgentResult(outputs={t.id: "done"}))[1])
    o = orch(ctx, a, workers=1)
    o.add_tasks([TaskSpec("a1", "", "a"), TaskSpec("b", "", "a", ["a1"]), TaskSpec("c", "", "a", ["b"])])
    with pytest.raises(SimulatedCrash):
        o.run()
    assert "c" not in a.calls

    ctx2 = make_ctx(tmp_path)
    a2 = FnAgent("a")
    o2 = orch(ctx2, a2)
    o2.resume()
    assert ctx2.board.get("b") == "done", "blackboard restored"
    assert o2.run()
    assert a2.calls == ["c"], "completed work is not repeated"
