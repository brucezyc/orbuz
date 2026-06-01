"""
Finding — Structured Finding Schema
======================================
Borrowed from Compound Engineering's code-review pipeline.

Severity scale (P0-P3):
  P0 — Critical breakage, exploitable vulnerability, data loss
  P1 — High-impact defect, breaking contract
  P2 — Moderate issue with meaningful downside
  P3 — Low-impact, minor improvement

autofix_class routing:
  safe_auto        → review-fixer applies automatically
  gated_auto       → concrete fix exists, but needs review
  manual           → actionable work, hand off
  advisory         → report-only (learnings, rollout notes)
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AutofixClass(str, Enum):
    safe_auto = "safe_auto"
    gated_auto = "gated_auto"
    manual = "manual"
    advisory = "advisory"


class PersonaTier(str, Enum):
    always_on = "always_on"
    cross_cutting = "cross_cutting"
    stack_specific = "stack_specific"
    ce_conditional = "ce_conditional"


class Finding(BaseModel):
    """A single review finding, matching Compound Engineering's JSON contract."""

    # Identity
    id: str = ""
    persona: str = ""
    file: str = ""
    line: int | None = None

    # Classification
    severity: Severity = Severity.P3
    autofix_class: AutofixClass = AutofixClass.advisory
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Content
    title: str = ""
    description: str = ""
    why_it_matters: str = ""
    suggested_fix: str | None = None

    # Routing
    pre_existing: bool = False
    requires_verification: bool = False
    owner: str = "human"

    # Evidence
    evidence: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump()

    def short(self) -> str:
        """Compact one-line representation."""
        sev = self.severity.value
        conf = f"{self.confidence:.1f}"
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{sev}][{conf}] {self.title} ({loc})"


class FindingSet(BaseModel):
    """Collection of findings from one review run."""

    findings: list[Finding] = Field(default_factory=list)
    persona: str = ""
    review_id: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def add(self, finding: Finding):
        self.findings.append(finding)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def by_autofix_class(self, cls: AutofixClass) -> list[Finding]:
        return [f for f in self.findings if f.autofix_class == cls]

    def filter(self, min_confidence: float = 0.3) -> FindingSet:
        """Confidence gate — drop low-confidence findings."""
        return FindingSet(
            findings=[f for f in self.findings if f.confidence >= min_confidence],
            persona=self.persona,
            review_id=self.review_id,
        )

    def count(self) -> dict:
        return {
            "total": len(self.findings),
            "P0": len(self.by_severity(Severity.P0)),
            "P1": len(self.by_severity(Severity.P1)),
            "P2": len(self.by_severity(Severity.P2)),
            "P3": len(self.by_severity(Severity.P3)),
            "safe_auto": len(self.by_autofix_class(AutofixClass.safe_auto)),
            "manual": len(self.by_autofix_class(AutofixClass.manual)),
        }

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_data(cls, data: dict | list, persona: str = "") -> "FindingSet":
        """Parse raw findings data from a JSON response (json_object mode).

        Accepts both:
          {"findings": [...]}
          [...]  # bare list
        """
        if isinstance(data, list):
            raw = data
        else:
            raw = data.get("findings", [])
        if isinstance(raw, list) and raw:
            findings = []
            for item in raw:
                if isinstance(item, dict) and "severity" in item:
                    try:
                        finding = Finding(
                            persona=persona,
                            severity=item.get("severity", "P3"),
                            autofix_class=item.get("autofix_class", "advisory"),
                            confidence=float(item.get("confidence", 0.5)),
                            title=item.get("title", ""),
                            description=item.get("description", ""),
                            file=item.get("file", ""),
                            line=item.get("line"),
                            why_it_matters=item.get("why_it_matters", ""),
                            suggested_fix=item.get("suggested_fix"),
                            pre_existing=bool(item.get("pre_existing", False)),
                            requires_verification=bool(item.get("requires_verification", False)),
                        )
                        findings.append(finding)
                    except (ValueError, TypeError):
                        continue
            if findings:
                return cls(findings=findings, persona=persona)
        return cls(persona=persona)


class MergeDedupResult(BaseModel):
    """Result of merging multiple FindingSets."""

    merged: list[Finding] = Field(default_factory=list)
    duplicates_removed: int = 0
    severity_overrides: int = 0

    def by_severity(self) -> dict:
        result = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        for f in self.merged:
            sev = f.severity.value
            if sev in result:
                result[sev] += 1
        return result

    def to_dict(self) -> dict:
        return self.model_dump()
