"""Command-line entry point.

  python -m sdlc_agent run -r "Build a scalable URL shortener ..."            # greenfield
  python -m sdlc_agent run -f examples/requirements/brownfield.md --repo examples/legacy_links
  python -m sdlc_agent run -f req.md --approval interactive                   # human at every gate
  python -m sdlc_agent run -f req.md --approval file --answers answers.yaml   # recorded decisions
  python -m sdlc_agent run -f req.md --inject-fault bug:gen_api               # show repair loop
  python -m sdlc_agent resume runs/<run-id>                                   # continue from checkpoint
"""
# Store annotations as strings.
from __future__ import annotations

# argparse: command-line parsing.
import argparse
# shutil: remove an existing run directory.
import shutil
# sys: stderr for error messages.
import sys
# time: default run id from the current timestamp.
import time
# Path: run directory paths.
from pathlib import Path

# All agent implementations.
from .agents.architect import Architect
from .agents.base import Agent
from .agents.codebase import CodebaseAnalyst
from .agents.codegen import CodeGenerator
from .agents.docgen import DocGenerator
from .agents.planner import Planner
from .agents.reporter import Reporter
from .agents.requirements import RequirementAnalyst
from .agents.risk import RiskAnalyst
from .agents.testgen import TestGenerator
from .agents.validator import Validator
# Infrastructure: approval gates, shared context, guardrails, LLM factory.
from .approval import ApprovalGate
from .context import Blackboard, FaultInjector, Tracer
from .guardrails import Guardrails, Limits
from .llm import get_provider
from .models import TaskSpec
from .orchestrator import Orchestrator, RunContext, SimulatedCrash


# Instantiate every agent and index them by name (TaskSpec.agent refers to these names).
def build_agents() -> dict[str, Agent]:
    agents: list[Agent] = [RequirementAnalyst(), Planner(), CodebaseAnalyst(), Architect(),
                           CodeGenerator(), TestGenerator(), DocGenerator(), Validator(),
                           RiskAnalyst(), Reporter()]
    return {a.name: a for a in agents}


# Initial plan for every run; the Planner spawns the rest of the DAG.
BOOTSTRAP = [
    # Analyse the requirement, then pause at the "requirements" gate.
    TaskSpec("understand", "Interpret requirement, detect ambiguity, normalise", "requirements",
             gate="requirements", rationale="Nothing is built on an unconfirmed understanding."),
    # Plan the work, then pause at the "plan" gate.
    TaskSpec("plan", "Decompose into a task DAG", "planner", ["understand"], gate="plan",
             params={"stage": "initial"}, rationale="Humans approve the plan before execution."),
    # Always runs: writes a partial summary if planning did not succeed.
    TaskSpec("escalate", "Partial summary if the run stops before planning completes", "reporter",
             ["plan"], always_run=True, params={"mode": "escalation"},
             rationale="A stopped run must still leave a reviewable record."),
]


# Build the RunContext (workspace, tracer, gate, guardrails, faults, LLM) for a run directory.
def make_context(run_dir: Path, args: argparse.Namespace, echo: bool = True) -> RunContext:
    # All generated files go under <run_dir>/workspace.
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    # Trace file (appended to on resume).
    tracer = Tracer(run_dir / "trace.jsonl", echo=echo)
    # Limits with the repair-cycle cap from the command line.
    limits = Limits(max_repair_cycles=args.max_repairs)
    return RunContext(
        run_id=run_dir.name, run_dir=run_dir, board=Blackboard(), tracer=tracer,
        gate=ApprovalGate(args.approval, args.answers, reviewer=args.reviewer),
        guard=Guardrails(workspace, limits, tracer), faults=FaultInjector(list(args.inject_fault or [])),
        llm=get_provider(args.llm), options={"target_repo": args.repo})


# `run` subcommand: start a fresh run for a requirement.
def run(args: argparse.Namespace) -> int:
    # Requirement text from the file (-f) or the inline argument (-r).
    text = Path(args.requirement_file).read_text(encoding="utf-8") if args.requirement_file else args.requirement
    if not text or not text.strip():
        print("error: provide --requirement or --requirement-file", file=sys.stderr)
        # Exit code 2 = usage error.
        return 2
    # Run id: given, or a timestamp.
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) / run_id
    # Start clean: delete a previous run with the same id.
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    # Keep a copy of the requirement with the run.
    (run_dir / "requirement.md").write_text(text.strip() + "\n", encoding="utf-8")
    ctx = make_context(run_dir, args, echo=not args.quiet)
    # Seed the blackboard with the raw requirement.
    ctx.board.put("requirement_text", text, producer="user")
    orch = Orchestrator(ctx, build_agents(), max_workers=args.workers)
    # Add fresh copies of the bootstrap tasks (so the module-level list is never mutated).
    orch.add_tasks([TaskSpec(**{**t.__dict__, "depends_on": list(t.depends_on),
                                "params": dict(t.params)}) for t in BOOTSTRAP])
    try:
        ok = orch.run()
    except SimulatedCrash as crash:
        # Demo crash: tell the user how to resume.
        print(f"\n*** simulated crash after '{crash.task_id}' - state is checkpointed. Continue with:\n"
              f"    python -m sdlc_agent resume {run_dir}")
        return 75
    _final(ctx, run_dir, ok)
    # 0 = success, 1 = run did not complete.
    return 0 if ok else 1


# `resume` subcommand: continue a run from its checkpoint.
def resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / "checkpoint.pkl").exists():
        print(f"error: no checkpoint in {run_dir}", file=sys.stderr)
        return 2
    ctx = make_context(run_dir, args, echo=not args.quiet)
    orch = Orchestrator(ctx, build_agents(), max_workers=args.workers)
    # Load the saved state, then continue the main loop.
    orch.resume()
    ok = orch.run()
    _final(ctx, run_dir, ok)
    return 0 if ok else 1


# Print the final outcome and where to find the outputs.
def _final(ctx: RunContext, run_dir: Path, ok: bool) -> None:
    summary = ctx.board.get("summary") or {}
    print("\n" + "=" * 72)
    print(f"Run {run_dir.name}: {'SUCCEEDED' if ok else 'DID NOT COMPLETE'} "
          f"(status: {summary.get('status', 'n/a')})")
    print(f"Summary:   {ctx.workspace / 'ENGINEERING_SUMMARY.md'}")
    print(f"Artifacts: {ctx.workspace / 'project'}")
    print(f"Trace:     {run_dir / 'trace.jsonl'}")


# Parse arguments and dispatch to run/resume; returns the exit code.
def main(argv: list[str] | None = None) -> int:
    # Windows consoles may default to cp1252; never crash on symbols in trace output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    # The module docstring doubles as the --help description.
    p = argparse.ArgumentParser(prog="sdlc_agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    # Options shared by both subcommands.
    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--approval", choices=["auto", "interactive", "file"], default="auto",
                        help="who decides at gates (default: auto policy)")
        sp.add_argument("--answers", help="YAML with recorded gate decisions/answers (file mode)")
        sp.add_argument("--reviewer", default="reviewer")
        # May be given multiple times.
        sp.add_argument("--inject-fault", action="append", metavar="KIND:TASK",
                        help="demo recovery: transient:<task>, bug:<task>, crash:<task>")
        sp.add_argument("--max-repairs", type=int, default=2)
        sp.add_argument("--workers", type=int, default=4)
        sp.add_argument("--llm", default=None, help="none|anthropic|bedrock (default: $SDLC_LLM or none)")
        sp.add_argument("--repo", help="target repository for brownfield work")
        sp.add_argument("--quiet", action="store_true")

    # `run` subcommand.
    r = sub.add_parser("run", help="run the workflow for a requirement")
    r.add_argument("-r", "--requirement")
    r.add_argument("-f", "--requirement-file")
    r.add_argument("--out", default="runs")
    r.add_argument("--run-id")
    common(r)
    # `resume` subcommand.
    rs = sub.add_parser("resume", help="resume a run from its checkpoint")
    rs.add_argument("run_dir")
    common(rs)
    args = p.parse_args(argv)
    return run(args) if args.cmd == "run" else resume(args)


# Allow running this file directly.
if __name__ == "__main__":
    raise SystemExit(main())
