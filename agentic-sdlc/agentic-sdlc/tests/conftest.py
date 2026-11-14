from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from sdlc_agent.agents.base import Agent, AgentResult
from sdlc_agent.approval import ApprovalGate
from sdlc_agent.context import Blackboard, FaultInjector, Tracer
from sdlc_agent.guardrails import Guardrails, Limits
from sdlc_agent.orchestrator import RunContext

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def make_ctx(tmp_path: Path, mode: str = "auto", faults: list[str] | None = None,
             limits: Limits | None = None, **gate_kwargs) -> RunContext:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    tracer = Tracer(tmp_path / "trace.jsonl", echo=False)
    return RunContext("test", tmp_path, Blackboard(), tracer, ApprovalGate(mode, **gate_kwargs),
                      Guardrails(ws, limits or Limits(), tracer), FaultInjector(faults or []))


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


class FnAgent(Agent):
    """Test double: behaviour supplied as a function of (ctx, task, call_number)."""

    def __init__(self, name: str, fn=None) -> None:
        self.name = name
        self.fn = fn or (lambda ctx, task, n: AgentResult(outputs={task.id: n}))
        self.calls: list[str] = []

    def run(self, ctx, task):
        self.calls.append(task.id)
        return self.fn(ctx, task, self.calls.count(task.id))


def cli_args(tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(requirement=None, requirement_file=None, out=str(tmp_path / "runs"), run_id="t",
                approval="auto", answers=None, reviewer="r", inject_fault=None, max_repairs=2,
                workers=4, llm="none", repo=None, quiet=True)
    base.update(kw)
    return argparse.Namespace(**base)
