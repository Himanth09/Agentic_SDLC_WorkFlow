"""RequirementAnalyst: interpret intent, surface ambiguity, normalise.

Steps
  1. classify the scenario (greenfield / brownfield / tests+docs)
  2. recognise the domain by scoring every domain pack (confidence-aware)
  3. extract capabilities that are *stated* vs *assumed by default*
  4. detect ambiguity:
       - vague wording (fast, secure, stuff...) -> mapped to concrete dimensions
       - unspecified NFR dimensions (scale, latency, privacy...) -> question + default
       - low-confidence domain -> high-severity confirmation question
       - unknown domain -> blocker unless a human supplies direction
  5. derive acceptance criteria and resolved settings (knobs fed to codegen)
  6. optional LLM pass adds ambiguities it finds (schema-validated, merged)
The human gate then answers questions; answers are parsed into settings and a
bad answer is rejected back to the default (and recorded), never silently used.
"""
# Store annotations as strings.
from __future__ import annotations

# re: keyword/pattern matching over the requirement text.
import re
# Any: type for untyped LLM JSON payloads.
from typing import Any

# GateRequest: what the human sees at the requirements gate.
from ..approval import GateRequest
# Domain knowledge: generic vague-term questions, vague-term patterns, pack type and lookups.
from ..knowledge import GENERIC_QUESTIONS, VAGUE_TERMS, DomainPack, get_pack, packs
# structured: LLM call with schema validation.
from ..llm import structured
# Data models for the normalised requirement and its parts.
from ..models import (AcceptanceCriterion, Ambiguity, GateDecision, NormalizedRequirement,
                      ScenarioKind, Severity, TaskSpec)
# Agent base class and result container.
from .base import Agent, AgentResult

# Words that indicate work on an existing system (brownfield).
_BROWNFIELD = r"\b(existing|legacy|current(ly)?|fix|bug|refactor|enhance|extend|add .* to|migrat)"
# Phrases that indicate a tests/docs-only request.
_TEST_DOCS = r"\b(add|improve|write|increase)\b.*\b(tests?|coverage|documentation|docs)\b"


# Agent that turns free text into a NormalizedRequirement.
class RequirementAnalyst(Agent):
    # Registry name used by TaskSpec.agent.
    name = "requirements"

    # Entry point: run the analysis steps and publish the requirement.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # Raw requirement text supplied by the user.
        raw: str = ctx.board.require("requirement_text").strip()
        # Optional --repo path for brownfield work.
        repo = ctx.options.get("target_repo")
        # Lower-cased copy for case-insensitive matching.
        low = raw.lower()

        # 1. Scenario kind.
        kind = self._classify(low, repo)
        # 2. Best-matching domain pack and confidence score.
        pack, confidence = self._domain(raw)
        # 3. Capabilities (stated vs assumed) and acceptance criteria.
        stated, assumed, criteria = self._capabilities(low, pack, kind)
        # 4. Rule-based ambiguities.
        ambiguities = self._ambiguities(raw, low, pack, confidence, kind)
        # 6. Optional extra ambiguities from an LLM (deduplicated).
        ambiguities += self._llm_ambiguities(ctx, raw, ambiguities)

        # 5. Assemble the normalised requirement.
        req = NormalizedRequirement(
            # Original text.
            raw=raw,
            # Short title from the first sentence.
            title=self._title(raw),
            # Scenario kind.
            kind=kind,
            # Domain id or "unknown".
            domain=pack.id if pack else "unknown",
            # One-line summary.
            summary=self._summary(pack, kind, stated, assumed),
            # Capabilities tagged by origin.
            capabilities=[f"{c} (stated)" for c in stated] + [f"{c} (assumed)" for c in assumed],
            # Default NFRs for dimensions the text did not address.
            nfrs={d.title: d.default for d in (pack.dimensions if pack else [])
                  if not any(re.search(p, low) for p in d.answered_by)},
            # Acceptance criteria.
            acceptance_criteria=criteria,
            # Open questions.
            ambiguities=ambiguities,
            # Explicit non-goals.
            out_of_scope=self._out_of_scope(pack),
            # Repository path (brownfield).
            target_repo=repo,
            # Generation knobs; brownfield edits existing code so it has none.
            settings=pack.default_settings() if pack and kind is not ScenarioKind.BROWNFIELD else {},
        )
        # Notes for the trace.
        notes = [f"kind={kind.value} domain={req.domain} confidence={confidence:.2f}",
                 f"{len(stated)} stated / {len(assumed)} assumed capabilities, "
                 f"{len(ambiguities)} ambiguities, {len(criteria)} acceptance criteria"]
        # Publish the requirement and the domain confidence.
        return AgentResult(outputs={"requirement": req, "domain_confidence": confidence},
                           notes=notes)

    # ---------------------------------------------------------------- steps
    # Decide greenfield / brownfield / tests+docs.
    @staticmethod
    def _classify(low: str, repo: str | None) -> ScenarioKind:
        # A repo or brownfield vocabulary means changing existing code.
        if repo or re.search(_BROWNFIELD, low):
            return ScenarioKind.BROWNFIELD
        # Tests/docs vocabulary.
        if re.search(_TEST_DOCS, low):
            return ScenarioKind.TEST_DOCS
        # Otherwise build something new.
        return ScenarioKind.GREENFIELD

    # Score every domain pack and return the best (pack, confidence 0..1).
    @staticmethod
    def _domain(raw: str) -> tuple[DomainPack | None, float]:
        # (score, pack) pairs sorted by score, highest first.
        scored = sorted(((p.score(raw), p) for p in packs()), key=lambda t: -t[0])
        # No packs, or best score below the 1.5 recognition threshold -> unknown.
        if not scored or scored[0][0] < 1.5:
            return None, 0.0
        # Best match.
        score, pack = scored[0]
        # Normalise: a score of 6 or more counts as full confidence.
        return pack, min(1.0, score / 6.0)

    # Work out stated/assumed capabilities and their acceptance criteria.
    @staticmethod
    def _capabilities(low: str, pack: DomainPack | None, kind: ScenarioKind
                      ) -> tuple[list[str], list[str], list[AcceptanceCriterion]]:
        # Unknown domain: a single placeholder criterion.
        if pack is None:
            return [], [], [AcceptanceCriterion("AC-1", "Behaviour to be specified by a human or an "
                                                       "LLM-assisted analysis (domain not recognised)")]
        # Capabilities the user explicitly asked for.
        stated: list[str] = []
        # Capabilities included by default for production readiness.
        assumed: list[str] = []
        # Acceptance criteria gathered from both.
        criteria: list[AcceptanceCriterion] = []
        # Brownfield: capabilities = matched change recipes.
        if kind is ScenarioKind.BROWNFIELD:
            for recipe in pack.brownfield_recipes.values():
                # A recipe matches if any of its patterns appears in the text.
                if any(re.search(p, low) for p in recipe.patterns):
                    stated.append(recipe.title)
                    criteria.extend(recipe.criteria)
            # Brownfield always requires no regressions.
            criteria.append(AcceptanceCriterion(
                "AC-REGRESSION", "All pre-existing tests still pass on the changed code base", "regression"))
            return stated, assumed, criteria
        # Greenfield / tests+docs: walk the pack's capability catalogue.
        for cap in pack.capabilities:
            # Was this capability mentioned?
            mentioned = any(re.search(p, low) for p in cap.patterns)
            if mentioned:
                stated.append(cap.description)
            # Not mentioned but on by default -> assumed.
            elif cap.default:
                assumed.append(cap.description)
            # Neither -> skip, and don't add its criteria.
            else:
                continue
            # Included capabilities contribute their criteria.
            criteria.extend(cap.criteria)
        return stated, assumed, criteria

    # Rule-based ambiguity detection.
    def _ambiguities(self, raw: str, low: str, pack: DomainPack | None, confidence: float,
                     kind: ScenarioKind) -> list[Ambiguity]:
        # Collected ambiguities.
        out: list[Ambiguity] = []
        # Running counter for Q1, Q2, ... ids.
        n = 0

        # Helper that appends a numbered Ambiguity.
        def add(term: str, cat: str, q: str, default: str, sev: Severity, dim: str = "") -> None:
            # Modify the enclosing counter.
            nonlocal n
            n += 1
            out.append(Ambiguity(f"Q{n}", term, cat, q, default, sev, dimension=dim))

        # Unknown domain: a single blocker question, nothing else is meaningful.
        if pack is None:
            add("domain", "missing_scope", "The system to build was not recognised. What is it, "
                "who uses it, and what are its core operations?", "", Severity.BLOCKER)
            return out
        # Weak domain match: ask the human to confirm (HIGH severity).
        if confidence < 0.6:
            add("domain", "missing_scope",
                f"This reads like a {pack.title.lower()} but the wording is indirect. Confirm that is "
                "the intended system?", f"Build a {pack.title.lower()}.", Severity.HIGH)

        # concept -> original-case words in the text that matched a vague-term pattern.
        vague_hits: dict[str, list[str]] = {}
        for pattern, concept in VAGUE_TERMS.items():
            for m in re.finditer(pattern, low):
                # Slice from raw (same offsets as low) to keep the user's wording.
                vague_hits.setdefault(concept, []).append(raw[m.start():m.end()])

        if kind is ScenarioKind.BROWNFIELD:
            # Brownfield: only questions about the change itself.
            # If the expiration recipe matches, ask about backfill and expired-link status.
            if any(r.id == "add_expiration" and any(re.search(p, low) for p in r.patterns)
                   for r in pack.brownfield_recipes.values()):
                add("expiration", "missing_scope", "Should links that already exist get an expiry "
                    "(backfill), and what status should an expired link return?",
                    "Existing links never expire; expired links return 410 Gone.", Severity.MEDIUM)
            return out

        # One question per NFR dimension not addressed by the text.
        for dim in pack.dimensions:
            # Skip dimensions the text already answers.
            if any(re.search(p, low) for p in dim.answered_by):
                continue
            # Vague words in the text that relate to this dimension.
            triggers = [t for c in dim.triggered_by for t in vague_hits.get(c, [])]
            # Vague wording raises severity to MEDIUM; plain omission is LOW.
            sev = Severity.MEDIUM if triggers else Severity.LOW
            # Use the vague word as the term, else the dimension title.
            term = triggers[0] if triggers else dim.title.lower()
            # Category reflects why we're asking.
            cat = "vague_term" if triggers else "missing_nfr"
            # Prefix the question with a note that the word isn't measurable.
            question = (f"'{term}' is not measurable. {dim.question}" if triggers else dim.question)
            add(term, cat, question, dim.default, sev, dim.id)
        # Generic follow-ups for vague concepts that have a standard question.
        for concept, terms in vague_hits.items():
            if concept in GENERIC_QUESTIONS:
                q, default, sev = GENERIC_QUESTIONS[concept]
                add(terms[0], "vague_term", q.format(term=terms[0]), default, sev)
        return out

    # Optional LLM pass for ambiguities the rules missed.
    @staticmethod
    def _llm_ambiguities(ctx, raw: str, existing: list[Ambiguity]) -> list[Ambiguity]:
        # Validate/convert the LLM's JSON into Ambiguity objects.
        def validate(data: Any) -> list[Ambiguity]:
            # Accept {"ambiguities": [...]} or a bare list.
            items = data["ambiguities"] if isinstance(data, dict) else data
            result = []
            # Cap at 8 items.
            for i, it in enumerate(items[:8]):
                # Each item needs non-empty string term, question and default.
                if not all(isinstance(it.get(k), str) and it[k] for k in ("term", "question", "default")):
                    raise ValueError("each item needs non-empty term, question, default")
                # Ids L1, L2, ... mark LLM-sourced items; severity fixed at MEDIUM.
                result.append(Ambiguity(f"L{i + 1}", it["term"], "llm", it["question"], it["default"],
                                        Severity.MEDIUM))
            return result

        # Ask the LLM, listing already-known terms so it reports only new ones.
        found = structured(ctx.llm, "You are a principal engineer reviewing a requirement for "
                           "ambiguity. Only report issues not already listed.",
                           f"Requirement:\n{raw}\n\nAlready identified: "
                           f"{[a.term for a in existing]}\n\nReturn {{\"ambiguities\": [{{\"term\", "
                           f"\"question\", \"default\"}}]}}", validate)
        # No LLM configured / no result.
        if not found:
            return []
        # Drop duplicates of terms already raised (case-insensitive).
        known = {a.term.lower() for a in existing}
        return [a for a in found if a.term.lower() not in known]

    # -------------------------------------------------------------- helpers
    # First sentence/line of the requirement, trimmed to 90 chars.
    @staticmethod
    def _title(raw: str) -> str:
        # Split after sentence punctuation or at a newline; strip spaces, dots and quotes.
        first = re.split(r"(?<=[.!?])\s|\n", raw.strip())[0].strip(" .\"'“”")
        # Truncate with an ellipsis if too long.
        return first[:90] + ("..." if len(first) > 90 else "")

    # One-line description of what will be done.
    @staticmethod
    def _summary(pack: DomainPack | None, kind: ScenarioKind, stated: list[str],
                 assumed: list[str]) -> str:
        # Unknown domain.
        if pack is None:
            return "Unrecognised domain: analysis and planning only until clarified."
        # Verb phrase by scenario kind.
        verb = {"greenfield": "Build a new", "brownfield": "Change an existing",
                "test_docs": "Improve tests/docs of a"}[kind.value]
        # Stated capabilities, or a generic focus.
        focus = "; ".join(stated) if stated else "core capabilities"
        # Mention the number of assumed capabilities when there are any.
        return f"{verb} {pack.title}: {focus}." + (
            f" Also assumed: {len(assumed)} capabilities required for production use." if assumed else "")

    # Explicit non-goals (currently only defined for the URL-shortener pack).
    @staticmethod
    def _out_of_scope(pack: DomainPack | None) -> list[str]:
        if pack is None or pack.id != "url_shortener":
            return []
        return ["User accounts / UI dashboard", "Geo-IP enrichment", "Custom domains per tenant",
                "Malware/phishing reputation lookups (documented as production follow-up)"]

    # ----------------------------------------------------------------- gate
    # Build the requirements-gate request.
    def gate_request(self, ctx, task: TaskSpec) -> GateRequest:
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Header lines.
        lines = [f"Title: {req.title}", f"Kind: {req.kind.value}   Domain: {req.domain}",
                 f"Summary: {req.summary}", "Acceptance criteria:"]
        # One line per criterion.
        lines += [f"  - {c.id}: {c.text}" for c in req.acceptance_criteria]
        # Unresolved blockers prevent approval.
        blocking = [f"{a.id} unresolved: {a.question}" for a in req.open_blockers()]
        # Controlled autonomy: high-severity questions need a human answer; a policy may not
        # approve building on a guess about *what* the system is.
        blocking += [f"{a.id} ({a.severity.value}) needs a human answer: {a.question}"
                     for a in req.ambiguities if a.severity is Severity.HIGH and not a.answer]
        # Gate name, summary text, questions to answer and blocking reasons.
        return GateRequest("requirements", "\n".join(lines), req.ambiguities, blocking)

    # Apply human answers from the requirements gate.
    def on_gate(self, ctx, task: TaskSpec, decision: GateDecision) -> None:
        req: NormalizedRequirement = ctx.board.require("requirement")
        pack = get_pack(req.domain)
        # dimension id -> dimension (knows which setting it controls and how to parse answers).
        dims = {d.id: d for d in pack.dimensions} if pack else {}
        # Messages for answers that failed to parse.
        rejected: list[str] = []
        for amb in req.ambiguities:
            # Human's answer for this question id (if any).
            ans = decision.answers.get(amb.id)
            if not ans:
                continue
            # Record the answer.
            amb.answer = ans
            # Dimension this question belongs to.
            dim = dims.get(amb.dimension)
            # If the dimension maps to a setting, parse the answer into it.
            if dim and dim.setting and dim.parse:
                try:
                    req.settings[dim.setting] = dim.parse(ans)
                except ValueError as exc:
                    amb.answer = None  # fall back to default, but record why
                    rejected.append(f"{amb.id}: '{ans}' rejected ({exc}); default kept")
        # For unknown domains, append any free-text direction to the summary.
        if decision.answers.get("domain_override") and pack is None:
            req.summary += f" Human direction: {decision.answers['domain_override']}"
        # Republish the updated requirement.
        ctx.board.put("requirement", req, producer="gate:requirements")
        # Record rejected answers and emit guardrail events for each.
        if rejected:
            ctx.board.put("rejected_answers", rejected, producer="gate:requirements")
            for r in rejected:
                ctx.tracer.emit("guardrail", blocked=f"answer {r}")
