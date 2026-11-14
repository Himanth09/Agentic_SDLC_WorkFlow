"""Tool adapters used by agents: test runner, linter, type checker.

Every tool runs as a subprocess with a timeout, an isolated working directory,
no shell, and a scrubbed environment. A tool that is not installed is
reported as `unavailable` (a validation gap), never silently treated as passing.
"""
# Store annotations as strings.
from __future__ import annotations

# os: environment variables and os.devnull.
import os
# shutil.which: check whether ruff/mypy are installed.
import shutil
# subprocess: run the tools (never with shell=True).
import subprocess
# sys.executable: run pytest with the same Python interpreter.
import sys
import xml.etree.ElementTree as ET  # parses only files we generated ourselves
# dataclass/field: result containers.
from dataclasses import dataclass, field
# Path: project paths.
from pathlib import Path

# TransientError: a timeout is treated as retryable.
from .errors import TransientError


# One failed or errored test.
@dataclass
class TestFailure:
    # Test id "classname::name".
    test: str
    # Test file path guessed from the classname.
    file: str
    # Short failure message.
    message: str
    # Full traceback plus captured output (used for fault localisation).
    text: str


# Parsed outcome of a pytest run.
@dataclass
class PytestResult:
    # Number of test cases.
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    # Sum of per-test durations.
    duration_s: float = 0.0
    # Details for each failed/errored test.
    failures: list[TestFailure] = field(default_factory=list)
    # Ids of passing tests (used for regression comparison).
    passed_tests: list[str] = field(default_factory=list)
    # Last 25 lines of pytest output.
    output_tail: str = ""

    # True if at least one test ran and none failed or errored.
    @property
    def ok(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.errors == 0


# Minimal environment for subprocesses (avoids leaking secrets/config into tools).
def _env() -> dict[str, str]:
    # Keep only the variables tools need to run.
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")}
    # No .pyc files; deterministic hashing for reproducible runs.
    keep.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONUTF8": "1"})
    return keep


# Run pytest in `project` and parse its JUnit XML report.
def run_pytest(project: Path, timeout_s: int = 120, select: list[str] | None = None) -> PytestResult:
    # Temporary JUnit report location.
    report = project / ".sdlc-junit.xml"
    # Quiet output, no cache, short tracebacks, JUnit XML with captured logs, optional test selection.
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
           f"--junitxml={report}", "-o", "junit_family=xunit2", "-o", "junit_logging=all",
           *(select or [])]
    try:
        # Run with the project on PYTHONPATH; check=False because test failures are expected data.
        proc = subprocess.run(cmd, cwd=project, env={**_env(), "PYTHONPATH": str(project)},
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        # Timeouts may be flaky, so they are retryable.
        raise TransientError(f"pytest timed out after {timeout_s}s") from exc
    # Keep the last 25 lines of combined output.
    result = PytestResult(output_tail="\n".join((proc.stdout + proc.stderr).splitlines()[-25:]))
    # No report usually means a collection/import error.
    if not report.exists():
        result.errors = 1
        result.failures.append(TestFailure("<collection>", "", "pytest produced no report",
                                           result.output_tail))
        return result
    # Parse the XML, then delete the temporary file.
    root = ET.parse(report).getroot()
    report.unlink()
    # One <testcase> element per test.
    for case in root.iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        result.total += 1
        result.duration_s += float(case.get("time") or 0)
        # A <failure> or <error> child marks a bad outcome.
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        # Skipped tests.
        if case.find("skipped") is not None:
            result.skipped += 1
        # Failed or errored tests.
        elif bad is not None:
            if bad.tag == "failure":
                result.failed += 1
            else:
                result.errors += 1
            classname = case.get("classname") or ""
            # Captured logs carry tracebacks of exceptions the code converted into
            # responses (e.g. HTTP 500) - essential evidence for fault localisation.
            logs = "\n".join((el.text or "") for tag in ("system-out", "system-err")
                             for el in case.findall(tag))
            # File path derived from the dotted classname; message truncated to 300 chars.
            result.failures.append(TestFailure(name, classname.replace(".", "/") + ".py",
                                               (bad.get("message") or "")[:300],
                                               (bad.text or "") + "\n" + logs))
        # Passed tests.
        else:
            result.passed += 1
            result.passed_tests.append(name)
    return result


# Outcome of a lint/type-check tool.
@dataclass
class ToolResult:
    # Tool name.
    tool: str
    # False if the tool isn't installed (a validation gap).
    available: bool
    # True if it reported no problems.
    ok: bool
    # One line per issue.
    issues: list[str] = field(default_factory=list)


# Run ruff on the given paths (pyflakes, pycodestyle, warnings, bugbear; long lines ignored).
def run_ruff(project: Path, paths: list[str], timeout_s: int = 60) -> ToolResult:
    exe = shutil.which("ruff")
    # Not installed: unavailable (ok=True so it doesn't count as issues; the gap is reported separately).
    if not exe:
        return ToolResult("ruff", False, True)
    proc = subprocess.run([exe, "check", "--select", "E,F,W,B", "--ignore", "E501",
                           "--output-format", "concise", "--no-cache", *paths],
                          cwd=project, env=_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout_s, check=False)
    # Issue lines contain ":"; skip the "Found N errors" summary line.
    issues = [ln for ln in proc.stdout.splitlines() if ":" in ln and not ln.startswith("Found")]
    # ok = clean exit code.
    return ToolResult("ruff", True, proc.returncode == 0, issues)


# Run mypy on a package (missing third-party stubs ignored, no cache written).
def run_mypy(project: Path, package: str, timeout_s: int = 120) -> ToolResult:
    exe = shutil.which("mypy")
    if not exe:
        return ToolResult("mypy", False, True)
    proc = subprocess.run([exe, package, "--ignore-missing-imports", "--no-error-summary",
                           "--cache-dir", os.devnull], cwd=project, env=_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False)
    # Only error lines count (notes are ignored).
    issues = [ln for ln in proc.stdout.splitlines() if ": error:" in ln]
    return ToolResult("mypy", True, not issues, issues)
