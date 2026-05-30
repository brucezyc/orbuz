"""
Agent Registry — Agent Library Management
==========================================
When Orbit detects during Recon that no existing agent matches,
it generates a new agent definition and registers it.

Registration = writes agents/{name}.yaml + updates agents/index.yaml

Current implementation:
  - Scans index.yaml to check if an agent with the same name already exists
  - Writes the new agent YAML file
  - Appends to index.yaml

Not implemented:
  - Agent deduplication (same functionality, different name — left for manual resolution)
  - Version management (version field is preserved, but rollback is not yet supported)
"""

from pathlib import Path
from datetime import datetime

import yaml


class AgentRegistry:
    """
    Agent library manager.

    Usage:
        reg = AgentRegistry(agent_dir="/path/to/agents")
        reg.register_new(new_agent_dict)
    """

    def __init__(self, agent_dir: str | Path):
        self.agent_dir = Path(agent_dir)
        self.index_path = self.agent_dir / "index.yaml"

    def register_new(self, agent_def: dict) -> bool:
        """
        Register a new agent.

        If name already exists → do not overwrite, return False
        If name does not exist → write file + update index, return True
        """
        name = agent_def.get("name", "")
        if not name:
            print("  ❌ New agent missing 'name' field")
            return False

        # Check if already exists
        existing = self._find_agent(name)
        if existing is not None:
            print(f"  ⚠️ Agent '{name}' already exists, skipping")
            return False

        # Write file
        file_path = self.agent_dir / f"{name}.yaml"
        if file_path.exists():
            print(f"  ⚠️ File {file_path.name} already exists, skipping")
            return False

        # Fill in missing fields
        agent_def.setdefault("version", "0.1.0")
        agent_def.setdefault("summary", agent_def.get("description", "")[:40])
        agent_def.setdefault("toolsets", ["web", "terminal"])
        agent_def.setdefault("principles", ["Search by topic category", "Cite sources"])
        agent_def.setdefault("constraints", ["Single task should not exceed 3 minutes"])
        agent_def.setdefault("output", {"format": "markdown", "structure": ["## Key Findings", "## Source List"]})
        agent_def.setdefault("mode", {"execution": "subagent"})

        # Write YAML file
        with open(file_path, "w") as f:
            yaml.dump(agent_def, f, allow_unicode=True, default_flow_style=False,
                      sort_keys=False)
        print(f"  ✅ Wrote {file_path.name}")

        # Update index.yaml
        self._add_to_index(agent_def)

        return True

    def register_batch(self, agent_defs: list[dict]) -> int:
        """Batch register, returns success count"""
        count = 0
        for ad in agent_defs:
            if self.register_new(ad):
                count += 1
        return count

    # ── Internal ──

    def _find_agent(self, name: str) -> dict | None:
        """Search index.yaml by name"""
        if not self.index_path.exists():
            return None
        try:
            data = yaml.safe_load(self.index_path.read_text())
            if not data:
                return None
            for agent in data.get("agents", []):
                if agent.get("name") == name:
                    return agent
        except yaml.YAMLError:
            return None
        return None

    def _add_to_index(self, agent_def: dict):
        """Append an entry to index.yaml"""
        entry = {
            "name": agent_def["name"],
            "summary": agent_def.get("summary", agent_def.get("description", "")),
            "tags": agent_def.get("tags", []),
            "file": f"{agent_def['name']}.yaml",
        }

        if self.index_path.exists():
            try:
                data = yaml.safe_load(self.index_path.read_text()) or {"agents": []}
            except yaml.YAMLError:
                data = {"agents": []}
        else:
            data = {"agents": []}

        data["agents"].append(entry)

        with open(self.index_path, "w") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                      sort_keys=False)
        print(f"  ✅ Updated index.yaml → added {entry['name']}")

    def list_registry(self) -> list[dict]:
        """List all registered agents"""
        if not self.index_path.exists():
            return []
        try:
            data = yaml.safe_load(self.index_path.read_text())
            return data.get("agents", [])
        except yaml.YAMLError:
            return []


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        reg = AgentRegistry(tmp)

        # Register new agent
        reg.register_new({
            "name": "supply-chain-researcher",
            "description": "Search supply chain / geopolitical risk / raw material market changes",
            "tags": ["supply-chain", "geopolitics", "materials"],
            "principles": [
                "Focus on critical raw material supply routes",
                "Tag geographic risk areas",
            ],
        })

        # Verify
        reg.register_new({
            "name": "supply-chain-researcher",  # Duplicate → skip
        })

        print(f"\nAgents in library: {len(reg.list_registry())}")
        print(f"Files: {list(Path(tmp).glob('*.yaml'))}")
