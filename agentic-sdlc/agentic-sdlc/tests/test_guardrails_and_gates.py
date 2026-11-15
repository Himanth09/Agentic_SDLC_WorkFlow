import os

import pytest

from sdlc_agent.approval import ApprovalGate, GateRequest
from sdlc_agent.errors import GuardrailViolation
from sdlc_agent.guardrails import Guardrails, Limits, scan_python
from sdlc_agent.llm import structured
from sdlc_agent.models import Ambiguity, FileChange, Severity


@pytest.fixture
def guard(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Guardrails(root, Limits(max_files_changed=3, max_file_bytes=100))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "a/../../x", ""])
def test_writes_outside_workspace_are_blocked(guard, path):
    with pytest.raises(GuardrailViolation):
        guard.apply(FileChange(path, "create", "x"))


def test_symlink_escape_is_blocked(guard, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, guard.root / "link")
    except OSError as exc:  # Windows without admin / Developer Mode (WinError 1314)
        pytest.skip(f"cannot create symlinks here: {exc}")
    with pytest.raises(GuardrailViolation, match="symlink|outside"):
        guard.apply(FileChange("link/evil.py", "create", "x"))


def test_edit_requires_unique_anchor(guard):
    guard.apply(FileChange("f.py", "create", "a = 1\na = 1\nb = 2\n"))
    with pytest.raises(GuardrailViolation, match="2 times"):
        guard.apply(FileChange("f.py", "edit", anchor="a = 1", replacement="a = 3"))
    with pytest.raises(GuardrailViolation, match="0 times"):
        guard.apply(FileChange("f.py", "edit", anchor="zzz", replacement=""))
    guard.apply(FileChange("f.py", "edit", anchor="b = 2", replacement="b = 3"))
    assert (guard.root / "f.py").read_text(encoding="utf-8").endswith("b = 3\n")


def test_change_set_and_size_limits(guard):
    with pytest.raises(GuardrailViolation, match="exceeds 100 bytes"):
        guard.apply(FileChange("big.txt", "create", "x" * 101))
    for i in range(3):
        guard.apply(FileChange(f"f{i}.txt", "create", "x"))
    with pytest.raises(GuardrailViolation, match="change-set"):
        guard.apply(FileChange("f3.txt", "create", "x"))
    guard.apply(FileChange("f0.txt", "create", "rewrite allowed"))  # same file again is fine


@pytest.mark.parametrize("code,needle", [
    ("eval(x)", "eval"),
    ("import os\nos.system('ls')", "shell execution"),
    ("import subprocess\nsubprocess.run('ls', shell=True)", "shell=True"),
    ("import pickle\npickle.loads(b)", "deserialisation"),
    ("import yaml\nyaml.load(s)", "yaml.load"),
    ("cur.execute(f'SELECT * FROM t WHERE id = {i}')", "SQL"),
    ("cur.execute('SELECT * FROM t WHERE id = %s' % i)", "SQL"),
    ("API_KEY = 'sk-live-1234567890abcdef'", "secret"),
    ("x = (", "syntax"),
])
def test_static_scan_catches_unsafe_code(code, needle):
    findings = scan_python(code, "gen.py")
    assert any(needle in f.message for f in findings), findings
    assert any(f.severity.rank >= Severity.HIGH.rank for f in findings)


def test_static_scan_accepts_safe_code():
    code = ("import subprocess\nsubprocess.run(['ls'])\n"
            "cur.execute('SELECT * FROM t WHERE id = ?', (i,))\nAPI_KEY = 'example-key-for-docs'\n")
    assert scan_python(code, "ok.py") == []


def test_auto_policy_rejects_only_with_blocking_reasons():
    gate = ApprovalGate("auto")
    assert gate.decide(GateRequest("g", "s")).approved
    d = gate.decide(GateRequest("g", "s", blocking_reasons=["unknown domain"]))
    assert not d.approved and d.decided_by == "policy:auto"


def test_file_mode_records_answers_and_falls_back_to_policy(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("gates:\n  requirements:\n    approve: true\n    answers: {Q1: 'yes'}\n"
                 "  release:\n    approve: false\n    comment: not yet\n")
    gate = ApprovalGate("file", str(f))
    d = gate.decide(GateRequest("requirements", "s"))
    assert d.approved and d.answers == {"Q1": "yes"} and d.decided_by == "file:a.yaml"
    assert not gate.decide(GateRequest("release", "s")).approved
    assert gate.decide(GateRequest("plan", "s")).decided_by == "policy:auto"


def test_interactive_mode_collects_answers():
    script = iter(["8 characters", "", "y", "looks good"])
    out = []
    gate = ApprovalGate("interactive", input_fn=lambda _p: next(script), output_fn=out.append,
                        reviewer="alice")
    q = [Ambiguity("Q1", "code", "missing_nfr", "How long?", "7"),
         Ambiguity("Q2", "alias", "missing_nfr", "Aliases?", "yes")]
    d = gate.decide(GateRequest("requirements", "summary", q))
    assert d.approved and d.answers == {"Q1": "8 characters"} and d.decided_by == "human:alice"
    assert any("APPROVAL GATE" in line for line in out)


class FakeLLM:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_llm_output_is_validated_with_feedback_and_fallback():
    def validate(d):
        if "n" not in d:
            raise ValueError("missing n")
        return d["n"]

    llm = FakeLLM(['{"wrong": 1}', 'sure! {"n": 5}'])
    assert structured(llm, "s", "p", validate) == 5
    assert "rejected: missing n" in llm.prompts[1], "validation error is fed back"
    assert structured(FakeLLM(["no json", "still no"]), "s", "p", validate) is None
    assert structured(None, "s", "p", validate) is None
