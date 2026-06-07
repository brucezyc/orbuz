"""\
orbuz — Standalone Multi-Agent Workflow Runtime

Usage:
    orbuz run "Latest impact of US AI chip export controls" \
        --quality-model "anthropic/claude-opus-4" \
        --balanced-model "anthropic/claude-sonnet-4" \
        --cheap-model "deepseek/deepseek-chat"

    orbuz codegen --project-dir ./rustpricer \
        --goal "IborIndex 加固定日期方法" --spec spec.yaml \
        --project-context on \
        --compile-loop on --compile-command "cargo check" \
        --oracle on --oracle-command "cargo test --bench iv" \
        --impact-analysis on

    orbuz status            # view current run status
    orbuz stop              # abort the current run
    orbuz agents list       # list the agent library

You can load API keys and base URLs from a YAML config file:
    orbuz run "topic" --config ~/.orbuz/forge.yaml

Config file format:
    # Global fallback (applied to all tiers without their own config)
    api_key: sk-...
    api_base: http://localhost:8082/v1

    # Per-tier overrides
    quality:
      api_key: sk-ant-...
      api_base: https://api.anthropic.com
    cheap:
      api_key: sk-deep-...

CLI flags always override config file values.

Codegen 子命令 (orbuz codegen):
    5 个独立模块，均可单独开关:
    --project-context on|off   扫描项目结构注入 LLM prompt
    --compile-loop on|off      cargo check -> 修复循环
    --spec <path>              YAML spec 驱动多文件生成
    --oracle on|off            运行 benchmark 对比预期值
    --impact-analysis on|off   跨文件依赖影响分析
"""

import argparse
import os
import sys


def load_config(path: str) -> dict:
    """Load a YAML config file. Returns empty dict if file not found."""
    import yaml
    try:
        with open(os.path.expanduser(path)) as f:
            cfg = yaml.safe_load(f) or {}
            return cfg
    except FileNotFoundError:
        print(f"  Warning: Config file not found: {path}", file=sys.stderr)
        return {}


def _apply_config(args, cfg: dict):
    """Overlay config values onto args. CLI args (non-None) win."""
    # Global keys
    for cli_attr, cfg_key in [("api_key", "api_key"), ("api_base", "api_base")]:
        if cfg.get(cfg_key) and getattr(args, cli_attr, None) is None:
            setattr(args, cli_attr, cfg[cfg_key])

    # Per-tier keys
    for tier in ("quality", "balanced", "cheap"):
        tier_cfg = cfg.get(tier, {})
        for suffix in ("api_key", "api_base"):
            cli_attr = f"{tier}_{suffix}"
            if tier_cfg.get(suffix) and getattr(args, cli_attr, None) is None:
                setattr(args, cli_attr, tier_cfg[suffix])


def main():
    parser = argparse.ArgumentParser(prog="orbuz", description="Multi-agent workflow runtime")

    sub = parser.add_subparsers(dest="command", required=True)

    # orbuz run
    run = sub.add_parser("run", help="Start a workflow")
    run.add_argument("topic", help="Research topic")
    run.add_argument("--auto", action="store_true",
                     help="Auto-approve plan and checkpoints (no interactive prompts)")
    run.add_argument("--config", default=None,
                     help="YAML config file with API keys/base URLs")
    run.add_argument("--quality-model", default=None,
                     help="Quality model ID (default: from DEFAULT_MODELS)")
    run.add_argument("--balanced-model", default=None,
                     help="Balanced model ID (default: from DEFAULT_MODELS)")
    run.add_argument("--cheap-model", default=None,
                     help="Cheap model ID (default: from DEFAULT_MODELS)")
    run.add_argument("--api-key", default=None,
                     help="LLM API key (or set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY env var)")
    run.add_argument("--api-base", default=None,
                     help="API base URL (or set ANTHROPIC_API_BASE / DEEPSEEK_API_BASE env var)")
    run.add_argument("--quality-api-key", default=None,
                     help="Per-tier API key for quality model provider")
    run.add_argument("--quality-api-base", default=None,
                     help="Per-tier API base for quality model provider")
    run.add_argument("--balanced-api-key", default=None,
                     help="Per-tier API key for balanced model provider")
    run.add_argument("--balanced-api-base", default=None,
                     help="Per-tier API base for balanced model provider")
    run.add_argument("--cheap-api-key", default=None,
                     help="Per-tier API key for cheap model provider")
    run.add_argument("--cheap-api-base", default=None,
                     help="Per-tier API base for cheap model provider")
    run.add_argument("--guardrails", default=None, choices=["on", "off"],
                     help="Enable/disable LLM response guardrails (default: off)")
    run.add_argument("--guardrails-tools", default=None,
                     help="Comma-separated tool names for guardrail validation")
    run.add_argument("--workflow-name", default=None, help="Workflow name (default: auto)")
    run.add_argument("--agent-dir", default=None, help="Agent YAML directory")
    run.add_argument("--resume", action="store_true",
                     help="Resume: load last workspace state and continue from where it left off")
    run.add_argument("--project-dir", default=None,
                     help="Project directory (default: current working directory)")
    run.add_argument("--goal", default="",
                     help="Specific goal (overrides topic for targeted tasks)")
    run.add_argument("--language", default=None,
                     help="Language hint (rust/python/cpp, auto-detected if omitted)")

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

    # orbuz codegen
    codegen = sub.add_parser("codegen", help="Code generation with feedback loops")
    codegen.add_argument("--project-dir", required=True,
                         help="Project root directory")
    codegen.add_argument("--goal", default="",
                         help="Natural language goal description")
    codegen.add_argument("--spec", default=None,
                         help="YAML spec file for multi-file generation")
    codegen.add_argument("--project-context", default="off", choices=["on", "off"],
                         help="Scan project structure and inject context (default: off)")
    codegen.add_argument("--language", default=None,
                         help="Force language (rust/python/cpp). Auto-detect if omitted.")
    codegen.add_argument("--compile-loop", default="off", choices=["on", "off"],
                         help="Auto-fix compile errors with LLM (default: off)")
    codegen.add_argument("--compile-command", default="cargo check 2>&1",
                         help="Compile/check command (default: cargo check)")
    codegen.add_argument("--compile-max-attempts", type=int, default=5,
                         help="Max compile-fix attempts (default: 5)")
    codegen.add_argument("--oracle", default="off", choices=["on", "off"],
                         help="Run oracle benchmark and compare to expected (default: off)")
    codegen.add_argument("--oracle-command", default="",
                         help="Oracle benchmark command")
    codegen.add_argument("--oracle-expected", default=None,
                         help="YAML file with expected values")
    codegen.add_argument("--impact-analysis", default="off", choices=["on", "off"],
                         help="Analyze cross-file dependency impact (default: off)")
    codegen.add_argument("--no-llm", action="store_true",
                         help="Skip LLM calls (just scan/report)")
    codegen.add_argument("--api-key", default=None,
                         help="LLM API key")
    codegen.add_argument("--quality-model", default=None,
                         help="Model for generation tasks (default: $DEEPSEEK_MODEL or deepseek/deepseek-v4-flash)")
    codegen.add_argument("--balanced-model", default=None,
                         help="Balanced model ID for code generation")
    codegen.add_argument("--cheap-model", default=None,
                         help="Cheap model ID for code generation")
    codegen.add_argument("--tier", default="balanced", choices=["cheap", "balanced", "quality"],
                         help="Model tier for code generation")

    args = parser.parse_args()

    # Load config file if specified (overrides defaults, CLI args win)
    if args.command == "run" and args.config:
        cfg = load_config(args.config)
        _apply_config(args, cfg)

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "status":
        _cmd_status()
    elif args.command == "stop":
        _cmd_stop(args)
    elif args.command == "agents":
        _cmd_agents(args)
    elif args.command == "codegen":
        _cmd_codegen(args)


def _cmd_run(args):
    import json
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
                    mock=not (has_global_key or has_tier_key),
                    guardrails=args.guardrails,
                    guardrails_tools=args.guardrails_tools)

    if llm.mock:
        print("  Yellow Mock mode: no API key provided, output is placeholder text")
        print("     Pass --api-key (or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY)")
        print("     Example: orbuz run \"topic\" --quality-model anthropic/claude-opus-4-8 --api-key sk-...")

    # ── Resume mode: load last workspace, skip Orchestrator ──
    if args.resume:
        from orbuz.workspace.manager import WorkspaceManager as WSMgr
        resume_mgr = WSMgr()
        run_id = resume_mgr.read_current_run_id()
        if not run_id:
            print("  ❌ No previous run to resume. Run orbuz run first without --resume.")
            return
        status = resume_mgr.read_current_status()
        if not status:
            print(f"  ❌ Run {run_id} has no status. Cannot resume.")
            return
        if status.get("state") == "completed":
            print(f"  ✅ Run {run_id} already completed. No need to resume.")
            return

        # Reconstruct plan from manifest
        manifest_path = resume_mgr.base / run_id / "manifest.json"
        plan = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if not plan:
            print(f"  ❌ Run {run_id} has no manifest. Cannot resume.")
            return

        print(f"  🔄 Resuming run {run_id} (stage {status.get('current_stage_index', 0)} of {len(plan.get('stages', []))})")
        approved = True
        resume_run_id = run_id
    else:
        # 1. Orchestrator does Recon -> plan.json
        topic = args.goal or args.topic
        project_dir = args.project_dir or os.getcwd()
        orch = Orchestrator(
            llm_client=llm,
            agent_dir=args.agent_dir,
        )
        plan = orch.recon(topic=topic, workflow_name=args.workflow_name,
                          project_dir=project_dir)

        # Inject project_dir into codegen stages (from CLI arg, overrides LLM guess)
        stages = plan.get('plan', {}).get('stages', [])
        for stage in stages:
            if stage.get('pattern') == 'codegen':
                stage['project_dir'] = project_dir

        # 2. Display plan -> wait for user approval
        print_plan(plan)
        if args.auto:
            approved = True
            print("  Auto-approve: yes")
        else:
            approved = _wait_approval()
        if not approved:
            print("Stop User rejected, exiting")
            return
        resume_run_id = None

    # 3. Executor runs the plan
    exe = Executor(
        plan=plan,
        llm_client=llm,
    )

    if resume_run_id:
        exe.run_id = resume_run_id  # Attach existing run so workspace isn't re-created

    for event in exe.run():
        if event["type"] == "checkpoint":
            print_checkpoint(event)
            if args.auto:
                decision = {"action": "continue"}
                print("  Auto-continue checkpoint")
            else:
                decision = _wait_checkpoint_decision()
            exe.continue_with(decision)
        elif event["type"] == "done":
            print(f"\nDone. Output: {event['output_path']}")
            cost = event.get("cost_summary", {})
            if cost:
                total = cost.get("total_cost_usd", 0)
                tokens = cost.get("total_tokens", 0)
                print(f"   Money ${total:.4f} | {tokens:,} tokens total")
                if cost.get("per_agent"):
                    top = sorted(cost["per_agent"].items(),
                                 key=lambda x: x[1]["cost_usd"], reverse=True)[:3]
                    for name, stats in top:
                        print(f"      {name}: ${stats['cost_usd']:.4f} ({stats['calls']} calls)")

    # 4. Register new agents
    if plan.get("agent_registry_updates"):
        from orbuz.agent.registry import AgentRegistry
        reg = AgentRegistry(args.agent_dir or Path.cwd() / "agents")
        for update in plan["agent_registry_updates"]:
            ok = reg.register_new(update)
            if ok:
                print(f"  New agent registered: {update.get('name', '?')}")


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
        print(f"Stop {run_id} aborted")
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


def _cmd_codegen(args):
    """
    ⚠️ 已废弃: orbuz codegen 已合并到 orbuz run --mode codegen

    这条命令在 v0.2 中保留为向后兼容的别名。
    内部直接委托给 _cmd_run，使用 --mode codegen 标志。

    用法迁移:
        orbuz run --mode codegen --project-dir ./myproject --goal "..." --language rust
    """
    print("⚠️  orbuz codegen is deprecated. Use: orbuz run --mode codegen ...")
    print("")
    # 把 codegen 的参数转换成 run 的参数
    from argparse import Namespace
    run_args = Namespace(
        command="run",
        topic=args.project_dir,  # project-dir 作为 topic
        goal=args.goal,
        language=args.language,
        project_dir=args.project_dir,
        auto=True,  # codegen 模式下自动通过
        mode="codegen",
        config=args.config,
        quality_model=args.quality_model,
        balanced_model=args.balanced_model,
        cheap_model=args.cheap_model,
        api_key=args.api_key,
        api_base=args.api_base,
        quality_api_key=None,
        quality_api_base=None,
        balanced_api_key=None,
        balanced_api_base=None,
        cheap_api_key=None,
        cheap_api_base=None,
        guardrails=args.guardrails,
        guardrails_tools=args.guardrails_tools,
        workflow_name="codegen",
        agent_dir=args.agent_dir,
        resume=getattr(args, 'resume', False),
    )
    _cmd_run(run_args)


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
