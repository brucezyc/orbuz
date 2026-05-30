"""
Schema — Pydantic Data Models
===============================
Three modules:
  agent.py    → AgentDefinition, AgentIndex
  plan.py     → PlanJSON
  workspace.py → RunStatus, RunManifest

All models share the same structure as the project-root schema.py.
But this copy is independent from schema.py (schema.py is a project validator,
while orbuz/ is a standalone product that does not depend on the project directory structure).
"""

from orbuz.schema.agent import (
    AgentDefinition, OutputSpec, ModelHint,
    AgentIndex, IndexEntry,
    load_agent, load_index,
)

from orbuz.schema.plan import (
    PlanJSON, PlanStage, PlanAgent,
    ReconSummary, PlanModelAssignment,
)

from orbuz.schema.workspace import (
    RunStatus, RunManifest,
)
