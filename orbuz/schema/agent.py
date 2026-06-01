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


class AgentDefinition(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str = ""
    summary: str = ""
    toolsets: list[str] = []
    skills: list[str] = []
    principles: list[str] = []
    constraints: list[str] = []
    output: OutputSpec = OutputSpec()
    mode: dict = {"execution": "subagent"}
    model_hint: ModelHint = ModelHint()
    notes: str | None = None

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


def load_agent(name: str, agent_dir: str | Path | None = None) -> AgentDefinition:
    """Load an agent definition from agents/{name}.yaml"""
    base = Path(agent_dir) if agent_dir else Path.cwd() / "agents"
    path = base / f"{name}.yaml"
    if not path.exists():
        return AgentDefinition(name=name, description=f"Agent '{name}' (definition file not found)")
    data = yaml.safe_load(path.read_text())
    return AgentDefinition(**data)


def load_index(agent_dir: str | Path | None = None) -> AgentIndex:
    """Load the index from agents/index.yaml"""
    base = Path(agent_dir) if agent_dir else Path.cwd() / "agents"
    path = base / "index.yaml"
    if not path.exists():
        return AgentIndex(agents=[])
    data = yaml.safe_load(path.read_text())
    return AgentIndex(**data)
