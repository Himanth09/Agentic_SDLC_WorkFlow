"""Shared test helpers and fixtures.

pytest loads this file automatically before any test module in this folder. It provides:

- ``make_ctx`` / the ``ctx`` fixture: a fully wired but isolated ``RunContext`` (blackboard,
  tracer, approval gate, guardrails, fault injector) rooted in pytest's per-test ``tmp_path``,
  so no test touches the real ``runs/`` folder or another test's files.
- ``FnAgent``: a scriptable fake agent, so orchestrator tests can describe agent behaviour
  with a small function instead of running the real (slow, file-writing) agents.
- ``cli_args``: an ``argparse.Namespace`` with every CLI option set to a sensible default, so
  end-to-end tests can call ``cli.run()`` exactly as the command line would.
"""
# Store annotations as strings (allows "list[str] | None" on older Pythons).
from __future__ import annotations

# argparse: build the Namespace that cli.run() expects.
import argparse
# Path: filesystem paths for workspaces, traces and examples.
from pathlib import Path

# pytest: fixture decorator.
import pytest

# Agent base class and its result type (FnAgent subclasses these).
from sdlc_agent.agents.base import Agent, AgentResult
# Approval gate used at the requirements/plan/release checkpoints.
from sdlc_agent.approval import ApprovalGate
# Shared key/value store, demo fault injector, and JSONL trace writer.
from sdlc_agent.context import Blackboard, FaultInjector, Tracer
# Sandboxed file writer and its budget/size limits.
from sdlc_agent.guardrails import Guardrails, Limits
# Container handed to every agent and to the orchestrator.
from sdlc_agent.orchestrator import RunContext

# Project root (the folder that contains tests/, sdlc_agent/ and examples/).
ROOT = Path(__file__).resolve().parents[1]
# Sample requirements, recorded answers and the legacy repo used by brownfield tests.
EXAMPLES = ROOT / "examples"


# Build an isolated RunContext under tmp_path; every knob a test may want to vary is a parameter.
def make_ctx(tmp_path: Path, mode: str = "auto", faults: list[str] | None = None,
             limits: Limits | None = None, **gate_kwargs) -> RunContext:
    # Workspace directory: the only place Guardrails will allow writes.
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    # Trace file for this test; echo=False keeps test output quiet.
    tracer = Tracer(tmp_path / "trace.jsonl", echo=False)
    # Positional order matches RunContext: run_id, run_dir, board, tracer, gate, guard, faults.
    # mode selects the approval policy ("auto", "file", "interactive"); gate_kwargs pass
    # extras such as the answers file or scripted input functions.
    return RunContext("test", tmp_path, Blackboard(), tracer, ApprovalGate(mode, **gate_kwargs),
                      Guardrails(ws, limits or Limits(), tracer), FaultInjector(faults or []))


# Default fixture: a context with the auto approval policy, no faults and default limits.
@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


class FnAgent(Agent):
    """Test double: behaviour supplied as a function of (ctx, task, call_number).

    ``call_number`` counts how many times *this task id* has been run by this agent
    (1 on the first run, 2 on a retry or repair re-run, ...). That lets a test script
    "fail the first time, succeed the second" without any global state.
    """

    def __init__(self, name: str, fn=None) -> None:
        # Agent name; TaskSpec.agent refers to it.
        self.name = name
        # Default behaviour: succeed and publish {task_id: call_number} to the blackboard.
        self.fn = fn or (lambda ctx, task, n: AgentResult(outputs={task.id: n}))
        # Every task id this agent was asked to run, in order (used to assert re-runs).
        self.calls: list[str] = []

    def run(self, ctx, task):
        # Record the call first so a function that raises is still counted.
        self.calls.append(task.id)
        # Delegate to the scripted behaviour with the per-task call number.
        return self.fn(ctx, task, self.calls.count(task.id))


# Namespace equivalent to `python -m sdlc_agent run ...` with defaults; override any option via kw.
def cli_args(tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(
        # Requirement source: inline text (-r) or a file (-f); tests set one of them.
        requirement=None, requirement_file=None,
        # Runs go to tmp_path/runs/t so each test has its own run directory.
        out=str(tmp_path / "runs"), run_id="t",
        # Gate policy, optional recorded answers, and reviewer name.
        approval="auto", answers=None, reviewer="r",
        # Demo faults (e.g. ["bug:gen_api"]) and the repair-cycle budget.
        inject_fault=None, max_repairs=2,
        # Parallel workers, no LLM (deterministic), no brownfield repo, no console output.
        workers=4, llm="none", repo=None, quiet=True)
    # Apply the test's overrides.
    base.update(kw)
    return argparse.Namespace(**base)
