"""Agent-level behaviour: requirement understanding, codebase reasoning, recipes,
fault localisation.

These tests call individual agents and helper functions directly (no orchestrator), so a
failure here points at one agent's logic rather than at scheduling or recovery.
"""
# shutil: copy the sample legacy repo into a scratch folder.
import shutil
# Path: filesystem paths.
from pathlib import Path

# pytest: raises/parametrize helpers.
import pytest

# Brownfield code-base model (roles, routes, tables, flows) and change-impact analysis.
from sdlc_agent.agents.codebase import analyze, impact_analysis
# The agent that turns raw requirement text into a structured Requirement.
from sdlc_agent.agents.requirements import RequirementAnalyst
# Validator helpers: which task produced which file, and fault attribution for a failing test.
from sdlc_agent.agents.validator import artifact_index, attribute
# Deterministic brownfield change recipes (bug fix, add expiry) for the legacy repo.
from sdlc_agent.blueprints.url_shortener.brownfield import RECIPES
# Raised when an agent refuses to guess and needs a human decision.
from sdlc_agent.errors import NeedsHuman
# Sandboxed file writer.
from sdlc_agent.guardrails import Guardrails
# Core data models.
from sdlc_agent.models import FileChange, GateDecision, ScenarioKind, Severity, TaskSpec
# pytest runner wrapper used by the validator.
from sdlc_agent.tools import run_pytest

# Path to the examples/ folder (sample requirements and the legacy repo).
from .conftest import EXAMPLES


# Helper: run the RequirementAnalyst on `text` and return (agent, task, structured requirement).
def analyse(ctx, text, repo=None):
    # The analyst reads the raw requirement from the blackboard, as in a real run.
    ctx.board.put("requirement_text", text, "user")
    # A target repo makes the requirement brownfield; None means greenfield.
    ctx.options["target_repo"] = repo
    agent = RequirementAnalyst()
    # Same task shape as the "understand" bootstrap task in cli.py.
    task = TaskSpec("understand", "", "requirements", gate="requirements")
    # Run the agent and publish its result, exactly as the orchestrator would.
    ctx.board.put("requirement", agent.run(ctx, task).outputs["requirement"], "understand")
    return agent, task, ctx.board.get("requirement")


def test_mandatory_requirement_is_understood(ctx):
    """The assignment's one-line requirement is classified and decomposed correctly."""
    _, _, req = analyse(ctx, "Build a scalable URL shortener service with APIs, persistence, and analytics.")
    # New service in a known domain.
    assert req.kind is ScenarioKind.GREENFIELD and req.domain == "url_shortener"
    # Exactly the five capabilities the sentence states (others are marked "(assumed)").
    stated = [c for c in req.capabilities if c.endswith("(stated)")]
    assert len(stated) == 5
    # The core acceptance criteria are generated.
    ids = {a.id for a in req.acceptance_criteria}
    assert {"AC-CREATE", "AC-REDIRECT", "AC-PERSIST", "AC-ANALYTICS", "AC-CONTRACT"} <= ids
    # "scalable" is flagged as unmeasurable: a medium-severity question, not a blocker.
    scale = next(a for a in req.ambiguities if a.dimension == "scale")
    assert scale.severity is Severity.MEDIUM and scale.term == "scalable"


def test_ambiguous_requirement_raises_high_severity_domain_question(ctx):
    """Vague wording produces clarifying questions, and the auto policy may not approve it."""
    agent, task, req = analyse(ctx, "We need a link thing for campaigns. Fast, secure, track stuff.")
    # Unclear what is being built -> HIGH severity question about the domain.
    domain_q = next(a for a in req.ambiguities if a.term == "domain")
    assert domain_q.severity is Severity.HIGH
    # Each vague phrase becomes its own question.
    terms = {a.term.lower() for a in req.ambiguities}
    assert {"fast", "secure", "track stuff"} <= terms
    # The gate request carries blocking reasons, so policy:auto will reject (escalate).
    assert agent.gate_request(ctx, task).blocking_reasons, "auto policy must not approve a guess"


def test_unknown_domain_is_a_blocker(ctx):
    """A requirement outside every known domain pack blocks at the requirements gate."""
    agent, task, req = analyse(ctx, "Build a payroll engine for contractors in three countries.")
    assert req.domain == "unknown"
    # There is at least one unresolved blocking question...
    assert req.open_blockers()
    # ...so the gate request cannot be auto-approved.
    assert agent.gate_request(ctx, task).blocking_reasons


def test_brownfield_classification_and_criteria(ctx):
    """With a target repo, the requirement is a brownfield change with change-specific criteria."""
    _, _, req = analyse(ctx, (EXAMPLES / "requirements/brownfield_expiry_and_alias_bug.md").read_text(encoding="utf-8"),
                        repo=str(EXAMPLES / "legacy_links"))
    assert req.kind is ScenarioKind.BROWNFIELD
    # Criteria for the bug fix, the new feature, the schema migration, and "don't break anything".
    ids = {a.id for a in req.acceptance_criteria}
    assert {"AC-BUG-ALIAS", "AC-EXP-TTL", "AC-EXP-MIGRATION", "AC-REGRESSION"} <= ids


def test_gate_answers_become_settings_and_bad_answers_are_rejected(ctx):
    """Human answers at the requirements gate are parsed into generation settings."""
    agent, task, req = analyse(ctx, "Build a URL shortener with analytics.")
    # Map each question's dimension (code_length, privacy, ...) to its question id.
    by_dim = {a.dimension: a.id for a in req.ambiguities if a.dimension}
    # Simulate a human approving the gate with free-text answers.
    agent.on_gate(ctx, task, GateDecision("requirements", True, "human:t", answers={
        by_dim["code_length"]: "8 chars", by_dim["privacy"]: "don't store IPs",
        by_dim["retention"]: "forever"}))
    req = ctx.board.get("requirement")
    # Parseable answers change the settings: "8 chars" -> 8, "don't store IPs" -> drop mode.
    assert req.settings["code_length"] == 8 and req.settings["analytics_ip_mode"] == "drop"
    # "forever" is not a valid retention, so the safe default (90 days) is kept...
    assert req.settings["analytics_retention_days"] == 90, "unparseable answer keeps the default"
    # ...and the rejection is recorded so it appears in the engineering summary.
    assert any("forever" in r for r in ctx.board.get("rejected_answers"))


def test_codebase_model_recovers_architecture():
    """The codebase analyst reconstructs the legacy repo's structure from source alone."""
    model = analyze(EXAMPLES / "legacy_links")
    # Each architectural role is mapped to the file that plays it.
    assert model["roles"] == {"http": "links/app.py", "model": "links/models.py",
                              "service": "links/service.py", "store": "links/store.py"}
    # All HTTP routes are discovered.
    assert {(r["method"], r["path"]) for r in model["routes"]} == {
        ("POST", "/links"), ("GET", "/links/{code}"), ("GET", "/{code}")}
    # The SQL schema is recovered, column by column.
    assert model["tables"]["links"] == ["code", "target_url", "created_at", "clicks"]
    # Data flows record which functions write to the database.
    assert any("increment_clicks [UPDATE]" in f for f in model["flows"])


def test_impact_analysis(ctx):
    """Impact analysis picks the right recipes and lists affected symbols, APIs and files."""
    _, _, req = analyse(ctx, (EXAMPLES / "requirements/brownfield_expiry_and_alias_bug.md").read_text(encoding="utf-8"),
                        repo=str(EXAMPLES / "legacy_links"))
    impact = impact_analysis(analyze(EXAMPLES / "legacy_links"), req)
    # Bug fix first, then the feature (they touch overlapping code).
    assert impact["recipes"] == ["fix_alias_overwrite", "add_expiration"]
    # Symbols edited directly...
    assert "links/store.py::LinkStore.save" in impact["direct_symbols"]
    # ...and callers affected indirectly (ripple effect).
    assert "links/service.py::LinkService.info" in impact["ripple_symbols"]
    # Expiry needs a schema change and alters the redirect endpoint.
    assert impact["schema_change"] and "GET /{code}" in impact["apis"]
    # Exact file list for the bug-fix recipe.
    assert impact["files_by_recipe"]["fix_alias_overwrite"] == [
        "links/app.py", "links/service.py", "links/store.py"]


# Copy the sample legacy repo to tmp_path/ws/project so recipes can edit it safely.
def _copy_legacy(tmp_path) -> Path:
    project = tmp_path / "ws" / "project"
    shutil.copytree(EXAMPLES / "legacy_links", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
    return project


# Role -> file mapping for the legacy repo (what analyze() would produce), given to the recipes.
ROLES = {"model": "links/models.py", "store": "links/store.py", "service": "links/service.py",
         "http": "links/app.py"}


# Run the recipes in three combinations: both (in the planned order), and each one alone.
@pytest.mark.parametrize("order", [["fix_alias_overwrite", "add_expiration"], ["add_expiration"],
                                   ["fix_alias_overwrite"]])
def test_recipes_compose_and_keep_suite_green(tmp_path, order):
    """Recipes apply cleanly in any valid order and keep the legacy + new tests passing."""
    project = _copy_legacy(tmp_path)
    # All edits go through the guardrails, as in a real run.
    guard = Guardrails(tmp_path / "ws")
    for rid in order:
        # Apply each code edit the recipe produces.
        for ch in RECIPES[rid].build(project, ROLES, "project/"):
            guard.apply(ch)
        # Add the recipe's own regression/feature tests.
        for path, content in RECIPES[rid].tests.items():
            guard.apply(FileChange(f"project/{path}", "create", content))
    # Run the patched repo's whole test suite.
    result = run_pytest(project)
    assert result.ok, result.output_tail
    # At least the 5 original legacy tests still pass.
    assert result.passed >= 5


def test_recipe_refuses_to_guess_on_unfamiliar_code(tmp_path):
    """If the code doesn't look as expected, a recipe asks a human instead of editing blindly."""
    project = _copy_legacy(tmp_path)
    store = project / "links/store.py"
    # Rename the method the bug-fix recipe anchors on.
    store.write_text(store.read_text(encoding="utf-8").replace("def save(", "def persist("))
    # The recipe can't find "save" and escalates rather than guessing where to edit.
    with pytest.raises(NeedsHuman, match="save"):
        RECIPES["fix_alias_overwrite"].build(project, ROLES, "project/")


def test_fault_localisation(tmp_path):
    """A failing test is blamed on the generator task that produced the faulty code."""
    # Build a tiny project: pkg/api.py imports pkg/store.py; the test imports pkg/api.py.
    project = tmp_path
    (project / "pkg").mkdir()
    (project / "tests").mkdir()
    (project / "pkg/store.py").write_text("X = 1\n")
    (project / "pkg/api.py").write_text("from .store import X\n")
    (project / "tests/test_api.py").write_text("from pkg.api import X\n")
    # Record which generator task produced each file.
    index = artifact_index([FileChange("project/pkg/store.py", "create", produced_by="gen_store"),
                            FileChange("project/pkg/api.py", "create", produced_by="gen_api"),
                            FileChange("project/tests/test_api.py", "create", produced_by="gen_tests")])
    # Depth in the task graph (deeper = closer to the entry point).
    depth = {"gen_store": 1, "gen_api": 2}

    # Case 1: pytest's short traceback names a source frame -> blame that file's producer.
    tb = 'pkg/store.py:12: in get\n    raise X\nE   sqlite3.OperationalError'
    assert attribute(tb, "tests/test_api.py", project, index, depth, widen=False) == ["gen_store"]

    # Case 2: the app turned the exception into an HTTP 500, but the captured log still
    # holds the full traceback; the innermost generated frame (store.py) is blamed.
    logged = (f'E assert 500 == 302\nERROR unhandled\nTraceback:\n  File "{project}/pkg/api.py", line 3, '
              f'in h\n  File "{project}/pkg/store.py", line 9, in get\nOperationalError')
    assert attribute(logged, "tests/test_api.py", project, index, depth, widen=False) == ["gen_store"], \
        "exceptions swallowed into a 500 are localised via captured logs"

    # Case 3: no source frame at all -> blame the module the test imports directly (the entry point).
    assert attribute("assert 200 == 302", "tests/test_api.py", project, index, depth,
                     widen=False) == ["gen_api"], "no source frame: blame the entry point"

    # Case 4: second repair cycle (widen=True) -> blame everything the test transitively imports.
    assert attribute("assert 200 == 302", "tests/test_api.py", project, index, depth,
                     widen=True) == ["gen_api", "gen_store"], "second cycle widens to import closure"
