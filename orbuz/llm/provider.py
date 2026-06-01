"""
Provider — Model Provider Definitions
========================================
Inspired by OpenCode's plugin-based provider + catalog system.

Each provider has an endpoint type, base URL, headers, and a set of models.
Model IDs use the format: <provider_id>/<model_name>
  e.g., "anthropic/claude-sonnet-4", "deepseek/deepseek-chat", "openrouter/openai/gpt-5"

Endpoint types:
  openai/completions  → /v1/chat/completions (DeepSeek, Together, Groq, etc.)
  anthropic/messages  → /v1/messages (Anthropic native)
  openai/responses    → /v1/responses (OpenAI Responses API)
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from enum import Enum
from typing import Literal


class EndpointType(str, Enum):
    openai_completions = "openai/completions"
    anthropic_messages = "anthropic/messages"
    openai_responses = "openai/responses"


class ModelCapabilities(BaseModel):
    """What a model can do."""
    tools: bool = True
    streaming: bool = True
    image_input: bool = False
    audio_input: bool = False


class ModelInfo(BaseModel):
    """A model registered under a provider."""

    id: str = ""
    """Full model ID: <provider>/<name>, e.g. 'anthropic/claude-sonnet-4'."""

    provider_id: str = ""
    """Short provider ID, e.g. 'anthropic'."""

    api_id: str = ""
    """The actual model name sent to the API, e.g. 'claude-sonnet-4-20250514'."""

    family: str = ""
    """Grouping name, e.g. 'claude-sonnet'. Used for fallback selection."""

    capabilities: ModelCapabilities = ModelCapabilities()

    context_limit: int = 0
    output_limit: int = 0
    cost_input: float = 0.0
    cost_output: float = 0.0
    enabled: bool = True

    def display_name(self) -> str:
        return self.id or f"{self.provider_id}/{self.api_id}"


class ProviderConfig(BaseModel):
    """Configuration for a single provider."""

    id: str
    """Unique ID, e.g. 'anthropic', 'deepseek'."""

    name: str = ""
    """Human-readable name, e.g. 'Anthropic'."""

    endpoint_type: EndpointType = EndpointType.openai_completions
    api_key: str = ""
    base_url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    models: dict[str, ModelInfo] = Field(default_factory=dict)
    """Keyed by model name (without provider prefix)."""

    def add_model(self, name: str, **kwargs) -> ModelInfo:
        """Register a model under this provider."""
        full_id = f"{self.id}/{name}"
        model = ModelInfo(
            id=full_id,
            provider_id=self.id,
            api_id=kwargs.pop("api_id", name),
            **kwargs,
        )
        self.models[name] = model
        return model


KNOWN_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "name": "Anthropic",
        "endpoint_type": "anthropic/messages",
        "headers": {"anthropic-version": "2023-06-01"},
    },
    "deepseek": {
        "name": "DeepSeek",
        "endpoint_type": "openai/completions",
    },
    "openai": {
        "name": "OpenAI",
        "endpoint_type": "openai/completions",
    },
    "openrouter": {
        "name": "OpenRouter",
        "endpoint_type": "openai/completions",
    },
    "google": {
        "name": "Google",
        "endpoint_type": "openai/completions",
    },
    "github-copilot": {
        "name": "GitHub Copilot",
        "endpoint_type": "openai/completions",
    },
}
