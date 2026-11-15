"""Safety controls and human-oversight mechanics.

- Guardrails: every file an agent writes goes through ``Guardrails.apply()``, which keeps writes
  inside the workspace, enforces size/count budgets, requires unambiguous edits, and statically
  scans generated Python for unsafe patterns.
- Approval gates: the three decision modes (auto policy, recorded answers file, interactive).
- LLM output handling: replies are validated, errors are fed back for a retry, and invalid output
  falls back to "no answer" instead of being trusted.
"""
# os: create a symlink for the escape test.
import os

# pytest: fixtures, raises, parametrize, skip.
import pytest

# Approval gate and the request an agent submits to it.
from sdlc_agent.approval import ApprovalGate, GateRequest
# Raised whenever a guardrail blocks an action.
from sdlc_agent.errors import GuardrailViolation
# Sandboxed writer, its limits, and the static code scanner.
from sdlc_agent.guardrails import Guardrails, Limits, scan_python
# Validated, structured LLM call helper.
from sdlc_agent.llm import structured
# Data models used in requests and changes.
from sdlc_agent.models import Ambiguity, FileChange, Severity


# Guardrails over a fresh workspace with tiny limits (3 files, 100 bytes) so limits are easy to hit.
@pytest.fixture
def guard(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Guardrails(root, Limits(max_files_changed=3, max_file_bytes=100))


# Absolute path, parent escape, escape hidden after a subfolder, and an empty path.
@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "a/../../x", ""])
def test_writes_outside_workspace_are_blocked(guard, path):
    """No path an agent supplies can write outside the workspace."""
    with pytest.raises(GuardrailViolation):
        guard.apply(FileChange(path, "create", "x"))


def test_symlink_escape_is_blocked(guard, tmp_path):
    """A symlink inside the workspace pointing outside it cannot be used to escape."""
    # A folder outside the workspace...
    outside = tmp_path / "outside"
    outside.mkdir()
    # ...and a link to it from inside the workspace.
    try:
        os.symlink(outside, guard.root / "link")
    except OSError as exc:  # Windows without admin / Developer Mode (WinError 1314)
        pytest.skip(f"cannot create symlinks here: {exc}")
    # Writing through the link must be refused.
    with pytest.raises(GuardrailViolation, match="symlink|outside"):
        guard.apply(FileChange("link/evil.py", "create", "x"))


def test_edit_requires_unique_anchor(guard):
    """Edits replace text that occurs exactly once, so they can't hit the wrong spot."""
    guard.apply(FileChange("f.py", "create", "a = 1\na = 1\nb = 2\n"))
    # The anchor appears twice: ambiguous, refused.
    with pytest.raises(GuardrailViolation, match="2 times"):
        guard.apply(FileChange("f.py", "edit", anchor="a = 1", replacement="a = 3"))
    # The anchor doesn't appear at all: blind edit, refused.
    with pytest.raises(GuardrailViolation, match="0 times"):
        guard.apply(FileChange("f.py", "edit", anchor="zzz", replacement=""))
    # A unique anchor is replaced.
    guard.apply(FileChange("f.py", "edit", anchor="b = 2", replacement="b = 3"))
    assert (guard.root / "f.py").read_text(encoding="utf-8").endswith("b = 3\n")


def test_change_set_and_size_limits(guard):
    """Per-file size and total-files-changed budgets are enforced."""
    # 101 bytes is over the 100-byte limit.
    with pytest.raises(GuardrailViolation, match="exceeds 100 bytes"):
        guard.apply(FileChange("big.txt", "create", "x" * 101))
    # Three distinct files use up the budget...
    for i in range(3):
        guard.apply(FileChange(f"f{i}.txt", "create", "x"))
    # ...so a fourth new file is refused.
    with pytest.raises(GuardrailViolation, match="change-set"):
        guard.apply(FileChange("f3.txt", "create", "x"))
    # Rewriting a file already in the change set doesn't count again.
    guard.apply(FileChange("f0.txt", "create", "rewrite allowed"))  # same file again is fine


# Each case: unsafe code, and a word the scanner's finding must contain.
@pytest.mark.parametrize("code,needle", [
    ("eval(x)", "eval"),                                                  # arbitrary code execution
    ("import os\nos.system('ls')", "shell execution"),                    # shell command
    ("import subprocess\nsubprocess.run('ls', shell=True)", "shell=True"),  # shell injection risk
    ("import pickle\npickle.loads(b)", "deserialisation"),                # unsafe deserialisation
    ("import yaml\nyaml.load(s)", "yaml.load"),                           # unsafe YAML loader
    ("cur.execute(f'SELECT * FROM t WHERE id = {i}')", "SQL"),            # SQL built with an f-string
    ("cur.execute('SELECT * FROM t WHERE id = %s' % i)", "SQL"),          # SQL built with % formatting
    ("API_KEY = 'sk-live-1234567890abcdef'", "secret"),                   # hard-coded secret
    ("x = (", "syntax"),                                                  # code that doesn't parse
])
def test_static_scan_catches_unsafe_code(code, needle):
    """The static scanner flags each unsafe pattern as at least HIGH severity."""
    findings = scan_python(code, "gen.py")
    assert any(needle in f.message for f in findings), findings
    assert any(f.severity.rank >= Severity.HIGH.rank for f in findings)


def test_static_scan_accepts_safe_code():
    """Safe versions of the same patterns produce no findings (no false positives)."""
    # Argument-list subprocess, parameterised SQL, and an obviously fake example key.
    code = ("import subprocess\nsubprocess.run(['ls'])\n"
            "cur.execute('SELECT * FROM t WHERE id = ?', (i,))\nAPI_KEY = 'example-key-for-docs'\n")
    assert scan_python(code, "ok.py") == []


def test_auto_policy_rejects_only_with_blocking_reasons():
    """The auto policy approves by default and rejects only when the request lists blockers."""
    gate = ApprovalGate("auto")
    # Nothing blocking -> approved.
    assert gate.decide(GateRequest("g", "s")).approved
    # A blocking reason -> rejected, attributed to the policy.
    d = gate.decide(GateRequest("g", "s", blocking_reasons=["unknown domain"]))
    assert not d.approved and d.decided_by == "policy:auto"


def test_file_mode_records_answers_and_falls_back_to_policy(tmp_path):
    """File mode replays recorded decisions per gate and uses the auto policy for unlisted gates."""
    # Approve "requirements" (with an answer to Q1), reject "release", say nothing about "plan".
    f = tmp_path / "a.yaml"
    f.write_text("gates:\n  requirements:\n    approve: true\n    answers: {Q1: 'yes'}\n"
                 "  release:\n    approve: false\n    comment: not yet\n")
    gate = ApprovalGate("file", str(f))
    # Recorded approval and answers, attributed to the file.
    d = gate.decide(GateRequest("requirements", "s"))
    assert d.approved and d.answers == {"Q1": "yes"} and d.decided_by == "file:a.yaml"
    # Recorded rejection.
    assert not gate.decide(GateRequest("release", "s")).approved
    # Gate missing from the file -> auto policy.
    assert gate.decide(GateRequest("plan", "s")).decided_by == "policy:auto"


def test_interactive_mode_collects_answers():
    """Interactive mode asks each question, then asks for approval, using injected I/O."""
    # Scripted replies: answer Q1, skip Q2 (empty = keep default), approve, comment.
    script = iter(["8 characters", "", "y", "looks good"])
    # Everything the gate prints is captured here.
    out = []
    gate = ApprovalGate("interactive", input_fn=lambda _p: next(script), output_fn=out.append,
                        reviewer="alice")
    # Two open questions, each with a default answer.
    q = [Ambiguity("Q1", "code", "missing_nfr", "How long?", "7"),
         Ambiguity("Q2", "alias", "missing_nfr", "Aliases?", "yes")]
    d = gate.decide(GateRequest("requirements", "summary", q))
    # Approved by the named human; only the non-empty answer is recorded.
    assert d.approved and d.answers == {"Q1": "8 characters"} and d.decided_by == "human:alice"
    # The reviewer was shown the gate banner.
    assert any("APPROVAL GATE" in line for line in out)


class FakeLLM:
    """Stand-in LLM provider: returns canned replies in order and records the prompts it received."""
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_llm_output_is_validated_with_feedback_and_fallback():
    """LLM replies must pass validation; errors are fed back once, and invalid output yields None."""
    # Validator: the reply's JSON must contain "n".
    def validate(d):
        if "n" not in d:
            raise ValueError("missing n")
        return d["n"]

    # First reply is valid JSON but fails validation; the second has the JSON buried in chatter.
    llm = FakeLLM(['{"wrong": 1}', 'sure! {"n": 5}'])
    assert structured(llm, "s", "p", validate) == 5
    # The retry prompt told the model why its first answer was rejected.
    assert "rejected: missing n" in llm.prompts[1], "validation error is fed back"
    # Never valid -> None (the caller falls back to deterministic behaviour).
    assert structured(FakeLLM(["no json", "still no"]), "s", "p", validate) is None
    # No LLM configured -> None.
    assert structured(None, "s", "p", validate) is None
