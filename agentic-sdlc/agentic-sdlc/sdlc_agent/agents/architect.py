"""Architect: system design and contract-first interface definitions.

part=architecture -> architecture document (components, flows, scaling plan,
                     production mapping, capacity estimate, trade-offs/ADRs)
part=contract     -> OpenAPI 3.1 contract (JSON + YAML) and SQL data schema,
                     both derived from the *resolved* requirement settings.
                     The contract is self-checked before it is published,
                     because every downstream task consumes it.
"""
# Store annotations as strings (forward references, cheaper imports).
from __future__ import annotations

# json: serialise the OpenAPI spec and scan it for $refs.
import json
# re: regex helpers for $ref, path-parameter and SQL-table extraction.
import re

# PyYAML: emit the YAML flavour of the OpenAPI contract.
import yaml

# AgentError: raised when the generated contract fails its own self-check.
from ..errors import AgentError
# get_pack: look up the domain knowledge pack; render: fill a blueprint template with settings.
from ..knowledge import get_pack, render
# FileChange: a reviewable file write; NormalizedRequirement: analysed requirement; TaskSpec: task node.
from ..models import FileChange, NormalizedRequirement, TaskSpec
# Agent base class and its result container.
from .base import Agent, AgentResult


# Agent responsible for architecture docs and the API/data contract.
class Architect(Agent):
    # Registry name used by TaskSpec.agent.
    name = "architect"

    # Entry point: dispatch on task.params["part"].
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Fetch the normalised requirement from the blackboard (raises if absent).
        req: NormalizedRequirement = ctx.board.require("requirement")
        # "architecture" part -> write the architecture document.
        if task.params["part"] == "architecture":
            return self._architecture(ctx, task, req)
        # Any other part ("contract") -> produce OpenAPI + SQL schema.
        return self._contract(ctx, task, req)

    # ------------------------------------------------------------ architecture
    # Produce project/docs/architecture.md from the domain pack and resolved settings.
    def _architecture(self, ctx, task, req: NormalizedRequirement) -> AgentResult:
        # Load the domain knowledge pack (None if the domain is unknown).
        pack = get_pack(req.domain)
        # Unknown domain: emit a generic layered-architecture outline instead.
        if pack is None:
            # Minimal markdown placeholder that also echoes the raw requirement.
            doc = ("# Architecture outline (domain not recognised)\n\n"
                   "Layered service: HTTP adapter -> application service -> repository port -> "
                   "storage adapter. Concrete components require clarification of the domain.\n\n"
                   f"Requirement: {req.raw}\n")
            # Write the file through the guard (sandboxed, path-checked file writes).
            ctx.guard.apply(FileChange("project/docs/architecture.md", "create", doc, produced_by=task.id))
            # Publish a generic architecture style and no trade-offs.
            return AgentResult(outputs={"architecture": {"style": "layered (generic)"}, "tradeoffs": []})

        # Short aliases: a = architecture knowledge, s = settings resolved at the requirements gate.
        a, s = pack.architecture, req.settings
        # Document header, architecture style and the start of the components table.
        lines = [f"# Architecture: {pack.title}", "", f"**Style:** {a['style']}", "",
                 "## Components", "| Component | Responsibility |", "|---|---|"]
        # One table row per (component, responsibility) pair.
        lines += [f"| {c} | {r} |" for c, r in a["components"]]
        # Mermaid flowchart of the request path (create, redirect, analytics, stats).
        lines += ["", "```mermaid", "flowchart LR",
                  "  C[Client] -->|POST /api/v1/links| H[HTTP adapter]",
                  "  U[Browser] -->|GET /code| H",
                  "  H --> RL[Rate limiter]",
                  "  H --> S[LinkService]",
                  "  S --> CG[Code generator]",
                  "  S --> CA[(TTL cache)]",
                  "  S --> R[(Link repository)]",
                  "  S -->|enqueue, non-blocking| Q[[Click queue]]",
                  "  Q -->|batch flush| AS[(Analytics store)]",
                  "  H -->|GET /stats| S", "```", "", "## Request flows"]
        # One bullet per named request flow.
        lines += [f"- **{k}**: {v}" for k, v in a["flows"].items()]
        # Table header for the settings resolved at the requirements gate.
        lines += ["", "## Resolved design parameters (from requirements gate)", "| Setting | Value |", "|---|---|"]
        # One row per setting, sorted for deterministic output.
        lines += [f"| `{k}` | `{v}` |" for k, v in sorted(s.items())]
        # Table header for the production (AWS) mapping.
        lines += ["", "## Scaling to production (AWS)", "| Concern | Production design |", "|---|---|"]
        # One row per (concern, AWS design) pair.
        lines += [f"| {k} | {v} |" for k, v in a["aws_mapping"]]
        # Capacity-estimate section as bullets.
        lines += ["", "## Capacity estimate"] + [f"- {c}" for c in a["capacity"]]
        # Architecture Decision Record summary heading.
        lines += ["", "## Key decisions (ADR summary)"]
        # One numbered ADR block per trade-off (ADR-001, ADR-002, ...).
        for i, t in enumerate(pack.tradeoffs, 1):
            lines += [f"### ADR-{i:03d} {t.decision}", f"- **Chosen:** {t.chosen}",
                      f"- **Alternatives:** {t.alternatives}", f"- **Rationale:** {t.rationale}", ""]
        # Write the assembled document through the guard.
        ctx.guard.apply(FileChange("project/docs/architecture.md", "create", "\n".join(lines) + "\n",
                                   produced_by=task.id))
        # Publish the architecture and trade-offs for later agents (docgen, risk, reporter).
        return AgentResult(outputs={"architecture": a, "tradeoffs": pack.tradeoffs},
                           notes=[f"{len(a['components'])} components, {len(pack.tradeoffs)} ADRs"])

    # ---------------------------------------------------------------- contract
    # Produce the OpenAPI contract (JSON + YAML) and the SQL schema.
    def _contract(self, ctx, task, req: NormalizedRequirement) -> AgentResult:
        # Load the domain pack; contracts only exist for recognised domains.
        pack = get_pack(req.domain)
        # The planner only schedules this task for known domains, so pack must exist.
        assert pack is not None
        # Build the OpenAPI dict, parameterised by the resolved settings.
        spec = pack.build_contract(req.settings)
        # Run structural self-checks before anything downstream consumes the contract.
        problems = check_openapi(spec)
        # Fail the task loudly if the contract is defective.
        if problems:
            raise AgentError("contract self-check failed: " + "; ".join(problems))
        # Render the SQL schema blueprint with the resolved settings.
        schema_sql = render(pack, "shortener/schema.sql", req.settings)
        # Parse table -> column list from the DDL (used by the validator's schema check).
        tables = parse_tables(schema_sql)
        # The three artifacts to write: OpenAPI JSON, OpenAPI YAML and the SQL schema.
        for change in (
            FileChange("project/openapi.json", "create", json.dumps(spec, indent=2) + "\n"),
            FileChange("project/openapi.yaml", "create", yaml.safe_dump(spec, sort_keys=False)),
            FileChange("project/shortener/schema.sql", "create", schema_sql),
        ):
            # Attribute the file to this task (used for fault localisation).
            change.produced_by = task.id
            # Write it to the workspace through the guard.
            ctx.guard.apply(change)
            # Record it in the blackboard's artifact list.
            ctx.board.append("artifacts", [change], producer=task.id)
        # Count operations: total number of HTTP methods across all paths.
        ops = sum(len(v) for v in spec["paths"].values())
        # Publish the contract and data schema for codegen/testgen/docgen/validator.
        return AgentResult(outputs={"api_contract": spec, "data_schema": tables},
                           notes=[f"{ops} operations, {len(tables)} tables"])


# Lightweight OpenAPI linter: returns a list of problem descriptions (empty = OK).
def check_openapi(spec: dict) -> list[str]:
    """Structural checks that catch the usual contract defects."""
    # Collected problem messages.
    problems: list[str] = []
    # operationIds seen so far (to detect duplicates).
    seen_ids: set[str] = set()
    # Declared component schemas (targets of $ref).
    schemas = spec.get("components", {}).get("schemas", {})
    # Serialise the spec so all $refs can be found with one regex.
    text = json.dumps(spec)
    # Every distinct schema name referenced via "#/components/schemas/<Name>".
    for ref in set(re.findall(r'"#/components/schemas/(\w+)"', text)):
        # A $ref to an undeclared schema is a dangling reference.
        if ref not in schemas:
            problems.append(f"dangling $ref {ref}")
    # Walk every path and its operations.
    for path, item in spec.get("paths", {}).items():
        # Template parameters in the path string, e.g. {code}.
        params_in_path = set(re.findall(r"\{(\w+)\}", path))
        # Each HTTP method on this path.
        for method, op in item.items():
            # operationId must exist and be unique.
            oid = op.get("operationId")
            if not oid or oid in seen_ids:
                problems.append(f"{method.upper()} {path}: missing/duplicate operationId")
            # Remember it for later duplicate checks.
            seen_ids.add(oid)
            # Path parameters actually declared on the operation.
            declared = {p["name"] for p in op.get("parameters", []) if p.get("in") == "path"}
            # The declared set must exactly match the template placeholders.
            if declared != params_in_path:
                problems.append(f"{method.upper()} {path}: path params {params_in_path} != declared {declared}")
            # Every operation must document at least one response.
            if not op.get("responses"):
                problems.append(f"{method.upper()} {path}: no responses")
    # Empty list means the contract passed.
    return problems


# Extract {table_name: [column names]} from "CREATE TABLE IF NOT EXISTS" DDL.
def parse_tables(sql: str) -> dict[str, list[str]]:
    # Result map.
    tables: dict[str, list[str]] = {}
    # Match each table: group 1 = name, group 2 = body up to the closing "\n);" (re.S spans lines).
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        # Column names of this table.
        cols = []
        # Inspect each line of the table body.
        for line in m.group(2).splitlines():
            # Drop SQL "--" comments, whitespace and trailing commas.
            line = line.split("--")[0].strip().rstrip(",")
            # Non-empty lines start with the column name (constraints included as-is).
            if line:
                cols.append(line.split()[0])
        # Store columns under the table name.
        tables[m.group(1)] = cols
    # Map of table -> columns.
    return tables
