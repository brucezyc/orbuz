"""
Schema — Pydantic Data Models
===============================
Extended with Compound Engineering finding schema and persona types.
"""
from orbuz.schema.agent import (
    AgentDefinition, OutputSpec, ModelHint,
    AgentIndex, IndexEntry,
    SelectionRules, OutputContract,
    PersonaConfig,
    load_agent, load_index,
)
from orbuz.schema.finding import (
    Finding, FindingSet, MergeDedupResult,
    Severity, AutofixClass, PersonaTier,
)
from orbuz.schema.plan import (
    PlanJSON, PlanStage, PlanAgent,
    ReconSummary, PlanModelAssignment,
)
from orbuz.schema.workspace import (
    RunStatus, RunManifest,
)
