"""
Plan JSON — Output Format of the Recon Phase
"""
from pydantic import BaseModel, Field


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
        """Generate a sample plan.json for testing and scaffolding"""
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
                            {
                                "role": "official-researcher",
                                "rationale": "Need official information",
                                "goal": f"Search official policies and regulations on {topic}",
                                "output": "research_official.md",
                            },
                            {
                                "role": "media-researcher",
                                "rationale": "Need market interpretation",
                                "goal": f"Search media and investment bank analysis on {topic}",
                                "output": "research_media.md",
                            },
                            {
                                "role": "background-researcher",
                                "rationale": "Need technical background",
                                "goal": f"Search technical and competitive background on {topic}",
                                "output": "research_bg.md",
                            },
                        ],
                        "merge": {
                            "enabled": True,
                            "agent_role": "merge-agent",
                            "context": f"Merge three sets of search results on {topic}",
                        },
                    },
                    {
                        "id": "02_synthesis",
                        "name": "Synthesis Report",
                        "pattern": "pipeline",
                        "depends_on": ["01_research"],
                        "agents": [
                            {
                                "role": "synthesizer",
                                "rationale": "Write the final report",
                                "goal": f"Write a comprehensive report on {topic} based on merged results",
                                "output": "final_report.md",
                            }
                        ],
                    },
                ]
            },
        )
