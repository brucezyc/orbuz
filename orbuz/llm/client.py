"""
LLM Client — Model Call Abstraction Layer
===========================================
All LLM calls go through this interface.
Currently in mock mode (returns placeholder text);
provide an API key to switch to real calls.

Usage:
    client = LLMClient({"cheap": "flash", "balanced": "gpt4", "quality": "sonnet"})
    resp = client.chat("gpt4", system="...", messages=[...])
    
    To run in mock mode only (no API):
    client = LLMClient({}, mock=True)
"""

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Literal


# ── Call result ──

@dataclass
class LLMResponse:
    content: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    success: bool = True
    error: str | None = None


# ── Client ──

class LLMClient:
    """
    Model call client.

    models = {
        "cheap": "gemini-2.0-flash",
        "balanced": "gpt-4o-mini",
        "quality": "claude-sonnet-4",
    }

    If no model_map is provided or mock=True, uses mock mode.
    """

    def __init__(self, models: dict[str, str] | None = None,
                 api_key: str | None = None,
                 api_base: str | None = None,
                 mock: bool = False):
        self.models = models or {}
        # API key priority: argument > environment variable
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.api_base = (api_base or os.environ.get("OPENAI_API_BASE", "")
                         or "https://api.openai.com/v1")
        self.mock = mock
        # If at least one real model name + key is present → use real mode
        if not self.mock and bool(self.models) and bool(self.api_key):
            self.mock = False
        else:
            self.mock = True
        self._call_count = 0

    # ── Public interface ──

    def chat(self, model_tier: str, system: str,
             messages: list[dict] | None = None,
             temperature: float = 0.5,
             max_tokens: int = 4096) -> LLMResponse:
        """
        Call the LLM.

        model_tier: cheap / balanced / quality
        system: system prompt
        messages: conversation history (optional)

        Returns LLMResponse.
        """
        model_name = self.models.get(model_tier, model_tier)
        self._call_count += 1

        if self.mock:
            return self._mock_call(model_name, system, messages)

        # Real call (waiting for specific API integration)
        return self._real_call(model_name, system, messages, temperature, max_tokens)

    def get_model_name(self, tier: str) -> str:
        """Return the actual model name for a given tier"""
        return self.models.get(tier, tier)

    def reset_stats(self):
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    # ── Mock ──

    def _mock_call(self, model: str, system: str,
                   messages: list[dict] | None) -> LLMResponse:
        """Mock mode: returns placeholder text, does not call the API"""
        task_desc = system[:200].replace("\n", " ")

        # Extract goal from messages (if user message exists)
        goal = ""
        if messages:
            for m in reversed(messages):
                if m.get("role") == "user":
                    goal = m["content"][:200]
                    break

        content = (
            f"# {model} Mock Output\n\n"
            f"## System Instructions Summary\n{task_desc}\n\n"
        )
        if goal:
            content += f"## Task\n{goal}\n\n"
        content += (
            "## Mock Results\n\n"
            "This is a mock output.\n\n"
            "In real mode this will contain the full LLM output.\n\n"
            "### Key Findings (placeholder)\n"
            "- Finding 1: Mock data A\n"
            "- Finding 2: Mock data B\n"
            "- Finding 3: Mock data C\n\n"
            "### Sources (placeholder)\n"
            "- Source 1: https://example.com/source-a\n"
            "- Source 2: https://example.com/source-b\n"
        )

        return LLMResponse(
            content=content,
            model=model,
            input_tokens=len(system) // 4,
            output_tokens=len(content) // 4,
            duration_s=0.5,
        )

    # ── Real call adapter ──

    def _real_call(self, model: str, system: str,
                   messages: list[dict] | None,
                   temperature: float,
                   max_tokens: int) -> LLMResponse:
        """
        Real LLM API call (OpenAI-compatible format).

        Supports: OpenAI, DeepSeek, vLLM, Ollama, Together AI, Groq, Fireworks, etc.
        Anthropic also works via proxies like api.convex.dev that adopt this format.

        Environment variables:
            OPENAI_API_KEY   — API key
            OPENAI_API_BASE  — Custom endpoint (default: https://api.openai.com/v1)
        """
        url = f"{self.api_base.rstrip('/')}/chat/completions"

        # Build message array
        msgs = [{"role": "system", "content": system}]
        if messages:
            msgs.extend(messages)
        if not any(m.get("role") == "user" for m in msgs):
            msgs.append({"role": "user", "content": "Please execute the above task."})

        body = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            return LLMResponse(
                success=False, content="",
                error=f"HTTP {e.code}: {body_text[:500]}",
                duration_s=time.time() - start,
            )
        except Exception as e:
            return LLMResponse(
                success=False, content="",
                error=str(e), duration_s=time.time() - start,
            )

        duration = time.time() - start
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "") or ""

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            duration_s=duration,
            success=True,
        )


# ── Convenience factory ──

def create_llm_client(models: dict[str, str] | None = None,
                      api_key: str | None = None,
                      api_base: str | None = None,
                      provider: str = "auto") -> LLMClient:
    """Factory function: create a client by provider"""
    if provider == "mock":
        return LLMClient(mock=True)
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base = api_base or os.environ.get("OPENAI_API_BASE", "")
    has_models = bool(models) if models else False
    if has_models and key:
        return LLMClient(models=models, api_key=key, api_base=base)
    return LLMClient(mock=True)


if __name__ == "__main__":
    # Test mock mode
    client = LLMClient({"cheap": "mock-1", "balanced": "mock-2"})
    resp = client.chat("balanced",
                        system="You are an official policy researcher, searching BIS regulations",
                        messages=[{"role": "user", "content": "Search BIS 2026 entity list"}])
    print(f"Model: {resp.model}, Tokens: {resp.input_tokens}+{resp.output_tokens}")
    print(resp.content[:300])
