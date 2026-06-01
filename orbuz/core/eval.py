"""
Agent Eval — Lightweight Output Quality Evaluation
====================================================
Basic sanity checks on agent outputs. Not a full benchmark —
just catches obviously bad output before it reaches the user.

Checks:
  - Empty or near-empty output
  - JSON validity (for structured-findings agents)
  - Required field presence
  - Severity/confidence range
  - Repetitive/looping output (hallucination marker)
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from orbuz.schema.finding import FindingSet, Severity


@dataclass
class EvalResult:
    """Result of evaluating an agent output."""
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    score: float = 1.0  # 0.0 (worst) to 1.0 (best)

    def add_warning(self, msg: str, penalty: float = 0.1):
        self.warnings.append(msg)
        self.score = max(0.0, self.score - penalty)

    def add_error(self, msg: str, penalty: float = 0.3):
        self.errors.append(msg)
        self.score = max(0.0, self.score - penalty)
        if self.score <= 0.0:
            self.passed = False

    def summary(self) -> str:
        parts = [f"Score: {self.score:.2f}"]
        if self.warnings:
            parts.append(f"Warnings ({len(self.warnings)}): {self.warnings[0]}")
        if self.errors:
            parts.append(f"Errors ({len(self.errors)}): {self.errors[0]}")
            if not self.passed:
                parts.append("FAILED")
        return " | ".join(parts)


def evaluate_output(output: str, agent_name: str = "",
                    required_fields: list[str] | None = None,
                    min_length: int = 20,
                    max_repetition_ratio: float = 0.5) -> EvalResult:
    """
    Evaluate the quality of an agent's output.

    Args:
        output: agent's raw text output
        agent_name: for display
        required_fields: fields that must be present (for structured output)
        min_length: minimum acceptable output length
        max_repetition_ratio: max ratio of repeated lines before flagging

    Returns:
        EvalResult with pass/fail + score
    """
    result = EvalResult()

    if not output or len(output.strip()) < min_length:
        result.add_error(
            f"Output too short ({len(output.strip())} chars, min {min_length})",
            penalty=0.5,
        )
        return result

    # Check for repetitive output (hallucination marker)
    lines = output.strip().split("\n")
    if len(lines) >= 5:
        unique_lines = len(set(line.strip() for line in lines if line.strip()))
        if unique_lines / max(len(lines), 1) < max_repetition_ratio:
            result.add_warning(
                f"Repetitive output: {unique_lines}/{len(lines)} unique lines "
                f"(ratio {unique_lines/len(lines):.2f})",
                penalty=0.15,
            )

    # Check for common hallucination phrases
    hallmarks = [
        "as an ai", "as a large language model", "i don't have access to",
        "i cannot provide", "it is not possible for me",
    ]
    output_lower = output.lower()
    for phrase in hallmarks:
        if phrase in output_lower:
            result.add_warning(
                f"Hallucination marker detected: '{phrase}'",
                penalty=0.1,
            )
            break

    return result


def evaluate_finding_set(fs: FindingSet, required_fields: list[str] | None = None) -> EvalResult:
    """
    Evaluate a set of structured findings.

    Checks:
      - Valid severity values
      - Confidence in range [0, 1]
      - Required fields present
      - Non-empty
    """
    result = EvalResult()
    required = required_fields or ["severity", "title", "file"]

    if not fs.findings:
        result.add_warning("No findings produced", penalty=0.1)
        return result

    valid_severities = {s.value for s in Severity}

    for f in fs.findings:
        if f.severity.value not in valid_severities:
            result.add_warning(f"Invalid severity '{f.severity.value}' in '{f.title}'", penalty=0.05)

        if not (0.0 <= f.confidence <= 1.0):
            result.add_warning(f"Confidence out of range ({f.confidence}) in '{f.title}'", penalty=0.05)

        if "file" in required and not f.file:
            result.add_warning(f"Missing file in finding '{f.title}'", penalty=0.05)

        if "title" in required and not f.title:
            result.add_warning("Finding missing title", penalty=0.05)

        if "severity" in required and not f.severity:
            result.add_warning("Finding missing severity", penalty=0.05)

    return result


def evaluate_agent(agent_name: str, output: str,
                   findings: FindingSet | None = None,
                   required_output_fields: list[str] | None = None) -> EvalResult:
    """
    Full evaluation of an agent run: output quality + finding quality.

    Returns a single EvalResult.
    """
    result = evaluate_output(output, agent_name=agent_name)

    if findings is not None:
        finding_result = evaluate_finding_set(findings, required_output_fields)
        result.warnings.extend(finding_result.warnings)
        result.errors.extend(finding_result.errors)
        result.score = (result.score + finding_result.score) / 2
        if not finding_result.passed:
            result.passed = False

    return result
