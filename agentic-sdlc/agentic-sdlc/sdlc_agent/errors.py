"""Exception taxonomy. The orchestrator's recovery policy is keyed on type."""
# Store annotations as strings.
from __future__ import annotations

# Finding: a validation issue carried by ValidationFailed.
from .models import Finding


# Base class for every agent failure; on its own it means "permanent, don't retry".
class AgentError(Exception):
    """Permanent failure of a task (no automatic retry)."""


# Temporary problem: the orchestrator retries the task with exponential backoff.
class TransientError(AgentError):
    """Retryable failure (timeouts, rate limits, flaky tool). Retried with backoff."""


# Raised by the Validator; the orchestrator re-runs the generators named in the findings.
class ValidationFailed(AgentError):
    """Generated output did not pass validation. Triggers a repair cycle that
    re-runs the generation tasks named in the findings' task_hint."""

    # Keep the blocking findings so the orchestrator can localise the fault.
    def __init__(self, findings: list[Finding]):
        # Blocking findings (each has a task_hint naming the task to re-run).
        self.findings = findings
        # Message: count plus the first three finding messages.
        super().__init__(f"{len(findings)} blocking finding(s): "
                         + "; ".join(f.message for f in findings[:3]))


# A safety guardrail blocked an action (path escape, ambiguous edit, size limit...).
class GuardrailViolation(AgentError):
    """An action was blocked by a safety guardrail. Never retried automatically."""


# The agent can't continue on its own; the run escalates to a human.
class NeedsHuman(AgentError):
    """The agent cannot proceed autonomously; escalated to a human."""


# The run hit an execution budget (e.g. too many task executions).
class BudgetExceeded(AgentError):
    pass
