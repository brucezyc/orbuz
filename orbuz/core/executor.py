"""
Executor — plan.json Executor
==============================
Input: approved plan.json + LLMClient
Flow: iterate stages → dispatch by pattern → checkpoint → continue

Execution patterns:
  fanout            → parallel agents + message bus (Round 1..N)
  pipeline          → sequential agents
  producer_reviewer → produce → review → cycle
  code_review       → Compound Engineering-style: scope → persona selection →
                      parallel dispatch → merge-dedup → synthesis
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from orbuz.core.dispatcher import Dispatcher, DispatcherResult, merge_dedup_findings
from orbuz.core.selector import select_personas, describe_team
from orbuz.agent.message import MessageBus, Message, CommunicationSpec
from orbuz.workspace.manager import WorkspaceManager
from orbuz.llm.client import LLMClient
from orbuz.schema.agent import load_agent, ModelHint
from orbuz.schema.finding import FindingSet, Finding, Severity, AutofixClass
from orbuz.core.plugin import get_registry


_plugins = get_registry()


# ── Cost Tracker ──

@dataclass
class CostTracker:
    """Tracks token usage and cost across a workflow run."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    per_agent: dict[str, dict] = field(default_factory=dict)

    def record(self, agent_name: str, in_tokens: int, out_tokens: int, cost_usd: float):
        self.total_input_tokens += in_tokens
        self.total_output_tokens += out_tokens
        self.total_cost_usd += cost_usd
        if agent_name not in self.per_agent:
            self.per_agent[agent_name] = {
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            }
        self.per_agent[agent_name]["calls"] += 1
        self.per_agent[agent_name]["input_tokens"] += in_tokens
        self.per_agent[agent_name]["output_tokens"] += out_tokens
        self.per_agent[agent_name]["cost_usd"] += cost_usd

    def record_result(self, agent_name: str, result: DispatcherResult):
        """Record from a DispatcherResult."""
        self.record(agent_name, result.tokens // 2, result.tokens // 2, result.cost_usd)

    def summary(self) -> dict:
        return {
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "per_agent": self.per_agent,
        }

    def summary_str(self) -> str:
        s = self.summary()
        lines = [
            f"Cost: ${s['total_cost_usd']:.4f} | "
            f"Tokens: {s['total_tokens']:,} "
            f"({s['total_input_tokens']:,} in / {s['total_output_tokens']:,} out)"
        ]
        by_cost = sorted(s["per_agent"].items(), key=lambda x: x[1]["cost_usd"], reverse=True)
        for name, stats in by_cost[:5]:
            lines.append(f"  {name}: ${stats['cost_usd']:.4f} ({stats['calls']} calls, "
                         f"{stats['input_tokens']+stats['output_tokens']:,} tokens)")
        if len(by_cost) > 5:
            lines.append(f"  ... and {len(by_cost) - 5} more agents")
        return "\n".join(lines)


class Executor:
    """
    Reads plan.json → executes stages → checkpoints → delivers.

    Usage:
        exe = Executor(plan, llm_client=llm_client)
        for event in exe.run():
            if event["type"] == "checkpoint":
                decision = wait_for_input()
                exe.continue_with(decision)
            elif event["type"] == "done":
                print(f"Done: {event['output_path']}")
    """

    MAX_ROUNDS = 3  # Multi-round fanout limit

    def __init__(self, plan: dict, llm_client: LLMClient, run_id: str | None = None):
        self.plan = plan
        self.dispatcher = Dispatcher(llm_client)
        self.ws = WorkspaceManager()
        self.bus = None
        self._decision = None
        self._cost_tracker = CostTracker()
        self.run_id = run_id

    # ── Main entry ──

    def run(self):
        """Generator: yields events."""
        # ── Plugin hook: before_workflow ──
        plan = _plugins.run("before_workflow", self.plan) or self.plan
        stages = plan["plan"]["stages"] if "plan" in plan else plan.get("stages", [])
        stages = plan.get("stages", stages)  # manifest format

        # ── Resume or fresh run ──
        if self.run_id:
            run_id = self.run_id
            self.ws.set_state(run_id, "executing")
            status = self.ws.read_current_status()
            start_from = status.get("current_stage_index", 0) if status else 0
            # Mark run as resumed
            ctx_path = self.ws.base / run_id / "context.json"
            if ctx_path.exists():
                ctx = json.loads(ctx_path.read_text())
                ctx.setdefault("continuation", {})["resumed"] = True
                ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2))
        else:
            run_id = self.ws.init_run(plan)
            start_from = 0
            self.run_id = run_id

        self.bus = MessageBus(
            workspace_dir=str(self.ws.base / run_id)
        )

        # Register all agents' communication capabilities
        for stage in stages:
            for agent_cfg in stage.get("agents", []):
                defn = load_agent(agent_cfg["role"])
                comm_data = self._get_comm(defn)
                self.bus.register_agent(agent_cfg["role"], comm_data)

        for idx, stage in enumerate(stages):
            # Skip completed stages (resume mode)
            stage_id = stage["id"]
            stage_status = self.ws.get_stage_status(run_id, stage_id) if self.run_id else "pending"
            if stage_status == "completed":
                continue

            self.ws.set_current_stage(run_id, idx)

            # Validate dependencies
            for dep_id in stage.get("depends_on", []):
                dep_status = self.ws.get_stage_status(run_id, dep_id)
                if dep_status != "completed":
                    yield {"type": "error", "msg": f"Dependency {dep_id} not completed (status={dep_status})"}
                    return

            # Execute by pattern
            pattern = stage.get("pattern", "fanout")
            if pattern == "fanout":
                yield from self._exec_fanout(run_id, stage, idx)
            elif pattern == "pipeline":
                yield from self._exec_pipeline(run_id, stage, idx)
            elif pattern == "producer_reviewer":
                yield from self._exec_producer_reviewer(run_id, stage, idx)
            elif pattern == "code_review":
                yield from self._exec_code_review(run_id, stage, idx)
            elif pattern == "codegen":
                yield from self._exec_codegen(run_id, stage, idx)
            else:
                yield {"type": "error", "msg": f"Unknown pattern: {pattern}"}
                return

            # Mark complete
            self.ws.set_stage_completed(run_id, stage_id)

            # Checkpoint
            if idx < len(stages) - 1:
                summary = self.ws.get_stage_summary(run_id, stage_id)
                decision = yield {
                    "type": "checkpoint",
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "stage_name": stage.get("name", ""),
                    "summary": summary,
                    "next_stage": stages[idx + 1].get("name", ""),
                }
                if decision:
                    self._handle_checkpoint_decision(run_id, decision)
                while self._decision is None:
                    yield {"type": "waiting"}
                self._handle_checkpoint_decision(run_id, self._decision)
                self._decision = None

        # Done
        self.ws.set_state(run_id, "completed")

        cost_summary = self._cost_tracker.summary()

        # ── Plugin hook: after_workflow ──
        _plugins.run("after_workflow", {
            "run_id": run_id,
            "output_path": f"_workspace/{run_id}/deliver/",
            "cost_summary": cost_summary,
        })

        yield {"type": "done", "run_id": run_id, "output_path": f"_workspace/{run_id}/deliver/",
               "cost_summary": cost_summary}

    def continue_with(self, decision: dict):
        self._decision = decision

    # ── Core: Code Review Pattern ──

    def _exec_code_review(self, run_id, stage, idx):
        """
        Compound Engineering-style code review pipeline.

        Stage 1: Determine scope (diff range, file list)
        Stage 2: Persona selection via selector.py
        Stage 3: Parallel dispatch to all selected reviewers
        Stage 4: Merge + dedup findings
        Stage 5: Synthesis + confidence gating → final report
        """
        stage_id = stage["id"]
        yield {"type": "progress", "stage": stage_id, "msg": "Starting code review"}

        # ── Stage 1: Scope ──
        base_branch = stage.get("base", "main")
        yield {"type": "progress", "stage": stage_id, "msg": f"Computing diff against {base_branch}"}

        import subprocess
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", base_branch],
                capture_output=True, text=True, timeout=30
            )
            diff_stat = result.stdout
            diff_lines = len(diff_stat.splitlines())

            result2 = subprocess.run(
                ["git", "diff", "--name-only", base_branch],
                capture_output=True, text=True, timeout=30
            )
            file_list = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

            result3 = subprocess.run(
                ["git", "diff", base_branch],
                capture_output=True, text=True, timeout=30
            )
            diff_content = result3.stdout

            yield {"type": "progress", "stage": stage_id,
                   "msg": f"Diff: {len(file_list)} files, ~{diff_lines} lines changed"}
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            yield {"type": "error", "msg": f"Git diff failed: {e}"}
            return

        # ── Stage 2: Persona Selection ──
        if stage.get("review_agents"):
            # Explicit agent list from workflow YAML
            agents = [load_agent(name) for name in stage["review_agents"]]
        else:
            agents = select_personas(
                diff_file_list=file_list,
                diff_lines=diff_lines,
                diff_content=diff_content,
            )

        team_desc = describe_team(agents)
        yield {"type": "progress", "stage": stage_id, "msg": team_desc}

        # ── Stage 3: Parallel Dispatch ──
        all_finding_sets: list[FindingSet] = []
        goal = stage.get("goal", "Review the code changes and identify issues.")

        for agent_def in agents:
            yield {"type": "progress", "stage": stage_id,
                   "msg": f"Running {agent_def.name}..."}

            context = (
                f"## Diff Scope\n{len(file_list)} files changed\n\n"
                f"## Files Changed\n" + "\n".join(file_list) + "\n\n"
                f"## Diff Content\n{diff_content[:8000]}"
            )

            result = self.dispatcher.run_agent(
                agent_def,
                goal=goal,
                context=context,
                tier=agent_def.model_hint.tier,
                require_structured_findings=True,
            )
            self._cost_tracker.record_result(agent_def.name, result)
            self.ws.write_output(run_id, stage_id,
                                 f"{agent_def.name}", result.output)

            if result.findings and result.findings.findings:
                all_finding_sets.append(result.findings)
                yield {"type": "progress", "stage": stage_id,
                       "msg": f"  → {len(result.findings.findings)} findings from {agent_def.name}"}

        # ── Stage 4: Merge + Dedup ──
        dedup_result = merge_dedup_findings(all_finding_sets)
        yield {"type": "progress", "stage": stage_id,
               "msg": (f"Merge: {dedup_result.duplicates_removed} duplicates removed, "
                       f"{dedup_result.severity_overrides} overrides")}

        # Write merged findings
        merged_path = self.ws.write_output(
            run_id, stage_id, "merged_findings",
            json.dumps([f.to_dict() for f in dedup_result.merged],
                       ensure_ascii=False, indent=2)
        )

        # ── Stage 5: Synthesis (confidence gate) ──
        # Filter: min confidence 0.3
        high_conf = [f for f in dedup_result.merged if f.confidence >= 0.3]
        low_conf = [f for f in dedup_result.merged if f.confidence < 0.3]

        # Generate review report
        report_lines = ["# Code Review Report", ""]
        report_lines.append(f"**Reviewers:** {len(agents)}")
        report_lines.append(f"**Files:** {len(file_list)}")
        report_lines.append(f"**Findings:** {len(dedup_result.merged)} total "
                            f"({len(high_conf)} high-confidence, {len(low_conf)} gated)")
        report_lines.append("")

        # By severity
        for sev in ["P0", "P1", "P2", "P3"]:
            items = [f for f in dedup_result.merged
                     if f.severity.value == sev and f.confidence >= 0.3]
            if items:
                report_lines.append(f"## {sev} — {len(items)} items")
                for f in items:
                    loc = f"{f.file}:{f.line}" if f.line else f.file
                    report_lines.append(f"- **{f.title}** [{f.confidence:.1f}] ({loc})")
                    report_lines.append(f"  {f.description[:200]}")
                    if f.suggested_fix:
                        report_lines.append(f"  Fix: {f.suggested_fix}")
                    report_lines.append("")

        # Low confidence / gated
        if low_conf:
            report_lines.append(f"## Gated ({len(low_conf)} low-confidence items)")
            for f in low_conf:
                report_lines.append(f"- {f.short()}")

        # Summary counts
        by_sev = {}
        for f in dedup_result.merged:
            s = f.severity.value
            by_sev[s] = by_sev.get(s, 0) + 1
        report_lines.append("")
        report_lines.append("## Summary")
        for sev in ["P0", "P1", "P2", "P3"]:
            report_lines.append(f"- {sev}: {by_sev.get(sev, 0)}")

        report = "\n".join(report_lines)
        self.ws.write_output(run_id, stage_id, "review_report", report)
        yield {"type": "progress", "stage": stage_id,
               "msg": f"Review complete — {len(dedup_result.merged)} findings, "
                      f"{dedup_result.duplicates_removed} duplicates removed"}

    # ── Existing Pattern: Fanout ──

    def _exec_fanout(self, run_id, stage, idx):
        """Multi-round fanout + message bus routing"""
        stage_id = stage["id"]
        agents_cfg = stage["agents"]
        merge = stage.get("merge", {})

        all_agent_results: dict[str, DispatcherResult] = {}

        for round_num in range(1, self.MAX_ROUNDS + 1):
            yield {"type": "progress", "stage": stage_id,
                   "round": round_num, "agents": len(agents_cfg)}

            round_results: dict[str, DispatcherResult] = {}
            for agent_cfg in agents_cfg:
                role = agent_cfg["role"]
                defn = load_agent(role)
                tier = agent_cfg.get("model_assignment", {}).get("tier", "balanced")

                prev_output = all_agent_results.get(role, None)
                context = ""
                if prev_output:
                    context = f"Your previous round output length: {len(prev_output.output)} characters"

                bus_msgs = self.bus.build_cross_feed(stage_id)
                if not bus_msgs.strip():
                    bus_msgs = ""

                shared_ctx = self.ws.inject_shared_context(run_id)

                if round_num == 1:
                    base_ctx = f"Time range: {self.plan.get('recon_summary', {}).get('timeframe', '')}"
                    context = context + "\n" + base_ctx if context else base_ctx

                full_context = "\n".join(filter(None, [context, shared_ctx]))
                full_bus = bus_msgs if bus_msgs.strip() else ""

                result = self.dispatcher.run_agent(
                    defn, agent_cfg["goal"],
                    context=full_context,
                    tier=tier,
                    messages_from_bus=full_bus,
                )
                self._cost_tracker.record_result(role, result)
                if not result.success:
                    result = self.dispatcher.handle_failure(defn, agent_cfg["goal"],
                                                             full_context, result, tier)

                # ── Reentrant decomposition ──
                # If agent reports "marked for decomposition", split the subtask
                if not result.success and "decomposition" in (result.error or "").lower():
                    yield {"type": "progress", "stage": stage_id,
                           "msg": f"  🔄 分解任务: {role} 任务过大, 拆分为子任务"}
                    sub_results = self._decompose_goal(
                        run_id, stage_id, role, agent_cfg["goal"],
                        full_context, tier,
                    )
                    for sub_role, sub_result in sub_results:
                        round_results[f"{role}_sub_{sub_role}"] = sub_result
                        self._cost_tracker.record_result(f"{role}_sub_{sub_role}", sub_result)
                    # Skip writing original failed result
                    continue

                round_results[role] = result

                if result.claims:
                    msg = Message.discovery(
                        from_agent=role,
                        phase=stage_id,
                        round_num=round_num,
                        claims=result.claims,
                    )
                    self.bus.publish(msg)

                self.ws.write_output(run_id, stage_id, f"{role}_r{round_num}", result.output)

            for role, result in round_results.items():
                if role not in all_agent_results:
                    all_agent_results[role] = result
                elif result.output.strip():
                    all_agent_results[role] = result

            routing = self.bus.route(stage_id)
            has_cross_feed = bool(routing)
            if not has_cross_feed or round_num >= self.MAX_ROUNDS:
                break

        # Merge
        if merge.get("enabled"):
            merge_defn = load_agent(merge.get("agent_role", "merge-agent"))
            merge_tier = merge.get("model_assignment", {}).get("tier", "balanced")

            merge_ctx_parts = ["Merge the following search results:"]
            for role, result in all_agent_results.items():
                merge_ctx_parts.append(f"\n- {role}: {len(result.output)} characters")

            bus_summary = self.bus.build_cross_feed(stage_id)
            if bus_summary.strip():
                merge_ctx_parts.append(f"\n\nCross-agent findings:\n{bus_summary}")

            merge_result = self.dispatcher.run_agent(
                merge_defn,
                merge.get("context", ""),
                context="\n".join(merge_ctx_parts),
                tier=merge_tier,
            )
            self._cost_tracker.record_result("merge-agent", merge_result)
            self.ws.write_output(run_id, stage_id, "merged", merge_result.output)
            yield {"type": "progress", "stage": stage_id, "msg": "merge complete"}

        # Update summary
        summary = {
            "key_findings": [],
            "tokens_used": sum(r.tokens for r in all_agent_results.values()),
            "rounds": self.MAX_ROUNDS,
        }
        for role, result in all_agent_results.items():
            for claim in result.claims:
                summary["key_findings"].append(
                    f"{claim.get('statement', '')[:100]} ({role})"
                )

        status_path = self.ws.base / run_id / "status.json"
        if status_path.exists():
            data = json.loads(status_path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    s["summary"] = summary
            status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # ── Existing Pattern: Pipeline ──

    def _exec_pipeline(self, run_id, stage, idx):
        """Sequential execution"""
        stage_id = stage["id"]
        for i, agent_cfg in enumerate(stage.get("agents", [])):
            defn = load_agent(agent_cfg["role"])
            tier = agent_cfg.get("model_assignment", {}).get("tier", "balanced")

            prev_output = ""
            if i > 0:
                prev_role = stage["agents"][i - 1]["role"]
                prev_output = self.ws.read_output(run_id, stage_id, prev_role)

            result = self.dispatcher.run_agent(
                defn, agent_cfg["goal"],
                context=prev_output if prev_output else "",
                tier=tier,
            )
            self._cost_tracker.record_result(agent_cfg["role"], result)
            if not result.success:
                result = self.dispatcher.handle_failure(defn, agent_cfg["goal"],
                                                         prev_output, result, tier)
            self.ws.write_output(run_id, stage_id, agent_cfg["role"], result.output)
            yield {"type": "progress", "stage": stage_id, "agent": agent_cfg["role"]}

    # ── Existing Pattern: Producer-Reviewer ──

    def _exec_producer_reviewer(self, run_id, stage, idx):
        """Produce → review → cycle"""
        stage_id = stage["id"]
        agents_cfg = stage.get("agents", [])
        max_cycles = stage.get("max_cycles", 3)
        producer = agents_cfg[0] if agents_cfg else None
        reviewer = agents_cfg[1] if len(agents_cfg) > 1 else None

        if not producer:
            yield {"type": "error", "msg": "producer_reviewer requires at least 1 producer agent"}
            return

        for cycle in range(1, max_cycles + 1):
            p_defn = load_agent(producer["role"])
            p_tier = producer.get("model_assignment", {}).get("tier", "balanced")
            p_result = self.dispatcher.run_agent(p_defn, producer["goal"], tier=p_tier)
            self._cost_tracker.record_result(producer["role"], p_result)
            self.ws.write_output(run_id, stage_id, f"{producer['role']}_v{cycle}", p_result.output)
            yield {"type": "progress", "stage": stage_id, "cycle": cycle, "msg": "produced"}

            if reviewer:
                r_defn = load_agent(reviewer["role"])
                r_tier = reviewer.get("model_assignment", {}).get("tier", "balanced")
                r_ctx = f"Review the following content:\n{p_result.output[:2000]}"
                r_result = self.dispatcher.run_agent(r_defn, reviewer["goal"], r_ctx, tier=r_tier)
                self._cost_tracker.record_result(reviewer["role"], r_result)
                verdict = "PASS" if "PASS" in r_result.output else "FAIL"
                yield {"type": "progress", "stage": stage_id, "cycle": cycle, "verdict": verdict}
                if verdict == "PASS":
                    break

    # ── Reentrant Decomposition ──

    def _decompose_goal(self, run_id, stage_id, role, goal, context, tier):
        """
        当一个 agent 返回 reentrant/decomposition 时使用。
        调 Orchestrator 对子目标做 recon → 生成 sub-stages → 逐一 dispatch。

        返回: [(sub_role, DispatcherResult), ...]
        """
        from orbuz.core.orchestrator import Orchestrator
        sub_orch = Orchestrator(self.dispatcher.llm, agent_dir=str(self.ws.base))
        sub_plan = sub_orch.recon(
            topic=f"[子任务分解] {goal[:100]}",
            workflow_name=f"{stage_id}_{role}_sub",
        )
        sub_stages = sub_plan.get("plan", {}).get("stages", [])
        if not sub_stages:
            return []

        results = []
        for sub_stage in sub_stages:
            sub_pattern = sub_stage.get("pattern", "pipeline")
            if sub_pattern == "fanout":
                for ac in sub_stage.get("agents", []):
                    sub_role = ac["role"]
                    defn = load_agent(sub_role)
                    sub_result = self.dispatcher.run_agent(defn, ac["goal"], context=context, tier=tier)
                    self.ws.write_output(run_id, stage_id, f"{role}_sub_{sub_role}", sub_result.output)
                    results.append((sub_role, sub_result))
            elif sub_pattern == "pipeline":
                prev = ""
                for ac in sub_stage.get("agents", []):
                    sub_role = ac["role"]
                    defn = load_agent(sub_role)
                    sub_result = self.dispatcher.run_agent(defn, ac["goal"],
                                                          context=context + "\n" + prev if prev else context,
                                                          tier=tier)
                    self.ws.write_output(run_id, stage_id, f"{role}_sub_{sub_role}", sub_result.output)
                    results.append((sub_role, sub_result))
                    prev = sub_result.output

        return results

    # ── Internal ──


    def _get_comm(self, defn) -> CommunicationSpec:
        data = getattr(defn, 'communication', None) or {}
        if isinstance(data, dict) and data:
            return CommunicationSpec(data)
        return CommunicationSpec()

    def _handle_checkpoint_decision(self, run_id, decision):
        action = decision.get("action", "continue")
        if action == "stop":
            self.ws.set_state(run_id, "cancelled")
        elif action == "redirect":
            self.ws.set_continuation(run_id, decision.get("note", ""))
        elif action == "rerun":
            pass  # Simplified

    

    
    # ── Codegen pattern with YAML actions execution ──

    def _exec_codegen(self, run_id, stage, idx):
        """Codegen pattern: dispatch agents sequentially, execute actions from output.

        Each agent can output YAML actions blocks:
            ---actions---
            - write_file: path/to/file.rs
              content: |
                fn main() {}
            - run: cargo check
            ---
        """
        stage_id = stage['id']
        agents_cfg = stage.get('agents', [])
        project_dir = stage.get('project_dir', '.')
        project_path = Path(project_dir).resolve()

        yield {'type': 'progress', 'stage': stage_id,
               'msg': f'Starting codegen ({len(agents_cfg)} agents)'}

        prev_output = ''
        for i, agent_cfg in enumerate(agents_cfg):
            role = agent_cfg['role']
            defn = load_agent(role)
            tier = agent_cfg.get('model_assignment', {}).get('tier', 'balanced')

            yield {'type': 'progress', 'stage': stage_id,
                   'msg': f'Running {role}... ({i+1}/{len(agents_cfg)})'}

            result = self.dispatcher.run_agent(
                defn, agent_cfg['goal'],
                context=prev_output if prev_output else '',
                tier=tier,
            )
            self._cost_tracker.record_result(role, result)
            self.ws.write_output(run_id, stage_id, role, result.output)

            actions = self._exec_actions(result.output, project_path)
            if actions:
                yield {'type': 'progress', 'stage': stage_id,
                       'msg': f'  -> executed {len(actions)} actions from {role}'}

            prev_output = result.output

        yield {'type': 'progress', 'stage': stage_id, 'msg': 'codegen complete'}

    @staticmethod
    def _exec_actions(agent_output: str, project_path: Path) -> list[dict]:
        """Parse and execute YAML actions blocks from agent output.

        Format (YAML-like, parsed without pyyaml):
            ---actions---
            - write_file: path/to/file.rs
              content: |
                file content lines...
            - run: cargo check
            ---

        Supported actions: write_file, run, append_file, delete, rename.
        Returns list of {type, path|command, ...} results.
        """
        import re as _re
        import subprocess as _sp
        import shutil as _sh

        results = []
        pat_start = r'^---actions---\s*$(.+?)^---\s*$'
        block_re = _re.compile(pat_start, _re.MULTILINE | _re.DOTALL)

        for match in block_re.finditer(agent_output):
            body = match.group(1).strip()
            raw_actions = _re.split(r'\n(?=\s*- )', body)
            for raw in raw_actions:
                raw = raw.strip()
                if not raw or raw.startswith('#'):
                    continue
                raw = _re.sub(r'^\s*-\s*', '', raw, count=1)
                lines = raw.split('\n')
                if not lines:
                    continue
                first = lines[0].strip()
                if ':' not in first:
                    continue
                action_type, value = first.split(':', 1)
                action_type = action_type.strip()
                value = value.strip()
                if not action_type:
                    continue

                fields = {}
                content_lines = []
                in_content = False
                content_indent = None
                for line in lines[1:]:
                    if in_content:
                        stripped = line.rstrip()
                        if stripped and content_indent is not None:
                            indent = len(line) - len(line.lstrip())
                            if indent < content_indent and not line.strip().startswith('#'):
                                if stripped:
                                    if ':' in stripped and indent < 4:
                                        k, v = stripped.split(':', 1)
                                        fields[k.strip()] = v.strip()
                                        in_content = False
                                        content_indent = None
                                        continue
                            content_lines.append(line)
                        elif stripped:
                            content_lines.append(line)
                        else:
                            content_lines.append('')
                        continue
                    stripped = line.rstrip()
                    if stripped.endswith('|') and ':' in stripped:
                        in_content = True
                        content_lines = []
                        content_indent = None
                    elif ':' in stripped:
                        k, v = stripped.split(':', 1)
                        fields[k.strip()] = v.strip()

                try:
                    if action_type == 'write_file':
                        file_path = project_path / value
                        content = '\n'.join(content_lines)
                        content = _trim_content(content)
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        file_path.write_text(content)
                        results.append({'type': 'write_file', 'path': str(file_path)})

                    elif action_type == 'append_file':
                        file_path = project_path / value
                        content = '\n'.join(content_lines)
                        content = _trim_content(content)
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(file_path, 'a') as f:
                            f.write(content)
                        results.append({'type': 'append_file', 'path': str(file_path)})

                    elif action_type == 'run':
                        cmd = value or fields.get('command', '')
                        if not cmd:
                            continue
                        sp = _sp.run(
                            cmd, shell=True, capture_output=True, text=True,
                            cwd=str(project_path), timeout=120,
                        )
                        out = sp.stdout[-500:] if sp.stdout else ''
                        err = sp.stderr[-500:] if sp.stderr else ''
                        results.append({
                            'type': 'run', 'command': cmd,
                            'stdout': out, 'stderr': err,
                            'exit_code': sp.returncode,
                        })

                    elif action_type == 'delete':
                        target = project_path / value
                        if target.exists():
                            if target.is_file():
                                target.unlink()
                            elif target.is_dir():
                                _sh.rmtree(target)
                            results.append({'type': 'delete', 'path': value})

                    elif action_type == 'rename':
                        to_path = fields.get('to', '')
                        if to_path:
                            src = project_path / value
                            dst = project_path / to_path
                            if src.exists():
                                src.rename(dst)
                                results.append({'type': 'rename', 'from': value, 'to': to_path})

                except Exception as e:
                    results.append({'type': 'error', 'action': action_type, 'error': str(e)})

        return results


def _trim_content(content: str) -> str:
    """Trim trailing empty lines and dedent content."""
    _nl = chr(92) + chr(110)
    lines = content.split(_nl)
    while lines and not lines[-1].strip():
        lines.pop()
    # Dedent: find common leading whitespace
    non_empty = [l for l in lines if l.strip()]
    if non_empty:
        indent = min(len(l) - len(l.lstrip()) for l in non_empty)
        if indent:
            lines = [l[indent:] if l.strip() else l for l in lines]
    return _nl.join(lines)