"""Guardrails for safe, reliable autonomous execution.

1. Filesystem sandbox  - agents may only write inside the run workspace; no
                         absolute paths, `..`, or symlink escapes. Source repos
                         (brownfield) are copied, never modified in place.
2. Change discipline   - edits are anchor-based and the anchor must match
                         exactly once; change-set size is capped.
3. Static safety scan  - AST checks on every generated Python file (eval/exec,
                         shell=True, unsafe deserialisation, SQL built by string
                         formatting, hard-coded secrets, non-crypto RNG for tokens).
4. Execution budget    - caps on total task executions and repair cycles,
                         subprocess timeouts for tool runs.
"""
# Store annotations as strings.
from __future__ import annotations

# ast: static safety analysis of generated code.
import ast
# os: path separator and case normalisation.
import os
# re: secret detection.
import re
# dataclass: the Limits configuration.
from dataclasses import dataclass
# Path: workspace paths.
from pathlib import Path

# GuardrailViolation: raised whenever an action is blocked.
from .errors import GuardrailViolation
# FileChange to apply; Finding/Severity for scan results.
from .models import FileChange, Finding, Severity


# Budget and size limits for one run.
@dataclass
class Limits:
    # Max total task executions (including retries and repairs).
    max_task_executions: int = 80
    # Max validator-driven repair cycles.
    max_repair_cycles: int = 2
    # Max size of any single written file.
    max_file_bytes: int = 200_000
    # Max number of distinct files a run may write.
    max_files_changed: int = 80
    # Timeout for external tools such as pytest.
    tool_timeout_s: int = 120


# Comparable form of a path: no Windows "\\?\" prefix, case-folded where the OS ignores case.
def _norm(p: Path) -> str:
    s = str(p)
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(s)


# True for a Windows directory junction (a symlink-like redirect that is_symlink() misses).
def _is_junction(p: Path) -> bool:
    is_junction = getattr(p, "is_junction", None)  # Python 3.12+
    return bool(is_junction and is_junction())


# Sandboxed file writer: every agent write goes through apply().
class Guardrails:
    # Bind to the workspace root.
    def __init__(self, root: Path, limits: Limits | None = None, tracer=None) -> None:
        # Absolute, symlink-resolved workspace root.
        self.root = root.resolve()
        # Limits (defaults if not given).
        self.limits = limits or Limits()
        # Optional tracer for "guardrail" events.
        self.tracer = tracer
        # Distinct paths written so far.
        self.files_changed: set[str] = set()

    # -- filesystem ---------------------------------------------------------
    # Turn a relative path into an absolute one that is guaranteed to be inside the workspace.
    def safe_path(self, rel: str) -> Path:
        # Reject empty, absolute, or ".."-containing paths up front.
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            self._block(f"path '{rel}' is absolute or escapes the workspace")
        # Lexical join: with absolute and ".." paths rejected above, this is inside the root.
        p = self.root / rel
        # Walk up from the target to the root, rejecting any symlink/junction on the way.
        for parent in [p, *p.parents]:
            if parent == self.root:
                break
            if parent.is_symlink() or _is_junction(parent):
                self._block(f"path '{rel}' traverses a symlink")
        # Defence in depth: the resolved path must also stay below the root. On Windows,
        # resolve() can return a "\\?\"-prefixed form while a sibling thread is creating a
        # parent directory, so compare normalised strings rather than Path objects.
        resolved = _norm(p.resolve())
        root = _norm(self.root)
        if resolved != root and not resolved.startswith(root + os.sep):
            self._block(f"path '{rel}' resolves outside the workspace ({p.resolve()})")
        return p

    # Apply a FileChange (create or anchor-based edit) after all checks pass.
    def apply(self, change: FileChange) -> Path:
        # Validated target path.
        target = self.safe_path(change.path)
        # A new file would push the change set over its cap.
        if change.path not in self.files_changed and \
                len(self.files_changed) >= self.limits.max_files_changed:
            self._block(f"change-set exceeds {self.limits.max_files_changed} files")
        # create: write the given content.
        if change.op == "create":
            data = change.content
        # edit: replace an anchor that must appear exactly once.
        elif change.op == "edit":
            if not target.exists():
                self._block(f"edit target '{change.path}' does not exist")
            text = target.read_text(encoding="utf-8")
            count = text.count(change.anchor)
            # Zero or multiple matches would be a blind/ambiguous edit.
            if count != 1:
                self._block(f"edit anchor matched {count} times in '{change.path}' "
                            "(must be exactly 1)")
            data = text.replace(change.anchor, change.replacement, 1)
        # Any other op is invalid.
        else:
            self._block(f"unknown change op '{change.op}'")
        # Enforce the per-file size limit.
        if len(data.encode()) > self.limits.max_file_bytes:
            self._block(f"'{change.path}' exceeds {self.limits.max_file_bytes} bytes")
        # Create parent directories and write.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        # Count it towards the change set.
        self.files_changed.add(change.path)
        return target

    # Record a guardrail event and raise.
    def _block(self, msg: str) -> None:
        if self.tracer:
            self.tracer.emit("guardrail", blocked=msg)
        raise GuardrailViolation(msg)


# --------------------------------------------------------------------------- #
# Static safety scan
# --------------------------------------------------------------------------- #
# Matches AWS access key ids, or password/secret/api_key/token = "<8+ chars>" (case-insensitive).
_SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16})|((?i:password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}['\"])")


# Scan Python source for unsafe patterns; returns findings (syntax errors are BLOCKERs).
def scan_python(source: str, path: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        # Code that doesn't parse can't be scanned further.
        return [Finding("syntax", Severity.BLOCKER, f"syntax error: {exc.msg}",
                        f"{path}:{exc.lineno}")]

    # Helper to record a static_safety finding at the node's line.
    def add(sev: Severity, msg: str, node: ast.AST) -> None:
        findings.append(Finding("static_safety", sev, msg, f"{path}:{getattr(node, 'lineno', 0)}"))

    for node in ast.walk(tree):
        # Function calls.
        if isinstance(node, ast.Call):
            # Dotted name of what is called, e.g. "subprocess.run".
            name = _call_name(node.func)
            # Arbitrary code execution.
            if name in ("eval", "exec"):
                add(Severity.BLOCKER, f"use of {name}()", node)
            # Shell execution.
            elif name in ("os.system", "os.popen"):
                add(Severity.BLOCKER, f"shell execution via {name}()", node)
            # subprocess with a literal truthy shell= argument.
            elif name.startswith("subprocess.") and any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value
                    for k in node.keywords):
                add(Severity.BLOCKER, "subprocess with shell=True", node)
            # Deserialising untrusted data can run code.
            elif name in ("pickle.load", "pickle.loads", "marshal.loads"):
                add(Severity.HIGH, f"unsafe deserialisation via {name}()", node)
            # yaml.load without a Loader may construct arbitrary objects.
            elif name == "yaml.load" and not any(k.arg == "Loader" for k in node.keywords):
                add(Severity.HIGH, "yaml.load without explicit safe Loader", node)
            # DB execute calls whose SQL is built dynamically.
            elif name.endswith(".execute") or name.endswith(".executemany"):
                if node.args and _is_dynamic_sql(node.args[0]):
                    add(Severity.BLOCKER, "SQL built with string formatting (injection risk); "
                        "use bound parameters", node)
            # random.* used in token-related code (file or call name mentions "token").
            elif name.startswith("random.") and "token" in path.lower() + name.lower():
                add(Severity.MEDIUM, "non-cryptographic RNG used for tokens; use secrets", node)
        # "except:" with no exception type.
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            add(Severity.LOW, "bare except clause", node)

    # Line-by-line secret scan (lines mentioning "example" are ignored).
    for i, line in enumerate(source.splitlines(), 1):
        if _SECRET_RE.search(line) and "example" not in line.lower():
            findings.append(Finding("static_safety", Severity.BLOCKER,
                                    "possible hard-coded secret", f"{path}:{i}"))
    return findings


# Dotted name of a call target: foo -> "foo", a.b.c -> "a.b.c", other -> "".
def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_call_name(func.value)}.{func.attr}"
    return ""


# True if an SQL argument is built with an f-string, + or %, or str.format().
def _is_dynamic_sql(arg: ast.AST) -> bool:
    # f"..."
    if isinstance(arg, ast.JoinedStr):
        return True
    # "..." + x  or  "..." % x
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
        return True
    # "...".format(x)
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "format":
        return True
    return False
