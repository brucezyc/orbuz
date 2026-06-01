"""
LLM Client — Model Call Abstraction Layer
===========================================
All LLM calls go through this interface.
Currently in mock mode (returns placeholder text);
provide an API key to switch to real calls.

Supports per-tier API keys and base URLs for quality/balanced/cheap tiers,
each potentially from a different provider.

Usage:
    client = LLMClient({"cheap": "flash", "balanced": "sonnet", "quality": "opus"})
    resp = client.chat("quality", system="...", messages=[...])

    With per-tier keys:
    client = LLMClient(models, tier_config={
        "quality": {"api_key": "sk-ant-...", "api_base": "https://api.anthropic.com/v1"},
        "balanced": {"api_key": "sk-ds-...", "api_base": "https://api.deepseek.com/v1"},
    })

    To run in mock mode only (no API):
    client = LLMClient({}, mock=True)
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Literal


# ── Default API base ──

DEFAULT_API_BASE = "https://api.deepseek.com/v1"
"""Default base URL. DeepSeek uses the OpenAI-compatible /chat/completions format.

Supports: DeepSeek, vLLM, Ollama, Together AI, Groq, Fireworks, Anyscale, etc.
Anthropic users: set a proxy base URL (e.g., api.convex.dev) or set per-tier base.
"""


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


# ── Environment variable resolution ──

def _resolve_global_key() -> str:
    """Resolve the global fallback API key.
    Chain: ANTHROPIC_API_KEY → DEEPSEEK_API_KEY → empty string."""
    return (os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("DEEPSEEK_API_KEY", ""))


def _resolve_global_base() -> str:
    """Resolve the global fallback API base URL.
    Chain: ANTHROPIC_API_BASE → DEEPSEEK_API_BASE → DEFAULT_API_BASE."""
    return (os.environ.get("ANTHROPIC_API_BASE", "")
            or os.environ.get("DEEPSEEK_API_BASE", "")
            or DEFAULT_API_BASE)


# ── Client ──

class LLMClient:
    """
    Model call client with per-tier API key/base support.

    models = {
        "cheap": "claude-sonnet-4",
        "balanced": "deepseek-chat",
        "quality": "claude-opus-4",
    }

    Per-tier resolution order (for each tier):
      1. tier_config[tier]["api_key"] (from --quality-api-key etc.)
      2. ORBUZ_API_KEY_<TIER> env var (e.g. ORBUZ_API_KEY_QUALITY)
      3. Global api_key (ANTHROPIC_API_KEY → DEEPSEEK_API_KEY)

    Same chain for api_base.
    """

    TIERS = ("quality", "balanced", "cheap")

    def __init__(self, models: dict[str, str] | None = None,
                 api_key: str | None = None,
                 api_base: str | None = None,
                 tier_config: dict[str, dict] | None = None,
                 mock: bool = False):
        self.models = models or {}
        self.tier_config = tier_config or {}

        # Global fallbacks (user-supplied arg wins over env var)
        self._global_key = api_key or _resolve_global_key()
        self._global_base = (api_base
                             or os.environ.get("ANTHROPIC_API_BASE", "")
                             or os.environ.get("DEEPSEEK_API_BASE", "")
                             or DEFAULT_API_BASE)

        # Pre-resolve per-tier keys/bases
        self._tier_keys: dict[str, str] = {}
        self._tier_bases: dict[str, str] = {}
        for tier in self.TIERS:
            self._tier_keys[tier] = self._resolve_key(tier)
            self._tier_bases[tier] = self._resolve_base(tier)

        self.mock = mock
        has_any_key = any(self._tier_keys.values())
        if not self.mock and bool(self.models) and has_any_key:
            self.mock = False
        else:
            self.mock = True
        self._call_count = 0

    def _resolve_key(self, tier: str) -> str:
        """Resolve API key for a tier: tier_config → env var → global"""
        tc = self.tier_config.get(tier, {})
        if tc.get("api_key"):
            return tc["api_key"]
        env_key = os.environ.get(f"ORBUZ_API_KEY_{tier.upper()}", "")
        if env_key:
            return env_key
        return self._global_key

    def _resolve_base(self, tier: str) -> str:
        """Resolve API base for a tier: tier_config → env var → global → default"""
        tc = self.tier_config.get(tier, {})
        if tc.get("api_base"):
            return tc["api_base"]
        env_base = os.environ.get(f"ORBUZ_API_BASE_{tier.upper()}", "")
        if env_base:
            return env_base
        return self._global_base

    def get_key(self, tier: str) -> str:
        """Get the resolved API key for a tier"""
        return self._tier_keys.get(tier, self._global_key)

    def get_base(self, tier: str) -> str:
        """Get the resolved API base URL for a tier"""
        return self._tier_bases.get(tier, self._global_base)

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

        api_key = self.get_key(model_tier)
        api_base = self.get_base(model_tier)
        return self._real_call(model_name, system, messages,
                               temperature, max_tokens,
                               api_key=api_key, api_base=api_base)

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

    # ── Real call ──

    def _real_call(self, model: str, system: str,
                   messages: list[dict] | None,
                   temperature: float,
                   max_tokens: int,
                   api_key: str = "",
                   api_base: str = "") -> LLMResponse:
        """
        Real LLM API call (OpenAI-compatible format).

        Supports: DeepSeek, vLLM, Ollama, Together AI, Groq, Fireworks, etc.
        Anthropic also works via proxies that adopt this format (e.g. api.convex.dev).

        api_key and api_base are resolved per-tier by chat() and passed in.
        """
        base = api_base or self._global_base
        key = api_key or self._global_key
        url = f"{base.rstrip('/')}/chat/completions"

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
                "Authorization": f"Bearer {key}",
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
                      tier_config: dict[str, dict] | None = None,
                      provider: str = "auto") -> LLMClient:
    """Factory function: create a client by provider"""
    if provider == "mock":
        return LLMClient(mock=True)
    key = api_key or _resolve_global_key()
    base = api_base or _resolve_global_base()
    has_models = bool(models) if models else False
    if has_models and key:
        return LLMClient(models=models, api_key=key, api_base=base,
                         tier_config=tier_config)
    if has_models and tier_config and any(
        tc.get("api_key") for tc in tier_config.values()
    ):
        return LLMClient(models=models, api_key=key, api_base=base,
                         tier_config=tier_config)
    return LLMClient(mock=True)


if __name__ == "__main__":
    # Test mock mode
    client = LLMClient({"cheap": "claude-sonnet-4", "balanced": "deepseek-chat"})
    resp = client.chat("balanced",
                        system="You are an official policy researcher, searching BIS regulations",
                        messages=[{"role": "user", "content": "Search BIS 2026 entity list"}])
    print(f"Model: {resp.model}, Tokens: {resp.input_tokens}+{resp.output_tokens}")
    print(resp.content[:300])
