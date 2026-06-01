"""
orbuz — Standalone Multi-Agent Workflow Runtime

Usage:
    orbuz run "Latest impact of US AI chip export controls" \
        --quality-model "claude-sonnet-4" \
        --cheap-model "gemini-2.0-flash" \
        --api-key "..."
    
    orbuz status            # view current run status
    orbuz stop              # abort the current run
    orbuz agents list       # list the agent library
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(prog="orbuz", description="Multi-agent workflow runtime")

    sub = parser.add_subparsers(dest="command", required=True)

    # orbuz run
    run = sub.add_parser("run", help="Start a workflow")
    run.add_argument("topic", help="Research topic")
    run.add_argument("--quality-model", required=True, help="Model for the Orchestrator")
    run.add_argument("--balanced-model", required=True, help="Default execution model")
    run.add_argument("--cheap-model", required=True, help="Model for information gathering")
    run.add_argument("--api-key", help="LLM API key (or set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY env var)")
    run.add_argument("--api-base", default=None, help="API base URL (default https://api.deepseek.com/v1, or set ANTHROPIC_API_BASE / DEEPSEEK_API_BASE env var)")
    run.add_argument("--quality-api-key", help="Per-tier API key for quality model (overrides --api-key, or set ORBUZ_API_KEY_QUALITY)")
    run.add_argument("--quality-api-base", help="Per-tier API base for quality model (overrides --api-base, or set ORBUZ_API_BASE_QUALITY)")
    run.add_argument("--balanced-api-key", help="Per-tier API key for balanced model (or set ORBUZ_API_KEY_BALANCED)")
    run.add_argument("--balanced-api-base", help="Per-tier API base for balanced model (or set ORBUZ_API_BASE_BALANCED)")
    run.add_argument("--cheap-api-key", help="Per-tier API key for cheap model (or set ORBUZ_API_KEY_CHEAP)")
    run.add_argument("--cheap-api-base", help="Per-tier API base for cheap model (or set ORBUZ_API_BASE_CHEAP)")
    run.add_argument("--workflow-name", default=None, help="Workflow name (default: auto)")
    run.add_argument("--agent-dir", default=None, help="Agent YAML directory")

    # orbuz status
    sub.add_parser("status", help="View run status")

    # orbuz stop
    stop = sub.add_parser("stop", help="Abort a run")
    stop.add_argument("--run-id", default=None, help="Specific run ID (default: current)")

    # orbuz agents
    agents = sub.add_parser("agents", help="Manage agent library")
    agents.add_argument("action", choices=["list", "show"],
                        help="list=list agents, show=<name>=view details")
    agents.add_argument("--agent-dir", default=None, help="Agent YAML directory")

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "status":
        _cmd_status()
    elif args.command == "stop":
        _cmd_stop(args)
    elif args.command == "agents":
        _cmd_agents(args)


def _cmd_run(args):
    from pathlib import Path
    from orbuz.core.orchestrator import Orchestrator
    from orbuz.core.executor import Executor
    from orbuz.ui.cli_printer import print_plan, print_checkpoint
    from orbuz.llm.client import LLMClient

    # Create LLM client (auto-mocks when no key provided)
    models = {
        "quality": args.quality_model,
        "balanced": args.balanced_model,
        "cheap": args.cheap_model,
    }
    # Build per-tier config
    tier_config = {}
    for tier in ("quality", "balanced", "cheap"):
        cfg = {}
        k = getattr(args, f"{tier}_api_key", None)
        b = getattr(args, f"{tier}_api_base", None)
        if k:
            cfg["api_key"] = k
        if b:
            cfg["api_base"] = b
        if cfg:
            tier_config[tier] = cfg

    # Determine mock mode: mock if no global key AND no per-tier key
    has_global_key = bool(args.api_key or os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", ""))
    has_tier_key = any(
        tc.get("api_key") or os.environ.get(f"ORBUZ_API_KEY_{t.upper()}", "")
        for t, tc in tier_config.items()
    ) if tier_config else False

    llm = LLMClient(models, api_key=args.api_key,
                    api_base=args.api_base,
                    tier_config=tier_config if tier_config else None,
                    mock=not (has_global_key or has_tier_key))

    if llm.mock:
        print("  🟡 Mock mode: no API key provided, output is placeholder text")
        print("     Pass --api-key (or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY) to use real LLM calls")

    # 1. Orchestrator does Recon → plan.json
    orch = Orchestrator(
        llm_client=llm,
        agent_dir=args.agent_dir,
    )
    plan = orch.recon(topic=args.topic, workflow_name=args.workflow_name)

    # 2. Display plan → wait for user approval
    print_plan(plan)
    approved = _wait_approval()
    if not approved:
        print("⏹ User rejected, exiting")
        return

    # 3. Executor runs the plan
    exe = Executor(
        plan=plan,
        llm_client=llm,
    )

    for event in exe.run():
        if event["type"] == "checkpoint":
            print_checkpoint(event)
            decision = _wait_checkpoint_decision()
            exe.continue_with(decision)
        elif event["type"] == "done":
            print(f"\n✅ Done. Output: {event['output_path']}")

    # 4. Register new agents
    if plan.get("agent_registry_updates"):
        from orbuz.agent.registry import AgentRegistry
        reg = AgentRegistry(args.agent_dir or Path.cwd() / "agents")
        for update in plan["agent_registry_updates"]:
            ok = reg.register_new(update)
            if ok:
                print(f"  📦 New agent registered: {update.get('name', '?')}")


def _cmd_status():
    from orbuz.workspace.manager import WorkspaceManager
    wm = WorkspaceManager()
    status = wm.read_current_status()
    if not status:
        print("No workflow in progress")
        return
    print(f"Run: {status.get('run_id', '?')}")
    print(f"State: {status.get('state', '?')}")
    print(f"Stage: {status.get('current_stage_index', '?')}/{status.get('total_stages', '?')}")


def _cmd_stop(args):
    from orbuz.workspace.manager import WorkspaceManager
    wm = WorkspaceManager()
    run_id = args.run_id
    if not run_id:
        run_id = wm.read_current_run_id()
    if run_id:
        wm.set_state(run_id, "cancelled")
        print(f"⏹ {run_id} aborted")
    else:
        print("No workflow in progress")


def _cmd_agents(args):
    from orbuz.schema.agent import load_index
    agent_dir = args.agent_dir or getattr(args, 'agent_dir', None)
    index = load_index(agent_dir)
    if args.action == "list":
        print(f"{'Name':<30} {'Summary':<40} {'Tags'}")
        print("-" * 90)
        for a in index.agents:
            tags = ", ".join(a.tags)
            print(f"{a.name:<30} {a.summary:<40} {tags}")
    elif args.action == "show":
        name = input("agent name: ")
        print(f"View full definition of {name} (TODO)")


def _wait_approval() -> bool:
    """CLI prompt waiting for user approval of the plan"""
    while True:
        resp = input("\nApprove / Modify direction / Reject? [a/m/r]: ").strip().lower()
        if resp in ("a", "approve"):
            return True
        if resp in ("r", "reject"):
            return False
        if resp in ("m", "modify"):
            print("Modification not yet implemented, please re-enter")
            continue
        print("Enter a(approve) / r(reject)")

def _wait_checkpoint_decision() -> dict:
    """CLI prompt waiting for checkpoint decision"""
    while True:
        resp = input("\nContinue / Modify / Rerun / Stop? [c/m/r/s]: ").strip().lower()
        if resp in ("c", "continue"):
            return {"action": "continue"}
        if resp in ("s", "stop"):
            return {"action": "stop"}
        if resp in ("m", "modify"):
            note = input("Modification instructions: ")
            return {"action": "redirect", "note": note}
        if resp in ("r", "rerun"):
            return {"action": "rerun"}
        print("Enter c(continue) / m(modify) / r(rerun) / s(stop)")


if __name__ == "__main__":
    main()
