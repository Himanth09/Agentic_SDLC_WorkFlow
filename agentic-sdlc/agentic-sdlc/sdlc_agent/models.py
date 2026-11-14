"""Core data model shared by the orchestrator and all agents.

Everything an agent produces is a typed, serialisable record so that runs can be
checkpointed, resumed, audited and rendered into the final engineering summary.
"""
# Store annotations as strings.
from __future__ import annotations

# enum: string-valued enumerations.
import enum
# hashlib: short content hashes for artifacts.
import hashlib
# json: serialisation helper.
import json
# time: default timestamp for gate decisions.
import time
# Dataclass tools; asdict converts a dataclass to a dict recursively.
from dataclasses import asdict, dataclass, field
# Any: loosely-typed values.
from typing import Any


# Finding/ambiguity severity, ordered from least to most serious.
class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"

    # Numeric rank for comparisons (info=0 ... blocker=4).
    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "blocker"].index(self.value)


# Lifecycle state of a task in the DAG.
class TaskStatus(str, enum.Enum):
    # Not started yet.
    PENDING = "pending"
    # Currently executing.
    RUNNING = "running"
    # Finished successfully.
    SUCCEEDED = "succeeded"
    # Finished with an error.
    FAILED = "failed"
    SKIPPED = "skipped"          # upstream critical failure / rejected gate
    DEGRADED = "degraded"        # non-critical failure, run continued


# What kind of work the requirement describes.
class ScenarioKind(str, enum.Enum):
    # Build a new system.
    GREENFIELD = "greenfield"
    # Change an existing code base.
    BROWNFIELD = "brownfield"
    # Only add tests or documentation.
    TEST_DOCS = "test_docs"


# --------------------------------------------------------------------------- #
# Requirement understanding
# --------------------------------------------------------------------------- #
# A clarifying question raised during requirement analysis.
@dataclass
class Ambiguity:
    # Question id, e.g. "Q1" (rule-based) or "L1" (LLM-sourced).
    id: str
    term: str                 # the phrase that triggered it (or the missing dimension)
    category: str             # vague_term | missing_nfr | missing_scope | conflict
    question: str             # clarifying question for the human
    default_assumption: str   # what the agent will assume if not answered
    # How important the question is (HIGH needs a human; BLOCKER stops the run).
    severity: Severity = Severity.MEDIUM
    answer: str | None = None  # filled by the human gate
    dimension: str = ""        # domain dimension this question pins down (drives settings)

    # The effective value: the human's answer if given, otherwise the default.
    @property
    def resolution(self) -> str:
        return self.answer if self.answer else self.default_assumption


# A testable statement the delivered system must satisfy.
@dataclass
class AcceptanceCriterion:
    # Id such as "AC-1" or "AC-REGRESSION"; tests reference it via "Covers:".
    id: str
    # The criterion text.
    text: str
    kind: str = "functional"  # functional | nfr | security | regression


# The requirement after analysis: the main input to every downstream agent.
@dataclass
class NormalizedRequirement:
    # Original requirement text.
    raw: str
    # Short title (first sentence).
    title: str
    # Greenfield / brownfield / tests+docs.
    kind: ScenarioKind
    domain: str                          # e.g. "url_shortener" or "unknown"
    # One-line normalised summary.
    summary: str
    # Capabilities tagged "(stated)" or "(assumed)".
    capabilities: list[str]
    # Non-functional requirements: dimension title -> default value.
    nfrs: dict[str, str]
    # Testable criteria.
    acceptance_criteria: list[AcceptanceCriterion]
    # Open questions and their resolutions.
    ambiguities: list[Ambiguity]
    # Explicit non-goals.
    out_of_scope: list[str]
    # Path of the existing repository (brownfield only).
    target_repo: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)  # resolved knobs fed to codegen

    # BLOCKER questions with neither an answer nor a usable default.
    def open_blockers(self) -> list[Ambiguity]:
        return [a for a in self.ambiguities
                if a.severity is Severity.BLOCKER and not a.answer and not a.default_assumption]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
# One node of the task DAG.
@dataclass
class TaskSpec:
    # Unique task id, e.g. "design_contract".
    id: str
    # Human-readable title.
    title: str
    # Name of the agent that executes it (matches Agent.name).
    agent: str
    # Ids of tasks that must finish first.
    depends_on: list[str] = field(default_factory=list)
    # Agent-specific parameters (mode, files, recipe...).
    params: dict[str, Any] = field(default_factory=dict)
    critical: bool = True          # failure stops dependants; non-critical => DEGRADED
    max_retries: int = 2           # for TransientError
    gate: str | None = None        # name of a human approval gate to pass AFTER the task
    always_run: bool = False       # run even if dependencies failed (reporting, risk)
    # Why the task exists / why it is ordered this way.
    rationale: str = ""


# A set of tasks produced by one planning stage.
@dataclass
class Plan:
    # The tasks.
    tasks: list[TaskSpec]
    # Explanation of the decomposition.
    rationale: str

    # Map of task id -> TaskSpec.
    def by_id(self) -> dict[str, TaskSpec]:
        return {t.id: t for t in self.tasks}


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# A file write proposed by an agent and applied through the guardrails.
@dataclass
class FileChange:
    """A single, reviewable change. `create` writes content; `edit` replaces an
    anchor that must occur exactly once (guardrail against blind edits)."""
    # Workspace-relative path.
    path: str
    op: str                          # create | edit
    # Full file content (create).
    content: str = ""
    # Exact text to find (edit); must occur exactly once.
    anchor: str = ""
    # Text that replaces the anchor (edit).
    replacement: str = ""
    # Why the change was made.
    reason: str = ""
    # Task id that produced it (used for fault attribution).
    produced_by: str = ""


# One issue reported by the Validator.
@dataclass
class Finding:
    source: str          # validator check name
    # How serious it is (HIGH and above block the run).
    severity: Severity
    # Description.
    message: str
    # Where, e.g. "path.py:42" or a test id.
    location: str = ""
    task_hint: str = ""  # which generation task should repair it


# An entry in the risk register.
@dataclass
class Risk:
    # Id, e.g. "R-MIGRATION".
    id: str
    # Short description.
    title: str
    # low / medium / high.
    likelihood: str
    # low / medium / high.
    impact: str
    # How it is mitigated.
    mitigation: str
    # Grouping, e.g. technical, data, quality, process.
    category: str = "technical"


# An architecture decision (rendered as an ADR).
@dataclass
class TradeOff:
    # What was decided.
    decision: str
    # The option chosen.
    chosen: str
    # Options considered and rejected.
    alternatives: str
    # Why, and the consequences.
    rationale: str


# The recorded outcome of an approval gate.
@dataclass
class GateDecision:
    # Gate name.
    gate: str
    # Whether the run may proceed.
    approved: bool
    decided_by: str          # human:<name> | policy:auto | file:<path>
    # Optional reviewer comment.
    comment: str = ""
    # Answers to clarifying questions, keyed by question id.
    answers: dict[str, str] = field(default_factory=dict)
    # When the decision was made (epoch seconds).
    at: float = field(default_factory=time.time)


# Execution record for one task (status, attempts, timing, errors).
@dataclass
class TaskRecord:
    # Task id.
    id: str
    # Current status.
    status: TaskStatus = TaskStatus.PENDING
    # How many times it has been started.
    attempts: int = 0
    # Start time (epoch seconds).
    started_at: float | None = None
    # End time (epoch seconds).
    finished_at: float | None = None
    # Error text if it failed.
    error: str | None = None
    # Notes returned by the agent.
    notes: list[str] = field(default_factory=list)

    # Wall-clock duration in milliseconds (0 if not finished).
    @property
    def duration_ms(self) -> int:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at) * 1000)
        return 0


# First 12 hex characters of the SHA-256 of `text`.
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# Recursively convert dataclasses, enums and containers into JSON-compatible values.
def to_jsonable(obj: Any) -> Any:
    # Dataclass instance -> dict (values converted recursively).
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    # Enum -> its value.
    if isinstance(obj, enum.Enum):
        return obj.value
    # Dict -> dict with string keys.
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    # List/tuple/set -> list.
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    # Everything else as-is.
    return obj


# Pretty-printed JSON; values json can't handle are stringified.
def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), indent=2, default=str)
