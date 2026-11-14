"""CodeGenerator: produces code as reviewable FileChanges.

Three modes:
  greenfield  render the domain blueprint for this component with the resolved
              settings (so requirement answers change the code).
  brownfield  run a change recipe against the *current* workspace copy; the
              recipe locates symbols with ast and refuses ambiguous edits.
  freeform    no blueprint: ask the configured LLM for FileChanges (schema-
              validated, sandboxed, then validated like any other output);
              with no LLM the task escalates to a human (NeedsHuman).

On a repair cycle the generator receives the validator's findings for its
own files. Offline it regenerates from the blueprint; with an LLM the
findings are part of the prompt.
"""
# Store annotations as strings.
from __future__ import annotations

# Any: type for untyped LLM JSON payloads.
from typing import Any

# NeedsHuman: escalation exception when no automated path can produce code.
from ..errors import NeedsHuman
# render: fill a blueprint file with settings; require_pack: get the domain pack or raise.
from ..knowledge import render, require_pack
# structured: call the LLM and validate its JSON response with a callback.
from ..llm import structured
# Core data models used by this agent.
from ..models import FileChange, NormalizedRequirement, TaskSpec
# Agent base class and result container.
from .base import Agent, AgentResult


# Agent that writes source code (one task per component or change recipe).
class CodeGenerator(Agent):
    # Registry name used by TaskSpec.agent.
    name = "codegen"

    # Entry point: pick a mode, produce FileChanges, write and record them.
    def run(self, ctx, task: TaskSpec) -> AgentResult:
        # The analysed requirement (domain, settings, raw text...).
        req: NormalizedRequirement = ctx.board.require("requirement")
        # Generation mode; greenfield is the default.
        mode = task.params.get("mode", "greenfield")
        # Validator findings targeted at this task (non-empty only during a repair cycle).
        feedback = self._feedback_for(ctx, task)
        # Brownfield: apply a change recipe to the existing code.
        if mode == "brownfield":
            changes = self._brownfield(ctx, task, req)
        # Freeform: ask the LLM because no blueprint exists.
        elif mode == "freeform":
            changes = self._freeform(ctx, task, req)
        # Greenfield: render blueprint files.
        else:
            changes = self._greenfield(ctx, task, req)
        # Write every change.
        for ch in changes:
            # Attribute the file to this task for fault localisation.
            ch.produced_by = task.id
            # Persist through the guard (path sandboxing, size limits).
            ctx.guard.apply(ch)
        # Register the changes as artifacts on the blackboard.
        ctx.board.append("artifacts", changes, producer=task.id)
        # Summary note listing the distinct files touched.
        notes = [f"{len(changes)} file change(s): " + ", ".join(sorted({c.path for c in changes}))]
        # In a repair cycle, also note the feedback that triggered regeneration (truncated).
        if feedback:
            notes.append("repair: regenerated with feedback -> " + "; ".join(feedback)[:300])
        # No outputs; the artifacts themselves are the product.
        return AgentResult(notes=notes)

    # Greenfield: render each file listed in the task from the domain blueprint.
    def _greenfield(self, ctx, task: TaskSpec, req: NormalizedRequirement) -> list[FileChange]:
        # Domain pack (raises if unknown).
        pack = require_pack(req.domain)
        # One "create" change per blueprint path, rendered with the resolved settings.
        changes = [FileChange(f"project/{rel}", "create", render(pack, rel, req.settings),
                              reason=task.title) for rel in task.params["files"]]
        # Fault injection (demo/testing): if a "bug" fault is armed for this task and the pack has one,
        # deliberately corrupt the output to exercise the validator's repair loop.
        if ctx.faults.take("bug", task.id) and task.id in pack.bug_variants:
            # Which file to corrupt, the anchor text, and its buggy replacement.
            rel, find, repl = pack.bug_variants[task.id]
            # Locate the targeted file among the generated changes.
            for ch in changes:
                if ch.path == f"project/{rel}":
                    # Guard against the blueprint changing so the anchor no longer exists.
                    assert find in ch.content, "bug variant anchor drifted from blueprint"
                    # Replace only the first occurrence with the defective variant.
                    ch.content = ch.content.replace(find, repl, 1)
            # Record the injected fault in the execution trace.
            ctx.tracer.emit("fault_injected", task=task.id, kind="defective generation",
                            file=rel)
        # The rendered (possibly corrupted) changes.
        return changes

    # Brownfield: apply a named change recipe to the copied project.
    def _brownfield(self, ctx, task: TaskSpec, req: NormalizedRequirement) -> list[FileChange]:
        # Domain pack holding the brownfield recipes.
        pack = require_pack(req.domain)
        # Impact analysis from the CodebaseAnalyst (module roles etc.).
        impact = ctx.board.require("impact")
        # The specific recipe this task was planned to run.
        recipe = pack.brownfield_recipes[task.params["recipe"]]
        # Sandboxed absolute path of the workspace copy of the project.
        project = ctx.guard.safe_path("project")
        # Let the recipe compute edits against real code, using role -> module mapping.
        return recipe.build(project, impact["roles"], "project/")

    # Freeform: no blueprint; ask an LLM for files and validate the response.
    def _freeform(self, ctx, task: TaskSpec, req: NormalizedRequirement) -> list[FileChange]:
        # Validator for the LLM's JSON: converts it into FileChanges or raises ValueError.
        def validate(data: Any) -> list[FileChange]:
            # Expect {"files": [...]}; anything else yields None.
            files = data.get("files") if isinstance(data, dict) else None
            # Empty/missing list is a schema violation.
            if not files:
                raise ValueError("expected {'files': [{'path','content'}]}")
            # Accumulated FileChanges.
            out = []
            # Cap at 30 files to bound the blast radius of a runaway response.
            for f in files[:30]:
                # Pull out path and content.
                path, content = f.get("path"), f.get("content")
                # Both must be strings and the path must be relative (no absolute writes).
                if not isinstance(path, str) or not isinstance(content, str) or path.startswith("/"):
                    raise ValueError("each file needs a relative 'path' and string 'content'")
                # Prefix with project/ so the write lands inside the project sandbox.
                out.append(FileChange(f"project/{path}", "create", content, reason="llm"))
            # Validated changes.
            return out

        # Call the LLM: system prompt, user prompt (requirement + resolved assumptions + schema), validator.
        changes = structured(ctx.llm, "You are a senior engineer. Produce a small, production-quality "
                             "Python implementation with pytest tests.",
                             f"Requirement:\n{req.raw}\n\nResolved assumptions:\n"
                             + "\n".join(f"- {a.question} -> {a.resolution}" for a in req.ambiguities)
                             + "\n\nReturn {\"files\": [{\"path\", \"content\"}]}", validate)
        # No LLM configured (or no valid answer): escalate to a human.
        if not changes:
            raise NeedsHuman("no blueprint for this domain and no LLM provider configured "
                             "(set SDLC_LLM) - implementation escalated to a human")
        # LLM-produced changes.
        return changes

    # Collect validator messages attributed to this task in the current repair cycle.
    @staticmethod
    def _feedback_for(ctx, task: TaskSpec) -> list[str]:
        # Repair feedback published by the orchestrator (None when not repairing).
        fb = ctx.board.get("repair_feedback")
        # Nothing to do if there's no feedback or this task isn't a repair target.
        if not fb or task.id not in fb["targets"]:
            return []
        # Messages of findings whose task_hint points at this task.
        return [f.message for f in fb["findings"] if f.task_hint == task.id]
