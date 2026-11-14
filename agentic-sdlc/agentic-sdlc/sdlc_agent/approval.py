"""Human-in-the-loop approval gates (controlled autonomy).

Three interchangeable modes behind one interface:

* interactive - a human reviews the gate payload in the terminal, answers
                clarifying questions and approves/rejects.
* auto        - a *policy* decides: approve only when there are no blocking
                reasons. The decision is recorded as `policy:auto`, never as a human.
* file        - decisions and answers pre-recorded in YAML (reproducible demos, CI).
                Gates missing from the file fall back to the auto policy.

Agents never approve themselves: they only *describe* what needs approval
(summary, questions, blocking reasons). The gate owns the decision.
"""
# Store annotations as strings (forward references, cheaper imports).
from __future__ import annotations

# sys: detect whether stdin is an interactive terminal.
import sys
# dataclass/field: build the GateRequest value object.
from dataclasses import dataclass, field
# Path: read the YAML answers file and show its name.
from pathlib import Path
# Callable: type for the injectable input/output functions.
from typing import Callable

# PyYAML: parse pre-recorded gate decisions.
import yaml

# Ambiguity = a clarifying question; GateDecision = the recorded outcome of a gate.
from .models import Ambiguity, GateDecision


# What an agent asks the gate to decide (built by Agent.gate_request()).
@dataclass
class GateRequest:
    # Gate name, e.g. "requirements", "change_plan", "release".
    gate: str
    # Human-readable text describing what is being approved.
    summary: str
    # Clarifying questions the reviewer may answer (blank = accept the default).
    questions: list[Ambiguity] = field(default_factory=list)
    # Reasons approval must not be granted automatically (policy rejects if any).
    blocking_reasons: list[str] = field(default_factory=list)


# Decides gates in interactive, auto (policy) or file mode.
class ApprovalGate:
    # Configure the mode; input/output functions are injectable for testing.
    def __init__(self, mode: str = "auto", answers_file: str | None = None,
                 reviewer: str = "reviewer",
                 input_fn: Callable[[str], str] = input,
                 output_fn: Callable[[str], None] = print) -> None:
        # "interactive", "auto" or "file".
        self.mode = mode
        # Reviewer name recorded as "human:<name>" on interactive decisions.
        self.reviewer = reviewer
        # Function used to read answers (defaults to built-in input()).
        self.input = input_fn
        # Function used to print prompts (defaults to print()).
        self.out = output_fn
        # Pre-recorded decisions keyed by gate name (file mode only).
        self.recorded: dict = {}
        # File mode: load the YAML answers file up front.
        if mode == "file":
            # File mode is meaningless without a file.
            if not answers_file:
                raise ValueError("file mode requires --answers")
            # Keep the path to label decisions with its file name.
            self.answers_path = answers_file
            # Load the "gates" mapping (empty if the file or key is empty).
            self.recorded = (yaml.safe_load(Path(answers_file).read_text(encoding="utf-8")) or {}).get("gates", {})

    # Decide one gate request according to the configured mode.
    def decide(self, req: GateRequest) -> GateDecision:
        # Interactive: ask a human in the terminal.
        if self.mode == "interactive":
            return self._interactive(req)
        # File mode with a recorded entry for this gate: replay it.
        if self.mode == "file" and req.gate in self.recorded:
            # The recorded entry (None in YAML becomes an empty dict).
            rec = self.recorded[req.gate] or {}
            return GateDecision(
                # Gate name.
                gate=req.gate,
                # Approve unless the file explicitly says approve: false.
                approved=bool(rec.get("approve", True)),
                # Attribute the decision to the answers file.
                decided_by=f"file:{Path(self.answers_path).name}",
                # Optional comment.
                comment=rec.get("comment", ""),
                # Answers to clarifying questions, coerced to strings.
                answers={str(k): str(v) for k, v in (rec.get("answers") or {}).items()},
            )
        # Auto mode, or file mode without an entry for this gate: apply the policy.
        return self._policy(req)

    # -- modes --------------------------------------------------------------
    # Auto policy: reject if anything blocks, otherwise approve with the defaults.
    @staticmethod
    def _policy(req: GateRequest) -> GateDecision:
        # Any blocking reason means automatic rejection.
        if req.blocking_reasons:
            return GateDecision(req.gate, False, "policy:auto",
                                "rejected by policy: " + "; ".join(req.blocking_reasons))
        # Note how many unanswered questions were accepted with default assumptions.
        note = ""
        if req.questions:
            note = f"accepted {len(req.questions)} default assumption(s); flagged in summary"
        # Approved by policy (never recorded as a human).
        return GateDecision(req.gate, True, "policy:auto", note)

    # Interactive mode: show the payload, collect answers and a verdict.
    def _interactive(self, req: GateRequest) -> GateDecision:
        # With no terminal (e.g. CI) and the real input(), we can't prompt: use the policy.
        if not sys.stdin.isatty() and self.input is input:
            self.out(f"[gate {req.gate}] stdin is not a TTY; falling back to auto policy")
            return self._policy(req)
        # Visual separator line.
        bar = "=" * 72
        # Gate header and summary.
        self.out(f"\n{bar}\nAPPROVAL GATE: {req.gate}\n{bar}\n{req.summary}")
        # List any blocking reasons so the reviewer sees them.
        for reason in req.blocking_reasons:
            self.out(f"  BLOCKING: {reason}")
        # Answers keyed by question id.
        answers: dict[str, str] = {}
        # Ask each clarifying question, showing the default used if left blank.
        for q in req.questions:
            self.out(f"\n[{q.id}] ({q.severity.value}) {q.question}\n"
                     f"   default if blank: {q.default_assumption}")
            ans = self.input("   answer> ").strip()
            # Only record non-blank answers.
            if ans:
                answers[q.id] = ans
        # Approval prompt (default is No).
        verdict = self.input("\nApprove and continue? [y/N] > ").strip().lower()
        # Optional free-text comment.
        comment = self.input("Comment (optional) > ").strip()
        # Approved only on an explicit "y"/"yes".
        return GateDecision(req.gate, verdict in ("y", "yes"), f"human:{self.reviewer}",
                            comment, answers)
