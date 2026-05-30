"""
Workspace schema — structure definitions for _workspace/ files
"""
from pydantic import BaseModel, Field
from datetime import datetime, timezone


class RunManifest(BaseModel):
    """manifest.json — written once, never modified"""
    schema_version: str = "1.0"
    run_id: str = ""
    workflow: dict = {}
    parameters: dict = {}
    stages: list[dict] = []
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: str = "created"


class StageStatus(BaseModel):
    """Single stage status within status.json"""
    id: str
    status: str = "pending"   # pending / running / completed / failed / skipped
    agents: list[dict] = []
    summary: dict | None = None


class RunStatus(BaseModel):
    """status.json — runtime state, updated dynamically"""
    run_id: str
    state: str = "created"
    current_stage_index: int = 0
    total_stages: int = 0
    total_duration_seconds: int = 0
    total_tokens_estimated: int = 0
    stages: list[StageStatus] = []
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def set_stage(self, stage_id: str, status: str, agents: list | None = None,
                  summary: dict | None = None):
        for s in self.stages:
            if s.id == stage_id:
                s.status = status
                if agents is not None:
                    s.agents = agents
                if summary is not None:
                    s.summary = summary
                break
        self.updated_at = datetime.now(timezone.utc).isoformat()
