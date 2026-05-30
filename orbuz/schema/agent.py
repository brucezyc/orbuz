"""
Agent Definition, Index, Loading
"""
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
import yaml


class OutputSpec(BaseModel):
    format: str = "markdown"
    structure: list[str] = []
    file_pattern: str | None = None


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


class IndexEntry(BaseModel):
    name: str
    summary: str
    tags: list[str] = []
    file: str


class AgentIndex(BaseModel):
    agents: list[IndexEntry]


def load_agent(name: str, agent_dir: str | Path | None = None) -> AgentDefinition:
    """Load an agent definition from agents/{name}.yaml"""
    base = Path(agent_dir) if agent_dir else Path.cwd() / "agents"
    path = base / f"{name}.yaml"
    if not path.exists():
        # Fallback: return a minimal definition
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
