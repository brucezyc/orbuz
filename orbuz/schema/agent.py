"""
Agent Definition, Index, Loading
==================================
Extended with Compound Engineering persona tiers, selection rules,
and output contracts.

Backward compatible: existing YAML files without the new fields
still load correctly.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field, field_validator
import yaml

from orbuz.schema.finding import PersonaTier, Severity, AutofixClass


class OutputSpec(BaseModel):
    format: str = "markdown"
    structure: list[str] = []
    file_pattern: str | None = None


class SelectionRules(BaseModel):
    """Compound Engineering-style persona selection criteria."""

    always_on: bool = False
    """If True, this agent runs on every review regardless of diff."""

    diff_touches: list[str] = Field(default_factory=list)
    """Keywords: select when diff touches these domains (auth, payments, etc.)."""

    min_lines: int = 0
    """Minimum changed lines to trigger this agent."""

    file_extensions: list[str] = Field(default_factory=list)
    """Select when diff includes files with these extensions ('.swift', '.cs')."""

    max_lines: int = 999999
    """Only trigger when diff is BELOW this line count."""


class OutputContract(BaseModel):
    """Structured JSON output schema for findings-based agents."""

    produces_findings: bool = False
    """If True, agent returns structured Finding JSON."""

    findings_schema: list[dict] = Field(default_factory=list)
    """Per-field constraints for finding output."""

    required_fields: list[str] = Field(default_factory=list)
    """Fields every finding must contain."""


class ModelHint(BaseModel):
    tier: str = "balanced"
    fallback: str = "quality"
    reentrant: bool = False

    @field_validator("tier", "fallback")
    @classmethod
    def check_tier(cls, v):
        if v not in {"cheap", "balanced", "quality"}:
            raise ValueError(f"tier must be cheap/balanced/quality, got '{v}'")
        return v


class PersonaConfig(BaseModel):
    """Compound Engineering persona display config."""

    color: str = "blue"
    icon: str = "👤"


class MCPSpec(BaseModel):
    """MCP tool pre-fetch specification for an agent.

    Defines tool calls that are executed BEFORE the LLM call,
    with results injected into the agent's context.
    """
    server: str = ""
    """Name of the MCP server (from mcp config). If empty, searches all servers."""
    tool: str
    """Tool name to call."""
    params: dict = Field(default_factory=dict)
    """Static parameters for the tool call."""
    dynamic_params: list[str] = Field(default_factory=list)
    """Agent-definable parameter keys — values filled from the agent's goal/context."""
    required: bool = False
    """If True and the tool fails, the agent run also fails."""
    label: str = ""
    """Context section label. Defaults to tool name."""


class ExecutionConfig(BaseModel):
    """Per-agent execution control.
    
    Controls how many tool rounds, how much to spend, whether to auto-commit,
    and how to handle failures — all settable per agent role so the orchestrator
    doesn't waste budget on cheap reviewers and codegen writers don't run away.
    """
    max_tool_rounds: int = 25
    """Max tool-call iterations before forced stop (safety valve)."""
    max_cost_usd: float = 0.0
    """Hard budget cap in USD. 0 = no limit."""
    auto_git_commit: bool = False
    """If True, auto git add+commit after each tool round that modifies files."""
    retry_on_failure: str = "always"
    """How to handle LLM/tool failures: 'never' (skip), 'compile' (retry only compile errors), 'always'."""

    # ── Loop Engineering: No-Progress Detection ──
    stall_threshold: int = 3
    """Consecutive identical tool-call rounds before escalation (0 = disabled)."""
    per_round_budget_ratio: float = 0.3
    """Max fraction of total budget a single round can use before warning (0 = disabled)."""
    structured_error_parsing: bool = True
    """If True, preprocess terminal error output before feeding back to agent."""


class AgentDefinition(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str = ""
    summary: str = ""
    toolsets: list[str] = []
    """Legacy toolset names (terminal, web, etc.) — reserved for future tool routing."""
    skills: list[str] = []
    principles: list[str] = []
    constraints: list[str] = []
    output: OutputSpec = OutputSpec()
    mode: dict = {"execution": "subagent"}
    model_hint: ModelHint = ModelHint()
    execution: ExecutionConfig = ExecutionConfig()
    notes: str | None = None

    # ── MCP tool injection ──
    mcp_tools: list[MCPSpec] = Field(default_factory=list)
    """MCP tools to pre-fetch before the LLM call. Results injected into context."""

    # ── Compound Engineering extensions ──
    persona_tier: PersonaTier = PersonaTier.always_on
    selection_rules: SelectionRules = SelectionRules()
    output_contract: OutputContract = OutputContract()
    persona_config: PersonaConfig = PersonaConfig()
    archetype: str = ""

    # ── Legacy orbuz fields ──
    communication: dict | None = None
    permissions: dict | None = None


class IndexEntry(BaseModel):
    name: str
    summary: str
    tags: list[str] = []
    file: str
    persona_tier: str = "always_on"
    archetype: str = ""


class AgentIndex(BaseModel):
    agents: list[IndexEntry]


def _builtin_agent_dir() -> Path:
    """返回内置 agent 目录（随 orbuz 包安装）。"""
    return Path(__file__).parent.parent / "agents"


def load_agent(name: str, agent_dir: str | Path | None = None) -> AgentDefinition:
    """Load an agent definition from agents/{name}.yaml"""
    base = Path(agent_dir) if agent_dir else _builtin_agent_dir()
    path = base / f"{name}.yaml"
    if not path.exists():
        # Fallback: try built-in agents
        builtin = _builtin_agent_dir() / f"{name}.yaml"
        if builtin.exists():
            data = yaml.safe_load(builtin.read_text())
            return AgentDefinition(**data)
        print(f"  ⚠️ Agent definition '{name}.yaml' not found — using default shell (no toolsets/skills)")
        return AgentDefinition(name=name, description=f"Agent '{name}' (definition file not found)")
    data = yaml.safe_load(path.read_text())
    return AgentDefinition(**data)


def load_index(agent_dir: str | Path | None = None) -> AgentIndex:
    """Load the index from agents/index.yaml"""
    base = Path(agent_dir) if agent_dir else _builtin_agent_dir()
    # Try specified dir first, then built-in
    path = base / "index.yaml"
    if not path.exists():
        builtin_path = _builtin_agent_dir() / "index.yaml"
        if builtin_path.exists():
            data = yaml.safe_load(builtin_path.read_text())
            return AgentIndex(**data)
        return AgentIndex(agents=[])
    data = yaml.safe_load(path.read_text())
    return AgentIndex(**data)
