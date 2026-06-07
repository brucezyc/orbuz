"""
LLM Client — Model Call Abstraction Layer
===========================================
All LLM calls go through this interface.

Uses httpx for all HTTP transport (replaces urllib).
Supports streaming responses and structured output (JSON schema).

Usage:
    client = LLMClient()
    client.set_model("balanced", "anthropic/claude-sonnet-4")

    # Non-streaming
    resp = client.chat("balanced", system="...", messages=[...])

    # Streaming with callback
    resp = client.chat("balanced", system="...", messages=[...],
                       stream=True, on_chunk=lambda c: print(c, end=""))

    # Structured JSON output
    resp = client.chat("balanced", system="...", messages=[...],
                       response_format={"type": "json_object"})
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

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
    cost_usd: float = 0.0
    tool_calls: list[dict] | None = None
    finish_reason: str | None = None


# ── Cost tracking ──

# Approximate per-1K-token costs in USD (input, output) for common models.
# Used for cost estimation when the API doesn't return pricing.
MODEL_COST_CARDS: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":      (0.015, 0.075),
    "claude-sonnet-4-6":    (0.003, 0.015),
    "claude-haiku-4-5":     (0.001, 0.005),
    "deepseek-v4-pro":      (0.002, 0.008),
    "deepseek-v4-flash":    (0.0005, 0.002),
    "gpt-5.5":              (0.010, 0.040),
    "gpt-5.4-mini":         (0.0015, 0.006),
    "gemini-3.1-pro-preview":  (0.002, 0.010),
    "gemini-3.1-flash-lite":   (0.0003, 0.0015),
}


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    """Estimate cost in USD for a model call based on token counts."""
    for key, (cost_in, cost_out) in MODEL_COST_CARDS.items():
        if key in model:
            return (in_tokens / 1000 * cost_in) + (out_tokens / 1000 * cost_out)
    # Fallback: estimate ~$3/M input, $15/M output (conservative Opus-like)
    return (in_tokens / 1000 * 0.003) + (out_tokens / 1000 * 0.015)


# ── Environment variable resolution ──

def _resolve_global_key() -> str:
    """Resolve global fallback API key.
    Chain: ANTHROPIC_API_KEY -> DEEPSEEK_API_KEY -> OPENAI_API_KEY -> empty."""
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

    Uses httpx internally for all HTTP transport.
    Supports streaming via the stream=True + on_chunk callback.

    Each model tier (quality/balanced/cheap) maps to a qualified model ID
    like "anthropic/claude-sonnet-4". The catalog resolves provider + model
    config, then the client calls the appropriate API format.
    """

    TIERS = ("quality", "balanced", "cheap")

    def __init__(self, models: dict[str, str] | None = None,
                 api_key: str | None = None,
                 api_base: str | None = None,
                 tier_config: dict[str, dict] | None = None,
                 mock: bool = False,
                 guardrails: str | None = None,
                 guardrails_tools: str | None = None):
        """
        models: dict of tier -> qualified model ID, e.g.
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
                pid = Catalog.parse_model_id(tc["model_id"])[0]
                prov = self.catalog.get_provider(pid)
                if prov:
                    if tc.get("api_key"):
                        prov.api_key = tc["api_key"]
                    if tc.get("api_base"):
                        prov.base_url = tc["api_base"]

        # Set tier -> model mapping
        self.tier_models: dict[str, str] = {}
        if models:
            # Filter out None values so defaults still apply
            for k, v in models.items():
                if v is not None:
                    self.tier_models[k] = v
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

        # Guardrails setup
        self.guardrails_enabled = guardrails == "on"
        self._guardrails = None
        if self.guardrails_enabled and guardrails_tools:
            tool_names = [t.strip() for t in guardrails_tools.split(",")]
            from orbuz.llm.guardrails import Guardrails
            self._guardrails = Guardrails(tool_names=tool_names)

        # httpx session with connection pooling
        self._http = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
        )

    def set_model(self, tier: str, model_id: str):
        """Set the model for a tier (e.g., 'balanced' -> 'anthropic/claude-sonnet-4')."""
        self.tier_models[tier] = model_id

    def _has_any_key(self) -> bool:
        for prov in self.catalog.all_providers():
            if prov.api_key:
                return True
        return False

    def get_model_name(self, tier: str) -> str:
        return self.tier_models.get(tier, "")

    def get_cost_summary(self) -> dict:
        """Return per-tier cost and total cost for all calls this session."""
        # This is a simplified placeholder; real cost tracking requires
        # accumulating during calls. We return what we know.
        return {"note": "Enable per-call cost tracking for detailed numbers"}

    def close(self):
        """Close the httpx session."""
        self._http.close()

    # ── Public interface ──

    def chat(self, model_tier: str, system: str,
             messages: list[dict] | None = None,
             temperature: float = 0.5,
             max_tokens: int = 4096,
             stream: bool = False,
             on_chunk: Callable[[str], None] | None = None,
             response_format: dict | None = None,
             tools: list[dict] | None = None) -> LLMResponse:
        """
        Call the LLM.

        model_tier: cheap / balanced / quality (maps to a model ID)
        system: system prompt
        messages: conversation history (optional)
        stream: if True, use SSE streaming (calls on_chunk for each content delta)
        on_chunk: callback for streaming content deltas (called with str)
        response_format: structured output config, e.g. {"type": "json_object"}
                         or {"type": "json_schema", "json_schema": {...}}

        Returns LLMResponse.
        """
        model_id = self.tier_models.get(model_tier, model_tier)
        self._call_count += 1

        if self.mock:
            return self._mock_call(model_id, system, messages)

        resolved = self.catalog.resolve(model_id)

        # Wrap the call with guardrails if enabled
        if self.guardrails_enabled and self._guardrails:
            return self._chat_with_guardrails(
                model_id, system, messages, temperature, max_tokens,
                stream, on_chunk, response_format, resolved, tools,
            )

        # Normal call (no guardrails)
        return self._chat_direct(
            model_id, system, messages, temperature, max_tokens,
            stream, on_chunk, response_format, resolved, tools,
        )

    def _chat_direct(self, model_id, system, messages, temperature,
                     max_tokens, stream, on_chunk, response_format, resolved,
                     tools=None):
        """Direct LLM call without guardrails."""
        if not resolved:
            return self._call_openai_compatible(
                model=model_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=self._first_available_key(),
                api_base="",
                stream=stream,
                on_chunk=on_chunk,
                response_format=response_format,
                tools=tools,
            )

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
                stream=stream,
                on_chunk=on_chunk,
                response_format=response_format,
                tools=tools,
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
                stream=stream,
                on_chunk=on_chunk,
                # Anthropic doesn't have native response_format,
                # but we can still pass it for compatible APIs
                response_format=response_format if resolved.is_openai_compatible else None,
            )
        else:
            return self._call_openai_compatible(
                model=resolved.api_id,
                system=system,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=resolved.api_key,
                api_base=resolved.base_url,
                extra_headers=resolved.headers,
                stream=stream,
                on_chunk=on_chunk,
                response_format=response_format,
            )

    def _first_available_key(self) -> str:
        for prov in self.catalog.all_providers():
            if prov.api_key:
                return prov.api_key
        return ""

    def _chat_with_guardrails(self, model_id, system, messages, temperature,
                               max_tokens, stream, on_chunk, response_format, resolved,
                               tools=None):
        """Call LLM with guardrail retry loop."""
        from orbuz.llm.guardrails import Guardrails
        assert self._guardrails is not None, "guardrails must be enabled"
        local_messages = list(messages or [])
        resp: LLMResponse | None = None

        for attempt in range(5):  # max 5 retries
            resp = self._chat_direct(
                model_id, system, local_messages, temperature,
                max_tokens, stream, on_chunk, response_format, resolved,
                tools=tools,
            )
            if not resp.success:
                return resp  # pass through errors

            result = self._guardrails.check(resp.content)
            if result.action == "execute":
                self._guardrails.reset()
                # Store parsed tool calls in response metadata (if needed)
                return resp
            elif result.action == "retry":
                local_messages.append({"role": "assistant", "content": resp.content})
                local_messages.append({"role": "user", "content": result.nudge.content})
                continue
            elif result.action == "step_blocked":
                local_messages.append({"role": "assistant", "content": resp.content})
                local_messages.append({"role": "user", "content": result.nudge.content})
                continue
            else:  # fatal
                return LLMResponse(
                    content=resp.content,
                    model=resp.model,
                    success=False,
                    error=result.reason,
                )
        return resp  # return last response if max retries reached

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
                                extra_headers: dict | None = None,
                                stream: bool = False,
                                on_chunk: Callable[[str], None] | None = None,
                                response_format: dict | None = None,
                                tools: list[dict] | None = None) -> LLMResponse:
        """Call an OpenAI-compatible /v1/chat/completions API via httpx."""
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
        if response_format:
            body["response_format"] = response_format
        if tools:
            body["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        if extra_headers:
            headers.update(extra_headers)

        start = time.time()

        if stream:
            return self._stream_openai_compatible(
                url, body, headers, model, start, on_chunk
            )

        # Non-streaming: httpx POST
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            body_text = e.response.text[:500] if e.response else str(e)
            return LLMResponse(
                success=False, content="",
                error=f"HTTP {e.response.status_code}: {body_text}",
                duration_s=time.time() - start,
            )
        except Exception as e:
            return LLMResponse(
                success=False, content="",
                error=str(e), duration_s=time.time() - start,
            )

        duration = time.time() - start
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage", {})
        in_t = usage.get("prompt_tokens", 0)
        out_t = usage.get("completion_tokens", 0)
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            input_tokens=in_t,
            output_tokens=out_t,
            duration_s=duration,
            success=True,
            cost_usd=_estimate_cost(model, in_t, out_t),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    def _stream_openai_compatible(self, url: str, body: dict,
                                  headers: dict, model: str,
                                  start: float,
                                  on_chunk: Callable[[str], None] | None) -> LLMResponse:
        """SSE streaming for OpenAI-compatible endpoints."""
        body["stream"] = True
        full_content = ""
        in_tokens_est = 0
        out_tokens_est = 0

        try:
            with self._http.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    if not data_str:
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    usage = chunk.get("usage", {})
                    if usage:
                        in_tokens_est = usage.get("prompt_tokens", in_tokens_est)
                        out_tokens_est = usage.get("completion_tokens", out_tokens_est)

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_content += content
                        if on_chunk:
                            on_chunk(content)
        except httpx.HTTPStatusError as e:
            err_body = e.response.text[:500] if e.response else str(e)
            return LLMResponse(
                success=False, content=full_content if full_content else "",
                error=f"HTTP {e.response.status_code}: {err_body}",
                duration_s=time.time() - start,
            )
        except Exception as e:
            return LLMResponse(
                success=False, content=full_content if full_content else "",
                error=str(e), duration_s=time.time() - start,
            )

        duration = time.time() - start
        return LLMResponse(
            content=full_content,
            model=model,
            input_tokens=in_tokens_est,
            output_tokens=out_tokens_est,
            duration_s=duration,
            success=True,
            cost_usd=_estimate_cost(model, in_tokens_est, out_tokens_est),
        )

    # ── Anthropic Messages API (anthropic/messages) ──

    def _call_anthropic(self, model: str, system: str,
                        messages: list[dict] | None,
                        temperature: float, max_tokens: int,
                        api_key: str = "", api_base: str = "",
                        extra_headers: dict | None = None,
                        stream: bool = False,
                        on_chunk: Callable[[str], None] | None = None,
                        response_format: dict | None = None) -> LLMResponse:
        """Call Anthropic's /v1/messages API via httpx."""
        base = api_base or "https://api.anthropic.com/v1"
        key = api_key or self._first_available_key()
        url = f"{base.rstrip('/')}/messages"

        anthropic_messages = []
        if messages:
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    continue
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

        start = time.time()

        if stream:
            return self._stream_anthropic(url, body, headers, model, start, on_chunk)

        # Non-streaming: httpx POST
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            body_text = e.response.text[:500] if e.response else str(e)
            return LLMResponse(
                success=False, content="",
                error=f"HTTP {e.response.status_code}: {body_text}",
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
        in_t = usage.get("input_tokens", 0)
        out_t = usage.get("output_tokens", 0)
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            input_tokens=in_t,
            output_tokens=out_t,
            duration_s=duration,
            success=True,
            cost_usd=_estimate_cost(model, in_t, out_t),
        )

    def _stream_anthropic(self, url: str, body: dict,
                          headers: dict, model: str,
                          start: float,
                          on_chunk: Callable[[str], None] | None) -> LLMResponse:
        """SSE streaming for Anthropic Messages API."""
        body["stream"] = True
        full_content = ""
        in_tokens_est = 0
        out_tokens_est = 0

        try:
            with self._http.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                buffer = ""
                for line in resp.iter_lines():
                    if not line:
                        continue
                    # Anthropic SSE format: event: ..., data: {...}
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if not data_str:
                            continue
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # message_start -> initial metadata with usage
                        if chunk.get("type") == "message_start":
                            msg = chunk.get("message", {})
                            usage = msg.get("usage", {})
                            in_tokens_est = usage.get("input_tokens", 0)

                        # content_block_delta -> actual text content
                        if chunk.get("type") == "content_block_delta":
                            delta = chunk.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                full_content += text
                                if on_chunk:
                                    on_chunk(text)

                        # message_delta -> final usage
                        if chunk.get("type") == "message_delta":
                            usage = chunk.get("usage", {})
                            out_tokens_est = usage.get("output_tokens", 0)
        except httpx.HTTPStatusError as e:
            err_body = e.response.text[:500] if e.response else str(e)
            return LLMResponse(
                success=False, content=full_content if full_content else "",
                error=f"HTTP {e.response.status_code}: {err_body}",
                duration_s=time.time() - start,
            )
        except Exception as e:
            return LLMResponse(
                success=False, content=full_content if full_content else "",
                error=str(e), duration_s=time.time() - start,
            )

        duration = time.time() - start
        return LLMResponse(
            content=full_content,
            model=model,
            input_tokens=in_tokens_est,
            output_tokens=out_tokens_est,
            duration_s=duration,
            success=True,
            cost_usd=_estimate_cost(model, in_tokens_est, out_tokens_est),
        )
