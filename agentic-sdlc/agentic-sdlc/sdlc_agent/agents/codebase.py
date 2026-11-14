"""CodebaseAnalyst: system-level understanding of an existing repository.

Builds a model of the code base purely from static analysis (ast), then
runs the existing test suite to establish a behavioural baseline:

  modules      imports (internal dependency graph), classes, dataclass fields,
               functions/methods and the calls they make
  APIs         HTTP routes from route tables, decorators and if/path dispatch
  data         SQL tables/columns from DDL strings, SQL statements per function
  roles        architectural role of each module (model/store/service/http)
  data flows   route -> handler -> service -> store -> table
  tests        test functions and the production symbols they import

Impact analysis then maps requirement concepts onto that model: directly
impacted symbols, their transitive callers (ripple), affected APIs and data,
and the existing tests that form the regression scope.
"""
# Store annotations as strings.
from __future__ import annotations

# ast: static analysis of Python sources.
import ast
# re: SQL / route pattern matching.
import re
# shutil: copy the target repo into the sandbox and remove old copies.
import shutil
# defaultdict: convenient multi-maps (caller graph, direct symbols).
from collections import defaultdict
# Path: filesystem paths.
from pathlib import Path
# Any: loosely-typed model dictionaries.
from typing import Any

# AgentError: unparsable source; NeedsHuman: escalation when analysis can't proceed.
from ..errors import AgentError, NeedsHuman
# get_pack: domain pack with brownfield recipes.
from ..knowledge import get_pack
# Core data models.
from ..models import FileChange, NormalizedRequirement, TaskSpec
# run_pytest: execute the test suite and parse results.
from ..tools import run_pytest
# Agent base class and result container.
from .base import Agent, AgentResult

# Directories that are tooling/cache noise and must not be copied or diffed.
CACHE_DIRS = (".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache",
              "node_modules", ".tox")
# copytree ignore callback: cache dirs plus database files and compiled bytecode.
IGNORE = shutil.ignore_patterns(*CACHE_DIRS, "*.db", "*.pyc")
# Matches CREATE TABLE DDL; group 1 = table name, group 2 = column definitions.
_SQL_TABLE = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\)\s*$", re.S | re.I | re.M)
# Matches a string that starts with a DML statement (SELECT/INSERT/UPDATE/DELETE).
_SQL_STMT = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\b.*?\b(FROM|INTO|UPDATE)?\s*(\w+)?", re.I)
# HTTP verbs recognised when detecting routes.
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


# Agent that maps an existing repository and computes change impact.
class CodebaseAnalyst(Agent):
    # Registry name used by TaskSpec.agent.
    name = "codebase"

    # Entry point: copy repo, analyse, baseline-test, impact-analyse, write report.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Analysed requirement (holds target_repo).
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Absolute path of the source repository.
        src = Path(req.target_repo or "").resolve()
        # Missing repo -> human must provide one.
        if not src.is_dir():
            raise NeedsHuman(f"target repository '{req.target_repo}' not found")
        # Sandboxed working copy location.
        project = ctx.guard.safe_path("project")
        # Remove any stale copy from a previous run.
        if project.exists():
            shutil.rmtree(project)
        # Copy the repo, skipping caches; all edits happen on this copy.
        shutil.copytree(src, project, ignore=IGNORE)    # never modify the source repo

        # Build the static model of the code base.
        model = analyze(project)
        # Run the existing tests to capture the pre-change baseline.
        baseline = run_pytest(project, ctx.guard.limits.tool_timeout_s)
        # Warn (guardrail event) when there are no tests to detect regressions.
        if baseline.total == 0:
            ctx.tracer.emit("guardrail", blocked="no baseline tests: regression safety is reduced")
        # Map requirement concepts onto the model.
        impact = impact_analysis(model, req)
        # Human-readable markdown report.
        report = render_impact_report(model, impact, baseline, src)
        # Write it under review/ for the change-plan gate.
        ctx.guard.apply(FileChange("review/impact_analysis.md", "create", report,
                                   produced_by=task.id))
        # Publish model, impact, baseline and source path for planner/codegen/validator.
        return AgentResult(
            outputs={"codebase_model": model, "impact": impact, "baseline": baseline,
                     "source_repo": str(src)},
            notes=[f"{len(model['modules'])} modules, {len(model['routes'])} routes, "
                   f"{len(model['tables'])} tables; baseline {baseline.passed}/{baseline.total} passing",
                   f"impacted files: {', '.join(impact['files'])}"])


# --------------------------------------------------------------------------- #
# Static model
# --------------------------------------------------------------------------- #
# Build the full static model of every .py file under `project`.
def analyze(project: Path) -> dict[str, Any]:
    # relative path -> per-module info.
    modules: dict[str, dict[str, Any]] = {}
    # Every Python file, in deterministic order.
    for path in sorted(project.rglob("*.py")):
        # POSIX-style relative path used as the module key.
        rel = path.relative_to(project).as_posix()
        try:
            # Parse into an AST.
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            # Can't model code that doesn't parse.
            raise AgentError(f"cannot parse {rel}: {exc}") from exc
        # Extract imports, classes, functions, routes, SQL, etc.
        modules[rel] = _module_info(rel, tree, path.read_text(encoding="utf-8"))

    # dotted module name (e.g. "pkg.mod") -> relative file path; packages map via __init__.
    dotted = {rel[:-3].replace("/", ".").removesuffix(".__init__"): rel for rel in modules}
    # Resolve each module's raw imports to internal files.
    for rel, info in modules.items():
        # Dotted name of the module's package (for relative imports).
        pkg = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
        # Internal files this module imports.
        resolved = set()
        for imp in info["raw_imports"]:
            # Relative import ".x" -> "pkg.x" (approximation: all levels resolve against pkg).
            name = imp if not imp.startswith(".") else f"{pkg}.{imp.lstrip('.')}".strip(".")
            # Try the full name, then its parent (for "from pkg.mod import name").
            for cand in (name, name.rsplit(".", 1)[0]):
                # Internal and not a self-import.
                if cand in dotted and dotted[cand] != rel:
                    resolved.add(dotted[cand])
                    break
        # Store the internal dependency list.
        info["imports"] = sorted(resolved)

    # Flatten routes, tagging each with its module.
    routes = [r | {"module": rel} for rel, info in modules.items() for r in info["routes"]]
    # Merge every module's discovered tables.
    tables = {t: cols for info in modules.values() for t, cols in info["tables"].items()}
    # Assign architectural roles (http/store/model/service).
    roles = _roles(modules)
    # Callee "rel::qualname" -> set of callers.
    callers = defaultdict(set)
    # All function definitions: "rel::qualname" -> qualname.
    defs = {f"{rel}::{fn}": fn for rel, info in modules.items() for fn in info["functions"]}
    # Build the approximate call graph.
    for rel, info in modules.items():
        for fn, meta in info["functions"].items():
            visible = {rel, *info["imports"]}   # name-based calls resolved only to visible modules
            for called in meta["calls"]:
                # Match the called short name against every definition in a visible module.
                for qual, short in defs.items():
                    # Same short name, visible module, and not the function itself (no self-edges).
                    if (short.split(".")[-1] == called and qual.split("::")[0] in visible
                            and not qual.startswith(f"{rel}::{fn}")):
                        callers[qual].add(f"{rel}::{fn}")
    # Subset of modules that are test files.
    tests = {rel: info for rel, info in modules.items() if _is_test(rel)}
    # The complete model.
    return {"modules": modules, "routes": routes, "tables": tables, "roles": roles,
            "callers": {k: sorted(v) for k, v in callers.items()},
            "tests": {rel: sorted(info["functions"]) for rel, info in tests.items()},
            "flows": _flows(modules, routes, roles)}


# True if the path is a test module (under tests/ or named test_*.py).
def _is_test(rel: str) -> bool:
    return rel.startswith("tests/") or Path(rel).name.startswith("test_")


# Extract per-module facts from an AST.
def _module_info(rel: str, tree: ast.Module, text: str) -> dict[str, Any]:
    # Initial info record; "wsgi" is a cheap text heuristic for WSGI HTTP adapters.
    info: dict[str, Any] = {"doc": ast.get_docstring(tree) or "", "raw_imports": [], "classes": {},
                            "functions": {}, "routes": [], "tables": {}, "sql": [],
                            "uses_sqlite": False, "wsgi": "start_response" in text}
    # Single walk over every node for imports, classes and SQL DDL strings.
    for node in ast.walk(tree):
        # "import a, b"
        if isinstance(node, ast.Import):
            info["raw_imports"] += [a.name for a in node.names]
            # Flag modules that use sqlite3 (likely the storage layer).
            info["uses_sqlite"] |= any(a.name == "sqlite3" for a in node.names)
        # "from .x import y" -> record as ".x" (dots encode the relative level).
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            info["raw_imports"].append(base)
        # Classes: bases, decorators, annotated fields, methods, line number.
        elif isinstance(node, ast.ClassDef):
            info["classes"][node.name] = {
                "bases": [ast.unparse(b) for b in node.bases],
                "decorators": [ast.unparse(d) for d in node.decorator_list],
                "fields": [ast.unparse(s.target) for s in node.body if isinstance(s, ast.AnnAssign)],
                "methods": [s.name for s in node.body if isinstance(s, ast.FunctionDef)],
                "line": node.lineno}
        # String constants may contain CREATE TABLE DDL.
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in _SQL_TABLE.finditer(node.value):
                # First token of each comma-separated definition = column name.
                cols = [c.strip().split()[0] for c in m.group(2).split(",") if c.strip()]
                info["tables"][m.group(1)] = cols
    # Functions and methods (with their owning class).
    for fn, owner in _functions(tree):
        # Qualified name "Class.method" or plain "func".
        qual = f"{owner}.{fn.name}" if owner else fn.name
        # Distinct names of everything called inside the function.
        calls = sorted({_callee(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)} - {""})
        # SQL statements used by the function (first 80 chars each).
        sql = [n.value.strip()[:80] for n in ast.walk(fn) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and _SQL_STMT.match(n.value)]
        # Store function metadata (excluding "self" from args).
        info["functions"][qual] = {"line": fn.lineno, "end": fn.end_lineno,
                                   "args": [a.arg for a in fn.args.args if a.arg != "self"],
                                   "calls": calls, "sql": sql}
        # Module-level SQL list.
        info["sql"] += sql
    # HTTP routes defined in this module.
    info["routes"] = _routes(tree)
    return info


# Yield (FunctionDef, owning class name or "") for every function, including nested ones.
def _functions(tree: ast.Module):
    # Recursive generator that tracks the current owning class.
    def walk(node: ast.AST, owner: str):
        for child in ast.iter_child_nodes(node):
            # A function: yield it, then look inside (nested functions keep the same owner).
            if isinstance(child, ast.FunctionDef):
                yield child, owner
                yield from walk(child, owner)
            # A class: its methods are owned by this class.
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, child.name)
            # Anything else: descend with the current owner.
            else:
                yield from walk(child, owner)
    # Start at module level with no owner.
    yield from walk(tree, "")


# Short name of a call target: foo() -> "foo", obj.bar() -> "bar", else "".
def _callee(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# Detect HTTP routes using three common styles.
def _routes(tree: ast.Module) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for node in ast.walk(tree):
        # route tables: {("POST", "/links"): create}
        if isinstance(node, ast.Dict):
            # strict=True: keys and values lists must be the same length.
            for k, v in zip(node.keys, node.values, strict=True):
                # Only keys that are 2-tuples (method, path).
                if not (isinstance(k, ast.Tuple) and len(k.elts) == 2):
                    continue
                m_node, p_node = k.elts
                # Both must be literals and the method a known HTTP verb.
                if isinstance(m_node, ast.Constant) and isinstance(p_node, ast.Constant) \
                        and m_node.value in _HTTP_METHODS:
                    routes.append({"method": str(m_node.value), "path": str(p_node.value),
                                   "handler": _callee(v) or ast.unparse(v), "source": "route table"})
        # decorators: @app.get("/x")
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                # A call like app.<verb>("<literal path>").
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and \
                        d.func.attr.upper() in _HTTP_METHODS and d.args and isinstance(d.args[0], ast.Constant):
                    routes.append({"method": d.func.attr.upper(), "path": str(d.args[0].value),
                                   "handler": node.name, "source": "decorator"})
        # if method == "GET" and path.startswith("/links/"): info(...)
        if isinstance(node, ast.If):
            # Source text of the condition (ast.unparse emits single quotes).
            src = ast.unparse(node.test)
            # HTTP method compared in the condition.
            method = re.search(r"method == '(\w+)'", src)
            # Path prefix tested with startswith().
            prefix = re.search(r"path\.startswith\('([^']+)'\)", src)
            # Accept a startswith() prefix or a path.count(...) style check.
            if method and (prefix or "path.count" in src):
                # Handler = first call in the first statement of the if-body.
                call = next((c for c in ast.walk(node.body[0]) if isinstance(c, ast.Call)), None)
                # Path template: prefix + {code}, or the root-level /{code}.
                path = f"{prefix.group(1)}{{code}}" if prefix else "/{code}"
                routes.append({"method": method.group(1), "path": path,
                               "handler": _callee(call.func) if call else "?", "source": "dispatch"})
    return routes


# Heuristically assign one module per role: http, store, model, service.
def _roles(modules: dict[str, dict[str, Any]]) -> dict[str, str]:
    # Production modules only (no tests, no package __init__).
    prod = {rel: m for rel, m in modules.items() if not _is_test(rel) and not rel.endswith("__init__.py")}
    roles: dict[str, str] = {}
    # First pass: http, store and model (setdefault keeps the first match).
    for rel, m in prod.items():
        # Routes or WSGI -> HTTP adapter.
        if m["routes"] or m["wsgi"]:
            roles.setdefault("http", rel)
        # sqlite3 or DDL -> storage layer.
        elif m["uses_sqlite"] or m["tables"]:
            roles.setdefault("store", rel)
        # A dataclass with url/code-ish fields -> domain model.
        elif any("dataclass" in " ".join(c["decorators"]) and
                 any(re.search(r"url|code", f) for f in c["fields"]) for c in m["classes"].values()):
            roles.setdefault("model", rel)
    # Second pass: an unassigned module importing the store -> service layer.
    for rel, m in prod.items():
        if rel not in roles.values() and roles.get("store") in m["imports"]:
            roles.setdefault("service", rel)
    return roles


# Describe each route as "route -> handler -> service fn -> store fn [SQL verb]".
def _flows(modules, routes, roles) -> list[str]:
    flows = []
    # Service and store module paths (may be None).
    svc, store = roles.get("service"), roles.get("store")
    for r in routes:
        # Functions defined in the route's module.
        http_fns = modules[r["module"]]["functions"]
        # Qualified name of the handler function, if found.
        handler = next((q for q in http_fns if q.split(".")[-1] == r["handler"]), None)
        # Start with the route and the handler.
        steps = [f"{r['method']} {r['path']}", f"{r['module']}::{r['handler']}"]
        if handler and svc:
            # Calls from the handler that land on service functions.
            svc_calls = [c for c in http_fns[handler]["calls"]
                         if any(q.split(".")[-1] == c for q in modules[svc]["functions"])]
            for c in svc_calls:
                steps.append(f"{svc}::{c}")
                # Qualified service function name (guaranteed to exist by the filter above).
                sfn = next(q for q in modules[svc]["functions"] if q.split(".")[-1] == c)
                if store:
                    # Follow the service function's calls into the store.
                    for sc in modules[svc]["functions"][sfn]["calls"]:
                        sq = next((q for q in modules[store]["functions"] if q.split(".")[-1] == sc), None)
                        if sq:
                            # Annotate with the SQL verb of its first statement, if any.
                            sql = modules[store]["functions"][sq]["sql"]
                            steps.append(f"{store}::{sc}" + (f" [{sql[0].split()[0]}]" if sql else ""))
        # Join the chain with arrows.
        flows.append(" -> ".join(steps))
    return flows


# --------------------------------------------------------------------------- #
# Impact analysis
# --------------------------------------------------------------------------- #
# Map the requirement onto the code model: recipes, files, symbols, APIs, tests.
def impact_analysis(model: dict[str, Any], req: NormalizedRequirement) -> dict[str, Any]:
    pack = get_pack(req.domain)
    # Need a domain pack for its change recipes.
    if pack is None:
        raise NeedsHuman("brownfield change in an unrecognised domain requires human planning")
    low = req.raw.lower()
    # Recipes whose patterns appear in the requirement.
    recipes = [r for r in pack.brownfield_recipes.values()
               if any(re.search(p, low) for p in r.patterns)]
    if not recipes:
        raise NeedsHuman("no known change pattern matches this requirement")
    roles = model["roles"]
    # Roles the recipes need that the analysis could not find.
    missing = sorted({role for r in recipes for role in r.roles if role not in roles})
    if missing:
        raise NeedsHuman(f"could not locate module role(s) {missing} in the code base")

    # Directly impacted symbol -> ids of recipes touching it.
    direct: dict[str, list[str]] = defaultdict(list)
    for r in recipes:
        for role in r.roles:
            # Module playing this role.
            rel = roles[role]
            # Concepts for this role ("role:name") plus role-agnostic ones (no colon).
            wanted = {c.split(":", 1)[1] for c in r.concepts if c.startswith(f"{role}:")} | \
                     {c for c in r.concepts if ":" not in c}
            # Functions whose short name is a wanted concept.
            for fn in model["modules"][rel]["functions"]:
                if fn.split(".")[-1] in wanted:
                    direct[f"{rel}::{fn}"].append(r.id)
            # For the model role, every class is impacted.
            if role == "model":
                for cls in model["modules"][rel]["classes"]:
                    direct[f"{rel}::{cls}"].append(r.id)
    # Transitive callers of directly impacted symbols (BFS/DFS over the caller graph).
    ripple = set()
    frontier = list(direct)
    while frontier:
        sym = frontier.pop()
        for caller in model["callers"].get(sym, []):
            if caller not in direct and caller not in ripple:
                ripple.add(caller)
                frontier.append(caller)
    # Files to change = modules for every role any recipe needs.
    files = sorted({roles[role] for r in recipes for role in r.roles})
    impacted_files = set(files)
    # APIs whose handler is impacted or whose module will change.
    api = [f"{rt['method']} {rt['path']}" for rt in model["routes"]
           if any(s.startswith(f"{rt['module']}::") and s.split("::")[1].split(".")[-1] == rt["handler"]
                  for s in [*direct, *ripple]) or rt["module"] in impacted_files]
    # Ripple symbols in test files = existing tests exercising impacted code.
    impacted_tests = sorted(s for s in ripple if _is_test(s.split("::")[0]))
    # Keep only production symbols in the ripple set.
    ripple = {s for s in ripple if not _is_test(s.split("::")[0])}
    # Whole existing test suite is the regression scope.
    regression = sorted(model["tests"])
    # Copy of the data model.
    data = {t: cols for t, cols in model["tables"].items()}
    return {
        "recipes": [r.id for r in recipes],
        "roles": roles,
        "files": files,
        "files_by_recipe": {r.id: sorted({roles[x] for x in r.roles}) for r in recipes},
        "direct_symbols": {k: sorted(set(v)) for k, v in sorted(direct.items())},
        "ripple_symbols": sorted(ripple),
        "apis": sorted(set(api)),
        "data": data,
        "regression_tests": regression,
        "impacted_tests": impacted_tests,
        # Only the expiration recipe changes the schema.
        "schema_change": any(r.id == "add_expiration" for r in recipes),
    }


# Render review/impact_analysis.md for human reviewers.
def render_impact_report(model, impact, baseline, src: Path) -> str:
    # Title and role table header.
    lines = [f"# Impact analysis\n\nSource repository: `{src.name}` (analysed on a copy).\n",
             "## Architecture (recovered from code)\n", "| Role | Module |", "|---|---|"]
    lines += [f"| {role} | `{rel}` |" for role, rel in sorted(model["roles"].items())]
    # Mermaid dependency graph of production modules.
    lines += ["", "```mermaid", "graph LR"]
    for rel, info in model["modules"].items():
        if _is_test(rel):
            continue
        for dep in info["imports"]:
            lines.append(f"  {_nid(rel)}[{rel}] --> {_nid(dep)}[{dep}]")
    # HTTP route table.
    lines += ["```", "", "## HTTP API surface", "| Method | Path | Handler | Detected via |", "|---|---|---|---|"]
    lines += [f"| {r['method']} | `{r['path']}` | `{r['module']}::{r['handler']}` | {r['source']} |"
              for r in model["routes"]]
    # Tables and columns.
    lines += ["", "## Data model"]
    lines += [f"- table `{t}`: {', '.join(cols)}" for t, cols in model["tables"].items()]
    # Data flows.
    lines += ["", "## Data flows"] + [f"- {f}" for f in model["flows"]]
    # Change impact: matched recipes and direct symbols.
    lines += ["", "## Change impact", f"Matched change patterns: {', '.join(impact['recipes'])}", "",
              "Directly impacted symbols:"]
    lines += [f"- `{s}` ({', '.join(r)})" for s, r in impact["direct_symbols"].items()]
    # Ripple symbols (or "none").
    lines += ["", "Ripple (transitive callers):"]
    lines += [f"- `{s}`" for s in impact["ripple_symbols"]] or ["- none"]
    # APIs, schema change, and regression baseline.
    lines += ["", "Impacted APIs: " + ", ".join(f"`{a}`" for a in impact["apis"]),
              "Schema change: " + ("yes - `expires_at` column, online migration required"
                                   if impact["schema_change"] else "no"),
              "", "## Regression baseline",
              f"{baseline.passed}/{baseline.total} existing tests passing before any change "
              f"({len(impact['regression_tests'])} test module(s) form the regression scope).",
              "", "Existing tests exercising impacted code (must stay green):"]
    lines += [f"- `{t}`" for t in impact["impacted_tests"]] or ["- none found"]
    return "\n".join(lines) + "\n"


# Make a Mermaid-safe node id by replacing non-word characters with underscores.
def _nid(rel: str) -> str:
    return re.sub(r"\W", "_", rel)
