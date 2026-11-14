"""TestGenerator: tests derived from acceptance criteria and the API contract.

Tests are generated *contract-first* (in parallel with the code, not from it),
so they check the specification rather than mirroring the implementation.
Every test declares the acceptance criteria it covers ("Covers: AC-...") and
the generator publishes the resulting traceability matrix.
"""
# Store annotations as strings.
from __future__ import annotations

# ast: parse generated test files to find test functions and their docstrings.
import ast
# re: compiled regex for the "Covers:" marker.
import re

# get_pack: domain knowledge pack; render: fill blueprint templates with settings.
from ..knowledge import get_pack, render
# Core data models.
from ..models import FileChange, NormalizedRequirement, TaskSpec
# Agent base class and result container.
from .base import Agent, AgentResult

# Matches "Covers: AC-1, AC-2" in a test docstring; group 1 = comma-separated criterion ids.
_COVERS = re.compile(r"Covers:\s*([A-Z0-9, \-]+)")


# Agent that writes test suites and the acceptance-criteria traceability matrix.
class TestGenerator(Agent):
    # Registry name used by TaskSpec.agent.
    name = "testgen"

    # Entry point: generate test files, write them, and publish traceability.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Analysed requirement.
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Domain pack holding test blueprints / recipe tests.
        pack = get_pack(req.domain)
        # Test generation is only planned for recognised domains.
        assert pack is not None
        # Brownfield: use the tests bundled with each matched change recipe.
        if task.params.get("mode") == "brownfield":
            # Impact analysis tells us which recipes apply.
            impact = ctx.board.require("impact")
            # One FileChange per (recipe, test file) pair.
            changes = [FileChange(f"project/{path}", "create", content, reason=f"tests for {rid}")
                       for rid in impact["recipes"]
                       for path, content in pack.brownfield_recipes[rid].tests.items()]
        # Greenfield: render the test blueprint files listed in the task.
        else:
            changes = [FileChange(f"project/{rel}", "create", render(pack, rel, req.settings),
                                  reason=task.title) for rel in task.params["files"]]
        # Traceability: acceptance criterion id -> ["file::test_name", ...].
        matrix: dict[str, list[str]] = {}
        # Total number of test functions generated.
        n_tests = 0
        # Write each file and index its tests.
        for ch in changes:
            # Attribute the file to this task.
            ch.produced_by = task.id
            # Persist through the guard.
            ctx.guard.apply(ch)
            # For every test function and the criteria it claims to cover...
            for test, acs in covered(ch.content):
                # Count it.
                n_tests += 1
                # Add it under each criterion it covers (path without the project/ prefix).
                for ac in acs:
                    matrix.setdefault(ac, []).append(f"{ch.path.removeprefix('project/')}::{test}")
        # Register test files as artifacts.
        ctx.board.append("artifacts", changes, producer=task.id)
        # Merge this task's matrix into the shared per-task traceability map (atomic update).
        ctx.board.update("traceability_parts", lambda parts: {**(parts or {}), task.id: matrix},
                         producer=task.id)
        # Report counts.
        return AgentResult(notes=[f"{n_tests} test functions across {len(changes)} files; "
                                  f"covers {len(matrix)} acceptance criteria"])


# Parse Python source and return [(test_function_name, [AC ids from its docstring])].
def covered(source: str) -> list[tuple[str, list[str]]]:
    # Result list.
    out = []
    # Visit every node in the module's AST (including methods inside classes).
    for node in ast.walk(ast.parse(source)):
        # Only pytest-style test functions.
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            # Docstring text (empty if none).
            doc = ast.get_docstring(node) or ""
            # Look for the "Covers:" marker.
            m = _COVERS.search(doc)
            # Split the id list on commas and drop blanks; no marker -> no criteria.
            acs = [a.strip() for a in m.group(1).split(",") if a.strip()] if m else []
            # Record the test and its criteria.
            out.append((node.name, acs))
    # All discovered tests.
    return out
