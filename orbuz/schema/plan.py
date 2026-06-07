"""
Plan JSON — Output Format of the Recon Phase
"""
from pydantic import BaseModel, Field
from datetime import datetime, timezone


class PlanModelAssignment(BaseModel):
    tier: str = "balanced"
    fallback: str = "quality"
    reentrant: bool = False


class PlanAgent(BaseModel):
    role: str
    model_assignment: PlanModelAssignment = PlanModelAssignment()
    rationale: str = ""
    goal: str = ""
    output: str = "output.md"


class PlanMerge(BaseModel):
    enabled: bool = False
    agent_role: str = "merge-agent"
    model_assignment: PlanModelAssignment = PlanModelAssignment()
    context: str = ""


class PlanStage(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    pattern: str = "fanout"
    depends_on: list[str] = []
    agents: list[PlanAgent] = []
    merge: PlanMerge = PlanMerge()
    max_cycles: int = 3
    checkpoint: dict = {"auto_continue": False}


class ReconSummary(BaseModel):
    topic: str = ""
    complexity: str = "moderate"
    key_findings: list[str] = []
    estimated_total_seconds: int = 120
    estimated_total_tokens: int = 10000
    new_agents_created: int = 0


class PlanJSON(BaseModel):
    schema_version: str = "1.0"
    workflow: dict = {"name": "", "description": ""}
    recon_summary: ReconSummary = ReconSummary()
    plan: dict = {"stages": []}
    alternatives_considered: list[dict] = []
    agent_registry_updates: list[dict] = []
    generated_at: str = ""
    model_used: str = ""

    @classmethod
    def sample(cls, workflow_name: str = "deep-research",
               topic: str = "") -> "PlanJSON":
        """Generate a sample research plan for testing and scaffolding"""
        return cls(
            schema_version="1.0",
            workflow={"name": workflow_name, "description": topic},
            recon_summary=ReconSummary(
                topic=topic,
                complexity="moderate",
                key_findings=["Mock finding 1", "Mock finding 2"],
                estimated_total_seconds=180,
                estimated_total_tokens=35000,
                new_agents_created=0,
            ),
            plan={
                "stages": [
                    {
                        "id": "01_research",
                        "name": "Multi-angle parallel search",
                        "pattern": "fanout",
                        "agents": [
                            {"role": "official-researcher", "rationale": "Need official information", "goal": "Search official sources for: " + topic[:60]},
                            {"role": "media-researcher", "rationale": "Need market interpretation", "goal": "Search media sources for: " + topic[:60]},
                            {"role": "background-researcher", "rationale": "Need technical background", "goal": "Search background/technical sources for: " + topic[:60]},
                        ],
                        "merge": {"enabled": True, "agent_role": "merge-agent"},
                    },
                    {
                        "id": "02_synthesis",
                        "name": "Synthesis Report",
                        "pattern": "pipeline",
                        "depends_on": ["01_research"],
                        "agents": [
                            {"role": "synthesizer", "rationale": "Combine and write the final report", "goal": "Write a comprehensive report based on search results"},
                        ],
                    },
                ]
            },
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def codegen_sample(cls, workflow_name: str = "codegen",
                       topic: str = "",
                       project_dir: str = ".") -> "PlanJSON":
        """Generate a sample codegen plan (fallback when LLM output is unparseable)"""
        return cls(
            schema_version="1.0",
            workflow={"name": workflow_name, "description": topic},
            recon_summary=ReconSummary(
                topic=topic,
                complexity="low",
                key_findings=["Generate Rust project with axum server", "Write source files and Cargo.toml"],
                estimated_total_seconds=60,
                estimated_total_tokens=5000,
                new_agents_created=0,
            ),
            plan={
                "stages": [
                    {
                        "id": "01_codegen",
                        "name": "Generate project code",
                        "pattern": "pipeline",
                        "project_dir": project_dir,
                        "agents": [
                            {
                                "role": "codegen-writer",
                                "rationale": "Code generation agent with file system access",
                                "goal": topic,
                                "model_assignment": {"tier": "balanced"},
                            },
                        ],
                    },
                ]
            },
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
