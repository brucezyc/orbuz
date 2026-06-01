"""
LLM Client — Model Call Abstraction Layer
===========================================
All LLM calls go through this interface.

Uses Catalog + ResolvedModel for provider-aware routing.
Supports multiple endpoint types:
  openai/completions  → /v1/chat/completions (DeepSeek, Together, Groq, etc.)
  anthropic/messages  → /v1/messages (Anthropic native)
  openai/responses    → /v1/responses

Model ID format: <provider_id>/<model_name>
  e.g., "anthropic/claude-sonnet-4", "deepseek/deepseek-chat"

Usage:
    client = LLMClient()
    client.set_model("balanced", "anthropic/claude-sonnet-4")

    resp = client.chat("balanced", system="...", messages=[...])

With per-provider keys:
    client = LLMClient()
    client.catalog.get_provider("anthropic").api_key = "sk-ant-..."
    client.catalog.get_provider("deepseek").api_key = "sk-ds-..."
    client.set_model("balanced", "anthropic/claude-sonnet-4")
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from orbuz.llm.catalog import Catalog, ResolvedModel, DEFAULT_MODELS, build_catalog
from orbuz.llm.provider import EndpointType


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
    """Resolve global fallback API key.
    Chain: ANTHROPIC_API_KEY → DEEPSEEK_API_KEY → OPENAI_API_KEY → empty."""
    return (os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("DEEPSEEK_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", ""))


def _resolve_global_base() -> str:
    """Resolve global fallback base URL."""
    return (os.environ.get("ANTHROPIC_API_BASE", "")
            or os.environ.get("DEEPSEEK_API_BASE", "")
            or os.environ.get("OPENAI_API_BASE", "")
            or "")


# ── Client ──

class LLMClient:
    """
    Model call client with provider-aware routing via Catalog.

    Each model tier (quality/balanced/cheap) maps to a qualified model ID
    like "anthropic/claude-sonnet-4". The catalog resolves provider + model
    config, then the client calls the appropriate API format.
    """

    TIERS = ("quality", "balanced", "cheap")

    def __init__(self, models: dict[str, str] | None = None,
                 api_key: str | None = None,
                 api_base: str | None = None,
                 tier_config: dict[str, dict] | None = None,
                 mock: bool = False):
        """
        models: dict of tier → qualified model ID, e.g.
                {"quality": "anthropic/claude-opus-4", "balanced": "anthropic/claude-sonnet-4"}
        """
        # Build catalog with default models
        self.catalog = Catalog()
        self.catalog.add_default_models()

        # Apply global env vars to known providers
        global_key = api_key or _resolve_global_key()
        global_base = api_base or _resolve_global_base()

        # Apply global key/base to any provider that doesn't have one
        for prov in self.catalog.all_providers():
            if not prov.api_key and global_key:
                prov.api_key = global_key
            if not prov.base_url and global_base:
                prov.base_url = global_base

        # Apply per-tier overrides from CLI args
        tier_config = tier_config or {}
        self.tier_config = tier_config
        for tier in self.TIERS:
            tc = tier_config.get(tier, {})
            if tc.get("api_key") and tc.get("model_id"):
                # Per-tier specific provider
                pid = Catalog.parse_model_id(tc["model_id"])[0]
                prov = self.catalog.get_provider(pid)
                if prov:
                    if tc.get("api_key"):
                        prov.api_key = tc["api_key"]
                    if tc.get("api_base"):
                        prov.base_url = tc["api_base"]

        # Set tier → model mapping
        self.tier_models: dict[str, str] = {}
        if models:
            self.tier_models.update(models)
        # Fill in defaults for unset tiers
        for tier in self.TIERS:
            if tier not in self.tier_models:
                default = DEFAULT_MODELS.get(tier, "")
                self.tier_models[tier] = default

        self.mock = mock
        if not self.mock and self._has_any_key():
            self.mock = False
        else:
            self.mock = True
        self._call_count = 0

    def set_model(self, tier: str, model_id: str):
        """Set the model for a tier (e.g., 'balanced' → 'anthropic/claude-sonnet-4')."""
        self.tier_models[tier] = model_id

    def _has_any_key(self) -> bool:
        """Check if any provider has an API key set."""
        for prov in self.catalog.all_providers():
            if prov.api_key:
                return True
        return False

    def get_model_name(self, tier: str) -> str:
        """Return the resolved model name for a tier."""
        return self.tier_models.get(tier, "")

    # ── Public interface ──

    def chat(self, model_tier: str, system: str,
             messages: list[dict] | None = None,
             temperature: float = 0.5,
             max_tokens: int = 4096) -> LLMResponse:
        """
        Call the LLM.

        model_tier: cheap / balanced / quality (maps to a model ID)
        system: system prompt
        messages: conversation history (optional)

        Returns LLMResponse.
        """
        model_id = self.tier_models.get(model_tier, model_tier)
        self._call_count += 1

        if self.mock:
            return self._mock_call(model_id, system, messages)

        # Resolve through catalog
        resolved = self.catalog.resolve(model_id)
        if not resolved:
            # Fallback: treat as raw model ID
            return self._call_openai_compatible(
                model=model_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=self._first_available_key(),
                api_base="",
            )

        # Route by endpoint type
        if resolved.is_openai_compatible:
            return self._call_openai_compatible(
                model=resolved.api_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=resolved.api_key,
                api_base=resolved.base_url,
                extra_headers=resolved.headers,
            )
        elif resolved.is_anthropic:
            return self._call_anthropic(
                model=resolved.api_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=resolved.api_key,
                api_base=resolved.base_url,
                extra_headers=resolved.headers,
            )
        else:
            # Fallback to OpenAI-compatible
            return self._call_openai_compatible(
                model=resolved.api_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=resolved.api_key,
                api_base=resolved.base_url,
                extra_headers=resolved.headers,
            )

    def _first_available_key(self) -> str:
        """Get first non-empty API key from any provider."""
        for prov in self.catalog.all_providers():
            if prov.api_key:
                return prov.api_key
        return ""

    def reset_stats(self):
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    # ── Mock ──

    def _mock_call(self, model: str, system: str,
                   messages: list[dict] | None) -> LLMResponse:
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
            "### Key Findings (placeholder)\n"
            "- Finding 1: Mock data A\n"
            "- Finding 2: Mock data B\n"
            "- Finding 3: Mock data C\n\n"
            "### Sources (placeholder)\n"
            "- Source 1: https://example.com/source-a\n"
            "- Source 2: https://example.com/source-b\n"
        )
        return LLMResponse(
            content=content, model=model,
            input_tokens=len(system) // 4, output_tokens=len(content) // 4,
            duration_s=0.5,
        )

    # ── OpenAI-compatible (openai/completions) ──

    def _call_openai_compatible(self, model: str, system: str,
                                messages: list[dict] | None,
                                temperature: float, max_tokens: int,
                                api_key: str = "", api_base: str = "",
                                extra_headers: dict | None = None) -> LLMResponse:
        """Call an OpenAI-compatible /v1/chat/completions API."""
        base = api_base or "https://api.deepseek.com/v1"
        key = api_key or self._first_available_key()
        url = f"{base.rstrip('/')}/chat/completions"

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

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
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

    # ── Anthropic Messages API (anthropic/messages) ──

    def _call_anthropic(self, model: str, system: str,
                        messages: list[dict] | None,
                        temperature: float, max_tokens: int,
                        api_key: str = "", api_base: str = "",
                        extra_headers: dict | None = None) -> LLMResponse:
        """Call Anthropic's /v1/messages API."""
        base = api_base or "https://api.anthropic.com/v1"
        key = api_key or self._first_available_key()
        url = f"{base.rstrip('/')}/messages"

        # Convert system prompt to Anthropic's system parameter
        anthropic_messages = []
        if messages:
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    continue  # handled separately
                anthropic_messages.append({
                    "role": "assistant" if role == "assistant" else "user",
                    "content": content,
                })

        body = {
            "model": model,
            "system": system,
            "messages": anthropic_messages or [{"role": "user", "content": "Please execute the above task."}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
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
        content = ""
        if data.get("content"):
            for block in data["content"]:
                if block.get("type") == "text":
                    content += block.get("text", "")

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            duration_s=duration,
            success=True,
        )
