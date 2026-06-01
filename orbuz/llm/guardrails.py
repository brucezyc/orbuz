"""
Guardrails — LLM response validation and retry layer.

Extracted and rewritten from forge (github.com/antoinezambelli/forge).
Three-stage pipeline: format rescue → step enforcement → retry budget.

Usage in orbuz:
    guardrails = Guardrails(tool_names=["search", "summarize"])
    result = guardrails.check(llm_response)
    if result.action == "execute":
        execute(result.tool_calls)
    elif result.action == "retry":
        # append result.nudge to messages and re-call LLM
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Literal


# ── Data types ──

@dataclass
class ToolCall:
    tool: str
    args: dict


# ── Rescue parsers ──

_THINK_TAG_RE = re.compile(r"\[THINK\].*?\[/THINK\]|<think>.*?</think>", re.DOTALL)
_QWEN_FUNC_RE = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_QWEN_PARAM_RE = re.compile(r"<parameter=([^>]+)>([^<]*)</parameter>")
_REHEARSAL_RE = re.compile(r"(\w+)\[ARGS\](\{.*\})", re.DOTALL)


def strip_think_tags(text: str) -> str:
    return _THINK_TAG_RE.sub("", text).strip()


def _try_rescue_json(text: str, tool_names: list[str]) -> list[ToolCall] | None:
    """Try to extract tool calls from JSON objects embedded in text."""
    cleaned = re.sub(r"```(?:json)?\s*\n?", "", text)
    cleaned = re.sub(r"```", "", cleaned)
    found: list[ToolCall] = []
    i = 0
    while i < len(cleaned):
        if cleaned[i] == "{":
            depth = 0
            for j in range(i, len(cleaned)):
                if cleaned[j] == "{":
                    depth += 1
                elif cleaned[j] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = cleaned[i: j + 1]
                        try:
                            data = json.loads(candidate)
                        except json.JSONDecodeError:
                            i = j + 1
                            break
                        if isinstance(data, dict):
                            name = data.get("tool") or data.get("name")
                            if name and name in tool_names:
                                args = data.get("args") or data.get("arguments", {})
                                found.append(ToolCall(tool=name, args=args if isinstance(args, dict) else {}))
                        i = j + 1
                        break
            else:
                i += 1
        else:
            i += 1
    return found if found else None


def _try_rescue_qwen(text: str, tool_names: list[str]) -> list[ToolCall] | None:
    """Extract Qwen XML tool calls: <function=name>...</function>"""
    found: list[ToolCall] = []
    for match in _QWEN_FUNC_RE.finditer(text):
        name = match.group(1)
        body = match.group(2)
        if name in tool_names:
            args = {}
            for pm in _QWEN_PARAM_RE.finditer(body):
                args[pm.group(1)] = pm.group(2)
            found.append(ToolCall(tool=name, args=args))
    return found if found else None


def _try_rescue_rehearsal(text: str, tool_names: list[str]) -> list[ToolCall] | None:
    """Extract Mistral/thinking rehearsal format: tool_name[ARGS]{json}"""
    found: list[ToolCall] = []
    for match in _REHEARSAL_RE.finditer(text):
        name = match.group(1)
        if name in tool_names:
            try:
                args = json.loads(match.group(2))
                found.append(ToolCall(tool=name, args=args if isinstance(args, dict) else {}))
            except json.JSONDecodeError:
                pass
    return found if found else None


def rescue_tool_calls(text: str, tool_names: list[str]) -> list[ToolCall] | None:
    """Attempt to rescue tool calls from free-text model output.

    Tries multiple formats in order: Qwen XML → rehearsal → raw JSON.
    """
    cleaned = strip_think_tags(text)
    # Qwen format
    result = _try_rescue_qwen(cleaned, tool_names)
    if result:
        return result
    # Rehearsal format
    result = _try_rescue_rehearsal(cleaned, tool_names)
    if result:
        return result
    # Raw JSON
    result = _try_rescue_json(cleaned, tool_names)
    if result:
        return result
    return None


# ── Nudge templates ──

@dataclass
class Nudge:
    content: str


def retry_nudge(raw: str) -> str:
    return (
        "Your previous response was not a valid tool call. "
        "You must respond with a valid tool call, not free text. "
        "Please try again."
    )


def unknown_tool_nudge(name: str, available: list[str]) -> str:
    return f"Tool '{name}' does not exist. Available tools: {', '.join(available)}. Call one."


def step_nudge(pending: list[str], terminal: str, tier: int = 1) -> str:
    steps = ", ".join(pending)
    tier = max(1, min(3, tier))
    messages = {
        1: f"You must first complete: {steps} before calling {terminal}.",
        2: f"You MUST call one of these now: {steps}.",
        3: f"STOP. You MUST call: {steps}. Do NOT call {terminal}.",
    }
    return messages[tier]


def prerequisite_nudge(missing: list[str]) -> str:
    return f"You must first call: {', '.join(missing)}."


# ── ResponseValidator ──

@dataclass
class ValidationResult:
    tool_calls: list[ToolCall] | None
    nudge: Nudge | None
    needs_retry: bool


class ResponseValidator:
    """Checks if LLM response contains valid tool calls. Rescues from text."""

    def __init__(self, tool_names: list[str], rescue_enabled: bool = True):
        self.tool_names = tool_names
        self.rescue_enabled = rescue_enabled
        self._retry_fn = retry_nudge

    def validate(self, response_text: str) -> ValidationResult:
        """Validate an LLM response string."""
        # Try to parse as JSON tool call(s) first
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                name = data.get("tool") or data.get("name")
                args = data.get("args") or data.get("arguments", {})
                if name and name in self.tool_names:
                    return ValidationResult(
                        tool_calls=[ToolCall(tool=name, args=args if isinstance(args, dict) else {})],
                        nudge=None, needs_retry=False,
                    )
            elif isinstance(data, list):
                calls = []
                for item in data:
                    name = item.get("tool") or item.get("name")
                    args = item.get("args") or item.get("arguments", {})
                    if name and name in self.tool_names:
                        calls.append(ToolCall(tool=name, args=args if isinstance(args, dict) else {}))
                if calls:
                    return ValidationResult(tool_calls=calls, nudge=None, needs_retry=False)
        except json.JSONDecodeError:
            pass

        # Rescue from free text
        if self.rescue_enabled:
            rescued = rescue_tool_calls(response_text, self.tool_names)
            if rescued:
                return ValidationResult(tool_calls=rescued, nudge=None, needs_retry=False)

        # Unknown tool or text
        return ValidationResult(
            tool_calls=None,
            nudge=Nudge(content=self._retry_fn(response_text)),
            needs_retry=True,
        )


# ── StepEnforcer ──

class StepEnforcer:
    """Tracks which required steps have been completed."""

    def __init__(self, required_steps: list[str] | None = None,
                 terminal_tools: frozenset[str] | None = None,
                 max_premature: int = 3):
        self.required_steps = required_steps or []
        self.terminal_tools = terminal_tools or frozenset()
        self.max_premature = max_premature
        self._completed: set[str] = set()
        self._premature_count = 0

    def record(self, tool_name: str):
        if tool_name in self.required_steps:
            self._completed.add(tool_name)

    def check(self, tool_calls: list[ToolCall]) -> tuple[bool, Nudge | None]:
        """Returns (is_blocked, nudge)."""
        for tc in tool_calls:
            # Check terminal tool premature
            if tc.tool in self.terminal_tools:
                pending = [s for s in self.required_steps if s not in self._completed]
                if pending:
                    self._premature_count += 1
                    return True, Nudge(content=step_nudge(pending, tc.tool, min(self._premature_count, 3)))
            # Check prerequisites
            # (basic check: if this tool depends on uncompleted required steps)
            # More granular prerequisite tracking can be added later
        return False, None

    @property
    def premature_exhausted(self) -> bool:
        return self._premature_count >= self.max_premature

    @property
    def all_steps_complete(self) -> bool:
        return all(s in self._completed for s in self.required_steps)


# ── ErrorTracker ──

class ErrorTracker:
    """Tracks consecutive failures for retry budget."""

    def __init__(self, max_retries: int = 3, max_tool_errors: int = 2):
        self.max_retries = max_retries
        self.max_tool_errors = max_tool_errors
        self._retries = 0
        self._tool_errors = 0

    def record_retry(self):
        self._retries += 1

    def record_tool_error(self):
        self._tool_errors += 1

    def reset_retries(self):
        self._retries = 0

    @property
    def retries_exhausted(self) -> bool:
        return self._retries >= self.max_retries

    @property
    def tool_errors_exhausted(self) -> bool:
        return self._tool_errors >= self.max_tool_errors


# ── Guardrails (public API) ──

@dataclass
class CheckResult:
    action: Literal["execute", "retry", "step_blocked", "fatal"]
    tool_calls: list[ToolCall] | None = None
    nudge: Nudge | None = None
    reason: str = ""


class Guardrails:
    """Three-stage guardrail: response validation → step enforcement → error budget.

    Usage:
        g = Guardrails(tool_names=["search", "write"])
        result = g.check("{\"tool\": \"search\", \"args\": {\"q\": \"hello\"}}")
        if result.action == "execute":
            ...
        g.record(["search"])  # after successful execution
    """

    def __init__(self, tool_names: list[str],
                 required_steps: list[str] | None = None,
                 terminal_tool: str | None = None,
                 max_retries: int = 3,
                 rescue_enabled: bool = True,
                 max_premature_attempts: int = 3):
        self._validator = ResponseValidator(tool_names=tool_names, rescue_enabled=rescue_enabled)
        self._enforcer = StepEnforcer(
            required_steps=required_steps,
            terminal_tools=frozenset([terminal_tool]) if terminal_tool else None,
            max_premature=max_premature_attempts,
        )
        self._errors = ErrorTracker(max_retries=max_retries)

    def check(self, response_text: str) -> CheckResult:
        """Validate an LLM response. Returns action + optional tool_calls/nudge."""
        # Stage 1: format validation + rescue
        validation = self._validator.validate(response_text)
        if validation.needs_retry:
            self._errors.record_retry()
            if self._errors.retries_exhausted:
                return CheckResult(action="fatal", reason="too many consecutive bad responses")
            return CheckResult(action="retry", nudge=validation.nudge)

        self._errors.reset_retries()

        # Stage 2: step enforcement
        blocked, nudge = self._enforcer.check(validation.tool_calls or [])
        if blocked:
            if self._enforcer.premature_exhausted:
                return CheckResult(action="fatal", reason="model repeatedly skipped required steps")
            return CheckResult(action="step_blocked", nudge=nudge)

        return CheckResult(action="execute", tool_calls=validation.tool_calls)

    def record(self, tool_names: list[str]):
        """Record successfully executed tools. Call after execution."""
        for name in tool_names:
            self._enforcer.record(name)

    def reset(self):
        """Reset internal state for a new conversation."""
        self._errors = ErrorTracker(max_retries=self._errors.max_retries,
                                    max_tool_errors=self._errors.max_tool_errors)
