"""
Dispatcher — Sub-Agent Dispatcher
====================================
Input: (agent definition + goal + context + model_tier) + LLMClient
Flow: build system prompt → call LLM → parse claims → return results
Output: sub-agent output + structured claims (for message bus)

This is the core of the execution engine — all agent LLM calls go through here.

Design principles:
  - Dispatcher does not care about inter-agent communication; it only handles "one LLM call"
  - Communication routing is handled by Executor via MessageBus
  - Escalation chain is embedded in handle_failure
"""

from orbuz.schema.agent import AgentDefinition, ModelHint
from orbuz.llm.client import LLMClient, LLMResponse


class DispatcherResult:
    """Result of a single delegate call"""
    def __init__(self, success: bool, output: str = "",
                 claims: list[dict] = None, tier_used: str = "",
                 model_used: str = "", duration_s: float = 0.0,
                 tokens: int = 0, error: str = ""):
        self.success = success
        self.output = output
        self.claims = claims or []
        self.tier_used = tier_used
        self.model_used = model_used
        self.duration_s = duration_s
        self.tokens = tokens
        self.error = error


class Dispatcher:
    """
    Dispatch and run sub-agents.

    Usage:
        d = Dispatcher(llm_client)
        result = d.run_agent(agent_def, goal, context, tier="cheap")
        if not result.success:
            result = d.handle_failure(agent_def, goal, context, result)
    """

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def run_agent(self, agent_def: AgentDefinition, goal: str,
                  context: str = "", tier: str = "balanced",
                  messages_from_bus: str = "") -> DispatcherResult:
        """
        Run a sub-agent.

        agent_def: agent.yaml definition
        goal:      specific task description for this agent
        context:   context (prior output, user injection, etc.)
        tier:      model tier (cheap/balanced/quality)
        messages_from_bus: discovery summaries from other agents (injected in Round N+1)

        Returns DispatcherResult containing output + claims.
        """
        model_name = self.llm.get_model_name(tier)
        system = self._build_system_prompt(agent_def)
        user = self._build_user_prompt(agent_def, goal, context, messages_from_bus)

        resp = self.llm.chat(
            model_tier=tier,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        # Extract structured claims from output
        claims = self._extract_claims(resp.content, agent_def.name)

        return DispatcherResult(
            success=resp.success,
            output=resp.content,
            claims=claims,
            tier_used=tier,
            model_used=model_name,
            duration_s=resp.duration_s,
            tokens=resp.input_tokens + resp.output_tokens,
            error=resp.error or "",
        )

    def handle_failure(self, agent_def: AgentDefinition, goal: str,
                       context: str, prev_result: DispatcherResult,
                       failed_tier: str = "") -> DispatcherResult:
        """
        Escalation chain:
          1. Retry at a higher tier (e.g., cheap→balanced→quality)
          2. If reentrant=True → flag for decomposition (handled by the caller)
          3. All failed → return failure result
        """
        hint = agent_def.model_hint
        failed = failed_tier or prev_result.tier_used
        fallback = hint.fallback or "quality"

        # Retry at a higher tier
        if fallback != failed:
            print(f"    ⚠️ {agent_def.name} failed at {failed}, escalating to {fallback} for retry")
            retry_context = context + (
                f"\n\nNote: Previously failed at {failed} tier, error: {prev_result.error}"
            )
            result = self.run_agent(agent_def, goal, retry_context, tier=fallback)
            if result.success:
                return result

        # If reentrant and escalation also failed → flag for decomposition
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

    # ── Internal methods ──

    def _build_system_prompt(self, agent_def: AgentDefinition) -> str:
        """Build system prompt from agent.yaml"""
        parts = [f"You are a(n) {agent_def.description}."]

        if agent_def.principles:
            parts.append("\n## Working Principles")
            for p in agent_def.principles:
                parts.append(f"- {p}")

        if agent_def.output.structure:
            parts.append("\n## Output Structure Requirements")
            for s in agent_def.output.structure:
                parts.append(f"- {s}")

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

    def _build_user_prompt(self, agent_def: AgentDefinition, goal: str,
                           context: str, messages_from_bus: str) -> str:
        """Build user prompt (task + context + bus messages)"""
        parts = [f"## Task\n{goal}"]

        if context:
            parts.append(f"\n## Context\n{context}")

        if messages_from_bus:
            parts.append(f"\n## Other Agents' Findings\n{messages_from_bus}")

        parts.append(
            "\nPlease complete the work according to the output structure requirements above."
            "\nIf you have new findings relevant to other agents' domains, publish claims in JSON format at the end of your output."
        )

        return "\n".join(parts)

    def _extract_claims(self, output: str, agent_name: str) -> list[dict]:
        """Extract structured claims from LLM output"""
        import re
        import json as json_lib

        # Look for ```json ... ``` blocks
        pattern = r'```json\s*\n(.*?)\n```'
        matches = re.findall(pattern, output, re.DOTALL)
        for match in matches:
            try:
                data = json_lib.loads(match.strip())
                if "claims" in data and isinstance(data["claims"], list):
                    return data["claims"]
            except (json_lib.JSONDecodeError, TypeError):
                continue

        # Also look for inline JSON (no code block wrapping)
        pattern2 = r'\{"claims":\s*\[.*?\]\}'
        match = re.search(pattern2, output, re.DOTALL)
        if match:
            try:
                data = json_lib.loads(match.group())
                if "claims" in data and isinstance(data["claims"], list):
                    return data["claims"]
            except (json_lib.JSONDecodeError, TypeError):
                pass

        return []


if __name__ == "__main__":
    from orbuz.llm.client import LLMClient
    from orbuz.schema.agent import AgentDefinition

    client = LLMClient({"balanced": "mock"}, mock=True)
    d = Dispatcher(client)

    agent = AgentDefinition(
        name="test-agent",
        description="test agent",
        principles=["Search thoroughly", "Cite sources"],
        output={"structure": ["## Key Findings", "## Source List"]},
    )
    result = d.run_agent(agent, "Search BIS regulations", "Timeframe 2026", tier="balanced")
    print(f"Success: {result.success}, Claims: {len(result.claims)}, Tokens: {result.tokens}")
    print(f"Output ({len(result.output)} chars): {result.output[:200]}...")
