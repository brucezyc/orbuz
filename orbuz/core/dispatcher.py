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
                 tokens: int = 0, error: str = "",
                 findings: FindingSet | None = None):
        self.success = success
        self.output = output
        self.claims = claims or []
        self.tier_used = tier_used
        self.model_used = model_used
        self.duration_s = duration_s
        self.tokens = tokens
        self.error = error
        self.findings = findings or FindingSet(persona="")


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

        Returns DispatcherResult containing output + claims + findings.
        """
        model_name = self.llm.get_model_name(tier)

        if require_structured_findings and agent_def.output_contract.produces_findings:
            system = self._build_reviewer_prompt(agent_def)
        else:
            system = self._build_system_prompt(agent_def)

        user = self._build_user_prompt(agent_def, goal, context, messages_from_bus)

        resp = self.llm.chat(
            model_tier=tier,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        # Extract structured claims
        claims = self._extract_claims(resp.content, agent_def.name)

        # Extract structured findings
        findings = self._extract_findings(resp.content, agent_def.name)

        return DispatcherResult(
            success=resp.success,
            output=resp.content,
            claims=claims,
            findings=findings,
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

    # ── Internal: Prompt Building ──

    def _build_system_prompt(self, agent_def: AgentDefinition) -> str:
        """Build system prompt from agent.yaml (standard / research mode)."""
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
            "\nIf you have new findings relevant to other agents' domains, publish claims in JSON format at the end of your output."
        )

        return "\n".join(parts)

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
