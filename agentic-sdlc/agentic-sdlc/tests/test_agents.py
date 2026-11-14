"""Agent-level behaviour: requirement understanding, codebase reasoning, recipes,
fault localisation."""
import shutil
from pathlib import Path

import pytest

from sdlc_agent.agents.codebase import analyze, impact_analysis
from sdlc_agent.agents.requirements import RequirementAnalyst
from sdlc_agent.agents.validator import artifact_index, attribute
from sdlc_agent.blueprints.url_shortener.brownfield import RECIPES
from sdlc_agent.errors import NeedsHuman
from sdlc_agent.guardrails import Guardrails
from sdlc_agent.models import FileChange, GateDecision, ScenarioKind, Severity, TaskSpec
from sdlc_agent.tools import run_pytest

from .conftest import EXAMPLES


def analyse(ctx, text, repo=None):
    ctx.board.put("requirement_text", text, "user")
    ctx.options["target_repo"] = repo
    agent = RequirementAnalyst()
    task = TaskSpec("understand", "", "requirements", gate="requirements")
    ctx.board.put("requirement", agent.run(ctx, task).outputs["requirement"], "understand")
    return agent, task, ctx.board.get("requirement")


def test_mandatory_requirement_is_understood(ctx):
    _, _, req = analyse(ctx, "Build a scalable URL shortener service with APIs, persistence, and analytics.")
    assert req.kind is ScenarioKind.GREENFIELD and req.domain == "url_shortener"
    stated = [c for c in req.capabilities if c.endswith("(stated)")]
    assert len(stated) == 5
    ids = {a.id for a in req.acceptance_criteria}
    assert {"AC-CREATE", "AC-REDIRECT", "AC-PERSIST", "AC-ANALYTICS", "AC-CONTRACT"} <= ids
    scale = next(a for a in req.ambiguities if a.dimension == "scale")
    assert scale.severity is Severity.MEDIUM and scale.term == "scalable"


def test_ambiguous_requirement_raises_high_severity_domain_question(ctx):
    agent, task, req = analyse(ctx, "We need a link thing for campaigns. Fast, secure, track stuff.")
    domain_q = next(a for a in req.ambiguities if a.term == "domain")
    assert domain_q.severity is Severity.HIGH
    terms = {a.term.lower() for a in req.ambiguities}
    assert {"fast", "secure", "track stuff"} <= terms
    assert agent.gate_request(ctx, task).blocking_reasons, "auto policy must not approve a guess"


def test_unknown_domain_is_a_blocker(ctx):
    agent, task, req = analyse(ctx, "Build a payroll engine for contractors in three countries.")
    assert req.domain == "unknown"
    assert req.open_blockers()
    assert agent.gate_request(ctx, task).blocking_reasons


def test_brownfield_classification_and_criteria(ctx):
    _, _, req = analyse(ctx, (EXAMPLES / "requirements/brownfield_expiry_and_alias_bug.md").read_text(encoding="utf-8"),
                        repo=str(EXAMPLES / "legacy_links"))
    assert req.kind is ScenarioKind.BROWNFIELD
    ids = {a.id for a in req.acceptance_criteria}
    assert {"AC-BUG-ALIAS", "AC-EXP-TTL", "AC-EXP-MIGRATION", "AC-REGRESSION"} <= ids


def test_gate_answers_become_settings_and_bad_answers_are_rejected(ctx):
    agent, task, req = analyse(ctx, "Build a URL shortener with analytics.")
    by_dim = {a.dimension: a.id for a in req.ambiguities if a.dimension}
    agent.on_gate(ctx, task, GateDecision("requirements", True, "human:t", answers={
        by_dim["code_length"]: "8 chars", by_dim["privacy"]: "don't store IPs",
        by_dim["retention"]: "forever"}))
    req = ctx.board.get("requirement")
    assert req.settings["code_length"] == 8 and req.settings["analytics_ip_mode"] == "drop"
    assert req.settings["analytics_retention_days"] == 90, "unparseable answer keeps the default"
    assert any("forever" in r for r in ctx.board.get("rejected_answers"))


def test_codebase_model_recovers_architecture():
    model = analyze(EXAMPLES / "legacy_links")
    assert model["roles"] == {"http": "links/app.py", "model": "links/models.py",
                              "service": "links/service.py", "store": "links/store.py"}
    assert {(r["method"], r["path"]) for r in model["routes"]} == {
        ("POST", "/links"), ("GET", "/links/{code}"), ("GET", "/{code}")}
    assert model["tables"]["links"] == ["code", "target_url", "created_at", "clicks"]
    assert any("increment_clicks [UPDATE]" in f for f in model["flows"])


def test_impact_analysis(ctx):
    _, _, req = analyse(ctx, (EXAMPLES / "requirements/brownfield_expiry_and_alias_bug.md").read_text(encoding="utf-8"),
                        repo=str(EXAMPLES / "legacy_links"))
    impact = impact_analysis(analyze(EXAMPLES / "legacy_links"), req)
    assert impact["recipes"] == ["fix_alias_overwrite", "add_expiration"]
    assert "links/store.py::LinkStore.save" in impact["direct_symbols"]
    assert "links/service.py::LinkService.info" in impact["ripple_symbols"]
    assert impact["schema_change"] and "GET /{code}" in impact["apis"]
    assert impact["files_by_recipe"]["fix_alias_overwrite"] == [
        "links/app.py", "links/service.py", "links/store.py"]


def _copy_legacy(tmp_path) -> Path:
    project = tmp_path / "ws" / "project"
    shutil.copytree(EXAMPLES / "legacy_links", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
    return project


ROLES = {"model": "links/models.py", "store": "links/store.py", "service": "links/service.py",
         "http": "links/app.py"}


@pytest.mark.parametrize("order", [["fix_alias_overwrite", "add_expiration"], ["add_expiration"],
                                   ["fix_alias_overwrite"]])
def test_recipes_compose_and_keep_suite_green(tmp_path, order):
    project = _copy_legacy(tmp_path)
    guard = Guardrails(tmp_path / "ws")
    for rid in order:
        for ch in RECIPES[rid].build(project, ROLES, "project/"):
            guard.apply(ch)
        for path, content in RECIPES[rid].tests.items():
            guard.apply(FileChange(f"project/{path}", "create", content))
    result = run_pytest(project)
    assert result.ok, result.output_tail
    assert result.passed >= 5


def test_recipe_refuses_to_guess_on_unfamiliar_code(tmp_path):
    project = _copy_legacy(tmp_path)
    store = project / "links/store.py"
    store.write_text(store.read_text(encoding="utf-8").replace("def save(", "def persist("))
    with pytest.raises(NeedsHuman, match="save"):
        RECIPES["fix_alias_overwrite"].build(project, ROLES, "project/")


def test_fault_localisation(tmp_path):
    project = tmp_path
    (project / "pkg").mkdir()
    (project / "tests").mkdir()
    (project / "pkg/store.py").write_text("X = 1\n")
    (project / "pkg/api.py").write_text("from .store import X\n")
    (project / "tests/test_api.py").write_text("from pkg.api import X\n")
    index = artifact_index([FileChange("project/pkg/store.py", "create", produced_by="gen_store"),
                            FileChange("project/pkg/api.py", "create", produced_by="gen_api"),
                            FileChange("project/tests/test_api.py", "create", produced_by="gen_tests")])
    depth = {"gen_store": 1, "gen_api": 2}
    tb = 'pkg/store.py:12: in get\n    raise X\nE   sqlite3.OperationalError'
    assert attribute(tb, "tests/test_api.py", project, index, depth, widen=False) == ["gen_store"]
    logged = (f'E assert 500 == 302\nERROR unhandled\nTraceback:\n  File "{project}/pkg/api.py", line 3, '
              f'in h\n  File "{project}/pkg/store.py", line 9, in get\nOperationalError')
    assert attribute(logged, "tests/test_api.py", project, index, depth, widen=False) == ["gen_store"], \
        "exceptions swallowed into a 500 are localised via captured logs"
    assert attribute("assert 200 == 302", "tests/test_api.py", project, index, depth,
                     widen=False) == ["gen_api"], "no source frame: blame the entry point"
    assert attribute("assert 200 == 302", "tests/test_api.py", project, index, depth,
                     widen=True) == ["gen_api", "gen_store"], "second cycle widens to import closure"
