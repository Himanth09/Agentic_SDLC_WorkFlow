"""Validator: the quality gate. Evidence over assertion.

Greenfield checks
  1. syntax + static safety scan of every generated Python file (guardrails)
  2. lint (ruff) and type check (mypy) when the tools are installed
  3. cross-artifact consistency: SQL in code vs the data schema; routes in code
     vs the OpenAPI contract; generated config vs settings resolved at the gate
  4. execute the generated test suite (unit + integration + contract)
  5. requirement traceability: every acceptance criterion covered by >= 1 test

Brownfield checks
  1. change-scope: modified files must be inside the impact analysis
  2. static safety on changed files, lint on changed files
  3. full test suite; every test that passed at baseline must still pass
  4. new tests pass; traceability for the new acceptance criteria
  5. reviewable unified diff written to review/changes.patch

Blocking findings raise ValidationFailed. Each finding carries a task_hint
(fault localisation) so the orchestrator re-runs only the responsible
generator: direct attribution from tracebacks first, then the most
downstream generated module the failing test imports; on a second cycle the
scope widens to the failing test's whole import closure.
"""
# Store annotations as strings.
from __future__ import annotations

# ast: parse generated code for SQL strings, routes, config defaults and imports.
import ast
# difflib: build the unified diff for brownfield review.
import difflib
# re: SQL and traceback parsing.
import re
# Path: filesystem paths.
from pathlib import Path
# Any: loosely-typed report dictionaries.
from typing import Any

# ValidationFailed: raised with blocking findings to trigger a repair cycle.
from ..errors import ValidationFailed
# scan_python: syntax + static safety scan (eval, shell, SQL injection, secrets...).
from ..guardrails import scan_python
# Data models, severity enum and JSON serialiser.
from ..models import FileChange, Finding, NormalizedRequirement, Severity, TaskSpec, dumps
# Wrappers around pytest, mypy and ruff.
from ..tools import PytestResult, run_mypy, run_pytest, run_ruff
# Agent base class and result container.
from .base import Agent, AgentResult

# Findings at HIGH severity or above block the run.
BLOCKING = Severity.HIGH.rank
# Traceback frame matcher: 'File "x.py", line N' (group a) or 'x.py:N' (group b).
_FRAME = re.compile(r'File "(?P<a>[^"]+\.py)", line \d+|(?P<b>[\w./\\-]+\.py):\d+')


# Agent that validates generated/changed code and produces the validation report.
class Validator(Agent):
    # Registry name used by TaskSpec.agent.
    name = "validator"

    # Entry point: run the mode-specific checks, publish the report, fail on blockers.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Analysed requirement.
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Sandboxed project directory.
        project = ctx.guard.safe_path("project")
        # File -> tasks that wrote it (used for fault attribution).
        index = artifact_index(ctx.board.get("artifacts", []))
        # Choose the check suite by mode.
        if task.params.get("mode") == "brownfield":
            report, findings = self._brownfield(ctx, req, project, index)
        else:
            report, findings = self._greenfield(ctx, req, project, index)
        # Attach findings and derive the blocking subset and pass/fail verdict.
        report["findings"] = findings
        report["blocking"] = [f for f in findings if f.severity.rank >= BLOCKING]
        report["passed"] = not report["blocking"]
        # Publish the report on the blackboard and as a JSON file.
        ctx.board.put("validation", report, producer=task.id)
        ctx.guard.apply(FileChange("review/validation_report.json", "create", dumps(report),
                                   produced_by=task.id))
        # Blocking findings -> the orchestrator starts a targeted repair cycle.
        if report["blocking"]:
            raise ValidationFailed(report["blocking"])
        # Test summary for the note.
        t = report["tests"]
        return AgentResult(outputs={"validation": report},
                           notes=[f"tests {t['passed']}/{t['total']} passed; "
                                  f"{len(findings)} non-blocking finding(s)"])

    # ---------------------------------------------------------------- greenfield
    # Full greenfield check suite; returns (report, findings).
    def _greenfield(self, ctx, req, project: Path, index: dict[str, list[str]]):
        findings: list[Finding] = []
        # All generated Python files.
        py_files = sorted(p for p in index if p.endswith(".py"))
        # 1. Syntax + static safety scan, attributed to each file's owner task.
        for rel in py_files:
            for f in scan_python((project / rel).read_text(encoding="utf-8"), rel):
                f.task_hint = owner(index, rel)
                findings.append(f)
        # Production sources (exclude tests).
        src_files = [p for p in py_files if not p.startswith("tests/")]
        # Tool availability/results for the report.
        tools = {}
        # 2a. Lint with ruff.
        ruff = run_ruff(project, py_files)
        tools["ruff"] = {"available": ruff.available, "ok": ruff.ok, "issues": ruff.issues[:20]}
        for issue in ruff.issues:
            # Issue format "path:line:col: CODE message".
            rel = issue.split(":")[0]
            # Rule code (e.g. F821), if present.
            code = issue.split(": ", 1)[-1].split()[0] if ": " in issue else ""
            sev = Severity.HIGH if code.startswith("F8") else Severity.LOW   # undefined names block
            findings.append(Finding("lint", sev, issue, rel, owner(index, rel)))
        # 2b. Type check the "shortener" package with mypy.
        mypy = run_mypy(project, "shortener")
        tools["mypy"] = {"available": mypy.available, "ok": mypy.ok, "issues": mypy.issues[:20]}
        for issue in mypy.issues:
            rel = issue.split(":")[0]
            # Type errors are MEDIUM (reported, not blocking).
            findings.append(Finding("types", Severity.MEDIUM, issue, rel, owner(index, rel)))

        # 3. Cross-artifact consistency checks.
        findings += self._schema_conformance(ctx, project, src_files, index)
        findings += self._contract_conformance(ctx, project, index)
        findings += self._settings_conformance(req, project, index)

        # 4. Run the generated test suite and turn failures into findings.
        tests = run_pytest(project, ctx.guard.limits.tool_timeout_s)
        findings += self._test_findings(ctx, tests, project, index)
        # 5. Acceptance-criteria traceability.
        trace, trace_findings = self._traceability(ctx, req, index)
        findings += trace_findings
        # Report skeleton (findings/blocking/passed are added by run()).
        report = {"mode": "greenfield", "files_checked": len(py_files), "tools": tools,
                  "tests": _test_summary(tests), "traceability": trace,
                  "checks": ["syntax", "static_safety", "lint", "types", "schema_conformance",
                             "contract_conformance", "settings_conformance", "tests", "traceability"]}
        return report, findings

    # Check SQL column references in code against the published data schema.
    def _schema_conformance(self, ctx, project, src_files, index) -> list[Finding]:
        # table -> columns from the Architect.
        schema: dict[str, list[str]] = ctx.board.get("data_schema") or {}
        out = []
        # All known columns (used only to detect "no schema").
        known_cols = {c for cols in schema.values() for c in cols}
        for rel in src_files:
            tree = ast.parse((project / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Only string literals can hold SQL.
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                sql = node.value
                # SELECT <cols> FROM <table>
                m = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s+(\w+)", sql, re.S | re.I)
                if m and m.group(2) in schema:
                    for col in [c.strip() for c in m.group(1).split(",")]:
                        # Only plain identifiers (skip expressions like COUNT(*)) that aren't in the table.
                        if re.fullmatch(r"\w+", col) and col not in schema[m.group(2)]:
                            out.append(Finding("schema_conformance", Severity.BLOCKER,
                                               f"column '{col}' selected from '{m.group(2)}' is not in "
                                               f"the data schema", f"{rel}:{node.lineno}", owner(index, rel)))
                # INSERT INTO <table> (<cols>)
                m = re.match(r"\s*INSERT INTO\s+(\w+)\s*\(([^)]*)\)", sql, re.I)
                if m and m.group(1) in schema:
                    for col in [c.strip() for c in m.group(2).split(",")]:
                        if col not in schema[m.group(1)]:
                            out.append(Finding("schema_conformance", Severity.BLOCKER,
                                               f"column '{col}' inserted into '{m.group(1)}' is not in "
                                               f"the data schema", f"{rel}:{node.lineno}", owner(index, rel)))
        # Note (LOW) when no schema was available to check against.
        if not known_cols:
            out.append(Finding("schema_conformance", Severity.LOW, "no data schema published; skipped"))
        return out

    # Compare routes implemented in shortener/http.py with the OpenAPI contract (both directions).
    def _contract_conformance(self, ctx, project, index) -> list[Finding]:
        spec = ctx.board.get("api_contract")
        http = project / "shortener/http.py"
        # Missing contract or adapter -> blame the API generator task.
        if not spec or not http.exists():
            return [Finding("contract_conformance", Severity.HIGH, "contract or HTTP adapter missing",
                            task_hint="gen_api")]
        # (METHOD, path) pairs declared in the contract.
        declared = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}
        # (METHOD, path) pairs found as Route("METHOD", "path", ...) calls in code.
        implemented = set()
        for node in ast.walk(ast.parse(http.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Route" and len(node.args) >= 2:
                method, path = node.args[0], node.args[1]
                if isinstance(method, ast.Constant) and isinstance(path, ast.Constant):
                    implemented.add((str(method.value), str(path.value)))
        out = []
        # Declared but not implemented.
        for m, p in sorted(declared - implemented):
            out.append(Finding("contract_conformance", Severity.BLOCKER,
                               f"contract operation {m} {p} not implemented", "shortener/http.py",
                               owner(index, "shortener/http.py")))
        # Implemented but not declared.
        for m, p in sorted(implemented - declared):
            out.append(Finding("contract_conformance", Severity.BLOCKER,
                               f"route {m} {p} is not in the contract", "shortener/http.py",
                               owner(index, "shortener/http.py")))
        return out

    # Check that generated config defaults equal the settings approved at the requirements gate.
    def _settings_conformance(self, req, project, index) -> list[Finding]:
        cfg = project / "shortener/config.py"
        if not cfg.exists():
            return []
        # Annotated assignments with literal values, e.g. `code_length: int = 7`.
        defaults = {}
        for node in ast.walk(ast.parse(cfg.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and \
                    isinstance(node.value, ast.Constant):
                defaults[node.target.id] = node.value.value
        out = []
        # Any mismatch between approved setting and code default is HIGH (blocking).
        for key, val in req.settings.items():
            if key in defaults and defaults[key] != val:
                out.append(Finding("settings_conformance", Severity.HIGH,
                                   f"config default {key}={defaults[key]!r} does not match the "
                                   f"approved requirement ({val!r})", "shortener/config.py",
                                   owner(index, "shortener/config.py")))
        return out

    # Convert pytest failures into BLOCKER findings attributed to generator tasks.
    def _test_findings(self, ctx, tests: PytestResult, project: Path, index) -> list[Finding]:
        out: list[Finding] = []
        # No tests at all is itself a blocker.
        if tests.total == 0:
            return [Finding("tests", Severity.BLOCKER, "no tests were collected/executed",
                            task_hint="gen_unit_tests")]
        # After the first repair cycle, widen attribution to the whole import closure.
        fb = ctx.board.get("repair_feedback")
        widen = bool(fb and fb["cycle"] >= 1)
        # Task depth in the DAG (used to pick the most downstream owner).
        depth = _task_depths(ctx)
        for fail in tests.failures:
            # Which generator task(s) likely caused this failure.
            hints = attribute(fail.text, fail.file, project, index, depth, widen)
            # First line of the failure message, truncated.
            first = fail.message.splitlines()[0][:160] if fail.message else ""
            msg = f"test failed: {fail.test.split('::')[-1]} - {first}"
            # One finding per suspected task (or one unattributed finding).
            for hint in hints or [""]:
                out.append(Finding("tests", Severity.BLOCKER, msg, fail.test, hint))
        return out

    # Merge per-suite traceability and check every acceptance criterion is covered.
    def _traceability(self, ctx, req: NormalizedRequirement, index):
        # task id -> {AC id -> [tests]} published by TestGenerator tasks.
        parts = ctx.board.get("traceability_parts") or {}
        # Merged AC id -> [tests].
        matrix: dict[str, list[str]] = {}
        for part in parts.values():
            for ac, tests in part.items():
                matrix.setdefault(ac, []).extend(tests)
        out, trace = [], {}
        # First test-suite task is blamed for missing coverage.
        suites = sorted(parts) or [""]
        for ac in req.acceptance_criteria:
            covered = matrix.get(ac.id, [])
            # The regression criterion is covered by the validator's baseline comparison.
            if ac.id == "AC-REGRESSION":
                trace[ac.id] = {"text": ac.text, "tests": ["baseline comparison (validator)"]}
                continue
            trace[ac.id] = {"text": ac.text, "tests": sorted(covered)}
            # Uncovered criterion -> HIGH (blocking).
            if not covered:
                out.append(Finding("traceability", Severity.HIGH,
                                   f"acceptance criterion {ac.id} has no covering test",
                                   task_hint=suites[0]))
        # Tests that reference criteria not in the requirement -> LOW.
        for ac in sorted(set(matrix) - {a.id for a in req.acceptance_criteria}):
            out.append(Finding("traceability", Severity.LOW, f"tests reference unknown criterion {ac}"))
        return trace, out

    # ---------------------------------------------------------------- brownfield
    # Brownfield check suite; returns (report, findings).
    def _brownfield(self, ctx, req, project: Path, index):
        findings: list[Finding] = []
        # Original (untouched) repository.
        source = Path(ctx.board.require("source_repo"))
        # Approved impact analysis and pre-change test baseline.
        impact, baseline = ctx.board.require("impact"), ctx.board.require("baseline")
        # Changed files and a unified diff between original and working copy.
        changed, patch = diff_trees(source, project)
        # Write the reviewable patch.
        ctx.guard.apply(FileChange("review/changes.patch", "create", patch or "# no changes\n"))
        # New/changed test files are always in scope.
        new_tests = {p for p in changed if p.startswith("tests/")}
        # 1. Change-scope: any non-test, non-markdown change outside the impact analysis is HIGH.
        for rel in changed:
            if rel not in impact["files"] and rel not in new_tests and not rel.endswith(".md"):
                findings.append(Finding("change_scope", Severity.HIGH,
                                        f"{rel} changed but is outside the approved impact analysis",
                                        rel, owner(index, rel)))
        # Changed Python files that still exist (deleted ones can't be scanned).
        py_changed = [p for p in changed if p.endswith(".py") and (project / p).exists()]
        # 2a. Static safety scan of changed files.
        for rel in py_changed:
            for f in scan_python((project / rel).read_text(encoding="utf-8"), rel):
                f.task_hint = owner(index, rel)
                findings.append(f)
        # 2b. Lint changed files (F8xx = undefined names -> HIGH).
        ruff = run_ruff(project, py_changed)
        for issue in ruff.issues:
            rel = issue.split(":")[0]
            code = issue.split(": ", 1)[-1].split()[0] if ": " in issue else ""
            findings.append(Finding("lint", Severity.HIGH if code.startswith("F8") else Severity.LOW,
                                    issue, rel, owner(index, rel)))
        # 3. Run the full suite on the changed code.
        tests = run_pytest(project, ctx.guard.limits.tool_timeout_s)
        now_passing = set(tests.passed_tests)
        # Tests that passed at baseline but not now.
        regressions = sorted(set(baseline.passed_tests) - now_passing)
        # All change tasks that wrote files (fallback blame when attribution fails).
        change_tasks = sorted({t for ts in index.values() for t in ts if t.startswith("change_")})
        # One BLOCKER per regression, attributed via the traceback when possible.
        for name in regressions:
            fail = next((f for f in tests.failures if f.test == name), None)
            hints = attribute(fail.text, fail.file, project, index, {}, False) if fail else []
            for hint in (hints or change_tasks):
                findings.append(Finding("regression", Severity.BLOCKER,
                                        f"previously passing test now fails: {name}", name, hint))
        # 4. Other failures (e.g. new tests) also block.
        for fail in tests.failures:
            # Already reported as a regression.
            if fail.test in regressions:
                continue
            hints = attribute(fail.text, fail.file, project, index, {}, False)
            for hint in hints or change_tasks:
                findings.append(Finding("tests", Severity.BLOCKER, f"test failed: {fail.test}",
                                        fail.test, hint))
        # Traceability for the new acceptance criteria.
        trace, trace_findings = self._traceability(ctx, req, index)
        findings += trace_findings
        # Diff statistics (ignore the +++/--- file header lines).
        added = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        # Report skeleton (findings/blocking/passed are added by run()).
        report = {"mode": "brownfield", "changed_files": changed, "diff_stats": {"added": added,
                                                                               "removed": removed},
                  "baseline": _test_summary(baseline), "tests": _test_summary(tests),
                  "regressions": regressions, "traceability": trace,
                  "tools": {"ruff": {"available": ruff.available, "ok": ruff.ok,
                                     "issues": ruff.issues[:20]}},
                  "checks": ["change_scope", "static_safety", "lint", "regression_vs_baseline",
                             "tests", "traceability"]}
        return report, findings


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
# Build path -> [writer tasks] for files under project/.
def artifact_index(artifacts: list[FileChange]) -> dict[str, list[str]]:
    """project-relative path -> tasks that wrote it, in order."""
    index: dict[str, list[str]] = {}
    for ch in artifacts:
        # Only project files (skip review/ and top-level reports).
        if ch.path.startswith("project/"):
            rel = ch.path.removeprefix("project/")
            index.setdefault(rel, [])
            # Keep each writer once, preserving order.
            if ch.produced_by not in index[rel]:
                index[rel].append(ch.produced_by)
    return index


# The task that last wrote `rel` ("" if unknown).
def owner(index: dict[str, list[str]], rel: str) -> str:
    owners = index.get(rel, [])
    return owners[-1] if owners else ""


# Map a failing test to the generator task(s) most likely responsible.
def attribute(trace_text: str, test_file: str, project: Path, index: dict[str, list[str]],
              depth: dict[str, int], widen: bool) -> list[str]:
    """Fault localisation for a failing test -> generator task(s)."""
    # File paths in traceback frames, made project-relative.
    # Normalise Windows backslashes so frames match the posix keys of `index`.
    root = project.as_posix() + "/"
    frames = [(m.group("a") or m.group("b")).replace("\\", "/").removeprefix(root)
              for m in _FRAME.finditer(trace_text or "")]
    # Frames in generated, non-test files.
    src_frames = [f for f in frames if f in index and not f.startswith("tests/")]
    # Direct attribution: the deepest generated frame (first cycle only).
    if src_frames and not widen:
        return [owner(index, src_frames[-1])]          # innermost generated frame
    # Test file path if it exists in the project.
    test_rel = test_file if (project / test_file).exists() else ""
    # Without the test file, fall back to owners of any traceback frames.
    if not test_rel:
        return sorted({owner(index, f) for f in src_frames} - {""})
    # Generated modules the test imports directly.
    direct = _imported_modules(project, test_rel, index)
    # Widened mode: blame the whole transitive import closure.
    if widen:
        closure, frontier = set(direct), list(direct)
        while frontier:
            for dep in _imported_modules(project, frontier.pop(), index):
                if dep not in closure:
                    closure.add(dep)
                    frontier.append(dep)
        return sorted({owner(index, m) for m in closure | set(src_frames)} - {""})
    # Narrow mode: owners of directly imported modules.
    owners = {owner(index, m) for m in direct} - {""}
    if not owners:
        return []
    # Pick the deepest task in the DAG (the one the test most directly targets).
    return [max(owners, key=lambda t: depth.get(t, 0))]   # most downstream entry point


# Generated (non-test) modules imported via `from ... import` in file `rel`.
def _imported_modules(project: Path, rel: str, index: dict[str, list[str]]) -> list[str]:
    path = project / rel
    if not path.exists():
        return []
    # Directory of the importing file (base for relative imports).
    pkg = Path(rel).parent
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            # Relative import: climb (level - 1) directories from pkg.
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.parent
                cand = (base / (node.module or "").replace(".", "/")).as_posix() + ".py"
            # Absolute import: dotted -> path.
            else:
                cand = (node.module or "").replace(".", "/") + ".py"
            # Keep only generated production modules.
            if cand in index and not cand.startswith("tests/"):
                out.append(cand)
    return out


# Depth of every planned task in the DAG (1 = no dependencies).
def _task_depths(ctx) -> dict[str, int]:
    # All tasks across every planning stage.
    tasks = {t.id: t for p in ctx.board.get("plans", []) for t in p.tasks}
    # Memoisation cache.
    memo: dict[str, int] = {}

    # Recursive depth: 1 + deepest known dependency.
    def depth(t: str) -> int:
        if t not in memo:
            deps = [d for d in tasks[t].depends_on if d in tasks] if t in tasks else []
            memo[t] = 1 + max((depth(d) for d in deps), default=0)
        return memo[t]

    return {t: depth(t) for t in tasks}


# Compare two directory trees; return (changed relative paths, unified diff text).
def diff_trees(source: Path, project: Path) -> tuple[list[str], str]:
    # Local import avoids a circular import at module load time.
    from .codebase import CACHE_DIRS

    # Relevant files under root (excluding caches, DBs, bytecode and .sdlc files).
    def files(root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and not set(p.relative_to(root).parts) & set(CACHE_DIRS)
                and not p.name.endswith((".db", ".pyc")) and ".sdlc" not in p.name}

    # File lines (with line endings), or None if the file is binary.
    def lines(path: Path) -> list[str] | None:
        try:
            return path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return None   # binary

    # File sets of both trees.
    a, b = files(source), files(project)
    changed, chunks = [], []
    # Every file present in either tree.
    for rel in sorted(a | b):
        # Missing side = empty file (added/deleted).
        old = lines(source / rel) if rel in a else []
        new = lines(project / rel) if rel in b else []
        # Binary files: compare bytes and emit a git-style "differ" line.
        if old is None or new is None:
            if rel not in a or rel not in b or (source / rel).read_bytes() != (project / rel).read_bytes():
                changed.append(rel)
                chunks.append(f"Binary files a/{rel} and b/{rel} differ\n")
            continue
        # Text files: unified diff (/dev/null for newly added files).
        if old != new:
            changed.append(rel)
            chunks.append("".join(difflib.unified_diff(
                old, new, f"a/{rel}" if rel in a else "/dev/null", f"b/{rel}")))
    return changed, "".join(chunks)


# Serialisable summary of a PytestResult (first 20 failures, messages truncated).
def _test_summary(t: PytestResult) -> dict[str, Any]:
    return {"total": t.total, "passed": t.passed, "failed": t.failed, "errors": t.errors,
            "skipped": t.skipped, "duration_s": round(t.duration_s, 2),
            "failures": [{"test": f.test, "message": f.message[:200]} for f in t.failures[:20]]}
