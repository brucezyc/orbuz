"""
Dispatcher — Sub-Agent Dispatcher
====================================
Input: (agent definition + goal + context + model_tier) + LLMClient
Flow: build system prompt → call LLM → parse claims + findings → return results

Extended with Compound Engineering structured findings support:
- Finding extraction from JSON blocks
- JSON output contract enforcement
- Merge-dedup for multi-agent findings
"""
from __future__ import annotations
import json
import re
import textwrap
from orbuz.schema.agent import AgentDefinition, ModelHint
from orbuz.schema.finding import Finding, FindingSet, MergeDedupResult, Severity, AutofixClass
from orbuz.llm.client import LLMClient, LLMResponse
from orbuz.core.plugin import get_registry
from pathlib import Path
from dataclasses import dataclass, field


# ── Working Checkpoint ──
# Short-term memory for sub-agents across multiple tool-calling rounds.
# Inspired by GenericAgent's update_working_checkpoint pattern.
# A <200 token scratchpad that gets injected into system prompt each turn.
# The sub-agent can update it via update_checkpoint tool.

_CHECKPOINT_FILE = "_checkpoint.md"


@dataclass
class WorkingCheckpoint:
    """Sub-agent working memory across tool-calling rounds."""
    content: str = ""
    path: Path | None = None

    @classmethod
    def from_workspace(cls, workspace_dir: str | Path | None) -> "WorkingCheckpoint":
        """Load from workspace dir, or return empty."""
        if workspace_dir:
            p = Path(workspace_dir) / _CHECKPOINT_FILE
            if p.exists():
                return cls(content=p.read_text("utf-8").strip(), path=p)
        return cls()

    def save(self):
        """Write checkpoint to disk."""
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.content, "utf-8")

    def inject_prompt(self) -> str:
        """Return checkpoint section for system prompt injection."""
        if not self.content:
            return ""
        return (
            "\n## Working Checkpoint (ongoing task context)\n"
            f"{self.content}\n\n"
            "Keep this checkpoint updated as you make progress. "
            "You can update it using the update_checkpoint tool."
        )

    def update(self, key_info: str):
        """Replace checkpoint content (agent manages what to keep/remove)."""
        self.content = key_info.strip()[:500]  # 500 char safety limit
        self.save()


_plugins = get_registry()


def merge_dedup_findings(sets: list[FindingSet]) -> MergeDedupResult:
    """
    Merge multiple FindingSets, deduplicating by (file, line, title).
    On conflict: higher severity wins.
    """
    seen: dict[tuple, Finding] = {}
    duplicates = 0
    overrides = 0

    for fs in sets:
        for f in fs.findings:
            key = (f.file, f.line, f.title.lower() if f.title else "")
            if key in seen:
                existing = seen[key]
                duplicates += 1
                # Higher severity wins
                sev_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
                if sev_order.get(f.severity.value, 99) < sev_order.get(existing.severity.value, 99):
                    seen[key] = f
                    overrides += 1
                # Higher confidence breaks ties
                elif (sev_order.get(f.severity.value, 99)
                      == sev_order.get(existing.severity.value, 99)
                      and f.confidence > existing.confidence):
                    seen[key] = f
                    overrides += 1
            else:
                seen[key] = f

    return MergeDedupResult(
        merged=list(seen.values()),
        duplicates_removed=duplicates,
        severity_overrides=overrides,
    )


class DispatcherResult:
    """Result of a single delegate call — extended with findings."""

    def __init__(self, success: bool, output: str = "",
                 claims: list[dict] = None, tier_used: str = "",
                 model_used: str = "", duration_s: float = 0.0,
                 tokens: int = 0, input_tokens: int = 0, output_tokens: int = 0,
                 error: str = "",
                 cost_usd: float = 0.0,
                 findings: FindingSet | None = None):
        self.success = success
        self.output = output
        self.claims = claims or []
        self.tier_used = tier_used
        self.model_used = model_used
        self.duration_s = duration_s
        self.tokens = tokens
        self.input_tokens = input_tokens or tokens
        self.output_tokens = output_tokens or tokens
        self.error = error
        self.cost_usd = cost_usd
        self.findings = findings or FindingSet(persona="")


class Dispatcher:
    """
    Dispatch and run sub-agents.

    Usage:
        d = Dispatcher(llm_client)
        result = d.run_agent(agent_def, goal, context, tier="cheap")
        if not result.success:
            result = d.handle_failure(agent_def, goal, context, result)

    Supports MCP tool injection: if an agent definition includes mcp_tools,
    those tools are called BEFORE the LLM and their results injected as context.
    """

    def __init__(self, llm_client: LLMClient, mcp_manager=None):
        self.llm = llm_client
        self.mcp = mcp_manager

    @staticmethod
    def _extract_text_tool_calls(text: str) -> list[dict]:
        """Parse text-based tool calls (XML or JSON action blocks) from LLM output.
        
        Handles two formats:
        1. XML: <tool_call name="read_file"><path>...</path></tool_call>
        2. JSON: {"action": "read_file", "path": "..."} inside ```json blocks or bare
        """
        import re
        results = []
        if not text.strip():
            return results
        
        # Format 1: XML tool_call blocks
        xml_pattern = re.compile(
            r'<tool_call\s+name=["\'](\w+)["\']>(.*?)</tool_call>',
            re.DOTALL
        )
        for match in xml_pattern.finditer(text):
            name = match.group(1)
            inner = match.group(2).strip()
            args = {}
            # Parse inner XML params: <param>value</param> or <param_name>value</param_name>
            for param_match in re.finditer(r'<(\w+)>(.*?)</\1>', inner, re.DOTALL):
                args[param_match.group(1)] = param_match.group(2).strip()
            results.append({"name": name, "args": args})
        
        # Format 1b: XML self-closing <read_file path="..."> or <terminal command="...">
        self_close = re.compile(
            r'<(\w+)\s+((?:\w+=["\'][^"\']*["\']\s*)+)\s*/?>',
            re.DOTALL
        )
        for match in self_close.finditer(text):
            name = match.group(1)
            attrs = match.group(2)
            args = {}
            for attr_match in re.finditer(r'(\w+)=["\']([^"\']*)["\']', attrs):
                args[attr_match.group(1)] = attr_match.group(2)
            results.append({"name": name, "args": args})
        
        # Format 2: JSON action blocks
        json_blocks = re.finditer(
            r'(?:```json\s*)?\{\s*"action"\s*:\s*"(\w+)"\s*.*?\}(?:\s*```)?',
            text, re.DOTALL
        )
        for match in json_blocks:
            try:
                data = json.loads(re.search(r'\{.*\}', match.group(), re.DOTALL).group())
                name = data.pop("action", "")
                if name:
                    results.append({"name": name, "args": data})
            except (json.JSONDecodeError, AttributeError):
                pass
        
        return results

    def run_agent(self, agent_def: AgentDefinition, goal: str,
                  context: str = "", tier: str = "balanced",
                  messages_from_bus: str = "",
                  require_structured_findings: bool = False) -> DispatcherResult:
        """
        Run a sub-agent.

        agent_def: agent.yaml definition
        goal:      specific task description for this agent
        context:   context (prior output, user injection, etc.)
        tier:      model tier (cheap/balanced/quality)
        messages_from_bus: discovery summaries from other agents
        require_structured_findings: if True, enforce JSON findings output contract
                     (uses response_format for guaranteed JSON output)

        Returns DispatcherResult containing output + claims + findings.
        """
        model_name = self.llm.get_model_name(tier)

        use_structured = require_structured_findings and agent_def.output_contract.produces_findings

        if use_structured:
            system = self._build_reviewer_prompt(agent_def)
        else:
            system = self._build_system_prompt(agent_def)

        # ── MCP tool injection ──
        # Pre-fetch MCP tool results and inject into context
        mcp_context = self._resolve_mcp_tools(agent_def, goal, context)
        full_context = context
        if mcp_context:
            full_context = (context + "\n\n" + mcp_context) if context else mcp_context

        # ── Plugin hook: before_agent_run ──
        hook_context = _plugins.run("before_agent_run", full_context, goal, agent_def)
        if hook_context:
            full_context = hook_context

        user = self._build_user_prompt(agent_def, goal, full_context, messages_from_bus)

        # Use response_format for structured JSON output when the model supports it
        # (OpenAI-compatible APIs). Avoid for Anthropic native API which lacks this.
        resolved = self.llm.catalog.resolve(self.llm.get_model_name(tier))
        use_json_mode = use_structured and resolved and resolved.is_openai_compatible

        response_format = {"type": "json_object"} if use_json_mode else None

        resp = self.llm.chat(
            model_tier=tier,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_format=response_format,
        )

        # Extract structured claims
        claims = self._extract_claims(resp.content, agent_def.name)

        # Extract structured findings
        # In json_object mode, the entire response is valid JSON (with prose in system prompt)
        # so we try direct JSON parse first, then fall back to regex extraction
        if use_structured and resp.content.strip().startswith("{"):
            try:
                data = json.loads(resp.content)
                findings = FindingSet.from_data(data, persona=agent_def.name)
                if findings and findings.findings:
                    pass  # Successfully parsed from json_object mode
                else:
                    findings = self._extract_findings(resp.content, agent_def.name)
            except (json.JSONDecodeError, TypeError):
                findings = self._extract_findings(resp.content, agent_def.name)
        else:
            findings = self._extract_findings(resp.content, agent_def.name)

        # If json_object mode was used but no findings found via extraction,
        # try treating the entire response as a findings JSON document
        if use_json_mode and (not findings or not findings.findings):
            try:
                data = json.loads(resp.content)
                findings = FindingSet.from_data(data, persona=agent_def.name)
            except (json.JSONDecodeError, TypeError):
                pass

        result = DispatcherResult(
            success=resp.success,
            output=resp.content,
            claims=claims,
            findings=findings,
            tier_used=tier,
            model_used=model_name,
            duration_s=resp.duration_s,
            tokens=resp.input_tokens + resp.output_tokens,
            cost_usd=resp.cost_usd,
            error=resp.error or "",
        )

        # ── Plugin hook: after_agent_run ──
        _plugins.run("after_agent_run", agent_def, result)

        return result

    def _compress_messages(self, messages: list[dict], system: str, tier: str) -> list[dict]:
        """Summarize oldest messages to keep conversation window manageable.

        After many tool-calling rounds, the accumulated message history
        grows large. This method takes the first N messages, sends them
        to a cheap model for summarization, and replaces them with a
        single compressed message.
        """
        if len(messages) < 6:
            return messages

        # Keep the last 4 exchanges (user+assistant+tool triples), summarize the rest
        keep = messages[-4:]
        to_compress = messages[:-4]

        # Build a condensed summary from what's being removed
        summary_parts = []
        for msg in to_compress:
            role = msg.get("role", "?")
            content = str(msg.get("content", ""))[:100]
            if role == "tool" and content:
                # Tool results: just note what tool and approximate result
                name = msg.get("name", "?")
                summary_parts.append(f"[tool:{name}] {content[:80]}")
            elif role == "assistant" and content:
                tc = msg.get("tool_calls")
                if tc:
                    names = [t["function"]["name"] for t in tc]
                    summary_parts.append(f"[assistant → {', '.join(names)}]")
                else:
                    summary_parts.append(f"[assistant] {content[:80]}")
            elif role == "user" and content:
                summary_parts.append(f"[user] {content[:80]}")

        compressed = "\n".join(summary_parts)
        # Truncate to ~500 chars
        if len(compressed) > 500:
            compressed = compressed[:497] + "..."

        summary_msg = {
            "role": "user",
            "content": (
                "[Previous conversation compressed]\n"
                f"Summary of earlier rounds:\n{compressed}\n\n"
                "Continue from where you left off. You still have all tools available."
            ),
        }
        return [summary_msg] + keep

    def run_agent_with_tools(
        self, agent_def: AgentDefinition, goal: str,
        context: str = "", tier: str = "balanced",
        messages_from_bus: str = "",
        tools: list[dict] | None = None,
        project_path: str | None = None,
        workspace_dir: str | None = None,
        max_tool_rounds: int = 25,
        max_cost_usd: float = 0.0,
        auto_git: bool = False,
        progress_callback: callable = None,
    ) -> DispatcherResult:
        """
        Run a sub-agent with native function calling (tool loop).

        Instead of requiring the agent to output ---actions--- blocks,
        this method:
          1. Calls the LLM with tool schemas
          2. If the LLM calls a tool, executes it and feeds the result back
          3. Repeats until the LLM responds with content (no tool calls)
          4. Enforces budget (max_cost_usd), auto git commit, and max rounds

        Args:
            tools: OpenAI-format tool schemas (from orbuz.codegen.tools.TOOL_SCHEMAS)
            project_path: Project root for file/terminal tool resolution
            max_tool_rounds: Max tool-call iterations (safety limit)
            max_cost_usd: Hard budget cap (0 = no limit)
            auto_git: If True, auto git add+commit after each tool round
        """
        from orbuz.codegen.tools import dispatch as tool_dispatch
        from orbuz.codegen.tools import set_default_project_path

        if project_path:
            set_default_project_path(project_path)

        model_name = self.llm.get_model_name(tier)
        system = self._build_system_prompt(agent_def)

        # ── Working Checkpoint ──
        checkpoint = WorkingCheckpoint.from_workspace(workspace_dir)
        if checkpoint.content:
            system += checkpoint.inject_prompt()

        mcp_context = self._resolve_mcp_tools(agent_def, goal, context)
        full_context = context
        if mcp_context:
            full_context = (context + "\n\n" + mcp_context) if context else mcp_context

        user = self._build_user_prompt(agent_def, goal, full_context, messages_from_bus)

        messages: list[dict] = [{"role": "user", "content": user}]
        total_in_tokens = 0
        total_out_tokens = 0
        total_cost = 0.0
        all_claims: list[dict] = []
        all_output_parts: list[str] = []  # accumulate content from all rounds
        tool_calls_made = 0
        _compress_trigger = 15  # compress after 15 rounds

        for round_num in range(max_tool_rounds):
            # ── Conversation compression: after 15+ rounds, summarize old history ──
            if round_num >= _compress_trigger and round_num % 5 == 0:
                messages = self._compress_messages(messages, system, tier)

            resp = self.llm.chat(
                model_tier=tier,
                system=system,
                messages=messages,
                tools=tools,
            )

            total_in_tokens += resp.input_tokens
            total_out_tokens += resp.output_tokens
            total_cost += resp.cost_usd

            # ── Budget cap ──
            if max_cost_usd > 0 and total_cost >= max_cost_usd:
                if progress_callback:
                    progress_callback(agent_def.name, round_num+1, tool_calls_made, "budget_exhausted",
                                      f"${total_cost:.4f} >= ${max_cost_usd:.2f}")
                return DispatcherResult(
                    success=True,
                    output="\n\n".join(all_output_parts) or f"[Budget exhausted: ${total_cost:.4f} >= ${max_cost_usd:.2f}]",
                    claims=all_claims,
                    tier_used=tier,
                    model_used=model_name,
                    tokens=total_in_tokens + total_out_tokens,
                    input_tokens=total_in_tokens,
                    output_tokens=total_out_tokens,
                    cost_usd=total_cost,
                )

            if not resp.success:
                return DispatcherResult(
                    success=False,
                    output=resp.content,
                    error=resp.error or "LLM call failed",
                    tier_used=tier,
                    model_used=model_name,
                    tokens=total_in_tokens + total_out_tokens,
                    input_tokens=total_in_tokens,
                    output_tokens=total_out_tokens,
                    cost_usd=total_cost,
                )

            # Extract claims from any content produced
            if resp.content:
                claims = self._extract_claims(resp.content, agent_def.name)
                all_claims.extend(claims)
                all_output_parts.append(resp.content)

            # If no tool calls, this is the final response (usually)
            if not resp.tool_calls:
                # Force retry: agent has tools but didn't use them — push back
                if agent_def.toolsets and round_num == 0:
                    messages.append({"role": "assistant", "content": resp.content or ""})
                    messages.append({"role": "user", "content": "You have tools available but did not call any. You MUST use the terminal function to execute commands. Call the function now."})
                    continue
                # If we already retried (round 1+) and agent still didn't call tools,
                # return failure so handle_failure can escalate
                if agent_def.toolsets and round_num > 0 and not all_output_parts:
                    return DispatcherResult(
                        success=False,
                        output="",
                        error="Agent did not call tools after retry (no tool_calls in 2 rounds)",
                        tier_used=tier,
                        model_used=model_name,
                    )
                return DispatcherResult(
                    success=True,
                    output="\n\n".join(all_output_parts) or "",
                    claims=all_claims,
                    tier_used=tier,
                    model_used=model_name,
                    tokens=total_in_tokens + total_out_tokens,
                    input_tokens=total_in_tokens,
                    output_tokens=total_out_tokens,
                    cost_usd=total_cost,
                )

            # Append assistant message with tool_calls
            assistant_msg = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": resp.tool_calls,
            }
            messages.append(assistant_msg)

            # Execute each tool call
            for tc in resp.tool_calls:
                tool_calls_made += 1
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}

                # ── Progress callback: tool call start ──
                if progress_callback:
                    progress_callback(agent_def.name, round_num+1, tool_calls_made, "tool_start",
                                      f"{name}({json.dumps(args)[:200]})")

                # ── Checkpoint tool: handled locally ──
                if name == "update_checkpoint":
                    key_info = args.get("key_info", "")
                    checkpoint.update(key_info)
                    result = json.dumps({"ok": True, "checkpoint_updated": True})
                else:
                    result = tool_dispatch(name, args, project_path=project_path)

                # ── Progress callback: tool call result ──
                if progress_callback:
                    result_preview = result[:200] if result else "(empty)"
                    progress_callback(agent_def.name, round_num+1, tool_calls_made, "tool_result",
                                      f"{name} → {result_preview}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "content": result,
                })

            # ── Progress callback: round complete ──
            if progress_callback:
                progress_callback(agent_def.name, round_num+1, tool_calls_made, "round_end",
                                  f"Round {round_num+1} complete ({len(resp.tool_calls)} tools)")

            # ── Auto git commit after tool round ──
            if auto_git and project_path and tool_calls_made > 0:
                import subprocess as _sp
                _sp.run(
                    ["git", "add", "-A"],
                    cwd=project_path, capture_output=True, timeout=10,
                )
                _sp.run(
                    ["git", "commit", "-m", f"orbuz auto: {agent_def.name} round {round_num+1}"],
                    cwd=project_path, capture_output=True, timeout=10,
                )

        # Exceeded max rounds — return whatever we have
        return DispatcherResult(
            success=True,
            output="\n\n".join(all_output_parts) or "[Reached max tool rounds]",
            claims=all_claims,
            tier_used=tier,
            model_used=model_name,
            tokens=total_in_tokens + total_out_tokens,
            input_tokens=total_in_tokens,
            output_tokens=total_out_tokens,
            cost_usd=total_cost,
        )

    # ── Goal Mode ──
    # Wraps run_agent_with_tools with self-driving goal mode.
    # Inspired by GenericAgent's reflect/goal_mode.py.
    # The sub-agent gets a budget and self-directs until done or exhausted.

    GOAL_MODE_SYSTEM_HINT = (
        "\n\n[Goal Mode] You are working autonomously. "
        "Do NOT ask for confirmation or report 'done' prematurely. "
        "Continue making progress until the goal is fully achieved. "
        "When you are truly done (all code written, all files created, "
        "all verification passed), include [DONE] in your final response. "
        "If you encounter an unresolvable blocker, include [BLOCKED: reason] "
        "so the orchestrator can intervene."
    )

    def run_agent_goal_mode(
        self, agent_def: AgentDefinition, goal: str,
        context: str = "", tier: str = "balanced",
        tools: list[dict] | None = None,
        project_path: str | None = None,
        workspace_dir: str | None = None,
        max_tool_rounds: int = 50,
        progress_callback: callable = None,
    ) -> DispatcherResult:
        """Run sub-agent in Goal Mode: self-direct until [DONE] or budget exhausted.

        The agent autonomously progresses through the task. The goal mode
        system hint is appended to discourage premature completion. Detection
        of [DONE] or [BLOCKED] markers triggers clean summarization.
        """
        # Injects goal mode hint into system prompt by passing it as extra context
        goal_mode_context = (context + "\n\n" + self.GOAL_MODE_SYSTEM_HINT) if context else self.GOAL_MODE_SYSTEM_HINT

        # Read execution config from agent definition
        exe = agent_def.execution
        return self.run_agent_with_tools(
            agent_def=agent_def,
            goal=goal,
            context=goal_mode_context,
            tier=tier,
            tools=tools,
            project_path=project_path,
            workspace_dir=workspace_dir,
            max_tool_rounds=max_tool_rounds,
            max_cost_usd=exe.max_cost_usd,
            auto_git=exe.auto_git_commit,
            progress_callback=progress_callback,
        )

    def handle_failure(self, agent_def: AgentDefinition, goal: str,
                       context: str, prev_result: DispatcherResult,
                       failed_tier: str = "",
                       tools: list[dict] | None = None,
                       project_path: str | None = None) -> DispatcherResult:
        """
        Escalation chain, respecting retry_on_failure:
          1. If retry_on_failure="never" → return failure immediately
          2. Retry at a higher tier (e.g., cheap→balanced→quality)
          3. If reentrant=True → flag for decomposition (handled by the caller)
          4. All failed → return failure result
        """
        hint = agent_def.model_hint
        retry_mode = agent_def.execution.retry_on_failure

        # ── "never" → bail immediately ──
        if retry_mode == "never":
            print(f"    ⏭️  {agent_def.name} failed, retry_on_failure=never, skipping")
            return prev_result

        # ── "compile" → only retry if error looks like a compile failure ──
        if retry_mode == "compile":
            err = (prev_result.error or "").lower()
            out = (prev_result.output or "").lower()
            if not any(kw in err or kw in out for kw in ["compile", "error", "error[", "could not", "expected"]):
                print(f"    ⏭️  {agent_def.name} failed, retry_on_failure=compile but error not compile-related, skipping")
                return prev_result

        # ── Determine starting tier ──
        failed = failed_tier or prev_result.tier_used
        fallback = hint.fallback or "quality"

        # Retry at a higher tier — try fallback first, then quality as last resort
        tiers_to_try = []
        if fallback != failed:
            tiers_to_try.append(fallback)
        # If the current tier is the highest we'd normally try, push one more step
        current_failed = failed
        for attempt_tier in tiers_to_try + (["quality"] if fallback == failed and fallback != "quality" else []):
            print(f"    ⚠️ {agent_def.name} failed at {current_failed}, escalating to {attempt_tier} for retry")
            retry_context = context + (
                f"\n\nNote: Previously failed at {current_failed} tier, error: {prev_result.error}"
            )
            if tools and project_path:
                from orbuz.codegen.tools import TOOL_SCHEMAS
                result = self.run_agent_with_tools(
                    agent_def, goal, retry_context, tier=attempt_tier,
                    tools=TOOL_SCHEMAS, project_path=project_path,
                )
            else:
                result = self.run_agent(agent_def, goal, retry_context, tier=attempt_tier)
            if result.success:
                return result
            current_failed = attempt_tier

        # If reentrant and all escalation also failed → flag for decomposition
        if hint.reentrant:
            print(f"    ⚠️ {agent_def.name} failed at both tiers, marked for decomposition (reentrant)")
            return DispatcherResult(
                success=False,
                output=prev_result.output,
                error=f"Failed at {failed} and {fallback}; marked for decomposition",
            )

        # Give up
        print(f"    ❌ {agent_def.name} all attempts failed, skipping")
        return DispatcherResult(
            success=False,
            output="",
            error=f"All attempts failed (tiers: {failed}→{fallback})",
        )

    # ── Internal: Prompt Building ──

    def _build_system_prompt(self, agent_def: AgentDefinition) -> str:
        """Build system prompt from agent.yaml (standard / research mode)."""
        parts = [f"You are a(n) {agent_def.description}."]

        if agent_def.principles:
            parts.append("\n## Working Principles")
            for p in agent_def.principles:
                parts.append(f"- {p}")

        if agent_def.constraints:
            parts.append("\n## Constraints")
            for c in agent_def.constraints:
                parts.append(f"- {c}")

        # Tool-use directive — inject if agent has toolsets
        if agent_def.toolsets:
            parts.append(
                "\n## Function Calling"
                "\nYou have access to tools. Use function calling to call them."
                "\nDo NOT describe actions in text — call the function."
                "\nEVERY response must be EITHER a function call OR final output."
                "\nIf you can make progress by calling a function, do it."
            )

        if agent_def.output.structure and not agent_def.toolsets:
            parts.append("\n## Output Structure Requirements")
            for s in agent_def.output.structure:
                parts.append(f"- {s}")

        # Claims format — skip for tool-using agents (they call functions, not write JSON blocks)
        if not agent_def.toolsets:
            parts.append(
                "\n## Claims Format"
                "\nAt the end of your output, publish your key findings (if any) using the following JSON format:"
                "\n```json"
                '\n{"claims": ['
                '\n  {"statement": "...", "confidence": 0.9, "source": "...", '
                '\n"tags": ["..."], "relevance": ["other-agent-name"]}'
                "\n]}"
                "\n```"
                "\nYou may omit this if there are no cross-agent relevant findings."
            )

        return "\n".join(parts)

    def _build_reviewer_prompt(self, agent_def: AgentDefinition) -> str:
        """Build system prompt for structured-findings reviewer agents."""
        parts = [f"You are a(n) {agent_def.description}.",
                 "",
                 "## Working Principles"]
        for p in agent_def.principles:
            parts.append(f"- {p}")

        parts.extend([
            "",
            "## Output Format",
            "You MUST return your findings as a JSON code block at the end of your analysis.",
            "Include prose analysis first, then the JSON block.",
            "",
            "```json",
            "{",
            '  "findings": [',
            "    {",
            '      "severity": "P0|P1|P2|P3",',
            '      "confidence": 0.0-1.0,',
            '      "title": "short title",',
            '      "description": "detailed description",',
            '      "file": "path/to/file.ext",',
            '      "line": 42,',
            '      "why_it_matters": "why this is important",',
            '      "suggested_fix": "concrete fix suggestion",',
            '      "autofix_class": "safe_auto|gated_auto|manual|advisory",',
            '      "pre_existing": false,',
            '      "requires_verification": false',
            "    }",
            "  ]",
            "}",
            "```",
            "",
            "Severity scale:",
            "  P0 — Critical breakage, exploitable vulnerability, data loss",
            "  P1 — High-impact defect, likely hit in normal usage",
            "  P2 — Moderate issue, meaningful downside",
            "  P3 — Low-impact, minor improvement",
            "",
            "autofix_class:",
            "  safe_auto — deterministic fix, safe to auto-apply",
            "  gated_auto — fix exists but needs review",
            "  manual — actionable work, hand off",
            "  advisory — report-only",
        ])

        if agent_def.output.structure:
            parts.append("")
            parts.append("## Analysis Structure")
            for s in agent_def.output.structure:
                parts.append(f"- {s}")

        return "\n".join(parts)

    def _build_user_prompt(self, agent_def: AgentDefinition, goal: str,
                           context: str, messages_from_bus: str) -> str:
        """Build user prompt (task + context + bus messages)."""
        parts = [f"## Task\n{goal}"]

        if context:
            parts.append(f"\n## Context\n{context}")

        if messages_from_bus:
            parts.append(f"\n## Other Agents' Findings\n{messages_from_bus}")

        parts.append(
            "\nPlease complete the work according to the requirements above."
        )
        if agent_def.toolsets:
            parts.append(
                "\nUse your available tools to read, write, and modify files."
                "\nCall the function — do NOT describe what you would do."
            )
        else:
            parts.append(
                "\nIf you have new findings relevant to other agents' domains, "
                "publish claims in JSON format at the end of your output."
            )

        return "\n".join(parts)

    # ── MCP Tool Resolution ──

    def _resolve_mcp_tools(self, agent_def: AgentDefinition,
                           goal: str, context: str) -> str:
        """
        Pre-fetch MCP tool results for the agent and return as formatted context.

        Calls each tool defined in agent_def.mcp_tools and formats
        the results as structured sections for LLM injection.
        Returns empty string if no MCP tools are configured or no MCP manager.
        """
        if not agent_def.mcp_tools or not self.mcp:
            return ""

        sections = []
        for spec in agent_def.mcp_tools:
            try:
                result = self.mcp.call_tool_any(spec.tool, spec.params) if not spec.server \
                    else self.mcp.call_tool(spec.server, spec.tool, spec.params)
            except Exception as e:
                msg = f"  [MCP Error: {e}]"
                if spec.required:
                    raise
                sections.append(f"## {spec.label or spec.tool}\n{msg}")
                continue

            label = spec.label or spec.tool
            text = result.text.strip()
            if not text:
                text = "(no output)"

            # Truncate very long tool outputs to avoid blowing context
            if len(text) > 4000:
                text = text[:4000] + "\n... [truncated]"

            sections.append(f"## {label}\n{text}")

        if not sections:
            return ""

        return "## Pre-fetched Data (from MCP tools)\n" + "\n\n".join(sections)

    # ── Internal: Parsing ──

    def _extract_claims(self, output: str, agent_name: str) -> list[dict]:
        """Extract structured claims from LLM output."""
        # Look for ```json ... ``` blocks
        pattern = r'```json\s*\n(.*?)\n```'
        matches = re.findall(pattern, output, re.DOTALL)
        for match in matches:
            try:
                data = json.loads(match.strip())
                if "claims" in data and isinstance(data["claims"], list):
                    return data["claims"]
            except (json.JSONDecodeError, TypeError):
                continue
        return []

    def _extract_findings(self, output: str, persona_name: str) -> FindingSet:
        """Extract structured findings from LLM output."""
        # Look for ```json ... ``` blocks
        pattern = r'```json\s*\n(.*?)\n```'
        matches = re.findall(pattern, output, re.DOTALL)

        for match in matches:
            try:
                data = json.loads(match.strip())
                raw = data.get("findings", data if isinstance(data, list) else [])
                if isinstance(raw, list) and raw:
                    findings = []
                    for item in raw:
                        if isinstance(item, dict) and "severity" in item:
                            try:
                                finding = Finding(
                                    persona=persona_name,
                                    severity=item.get("severity", "P3"),
                                    autofix_class=item.get("autofix_class", "advisory"),
                                    confidence=float(item.get("confidence", 0.5)),
                                    title=item.get("title", ""),
                                    description=item.get("description", ""),
                                    file=item.get("file", ""),
                                    line=item.get("line"),
                                    why_it_matters=item.get("why_it_matters", ""),
                                    suggested_fix=item.get("suggested_fix"),
                                    pre_existing=bool(item.get("pre_existing", False)),
                                    requires_verification=bool(item.get("requires_verification", False)),
                                )
                                findings.append(finding)
                            except (ValueError, TypeError):
                                continue

                    if findings:
                        return FindingSet(findings=findings, persona=persona_name)
            except (json.JSONDecodeError, TypeError):
                continue

        return FindingSet(persona=persona_name)


if __name__ == "__main__":
    from orbuz.llm.client import LLMClient
    from orbuz.schema.agent import AgentDefinition

    client = LLMClient({"balanced": "mock"}, mock=True)
    d = Dispatcher(client)

    # Test structured findings extraction
    test_output = """I reviewed the code and found issues.

```json
{
  "findings": [
    {
      "severity": "P1",
      "confidence": 0.9,
      "title": "SQL injection in user query",
      "description": "Direct string interpolation in SQL query",
      "file": "src/db/users.py",
      "line": 42,
      "why_it_matters": "Allows attackers to execute arbitrary SQL",
      "suggested_fix": "Use parameterized queries",
      "autofix_class": "safe_auto",
      "pre_existing": false,
      "requires_verification": false
    }
  ]
}
```"""

    fs = d._extract_findings(test_output, "test-reviewer")
    print(f"Findings: {len(fs.findings)}")
    for f in fs.findings:
        print(f"  {f.short()}")

    # Test merge-dedup
    from orbuz.core.dispatcher import merge_dedup_findings
    result = merge_dedup_findings([fs])
    print(f"\nMerged: {len(result.merged)}, Dups removed: {result.duplicates_removed}")
