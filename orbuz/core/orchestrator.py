"""
Orchestrator — Recon Phase
============================
Input: user topic + LLMClient
Flow: web-search topic understanding → decomposition → match agent library → output plan.json
Output: plan.json (handed to Executor after user approval)

Design:
  - Currently in mock mode, returns PlanJSON.sample() as a sample plan
  - Real mode will use LLM + web_search for recon
  - Which LLM to use is determined by the LLMClient passed by the caller
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from orbuz.schema.plan import PlanJSON, ReconSummary
from orbuz.schema.agent import load_index, load_agent
from orbuz.llm.client import LLMClient


class Orchestrator:
    """
    Recon executor.

    Usage:
        orch = Orchestrator(llm_client)
        plan = orch.recon("US AI chip export controls")
    """

    def __init__(self, llm_client: LLMClient,
                 agent_dir: str | Path | None = None):
        self.llm = llm_client
        self.agent_dir = Path(agent_dir) if agent_dir else Path.cwd() / "agents"

    def recon(self, topic: str, workflow_name: str | None = None,
              project_dir: str | None = None) -> dict:
        """Execute Recon → return plan.json (dict)"""
        name = workflow_name or topic.replace(" ", "-")[:40].lower()

        # Scan agent library
        index = load_index(self.agent_dir)
        print(f"  📋 Agent library: {len(index.agents)} available")

        # Mock mode: return sample plan
        if self.llm.mock:
            print(f"  🟡 Mock mode: returning sample plan")
            plan = PlanJSON.sample(workflow_name=name, topic=topic)
            return plan.model_dump()

        # Real mode: use LLM for recon → output plan (TODO)
        plan = self._real_recon(topic, name, index, project_dir=project_dir)
        return plan.model_dump()

    def _real_recon(self, topic: str, workflow_name: str,
                    index, project_dir: str | None = None) -> PlanJSON:
        """Real mode: LLM analyzes the topic → designs a plan → parses JSON output"""

        # ── Build prompt ──
        agent_list_lines = []
        for a in index.agents:
            tags = ", ".join(a.tags) if a.tags else ""
            # Load full definition to show capabilities
            full_defn = load_agent(a.name)
            has_tools = bool(full_defn.toolsets)
            tools_hint = f" [tools: {', '.join(full_defn.toolsets)}]" if has_tools else ""
            agent_list_lines.append(f"  - {a.name}{tools_hint}")
            agent_list_lines.append(f"    Summary: {a.summary}")
            if tags:
                agent_list_lines.append(f"    Tags: {tags}")
        if not agent_list_lines:
            agent_list_lines.append("  (no agents defined — create appropriate generic role names)")

        system = (
            "You are a task planning expert (Recon Orchestrator). Your responsibilities are:\n"
            "1. Analyze the user's research topic\n"
            "2. Decompose it into appropriate execution stages\n"
            "3. Select matching roles from the available agent library\n"
            "   If no agents are defined, invent appropriate generic role names\n"
            "4. Assign a model tier (cheap/balanced/quality) to each agent\n"
            "5. Output a strict JSON-format plan\n"
            "Do not execute the tasks — only output the plan."
        )

        prompt = (
            f"## Topic\n{topic}\n\n"
            f"## Project Directory\n{project_dir or '(not specified)'}\n\n"
            "Use this path for any 'cd' commands in codegen run actions.\n"
            f"## Available Agent Library\n{chr(10).join(agent_list_lines)}\n\n"
            "IMPORTANT: Use agent names EXACTLY as they appear in the library above.\n"
            "Do NOT invent new role names — pick from the listed agents.\n"
            "For code writing tasks, use 'codegen-writer' (not 'developer' or 'code-generator').\n"
            "For compilation/fixing, use 'codegen-compiler'.\n"
            "## Output Requirements\n"
            "Output a JSON strictly following this structure (no extra text, no markdown wrapping):\n\n"
            "```json\n"
            '{\n'
            '  "workflow": {\n'
            '    "name": "workflow-name",\n'
            '    "description": "brief topic description"\n'
            '  },\n'
            '  "recon_summary": {\n'
            '    "topic": "original topic",\n'
            '    "complexity": "low|moderate|high",\n'
            '    "key_findings": ["key insight 1", "key insight 2"],\n'
            '    "estimated_total_seconds": 300,\n'
            '    "estimated_total_tokens": 50000\n'
            '  },\n'
            '  "plan": {\n'
            '    "stages": [\n'
            '      {\n'
            '        "id": "01_research",\n'
            '        "name": "Multi-angle search",\n'
            '        "pattern": "fanout",\n'
            '        "agents": [\n'
            '          {\n'
            '            "role": "agent-name-from-library",\n'
            '            "rationale": "why this agent was chosen",\n'
            '            "goal": "specific task for this agent",\n'
            '            "model_assignment": {"tier": "balanced"}\n'
            '          }\n'
            '        ],\n'
            '        "merge": {"enabled": true, "agent_role": "merge-agent"}\n'
            '      },\n'
            '      {\n'
            '        "id": "02_synthesis",\n'
            '        "name": "Synthesis report",\n'
            '        "pattern": "pipeline",\n'
            '        "depends_on": ["01_research"],\n'
            '        "agents": [\n'
            '          {\n'
            '            "role": "synthesizer",\n'
            '            "rationale": "Combine and write the final report",\n'
            '            "goal": "Write a comprehensive report based on search results"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ]\n'
            '  },\n'
            '  "alternatives_considered": [\n'
            '    {"approach": "...", "reason_rejected": "..."}\n'
            '  ]\n'
            '}\n'
            '```\n\n'
            "Available patterns:\n"
            "  - fanout (parallel research agents, optionally merged)\n"
            "  - pipeline (sequential agents, output feeds next)\n"
            "  - producer_reviewer (produce -> review -> cycle)\n"
            "  - codegen (sequential or fanout codegen agents)\n"
            "    For codegen stages, EACH agent SHOULD INCLUDE an 'actions' array:\n"
            "      actions: [\n"
            "        {\"action\": \"write_file\", \"file_path\": \"path/to/file.rs\",\n"
            "         \"content\": \"...file content...\"},\n"
            "        {\"action\": \"run\", \"command\": \"cd {project_dir} && cargo check\"}\n"
            "      ]\n"
            "    Set 'project_dir' (absolute path) for codegen stages.\n"
            "    For multi-file projects, use MULTIPLE agents in one fanout stage -\n"
            "    each agent writes different files in parallel.\n"
            "Available model tiers: cheap (info gathering), balanced (default drafting), quality (analysis/synthesis)\n"
            "Select the best-matching agent name from the library for the `role` field.\n"
            "If no exact match exists, use the closest one available.\n"
            "For codegen stages, set `project_dir` to the target directory and use codegen-tagged agents.\n"
            "NOTE: For code generation / compilation / testing tasks, prefer agents tagged with 'codegen'.\n"
            "Output only JSON, no explanation."
        )

        resp = self.llm.chat(
            model_tier="quality",
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        if not resp.success:
            print(f"  ⚠️ Recon LLM call failed: {resp.error}")
            print(f"  → Falling back to sample plan")
            return self._fallback_plan(workflow_name, topic, project_dir)

        # ── Parse JSON ──
        plan_data = self._parse_llm_json(resp.content)
        if plan_data is None:
            print(f"  ⚠️ Unable to parse LLM output as JSON")
            print(f"  Raw response ({len(resp.content)} chars):")
            print(f"  {resp.content[:500]}")
            print(f"  → Falling back to sample plan")
            return self._fallback_plan(workflow_name, topic, project_dir)

        # ── Build PlanJSON ──
        try:
            plan = PlanJSON(
                schema_version="1.0",
                workflow=plan_data.get("workflow", {"name": workflow_name, "description": topic}),
                recon_summary=ReconSummary(
                    topic=topic,
                    complexity=plan_data.get("recon_summary", {}).get("complexity", "moderate"),
                    key_findings=plan_data.get("recon_summary", {}).get("key_findings", []),
                    estimated_total_seconds=plan_data.get("recon_summary", {}).get(
                        "estimated_total_seconds", 120),
                    estimated_total_tokens=plan_data.get("recon_summary", {}).get(
                        "estimated_total_tokens", 10000),
                ),
                plan=plan_data.get("plan", {"stages": []}),
                alternatives_considered=plan_data.get("alternatives_considered", []),
                generated_at=datetime.now(timezone.utc).isoformat(),
                model_used=self.llm.get_model_name("quality"),
            )

            # Validate stages are not empty
            if not plan.plan.get("stages"):
                print(f"  ⚠️ LLM returned plan with no stages, falling back to sample")
                return self._fallback_plan(workflow_name, topic, project_dir)

            stages_count = len(plan.plan["stages"])
            print(f"  ✅ Plan generated successfully: {stages_count} stage(s)")
            return plan

        except Exception as e:
            print(f"  ⚠️ PlanJSON construction failed: {e}")
            print(f"  → Falling back to sample plan")
            return self._fallback_plan(workflow_name, topic, project_dir)

    # ── JSON parsing helpers ──

    @staticmethod
    def _fallback_plan(workflow_name: str, topic: str,
                       project_dir: str | None = None) -> PlanJSON:
        """Choose appropriate fallback: codegen_sample for code tasks, sample for research."""
        codegen_keywords = [
            "build", "create", "write", "generate", "code", "server",
            "app", "project", "rust", "axum", "backend", "frontend",
            "api", "endpoint", "upload", "download", "file",
        ]
        topic_lower = topic.lower()
        is_codegen = any(kw in topic_lower for kw in codegen_keywords)
        if is_codegen and project_dir:
            return PlanJSON.codegen_sample(
                workflow_name=workflow_name, topic=topic,
                project_dir=project_dir
            )
        return PlanJSON.sample(workflow_name=workflow_name, topic=topic)

    @staticmethod
    def _parse_llm_json(text: str) -> dict | None:
        """Extract JSON from LLM output, supports multiple formats"""

        # Strategy 1: ```json ... ``` code block
        m = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 2: Bare JSON object (from first { to last })
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Strategy 3: Attempt to fix common issues — remove trailing commas
        cleaned = re.sub(r',\s*}', '}', text)
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        return None


if __name__ == "__main__":
    from orbuz.llm.client import LLMClient
    llm = LLMClient(mock=True)
    orch = Orchestrator(llm)
    plan = orch.recon("US AI chip export controls")
    stages = plan["plan"]["stages"]
    print(f"Stage count: {len(stages)}")
    for s in stages:
        roles = [a["role"] for a in s.get("agents", [])]
        print(f"  {s['id']}: {s['pattern']} ({', '.join(roles)})")
