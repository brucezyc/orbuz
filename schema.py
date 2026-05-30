"""
Agent Workflow — Schema Validation
====================================
Validates agent.yaml, index.yaml, and plan.json against design specifications.

Usage:
    python schema.py                      # validate all
    python schema.py agents/official-researcher.yaml  # validate a single file
    python schema.py _workspace/*/plan.json         # validate plan

When adding new fields:
    1. Add a Field() to the corresponding Model class
    2. If it is required and existing files lack it, add compatibility in model_config
"""

import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
import yaml

# ────────────────────────────────────────────────────────
# Project root directory (auto-detected)
# ────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_AGENTS_DIR = _HERE / "agents"
_WORKSPACE_DIR = _HERE / "_workspace"
_WORKFLOWS_DIR = _HERE / "workflows"


# ════════════════════════════════════════════════════════
# Agent definition (agents/*.yaml)
# ════════════════════════════════════════════════════════

class OutputSpec(BaseModel):
    """Output contract"""
    format: str = Field(default="markdown", description="Output format: markdown/json/yaml/text")
    structure: list[str] = Field(default_factory=list, description="Output structure sections")
    file_pattern: str | None = Field(default=None, description="Filename template")

class ModelHint(BaseModel):
    """Model strategy"""
    tier: str = Field(default="balanced", description="Model tier: cheap/balanced/quality")
    fallback: str = Field(default="quality", description="Fallback tier on failure")
    reentrant: bool = Field(default=False, description="Whether decomposition retry is allowed")

    @field_validator("tier", "fallback")
    @classmethod
    def check_tier(cls, v):
        allowed = {"cheap", "balanced", "quality"}
        if v not in allowed:
            raise ValueError(f"tier must be one of {allowed}, got '{v}'")
        return v

class AgentDefinition(BaseModel):
    """Full definition of an agent. Corresponds to agents/{name}.yaml"""

    name: str = Field(description="Unique identifier, should match filename")
    version: str = Field(default="1.0.0", description="Semantic version")
    description: str = Field(description="One-line role description")
    summary: str = Field(default="", description="Short summary for Orchestrator index scan")

    toolsets: list[str] = Field(default_factory=list, description="Available tool sets")
    skills: list[str] = Field(default_factory=list, description="Referenced Hermes skills")

    principles: list[str] = Field(default_factory=list, description="Working principles")
    constraints: list[str] = Field(default_factory=list, description="Hard constraints")

    output: OutputSpec = Field(default_factory=OutputSpec, description="Output contract")

    mode: dict = Field(default_factory=lambda: {"execution": "subagent"}, description='Execution mode: {"execution": "subagent" | "inline"}')
    model_hint: ModelHint = Field(default_factory=ModelHint, description="Model strategy (optional)")

    notes: str | None = Field(default=None, description="Design notes, not injected into context")

    @field_validator("name")
    @classmethod
    def name_matches_filename(cls, v, info):
        """Hint: name should match filename (not enforced, only warning)"""
        return v

    @field_validator("toolsets")
    @classmethod
    def check_toolsets(cls, v):
        allowed = {"web", "terminal", "file", "browser", "search", "vision", "session_search"}
        for t in v:
            if t not in allowed:
                raise ValueError(f"Unknown toolset: '{t}'. Allowed: {allowed}")
        return v


# ════════════════════════════════════════════════════════
# Agent index (agents/index.yaml)
# ════════════════════════════════════════════════════════

class IndexEntry(BaseModel):
    """An agent record in index.yaml"""
    name: str = Field(description="Agent name, corresponds to filename without .yaml")
    summary: str = Field(description="Summary for Orchestrator scan, ~10-20 characters")
    tags: list[str] = Field(default_factory=list, description="Classification tags")
    file: str = Field(description="Corresponding agents/{name}.yaml filename")

class AgentIndex(BaseModel):
    """agents/index.yaml"""
    agents: list[IndexEntry] = Field(description="Agent list")


# ════════════════════════════════════════════════════════
# Plan JSON (_workspace/{run_id}/plan.json)
# ════════════════════════════════════════════════════════

class PlanModelAssignment(BaseModel):
    """Model assignment for each agent in plan.json"""
    tier: str = Field(default="balanced")
    fallback: str = Field(default="quality")
    reentrant: bool = Field(default=False)

class PlanAgent(BaseModel):
    """An agent task within a stage in plan.json"""
    role: str = Field(description="Agent role, corresponds to agents/{role}.yaml")
    model_assignment: PlanModelAssignment = Field(default_factory=PlanModelAssignment)
    rationale: str = Field(default="", description="Why this agent was selected")
    goal: str = Field(description="Specific task for this agent")
    output: str = Field(default="output.md", description="Output filename")

class PlanMerge(BaseModel):
    """Merge configuration for fanout pattern"""
    enabled: bool = Field(default=False)
    agent_role: str = Field(default="merge-agent")
    model_assignment: PlanModelAssignment = Field(default_factory=PlanModelAssignment)
    context: str = Field(default="")

class PlanStage(BaseModel):
    """A stage in plan.json"""
    id: str = Field(description="Stage identifier")
    name: str = Field(description="Stage name")
    description: str = Field(default="")
    pattern: str = Field(description="Architecture pattern: pipeline/fanout/producer_reviewer")
    depends_on: list[str] = Field(default_factory=list)
    agents: list[PlanAgent] = Field(description="Agent list for this stage")
    merge: PlanMerge = Field(default_factory=PlanMerge)

class ReconSummary(BaseModel):
    """Recon phase output summary"""
    topic: str = Field(default="")
    complexity: str = Field(default="moderate")
    key_findings: list[str] = Field(default_factory=list)
    estimated_total_seconds: int = Field(default=120)
    estimated_total_tokens: int = Field(default=10000)
    new_agents_created: int = Field(default=0)

class PlanJSON(BaseModel):
    """Orchestrator Recon phase output"""
    schema_version: str = Field(default="1.0")
    workflow: dict = Field(default_factory=lambda: {"name": "", "description": ""})
    recon_summary: ReconSummary = Field(default_factory=ReconSummary)
    plan: dict = Field(description="Contains stages array")
    alternatives_considered: list[dict] = Field(default_factory=list)
    agent_registry_updates: list[dict] = Field(default_factory=list)

    @field_validator("plan")
    @classmethod
    def plan_has_stages(cls, v):
        if "stages" not in v or not v["stages"]:
            raise ValueError("plan must contain a non-empty stages array")
        return v


# ════════════════════════════════════════════════════════
# Validation functions
# ════════════════════════════════════════════════════════

_errors = []
_warnings = []

def ok(msg):
    print(f"  ✅ {msg}")

def warn(msg):
    _warnings.append(msg)
    print(f"  ⚠️  {msg}")

def fail(msg):
    _errors.append(msg)
    print(f"  ❌ {msg}")


def validate_agent_file(path: Path):
    """Validate an agent YAML file"""
    print(f"\n📄 {path.relative_to(_HERE)}")
    try:
        data = yaml.safe_load(path.read_text())
        if not data:
            fail("File is empty")
            return
        agent = AgentDefinition(**data)
        ok(f"name={agent.name}, tier={agent.model_hint.tier}")

        # Additional checks
        if agent.name != path.stem:
            warn(f"name field '{agent.name}' does not match filename '{path.stem}'")
        if not agent.summary and agent.mode.get("execution") == "subagent":
            warn("subagent mode without summary — Orchestrator may skip during scan")

    except Exception as e:
        fail(f"Validation failed: {e}")


def validate_index(path: Path):
    """Validate agents/index.yaml"""
    print(f"\n📄 {path.relative_to(_HERE)}")
    try:
        data = yaml.safe_load(path.read_text())
        index = AgentIndex(**data)
        ok(f"{len(index.agents)} agent(s) indexed")

        # Check that referenced files exist
        for entry in index.agents:
            agent_path = _AGENTS_DIR / entry.file
            if not agent_path.exists():
                warn(f"Index references '{entry.file}' but file does not exist")

        # Check for files in agents/ not in index
        indexed_files = {e.file for e in index.agents}
        for f in _AGENTS_DIR.glob("*.yaml"):
            if f.name == "index.yaml":
                continue
            if f.name not in indexed_files:
                warn(f"'{f.name}' exists but is not registered in index.yaml")

    except Exception as e:
        fail(f"Validation failed: {e}")


def validate_plan_file(path: Path):
    """Validate a plan.json"""
    print(f"\n📄 {path.relative_to(_HERE)}")
    try:
        data = json.loads(path.read_text())
        plan = PlanJSON(**data)
        stages = plan.plan["stages"]
        ok(f"{len(stages)} stage(s), {sum(len(s.get('agents', [])) for s in stages)} agent(s)")

        # Check that agent references exist in the library
        for stage in stages:
            for agent in stage.get("agents", []):
                agent_file = _AGENTS_DIR / f"{agent['role']}.yaml"
                if not agent_file.exists() and agent["role"] not in {
                    u["name"] for u in plan.agent_registry_updates
                }:
                    warn(f"agent '{agent['role']}' not found in agents/ nor in registry_updates")

    except Exception as e:
        fail(f"Validation failed: {e}")


# ════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════

def run_all():
    """Validate all known files"""
    _errors.clear()
    _warnings.clear()

    print("=" * 50)
    print("Agent Workflow — Schema Validation")
    print("=" * 50)

    # Validate index
    index_path = _AGENTS_DIR / "index.yaml"
    if index_path.exists():
        validate_index(index_path)

    # Validate all agents
    for f in sorted(_AGENTS_DIR.glob("*.yaml")):
        if f.name == "index.yaml":
            continue
        validate_agent_file(f)

    # Validate plan (if any)
    for ws in _WORKSPACE_DIR.iterdir():
        if ws.is_dir():
            plan_path = ws / "plan.json"
            if plan_path.exists():
                validate_plan_file(plan_path)

    # Summary
    print("\n" + "=" * 50)
    if _errors:
        print(f"❌ {len(_errors)} error(s)")
        for e in _errors:
            print(f"   {e}")
    if _warnings:
        print(f"⚠️  {len(_warnings)} warning(s)")
        for w in _warnings:
            print(f"   {w}")
    if not _errors and not _warnings:
        print("✅ All passed")
    print("=" * 50)

    return len(_errors)


def run_single(path: str):
    """Validate a single file"""
    _errors.clear()
    _warnings.clear()
    p = Path(path).resolve()

    if not p.exists():
        print(f"❌ File does not exist: {p}")
        return 1

    if p.name == "index.yaml":
        validate_index(p)
    elif p.suffix == ".yaml":
        validate_agent_file(p)
    elif p.name == "plan.json":
        validate_plan_file(p)
    else:
        print(f"❌ Unsupported file type: {p.suffix}")

    if _errors:
        print(f"\n❌ {len(_errors)} error(s)")
    return len(_errors)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args:
        exit_code = 0
        for arg in args:
            ec = run_single(arg)
            if ec:
                exit_code = ec
        sys.exit(exit_code)
    else:
        sys.exit(run_all())
