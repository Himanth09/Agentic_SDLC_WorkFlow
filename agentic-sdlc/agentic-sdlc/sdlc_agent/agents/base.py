# Enable postponed evaluation of annotations so type hints are stored as strings
# (allows forward references such as "RunContext" without importing at runtime).
from __future__ import annotations

# dataclass/field build the lightweight AgentResult container below.
from dataclasses import dataclass, field
# TYPE_CHECKING is True only for static type checkers; Any is the catch-all type.
from typing import TYPE_CHECKING, Any

# GateRequest describes what a human (or policy) is asked to approve at a gate.
from ..approval import GateRequest
# GateDecision = the human's answer at a gate; TaskSpec = one node of the task DAG.
from ..models import GateDecision, TaskSpec

# Import RunContext only for type checking to avoid a circular import with the orchestrator.
if TYPE_CHECKING:
    from ..orchestrator import RunContext


# Value object that every agent returns from run(); the orchestrator consumes it.
@dataclass
class AgentResult:
    # Key/value outputs the orchestrator writes to the shared blackboard for downstream tasks.
    outputs: dict[str, Any] = field(default_factory=dict)   # published to the blackboard
    # New tasks to add to the plan at runtime (e.g. the Planner spawning the task DAG).
    spawn: list[TaskSpec] = field(default_factory=list)      # dynamic plan extension
    # Human-readable notes recorded in the execution trace / engineering summary.
    notes: list[str] = field(default_factory=list)


# Abstract base class for every agent in the pipeline.
class Agent:
    """An agent owns one kind of work. It reads inputs from the blackboard and
    returns outputs; it never schedules other agents or approves its own work."""

    # Registry name; TaskSpec.agent refers to agents by this string.
    name = "agent"

    # Main entry point: execute `task` using shared state in `ctx`; subclasses must override.
    def run(self, ctx: "RunContext", task: TaskSpec) -> AgentResult:  # pragma: no cover
        # The base class has no behaviour of its own.
        raise NotImplementedError

    # Human gate hooks (only used when the task declares `gate=`)
    # Build the request shown to the approver; default is a generic "review output" prompt.
    def gate_request(self, ctx: "RunContext", task: TaskSpec) -> GateRequest:
        # Use the task's declared gate name, falling back to the task id.
        return GateRequest(gate=task.gate or task.id, summary=f"Review output of {task.id}")

    # Callback invoked after the gate is decided; agents override it to apply human answers.
    def on_gate(self, ctx: "RunContext", task: TaskSpec, decision: GateDecision) -> None:
        # Default: nothing to apply.
        return None
