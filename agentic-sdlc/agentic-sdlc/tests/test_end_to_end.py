"""Full workflow runs through the CLI entry point (the same path a user takes).

Each test calls ``cli.run()`` with arguments equivalent to a real command line, lets the whole
agent pipeline run (understand -> plan -> design -> generate -> validate -> risk -> report), and
then inspects the three durable outputs of a run:

- the process exit code (0 = completed, 1 = did not complete),
- ``checkpoint.pkl``: the final orchestrator state (task records, specs, blackboard),
- ``trace.jsonl``: the event log (retries, repair cycles, gate decisions).

These are the slowest tests (~2-4 s each) and are marked ``e2e``; skip them with ``-m "not e2e"``.
"""
# json: parse trace.jsonl lines.
import json
# pickle: load the final checkpoint.
import pickle
# Path: run directory paths.
from pathlib import Path

# pytest: markers.
import pytest

# The function behind `python -m sdlc_agent run`.
from sdlc_agent.cli import run

# examples/ folder and the default CLI argument builder.
from .conftest import EXAMPLES, cli_args

# Sample requirement documents.
REQ = EXAMPLES / "requirements"


# Run the workflow and return (exit code, summary, final state, trace events, run dir).
def _run(tmp_path, **kw):
    # CLI arguments with defaults; the test overrides only what it cares about.
    args = cli_args(tmp_path, **kw)
    code = run(args)
    run_dir = Path(args.out) / args.run_id
    # Final orchestrator state written by the last checkpoint.
    state = pickle.loads((run_dir / "checkpoint.pkl").read_bytes())
    # The engineering summary the reporter put on the blackboard (empty if it never ran).
    summary = state["board"]["data"].get("summary") or {}
    # Every trace event, in order.
    events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    return code, summary, state, events, run_dir


@pytest.mark.e2e
def test_greenfield_url_shortener(tmp_path):
    """Happy path: the mandatory requirement produces a validated, reviewable service."""
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"))
    # Completed and ready for a human reviewer.
    assert code == 0 and summary["status"] == "ready_for_review"
    v = summary["validation"]
    # Validation passed, no failing generated tests, and a substantial test suite was produced.
    assert v["passed"] and v["tests"]["failed"] == 0 and v["tests"]["total"] >= 80
    # Every acceptance criterion is traced to at least one test.
    assert all(row["tests"] for row in v["traceability"].values()), "every AC covered"
    # The key artifacts exist on disk.
    ws = run_dir / "workspace"
    for rel in ("project/openapi.yaml", "project/shortener/service.py", "project/docs/architecture.md",
                "project/README.md", "ENGINEERING_SUMMARY.md", "review/validation_report.json"):
        assert (ws / rel).exists(), rel
    # All three approval gates were decided (here by the auto policy).
    assert {e["gate"] for e in events if e["event"] == "gate_decided"} == {"requirements", "plan", "release"}


@pytest.mark.e2e
def test_transient_fault_and_schema_drift_are_recovered(tmp_path):
    """A flaky step is retried, and a defect is repaired by re-running only its generator."""
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"),
        # transient: design_contract fails once with a retryable error.
        # bug: gen_persistence writes code with a deliberate schema mismatch.
        inject_fault=["transient:design_contract", "bug:gen_persistence"])
    # The run still finishes successfully.
    assert code == 0 and summary["status"] == "ready_for_review"
    # The transient failure was retried.
    assert any(e["event"] == "task_retry" and e["task"] == "design_contract" for e in events)
    # Exactly one repair cycle, aimed at the generator that introduced the bug.
    repairs = [e for e in events if e["event"] == "repair_cycle"]
    assert len(repairs) == 1 and repairs[0]["targets"] == ["gen_persistence"]
    # Unrelated work (tests, domain code) was not redone: the repair is targeted.
    assert "gen_unit_tests" not in repairs[0]["rerun"] and "gen_domain" not in repairs[0]["rerun"]


@pytest.mark.e2e
def test_api_defect_without_traceback_is_localised_to_entry_point(tmp_path):
    """A wrong HTTP response with no stack trace is blamed on the API layer."""
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "greenfield_url_shortener.md"), inject_fault=["bug:gen_api"])
    assert code == 0
    repairs = [e for e in events if e["event"] == "repair_cycle"]
    # One cycle targeting gen_api...
    assert len(repairs) == 1 and repairs[0]["targets"] == ["gen_api"]
    # ...re-running only gen_api and the tasks downstream of it.
    assert repairs[0]["rerun"] == ["gen_api", "report", "risk_review", "validate"]


@pytest.mark.e2e
def test_ambiguous_requirement_escalates_under_auto_policy(tmp_path):
    """Without a human, a vague requirement is escalated rather than guessed at."""
    code, summary, state, events, _ = _run(
        tmp_path, requirement_file=str(REQ / "ambiguous_campaign_links.md"))
    # The run stops (exit 1) with status "escalated".
    assert code == 1 and summary["status"] == "escalated"
    # Planning never happened because the requirements gate rejected.
    assert state["records"]["plan"].status.value == "skipped"


@pytest.mark.e2e
def test_ambiguous_requirement_with_human_answers(tmp_path):
    """Recorded human answers unblock the run and change what is generated."""
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "ambiguous_campaign_links.md"), approval="file",
        answers=str(EXAMPLES / "answers/ambiguous_campaign_links.yaml"))
    assert code == 0 and summary["status"] == "ready_for_review"
    # The answers were turned into settings...
    settings = summary["requirement"].settings
    assert settings["code_length"] == 8 and settings["analytics_ip_mode"] == "drop"
    assert settings["default_ttl_days"] == 180 and settings["create_rate_per_min"] == 20
    # ...and the settings reached the generated code.
    config = (run_dir / "workspace/project/shortener/config.py").read_text(encoding="utf-8")
    assert "code_length: int = 8" in config, "human answers change the generated code"
    # The answers file contains one deliberately invalid answer; it is recorded, not silently used.
    assert summary["rejected_answers"], "unparseable answer recorded"


@pytest.mark.e2e
def test_brownfield_change(tmp_path):
    """Changing an existing repo: no regressions, ordered edits, source untouched, patch produced."""
    code, summary, state, events, run_dir = _run(
        tmp_path, requirement_file=str(REQ / "brownfield_expiry_and_alias_bug.md"),
        repo=str(EXAMPLES / "legacy_links"))
    assert code == 0 and summary["status"] == "ready_for_review"
    v = summary["validation"]
    # The 5 original tests still pass, nothing regressed, and new tests were added.
    assert v["regressions"] == [] and v["baseline"]["passed"] == 5 and v["tests"]["passed"] >= 15
    # The two change tasks edit the same files, so the planner ran them in sequence (bug fix first).
    specs = state["specs"]
    assert "change_fix_alias_overwrite" in specs["change_add_expiration"].depends_on, \
        "overlapping edits are serialised, bug fix first"
    # The original repo still contains the bug: all work happened on a copy.
    assert "INSERT OR REPLACE" in (EXAMPLES / "legacy_links/links/store.py").read_text(encoding="utf-8"), \
        "source repo is never modified"
    # The reviewable patch removes the buggy line.
    patch = (run_dir / "workspace/review/changes.patch").read_text(encoding="utf-8")
    assert "-                \"INSERT OR REPLACE INTO links" in patch


@pytest.mark.e2e
def test_unknown_domain_escalates_generation(tmp_path):
    """Even with the gates approved, an unknown domain stops at generation, and a report is still written."""
    # Answers file that approves the requirements and plan gates.
    answers = tmp_path / "a.yaml"
    answers.write_text("gates:\n  requirements: {approve: true}\n  plan: {approve: true}\n")
    code, summary, state, events, _ = _run(
        tmp_path, requirement="Build a payroll engine for contractors.", approval="file",
        answers=str(answers))
    assert code == 1 and summary["status"] == "escalated"
    # No domain pack and no LLM: the generator refuses (NeedsHuman) instead of inventing code.
    assert "NeedsHuman" in state["records"]["gen_implementation"].error
    # The report task is always_run, so a summary exists even though the run failed.
    assert state["records"]["report"].status.value in ("succeeded", "failed")  # report always runs


@pytest.mark.e2e
def test_brownfield_without_repo_asks_for_it(tmp_path):
    """A requirement about existing code, but no --repo given, asks for the repo instead of guessing."""
    code, summary, state, events, _ = _run(
        tmp_path, requirement="Fix the bug in our existing URL shortener where an alias is overwritten.")
    assert code == 1 and "no --repo" in state["records"]["need_repo"].error
