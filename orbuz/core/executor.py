"""
Executor — plan.json Executor
==============================
Input: approved plan.json + LLMClient
Flow: iterate stages → dispatch by pattern → checkpoint → continue

Key improvements (this iteration):
  - Multi-round fanout: Round 1 output → bus.route() → Round 2 injects cross-feed
  - Discovery messages are automatically published to the bus
  - Agent communication registration
  - Escalation chain integration

All LLM calls go through Dispatcher + LLMClient, never directly to the API.
Mock mode and real mode share the same code path.
"""

from orbuz.core.dispatcher import Dispatcher, DispatcherResult
from orbuz.core.shell_runner import ShellRunner, ShellResult
from orbuz.agent.message import MessageBus, Message, CommunicationSpec
from orbuz.workspace.manager import WorkspaceManager
from orbuz.llm.client import LLMClient
from orbuz.schema.agent import load_agent, ModelHint


class Executor:
    """
    Reads plan.json → executes stages → checkpoints → delivers

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

    def __init__(self, plan: dict, llm_client: LLMClient):
        self.plan = plan
        self.dispatcher = Dispatcher(llm_client)
        self.ws = WorkspaceManager()
        self.bus = None  # Created in init_run
        self._decision = None

    # ── Main entry ──

    def run(self):
        """
        Generator: yields events.
        Caller receives checkpoint and calls continue_with() with user decision.
        """
        stages = self.plan["plan"]["stages"]
        run_id = self.ws.init_run(self.plan)
        self.bus = MessageBus(
            workspace_dir=str(self.ws.base / run_id)
        )

        # Register all agents' communication capabilities
        for stage in stages:
            for agent_cfg in stage.get("agents", []):
                defn = load_agent(agent_cfg["role"])
                comm_data = self._get_comm(defn)
                self.bus.register_agent(agent_cfg["role"], comm_data)

        self.ws.set_state(run_id, "executing")

        for idx, stage in enumerate(stages):
            self.ws.set_current_stage(run_id, idx)
            stage_id = stage["id"]

            # Validate dependencies
            for dep_id in stage.get("depends_on", []):
                if self.ws.get_stage_status(run_id, dep_id) != "completed":
                    yield {"type": "error", "msg": f"Dependency {dep_id} not completed"}
                    return

            # Execute
            if stage["pattern"] == "fanout":
                yield from self._exec_fanout(run_id, stage, idx)
            elif stage["pattern"] == "pipeline":
                yield from self._exec_pipeline(run_id, stage, idx)
            elif stage["pattern"] == "producer_reviewer":
                yield from self._exec_producer_reviewer(run_id, stage, idx)
            else:
                yield {"type": "error", "msg": f"Unknown pattern: {stage.get('pattern')}"}
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
                # If caller passed a decision (sync mode), handle it directly
                if decision:
                    self._handle_checkpoint_decision(run_id, decision)

                # Wait for async decision (generator mode)
                while self._decision is None:
                    yield {"type": "waiting"}
                self._handle_checkpoint_decision(run_id, self._decision)
                self._decision = None

        # Done
        self.ws.set_state(run_id, "completed")
        yield {"type": "done", "run_id": run_id, "output_path": f"_workspace/{run_id}/deliver/"}

    def continue_with(self, decision: dict):
        """Called after a checkpoint"""
        self._decision = decision

    # ── Pattern execution ──

    def _exec_fanout(self, run_id, stage, idx):
        """Multi-round fanout + message bus routing"""
        stage_id = stage["id"]
        agents = stage["agents"]
        merge = stage.get("merge", {})

        all_agent_results = {}

        for round_num in range(1, self.MAX_ROUNDS + 1):
            yield {"type": "progress", "stage": stage_id,
                   "round": round_num, "agents": len(agents)}

            # Build context for each agent this round
            round_results = {}
            for agent_cfg in agents:
                role = agent_cfg["role"]
                defn = load_agent(role)
                tier = agent_cfg.get("model_assignment", {}).get("tier", "balanced")

                # If previous round results exist, inject them into context
                prev_output = all_agent_results.get(role, None)
                context = ""
                if prev_output:
                    context = f"Your previous round output length: {len(prev_output.output)} characters"

                # Get relevant messages from the bus
                bus_msgs = self.bus.build_cross_feed(stage_id)
                if not bus_msgs.strip():
                    bus_msgs = ""

                # Inject shared space file list
                shared_ctx = self.ws.inject_shared_context(run_id)

                # First execution: include the goal-specified context
                if round_num == 1:
                    base_ctx = f"Time range: {self.plan.get('recon_summary', {}).get('timeframe', '')}"
                    context = context + "\n" + base_ctx if context else base_ctx

                # Merge context + shared + bus
                full_context = "\n".join(filter(None, [context, shared_ctx]))
                full_bus = bus_msgs if bus_msgs.strip() else ""

                # Execute
                result = self.dispatcher.run_agent(
                    defn, agent_cfg["goal"],
                    context=full_context,
                    tier=tier,
                    messages_from_bus=full_bus,
                )

                if not result.success:
                    result = self.dispatcher.handle_failure(defn, agent_cfg["goal"],
                                                             full_context, result, tier)

                round_results[role] = result

                # Publish claims to the bus
                if result.claims:
                    msg = Message.discovery(
                        from_agent=role,
                        phase=stage_id,
                        round_num=round_num,
                        claims=result.claims,
                    )
                    self.bus.publish(msg)

                # Write output to workspace
                self.ws.write_output(run_id, stage_id, f"{role}_r{round_num}", result.output)

            # Merge this round's results into the overall results
            for role, result in round_results.items():
                if role not in all_agent_results:
                    all_agent_results[role] = result
                else:
                    # Keep the most recent non-empty result
                    if result.output.strip():
                        all_agent_results[role] = result

            # Determine if another round is needed
            routing = self.bus.route(stage_id)
            has_cross_feed = bool(routing)
            if not has_cross_feed or round_num >= self.MAX_ROUNDS:
                break

        # Merge
        if merge.get("enabled"):
            merge_defn = load_agent(merge.get("agent_role", "merge-agent"))
            merge_tier = merge.get("model_assignment", {}).get("tier", "balanced")

            # Build merge context: list all output paths
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
            self.ws.write_output(run_id, stage_id, "merged", merge_result.output)
            yield {"type": "progress", "stage": stage_id, "msg": "merge complete"}

        # Update stage summary
        summary = {
            "key_findings": [],
            "tokens_used": sum(r.tokens for r in all_agent_results.values()),
            "rounds": self.MAX_ROUNDS,
        }
        # Extract key_findings from claims
        for role, result in all_agent_results.items():
            for claim in result.claims:
                summary["key_findings"].append(
                    f"{claim.get('statement', '')[:100]} ({role})"
                )
        # Update status.json (via workspace manager)
        import json
        status_path = self.ws.base / run_id / "status.json"
        if status_path.exists():
            data = json.loads(status_path.read_text())
            for s in data.get("stages", []):
                if s["id"] == stage_id:
                    s["summary"] = summary
            status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _exec_pipeline(self, run_id, stage, idx):
        """Sequential execution with optional error_handler self-healing loop.

        When an agent has error_handler configured (run_command, max_retries > 1,
        fallback_role, or non-default pass_condition), uses _run_retry_loop
        instead of single-shot execution.
        """
        stage_id = stage["id"]
        for i, agent_cfg in enumerate(stage.get("agents", [])):
            defn = load_agent(agent_cfg["role"])
            tier = agent_cfg.get("model_assignment", {}).get("tier", "balanced")

            prev_output = ""
            if i > 0:
                prev_role = stage["agents"][i - 1]["role"]
                prev_output = self.ws.read_output(run_id, stage_id, prev_role)

            # Check if error_handler is configured
            eh = agent_cfg.get("error_handler", {}) or {}
            has_eh = bool(
                eh.get("run_command")
                or eh.get("max_retries", 0) > 1
                or eh.get("fallback_role")
                or eh.get("pass_condition", "") not in ("", "exit_code == 0")
            )

            if has_eh:
                # Self-healing loop
                final = yield from self._run_retry_loop(
                    run_id, stage_id, i, agent_cfg, defn, tier, prev_output
                )
                result = final.get("result", DispatcherResult(success=False))
                status = final.get("status", "failed")
                retries = final.get("retries", 0)
                self.ws.write_output(run_id, stage_id, agent_cfg["role"],
                                     result.output)
                yield {
                    "type": "progress", "stage": stage_id,
                    "agent": agent_cfg["role"], "status": status,
                    "retries": retries, "tokens": result.tokens,
                }
            else:
                # Simple single-shot (original behavior)
                result = self.dispatcher.run_agent(
                    defn, agent_cfg["goal"],
                    context=prev_output if prev_output else "",
                    tier=tier,
                )
                if not result.success:
                    result = self.dispatcher.handle_failure(
                        defn, agent_cfg["goal"], prev_output, result, tier
                    )

                self.ws.write_output(run_id, stage_id, agent_cfg["role"],
                                     result.output)
                yield {"type": "progress", "stage": stage_id,
                       "agent": agent_cfg["role"]}

    def _exec_producer_reviewer(self, run_id, stage, idx):
        """Produce → review → cycle"""
        stage_id = stage["id"]
        agents = stage.get("agents", [])
        max_cycles = stage.get("max_cycles", 3)
        producer = agents[0] if agents else None
        reviewer = agents[1] if len(agents) > 1 else None

        if not producer:
            yield {"type": "error", "msg": "producer_reviewer requires at least 1 producer agent"}
            return

        for cycle in range(1, max_cycles + 1):
            # Produce
            p_defn = load_agent(producer["role"])
            p_tier = producer.get("model_assignment", {}).get("tier", "balanced")
            p_result = self.dispatcher.run_agent(p_defn, producer["goal"], tier=p_tier)
            self.ws.write_output(run_id, stage_id, f"{producer['role']}_v{cycle}", p_result.output)
            yield {"type": "progress", "stage": stage_id, "cycle": cycle, "msg": "produced"}

            # Review
            if reviewer:
                r_defn = load_agent(reviewer["role"])
                r_tier = reviewer.get("model_assignment", {}).get("tier", "balanced")
                r_ctx = f"Review the following content:\n{p_result.output[:2000]}"
                r_result = self.dispatcher.run_agent(r_defn, reviewer["goal"], r_ctx, tier=r_tier)
                verdict = "PASS" if "PASS" in r_result.output else "FAIL"
                yield {"type": "progress", "stage": stage_id, "cycle": cycle, "verdict": verdict}
                if verdict == "PASS":
                    break

    # ── Self-healing retry loop ──

    def _run_retry_loop(self, run_id, stage_id, agent_idx, agent_cfg,
                         defn, tier, prev_output, max_cycles=3):
        """
        Execute an agent with error_handler: run → check → retry/escalate → loop.

        Yields progress events, returns final dispatcher result + metadata.
        """
        eh = agent_cfg.get("error_handler", {}) or {}
        on_fail = eh.get("on_fail", "retry")
        max_retries = eh.get("max_retries", 3)
        retry_role = eh.get("retry_role", "") or agent_cfg["role"]
        fallback_role = eh.get("fallback_role", "")
        pass_condition = eh.get("pass_condition", "exit_code == 0")
        run_command = eh.get("run_command", "")
        input_from_role = eh.get("input_from_role", "")

        # Determine the actual role to use for retries
        current_role = agent_cfg["role"]

        attempts = 0
        last_result = None
        last_shell = None
        last_output = prev_output

        for attempt in range(1, max_retries + 1):
            attempts = attempt
            role_to_run = current_role

            # If this is a retry attempt and retry_role differs, switch roles
            if attempt > 1 and retry_role and retry_role != current_role:
                role_to_run = retry_role

            # Load agent def for the role we're actually using
            try:
                run_defn = load_agent(role_to_run) if role_to_run != defn.name else defn
            except Exception:
                run_defn = defn  # fallback

            # Build context with previous failure info
            context = last_output
            if last_shell and not last_shell.exit_code == 0:
                context += (
                    f"\n\n## Previous Run Output (exit={last_shell.exit_code})\n"
                    f"{last_shell.output[:2000]}"
                )

            # Dispatch agent
            result = self.dispatcher.run_agent(
                run_defn, agent_cfg["goal"],
                context=context,
                tier=tier,
            )
            if not result.success:
                result = self.dispatcher.handle_failure(
                    run_defn, agent_cfg["goal"], context, result, tier
                )

            last_result = result
            self.ws.write_output(run_id, stage_id, f"{role_to_run}_a{attempt}",
                                 result.output)
            yield {
                "type": "progress", "stage": stage_id,
                "agent": role_to_run, "attempt": attempt,
                "tokens": result.tokens, "cost": result.tokens * 0.00005,
            }

            # If no run_command check, consider agent output success as pass
            if not run_command:
                if result.success and result.output.strip():
                    return {"result": result, "retries": attempt - 1,
                            "status": "ok", "role": role_to_run}

            # Execute shell command (compile, test, etc.)
            if run_command:
                # Inject agent output if command has placeholder
                cmd = run_command
                if "{output}" in cmd:
                    cmd = cmd.replace("{output}",
                                      f"{result.output[:3000]}")
                last_shell = ShellRunner.run(cmd, timeout=eh.get("timeout", 60))

                # Write shell output
                self.ws.write_output(run_id, stage_id,
                                     f"{role_to_run}_a{attempt}_shell",
                                     last_shell.output)

                # Evaluate pass condition
                passed, reason = ShellRunner.check_condition(
                    pass_condition, last_shell, result.output
                )
                yield {
                    "type": "progress", "stage": stage_id,
                    "agent": role_to_run, "attempt": attempt,
                    "shell_exit": last_shell.exit_code,
                    "passed": passed, "reason": reason,
                }

                if passed:
                    return {"result": result, "shell": last_shell,
                            "retries": attempt - 1, "status": "ok",
                            "role": role_to_run}

                # Save shell output for next attempt's context
                last_output = (
                    f"## Previous Attempt #{attempt} ({role_to_run})\n"
                )
                last_output += f"Agent output ({len(result.output)} chars):\n"
                last_output += f"{result.output[:1000]}\n"
                last_output += f"\nShell command: {cmd}\n"
                last_output += f"Exit code: {last_shell.exit_code}\n"
                if last_shell.output:
                    last_output += f"Output:\n{last_shell.output[:2000]}\n"

        # --- All attempts exhausted ---
        # Try fallback_role if configured
        if fallback_role and fallback_role != current_role:
            yield {
                "type": "progress", "stage": stage_id,
                "msg": f"All {max_retries} attempts failed, escalating to {fallback_role}",
            }
            try:
                fb_defn = load_agent(fallback_role)
                fb_context = (
                    f"Previous agent ({current_role}) failed after "
                    f"{max_retries} attempts.\n"
                    f"Last error:\n{last_shell.output if last_shell else ''}\n"
                    f"\n{prev_output}"
                )
                fb_result = self.dispatcher.run_agent(
                    fb_defn, agent_cfg["goal"],
                    context=fb_context, tier=tier,
                )
                self.ws.write_output(run_id, stage_id,
                                     f"{fallback_role}_fallback", fb_result.output)
                return {"result": fb_result, "retries": max_retries,
                        "status": "escalated", "role": fallback_role}
            except Exception as e:
                yield {"type": "error", "msg": f"Fallback failed: {e}"}

        # Give up
        return {"result": last_result, "retries": max_retries,
                "status": "failed", "shell": last_shell, "role": current_role}

    # ── Internal ──

    def _get_comm(self, defn) -> CommunicationSpec:
        """Extract the communication field from the agent definition"""
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
            pass  # Simplified handling


if __name__ == "__main__":
    from orbuz.llm.client import LLMClient
    from orbuz.schema.plan import PlanJSON

    llm = LLMClient({"balanced": "mock", "cheap": "mock"}, mock=True)
    plan = PlanJSON.sample().model_dump()
    exe = Executor(plan, llm_client=llm)

    for event in exe.run():
        if event["type"] == "progress":
            print(f"  📡 {event}")
        elif event["type"] == "checkpoint":
            print(f"\n⏸  Checkpoint: {event['stage_name']}")
            exe.continue_with({"action": "continue"})
        elif event["type"] == "done":
            print(f"\n✅ Done: {event['output_path']}")
        elif event["type"] == "waiting":
            break  # Simplified: synchronous mode just exits
