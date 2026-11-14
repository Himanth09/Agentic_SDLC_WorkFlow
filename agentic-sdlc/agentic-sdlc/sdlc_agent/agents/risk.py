"""RiskAnalyst: risks, trade-offs, failure scenarios and the validation strategy.

Combines static domain knowledge with evidence from *this* run: assumptions
that were defaulted rather than answered, validation findings that remain,
degraded/failed tasks, validation tools that were unavailable, and how many
repair cycles generation needed.
"""
# Store annotations as strings.
from __future__ import annotations

# get_pack: domain knowledge pack (static risks and failure scenarios).
from ..knowledge import get_pack
# Risk record, scenario kind enum, severity enum and task/requirement models.
from ..models import NormalizedRequirement, Risk, ScenarioKind, Severity, TaskSpec
# Agent base class and result container.
from .base import Agent, AgentResult


# Agent that builds the risk register and validation strategy.
class RiskAnalyst(Agent):
    # Registry name used by TaskSpec.agent.
    name = "risk"

    # Entry point: gather static + run-specific risks.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Analysed requirement.
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Domain pack (may be None for unknown domains).
        pack = get_pack(req.domain)
        # Accumulated risk register.
        risks: list[Risk] = []
        # Greenfield in a known domain: start from the pack's curated risks.
        if pack and req.kind is ScenarioKind.GREENFIELD:
            risks += pack.risks
        # Brownfield: risks specific to changing an existing system.
        if req.kind is ScenarioKind.BROWNFIELD:
            risks += [
                # Behaviour change: alias overwrite now returns 409 Conflict.
                Risk("R-BEHAVIOUR", "Clients relying on alias overwrite now receive 409", "medium", "medium",
                     "Called out in change notes/CHANGELOG; communicate before rollout", "compatibility"),
                # Data risk: start-up schema migration on a big/locked database.
                Risk("R-MIGRATION", "Schema migration on start-up against a large/locked database", "low",
                     "medium", "ALTER TABLE ADD COLUMN is metadata-only in SQLite; idempotent; tested "
                     "against an old-schema database", "data"),
                # Quality risk: weak legacy test coverage.
                Risk("R-COVERAGE", "Legacy suite is small; untested legacy paths may hide regressions",
                     "medium", "medium", "Baseline comparison + new tests; recommend characterization "
                     "tests for redirect/info paths", "quality"),
            ]
        # Counter for unconfirmed-assumption risks.
        n = 0
        # Each ambiguity that a human never answered and is at least MEDIUM severity becomes a risk.
        for amb in req.ambiguities:
            if not amb.answer and amb.severity.rank >= Severity.MEDIUM.rank:
                # Number the assumption risks sequentially.
                n += 1
                # Record that the default was used and must be confirmed.
                risks.append(Risk(f"R-ASSUME-{n}", f"Assumption not confirmed by a human: {amb.term}",
                                  "medium", "medium", f"Defaulted to: {amb.resolution}. Confirm before "
                                  "production; the value is configurable.", "requirements"))
        # Validation report (may be missing if validation never ran).
        validation = ctx.board.get("validation") or {}
        # Each remaining MEDIUM+ finding becomes a quality risk.
        for f in validation.get("findings", []):
            if f.severity.rank >= Severity.MEDIUM.rank:
                # Impact is "high" for HIGH+ findings, otherwise "medium"; message truncated to 90 chars.
                risks.append(Risk(f"R-FINDING-{len(risks)}", f"Unresolved {f.source} finding: {f.message[:90]}",
                                  "medium", "high" if f.severity.rank >= Severity.HIGH.rank else "medium",
                                  "Must be fixed or explicitly accepted at the release gate", "quality"))
        # Each validation tool (ruff, mypy...) that wasn't installed is a process gap.
        for name, tool in (validation.get("tools") or {}).items():
            if not tool.get("available"):
                risks.append(Risk(f"R-TOOL-{name}", f"Validation gap: {name} not installed", "low", "low",
                                  f"Install {name} in CI; its checks did not run", "process"))
        # Final status of every task so far.
        records = ctx.board.get("task_status", {})
        # Any failed/degraded/skipped task (other than this one and the report) is a delivery risk.
        for tid, status in records.items():
            if status in ("failed", "degraded", "skipped") and tid not in ("risk_review", "report"):
                risks.append(Risk(f"R-TASK-{tid}", f"Task {tid} ended {status}", "high", "high",
                                  "See execution trace; output incomplete for this step", "delivery"))
        # Validation strategy appropriate to the scenario.
        strategy = self._strategy(req, pack)
        # Designed failure scenarios from the pack (greenfield only).
        failure = pack.architecture.get("failure_scenarios", []) if pack and \
            req.kind is ScenarioKind.GREENFIELD else []
        # Publish everything for the Reporter.
        return AgentResult(outputs={"risks": risks, "validation_strategy": strategy,
                                    "failure_scenarios": failure},
                           notes=[f"{len(risks)} risks ({n} from unconfirmed assumptions)"])

    # Return {stage name: [validation activities]} for the scenario.
    @staticmethod
    def _strategy(req: NormalizedRequirement, pack) -> dict[str, list[str]]:
        # Brownfield strategy centres on regression safety.
        if req.kind is ScenarioKind.BROWNFIELD:
            return {
                # Checks executed by this pipeline run.
                "Automated (this run)": [
                    "Baseline: run the existing suite on an untouched copy before any edit",
                    "Regression: every baseline-passing test must still pass after the change",
                    "New tests per acceptance criterion (feature, bug regression, migration)",
                    "Change-scope check against the approved impact analysis; static safety + lint on "
                    "changed files", "Reviewable unified diff (review/changes.patch)"],
                # Manual steps before merging.
                "Before merge": ["Human code review of the diff and change notes",
                                 "Run against a copy of the production database (migration rehearsal)"],
                # Post-deploy monitoring and rollback.
                "After deploy": ["Watch 409/410 rates and 5xx on POST /links and GET /{code}",
                                 "Rollback plan: previous build ignores the new column"],
            }
        # Greenfield strategy: full static -> test -> pre-prod -> prod ladder.
        return {
            # Checks executed by this pipeline run.
            "Automated (this run)": [
                "Static: syntax, safety scan (eval/shell/SQL injection/secrets), ruff, mypy",
                "Cross-artifact: SQL vs data schema, routes vs OpenAPI contract, config vs approved "
                "requirement settings",
                "Unit tests: domain validation, code generation, cache, rate limiter, service rules, "
                "analytics pipeline",
                "Integration tests: HTTP API over real SQLite, contract conformance, durability across "
                "restart, concurrent writers (unique codes, alias race)",
                "Traceability: every acceptance criterion covered by at least one passing test"],
            # Load, soak, chaos and security testing before production.
            "Pre-production": [
                "Load test (k6/Locust) at 2x target peak: redirect p99 < 50 ms, 0 errors, cache hit > 90%",
                "Soak test 24h: memory flat, analytics backlog bounded",
                "Chaos: stop analytics store (redirects unaffected), throttle link store (graceful 5xx)",
                "Security: DAST on API, abuse simulation against rate limits"],
            # Progressive rollout and ongoing monitoring.
            "Production": [
                "Canary 5% with automated rollback on p99/5xx SLO burn",
                "Synthetic probes: create -> redirect -> stats every minute per region",
                "Dashboards/alerts from RUNBOOK.md"],
        }
