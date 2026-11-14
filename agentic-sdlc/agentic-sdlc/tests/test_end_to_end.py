"""Full workflow runs through the CLI entry point (the same path a user takes)."""
import json
import pickle
from pathlib import Path

import pytest

from sdlc_agent.cli import run

from .conftest import EXAMPLES, cli_args

REQ = EXAMPLES / "requirements"


def _run(tmp_path, **kw):
    args = cli_args(tmp_path, **kw)
    code = run(args)
    run_dir = Path(args.out) / args.run_id
    state = pickle.loads((run_dir / "checkpoint.pkl").read_bytes())
    summary = state["board"]["data"].get("summary") or {}
    events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    return code, summary, state, events, run_dir


@pytest.mark.e2e
def test_greenfield_url_shortener(tmp_path):
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"))
    assert code == 0 and summary["status"] == "ready_for_review"
    v = summary["validation"]
    assert v["passed"] and v["tests"]["failed"] == 0 and v["tests"]["total"] >= 80
    assert all(row["tests"] for row in v["traceability"].values()), "every AC covered"
    ws = run_dir / "workspace"
    for rel in ("project/openapi.yaml", "project/shortener/service.py", "project/docs/architecture.md",
                "project/README.md", "ENGINEERING_SUMMARY.md", "review/validation_report.json"):
        assert (ws / rel).exists(), rel
    assert {e["gate"] for e in events if e["event"] == "gate_decided"} == {"requirements", "plan", "release"}


@pytest.mark.e2e
def test_transient_fault_and_schema_drift_are_recovered(tmp_path):
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"),
        inject_fault=["transient:design_contract", "bug:gen_persistence"])
    assert code == 0 and summary["status"] == "ready_for_review"
    assert any(e["event"] == "task_retry" and e["task"] == "design_contract" for e in events)
    repairs = [e for e in events if e["event"] == "repair_cycle"]
    assert len(repairs) == 1 and repairs[0]["targets"] == ["gen_persistence"]
    assert "gen_unit_tests" not in repairs[0]["rerun"] and "gen_domain" not in repairs[0]["rerun"]


@pytest.mark.e2e
def test_api_defect_without_traceback_is_localised_to_entry_point(tmp_path):
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"), inject_fault=["bug:gen_api"])
    assert code == 0
    repairs = [e for e in events if e["event"] == "repair_cycle"]
    assert len(repairs) == 1 and repairs[0]["targets"] == ["gen_api"]
    assert repairs[0]["rerun"] == ["gen_api", "report", "risk_review", "validate"]


@pytest.mark.e2e
def test_ambiguous_requirement_escalates_under_auto_policy(tmp_path):
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "ambiguous_campaign_links.md"))
    assert code == 1 and summary["status"] == "escalated"
    assert state["records"]["plan"].status.value == "skipped"


@pytest.mark.e2e
def test_ambiguous_requirement_with_human_answers(tmp_path):
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "ambiguous_campaign_links.md"), approval="file",
        answers=str(EXAMPLES / "answers/ambiguous_campaign_links.yaml"))
    assert code == 0 and summary["status"] == "ready_for_review"
    settings = summary["requirement"].settings
    assert settings["code_length"] == 8 and settings["analytics_ip_mode"] == "drop"
    assert settings["default_ttl_days"] == 180 and settings["create_rate_per_min"] == 20
    config = (run_dir / "workspace/project/shortener/config.py").read_text(encoding="utf-8")
    assert "code_length: int = 8" in config, "human answers change the generated code"
    assert summary["rejected_answers"], "unparseable answer recorded"


@pytest.mark.e2e
def test_brownfield_change(tmp_path):
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "brownfield_expiry_and_alias_bug.md"),
        repo=str(EXAMPLES / "legacy_links"))
    assert code == 0 and summary["status"] == "ready_for_review"
    v = summary["validation"]
    assert v["regressions"] == [] and v["baseline"]["passed"] == 5 and v["tests"]["passed"] >= 15
    specs = state["specs"]
    assert "change_fix_alias_overwrite" in specs["change_add_expiration"].depends_on, \
        "overlapping edits are serialised, bug fix first"
    assert "INSERT OR REPLACE" in (EXAMPLES / "legacy_links/links/store.py").read_text(encoding="utf-8"), \
        "source repo is never modified"
    patch = (run_dir / "workspace/review/changes.patch").read_text(encoding="utf-8")
    assert "-                \"INSERT OR REPLACE INTO links" in patch


@pytest.mark.e2e
def test_unknown_domain_escalates_generation(tmp_path):
    answers = tmp_path / "a.yaml"
    answers.write_text("gates:\n  requirements: {approve: true}\n  plan: {approve: true}\n")
    code, summary, state, events, _ = _run(
        tmp_path, requirement="Build a payroll engine for contractors.", approval="file",
        answers=str(answers))
    assert code == 1 and summary["status"] == "escalated"
    assert "NeedsHuman" in state["records"]["gen_implementation"].error
    assert state["records"]["report"].status.value in ("succeeded", "failed")  # report always runs


@pytest.mark.e2e
def test_brownfield_without_repo_asks_for_it(tmp_path):
    code, summary, state, events, _ = _run(
        tmp_path, requirement="Fix the bug in our existing URL shortener where an alias is overwritten.")
    assert code == 1 and "no --repo" in state["records"]["need_repo"].error
