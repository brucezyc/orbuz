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
              project_dir: str | None = None,
              previous_context: str | None = None) -> dict:
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
        plan = self._real_recon(topic, name, index, project_dir=project_dir,
                                previous_context=previous_context)
        return plan.model_dump()

    def _real_recon(self, topic: str, workflow_name: str,
                    index, project_dir: str | None = None,
                    previous_context: str | None = None) -> PlanJSON:
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
            f"\n## Previous Work Context\n{previous_context or '(none)'}\n\n"
            f"## Available Agent Library\n{chr(10).join(agent_list_lines)}\n\n"
            "## Agent Selection Rules\n"
            "Use agent names EXACTLY as they appear in the library. Do NOT invent new role names.\n"
            "  - 'debugger' → ANALYZES ONLY. Reads code, runs diagnostics, writes analysis report. "
            "Does NOT modify files. If only debugger is assigned, no code changes happen.\n"
            "  - 'codegen-writer' → WRITES/MODIFIES source code using write_file/patch tools. "
            "Can read files. Cannot compile or verify.\n"
            "  - 'codegen-compiler' → COMPILES and FIXES compile errors. Runs cargo check, "
            "reads error output, patches files. Only fixes compiler errors.\n"
            "  - For research: use the best-matching researcher agents\n"
            "IMPORTANT: To actually FIX code, you need BOTH a writer (to make changes) "
            "AND optionally a compiler (to verify builds). "
            "A debugger alone only produces a report, no changes.\n"
            "## Output Requirements\n"
            "Output a valid JSON object with this structure:\n\n"
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
            '}\n\n'
            "Available patterns:\n"
            "  - pipeline (sequential agents, output feeds next)\n"
            "Use ONLY 'pipeline' pattern for all stages — unified execution model.\n"
            "Agent selection: for code generation/fixing use 'codegen-writer' or 'debugger'; "
            "for compilation use 'codegen-compiler'.\n"
            "### Post-Plan Instructions\n"
            "After the plan is approved, the executor will run agents in sequence. "
            "Each agent uses tools (read_file, write_file, patch, terminal, search_files) "
            "via function calling to do its work. No ---actions--- blocks needed.\n"
        )

        resp = self.llm.chat(
            model_tier="quality",
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192,
            response_format={"type": "json_object"},
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

            # ── Ensure all agent roles have definitions ──
            new_count = self._ensure_agent_definitions(plan)
            if new_count > 0:
                print(f"  🆕 {new_count} new agent definition(s) auto-generated")

            return plan

        except Exception as e:
            print(f"  ⚠️ PlanJSON construction failed: {e}")
            print(f"  → Falling back to sample plan")
            return self._fallback_plan(workflow_name, topic, project_dir)

    # ── Agent auto-creation ──

    def _ensure_agent_definitions(self, plan: PlanJSON) -> int:
        """Auto-generate YAML definitions for agent roles that don't exist yet.
        Returns count of new agents created.
        Returns:
            int: number of new agents created
        """
        import yaml

        index = load_index(self.agent_dir)
        existing = {a.name for a in index.agents}
        new_count = 0
        created_roles: list[str] = []

        for stage in plan.plan.get("stages", []):
            for agent_cfg in stage.get("agents", []):
                role = agent_cfg.get("role", "")
                if not role or role in existing:
                    continue
                # Check if file already exists (might not be in index yet)
                agent_file = self.agent_dir / f"{role}.yaml"
                if agent_file.exists():
                    # Register in index if missing
                    self._register_in_index(role)
                    continue

                # Same agent with different name? Check close similarity
                close_match = self._has_close_equivalent(role, existing)
                if close_match:
                    print(f"  ⚠️ Agent '{role}' is very similar to '{close_match}' — keeping as-is")
                    print(f"  ℹ️  Generated '{role}' as a new agent (closest existing: {close_match})")

                # Auto-generate the YAML definition
                self._generate_agent_yaml(role, agent_cfg)
                self._register_in_index(role)
                existing.add(role)
                created_roles.append(role)
                new_count += 1
                print(f"  🆕 Generated agent definition: {role}.yaml")

        if new_count > 0:
            plan.recon_summary.new_agents_created = new_count
            plan.agent_registry_updates = [{"name": r} for r in created_roles]

        return new_count

    @staticmethod
    def _has_close_equivalent(name: str, existing: set[str]) -> str | None:
        """Check if agent name is nearly identical to an existing one."""
        from difflib import SequenceMatcher
        name_lower = name.lower()
        for e in existing:
            if e.lower() == name_lower:
                return e
            if SequenceMatcher(None, name_lower, e.lower()).ratio() > 0.7:
                return e
            # Token overlap
            name_tokens = set(name_lower.replace("-", "_").split("_"))
            e_tokens = set(e.lower().replace("-", "_").split("_"))
            if len(name_tokens & e_tokens) >= 2 and len(name_tokens) >= 2:
                return e
        return None

    def _generate_agent_yaml(self, role: str, agent_cfg: dict) -> None:
        """Generate a minimal but functional YAML definition for a new agent role."""
        import yaml

        # Infer toolsets from role name
        toolsets = self._infer_toolsets(role)
        goal = agent_cfg.get("goal", "")
        rationale = agent_cfg.get("rationale", "")
        description = rationale or f"Agent for {goal[:80]}" if goal else f"{role} agent (auto-generated)"

        defn = {
            "name": role,
            "version": "1.0.0",
            "description": description,
            "summary": f"{role.replace('-', ' ').title()}: {goal[:100] if goal else 'auto-generated agent'}",
            "toolsets": toolsets,
            "skills": [],
            "principles": [
                f"Use available tools to accomplish the goal: {goal[:60] if goal else 'execute tasks'}",
                "Report progress clearly",
                "Ask for clarification if stuck",
            ],
            "constraints": [
                "Do not remove or modify files outside the project directory",
            ],
            "output": {
                "format": "markdown",
                "structure": ["summary", "actions_taken", "results"],
            },
            "mode": {"execution": "subagent"},
            "model_hint": {"tier": "balanced", "fallback": "quality"},
            "execution": {
                "max_tool_rounds": 15,
                "max_cost_usd": 0.0,
                "auto_git_commit": False,
                "retry_on_failure": "never",
            },
        }

        # Write YAML
        agent_file = self.agent_dir / f"{role}.yaml"
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        with open(agent_file, "w") as f:
            yaml.dump(defn, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _infer_toolsets(role: str) -> list[str]:
        """Infer appropriate toolsets from the agent role name."""
        role_lower = role.lower()
        toolsets = ["terminal"]

        codegen_keywords = ["codegen", "writer", "compiler", "developer", "coder"]
        debug_keywords = ["debug", "diagnostic", "inspect", "audit"]
        research_keywords = ["research", "search", "investigat", "analyst", "fact"]
        web_keywords = ["web", "scrape", "fetch", "api"]
        file_keywords = ["write", "read", "file", "patch", "edit", "setup"]

        if any(k in role_lower for k in codegen_keywords):
            toolsets = ["terminal", "file"]
        elif any(k in role_lower for k in debug_keywords):
            toolsets = ["terminal", "file"]
        elif any(k in role_lower for k in research_keywords):
            toolsets = ["terminal", "web"]
        elif any(k in role_lower for k in web_keywords):
            toolsets = ["terminal", "web"]
        elif any(k in role_lower for k in file_keywords):
            toolsets = ["terminal", "file"]

        return toolsets

    def _register_in_index(self, role: str) -> None:
        """Add the agent to index.yaml if not already present."""
        import yaml

        index_file = self.agent_dir / "index.yaml"
        if not index_file.exists():
            return

        with open(index_file) as f:
            index = yaml.safe_load(f) or {"agents": []}

        for entry in index.get("agents", []):
            if entry.get("name") == role:
                return  # already in index

        # Add entry
        index.setdefault("agents", []).append({
            "name": role,
            "summary": f"{role.replace('-', ' ').title()}: auto-generated agent",
            "tags": [role.split("-")[0]] if "-" in role else [role],
            "file": f"{role}.yaml",
        })

        with open(index_file, "w") as f:
            yaml.dump(index, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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

        # Strategy 4: Truncated JSON — progressive strip from end
        m = re.search(r'\{.*', text, re.DOTALL)
        if m:
            partial = m.group(0)
            # Try progressively shorter versions
            for pct in [1.0, 0.95, 0.9, 0.85, 0.8, 0.75]:
                cutoff = int(len(partial) * pct)
                candidate = partial[:cutoff]
                # Close unmatched braces
                opens = candidate.count('{')
                closes = candidate.count('}')
                if opens > closes:
                    candidate = candidate.rstrip().rstrip(',') + '\n' * (opens - closes) + '}'
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

            # Last resort: try raw_decode (finds valid prefix)
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(partial)
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, ValueError):
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
