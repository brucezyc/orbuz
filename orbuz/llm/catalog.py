"""
Catalog — Model Registry + Resolution
========================================
Inspired by OpenCode's Catalog service.

Central registry for providers and their models.
Supports resolution chains: model-level → provider-level fallback.
"""
from __future__ import annotations
from pathlib import Path
from orbuz.llm.provider import ProviderConfig, ModelInfo, EndpointType, KNOWN_PROVIDERS


# ── Default model presets ──

DEFAULT_MODELS: dict[str, str] = {
    "quality": "anthropic/claude-opus-4-8",
    "balanced": "anthropic/claude-sonnet-4-6",
    "cheap": "deepseek/deepseek-v4-flash",
}


# ── Resolved Model (merged provider + model config) ──

class ResolvedModel:
    """Fully resolved model ready for API calls."""

    def __init__(self, provider: ProviderConfig, model: ModelInfo):
        self.provider = provider
        self.model = model
        self.api_id = model.api_id or model.id.split("/")[-1] if "/" in model.id else model.id
        self.endpoint_type = provider.endpoint_type
        self.base_url = provider.base_url
        self.api_key = provider.api_key

        # Merge headers: provider-level + model-level
        self.headers = dict(provider.headers)
        # api_key can also come from model level if specified

    @property
    def full_id(self) -> str:
        return self.model.id

    @property
    def is_openai_compatible(self) -> bool:
        return self.endpoint_type in (EndpointType.openai_completions, EndpointType.openai_responses)

    @property
    def is_anthropic(self) -> bool:
        return self.endpoint_type == EndpointType.anthropic_messages

    def __repr__(self) -> str:
        return f"ResolvedModel({self.full_id}, {self.endpoint_type.value})"


# ── Catalog ──

class Catalog:
    """
    Central model registry.

    Usage:
        catalog = Catalog()
        catalog.add_provider("anthropic", api_key="sk-ant-...", base_url="...")
        catalog.add_default_models()

        model = catalog.resolve("anthropic/claude-sonnet-4")
        # → ResolvedModel with merged config
    """

    def __init__(self):
        self.providers: dict[str, ProviderConfig] = {}

    # ── Provider management ──

    def add_provider(self, provider_id: str, api_key: str = "",
                     base_url: str = "", enabled: bool = True) -> ProviderConfig:
        """Add or update a provider."""
        known = KNOWN_PROVIDERS.get(provider_id, {})
        if provider_id in self.providers:
            prov = self.providers[provider_id]
            if api_key:
                prov.api_key = api_key
            if base_url:
                prov.base_url = base_url
            prov.enabled = enabled
            return prov

        prov = ProviderConfig(
            id=provider_id,
            name=known.get("name", provider_id),
            endpoint_type=known.get("endpoint_type", "openai/completions"),
            api_key=api_key,
            base_url=base_url,
            headers=dict(known.get("headers", {})),
            enabled=enabled,
        )
        self.providers[provider_id] = prov
        return prov

    def remove_provider(self, provider_id: str):
        self.providers.pop(provider_id, None)

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)

    def all_providers(self) -> list[ProviderConfig]:
        return [p for p in self.providers.values() if p.enabled]

    # ── Model management ──

    def add_model(self, provider_id: str, name: str, **kwargs) -> ModelInfo:
        """Register a model under a provider."""
        prov = self.get_provider(provider_id)
        if not prov:
            raise ValueError(f"Unknown provider: {provider_id}")
        return prov.add_model(name, **kwargs)

    def resolve(self, model_id: str) -> ResolvedModel | None:
        """
        Resolve a model ID (provider/model) to a fully configured model.

        Resolution chain:
          1. Parse provider_id / model_name
          2. Find provider and model in catalog
          3. Merge options: model-level → provider-level
        """
        if "/" not in model_id:
            return None

        provider_id, model_name = model_id.split("/", 1)
        prov = self.get_provider(provider_id)
        if not prov:
            return None

        model = prov.models.get(model_name)
        if not model:
            # Allow unresolved model IDs — catalog knows the provider
            model = ModelInfo(
                id=model_id,
                provider_id=provider_id,
                api_id=model_name,
                family=model_name,
            )

        if not model.enabled:
            return None

        return ResolvedModel(prov, model)

    def available_models(self) -> list[ModelInfo]:
        """All enabled models across all enabled providers."""
        result = []
        for prov in self.all_providers():
            for model in prov.models.values():
                if model.enabled:
                    result.append(model)
        return result

    def small_model(self, provider_id: str) -> ModelInfo | None:
        """Get the smallest/cheapest model for a provider."""
        prov = self.get_provider(provider_id)
        if not prov:
            return None
        # Prefer models with "light", "mini", "flash", "haiku" in name/family
        candidates = [m for m in prov.models.values() if m.enabled]
        for keyword in ["mini", "flash", "haiku", "light", "small"]:
            for m in candidates:
                if keyword in m.family.lower() or keyword in m.api_id.lower():
                    return m
        return candidates[0] if candidates else None

    # ── Presets ──

    def add_default_models(self):
        """Register a sensible set of default models."""

        # Anthropic
        anth = self.add_provider("anthropic")
        anth.add_model("claude-opus-4-8",     api_id="claude-opus-4-8", family="claude-opus",
                       context_limit=1000000, output_limit=131072)
        anth.add_model("claude-sonnet-4-6",   api_id="claude-sonnet-4-6", family="claude-sonnet",
                       context_limit=1000000, output_limit=65536)
        anth.add_model("claude-haiku-4-5",    api_id="claude-haiku-4-5", family="claude-haiku",
                       context_limit=200000, output_limit=65536)

        # DeepSeek (V4 — deepseek-chat/reasoner deprecated Jul 2026)
        ds = self.add_provider("deepseek")
        ds.add_model("deepseek-v4-pro",       api_id="deepseek-v4-pro", family="deepseek-v4",
                     context_limit=1000000, output_limit=8192)
        ds.add_model("deepseek-v4-flash",     api_id="deepseek-v4-flash", family="deepseek-v4",
                     context_limit=131072, output_limit=8192)

        # OpenAI
        oai = self.add_provider("openai")
        oai.add_model("gpt-5.5",              api_id="gpt-5.5", family="gpt-5.5",
                      context_limit=1100000, output_limit=131072)
        oai.add_model("gpt-5.4-mini",         api_id="gpt-5.4-mini", family="gpt-5.4",
                      context_limit=131072, output_limit=16384)

        # OpenRouter (generic routing)
        or_ = self.add_provider("openrouter")
        or_.add_model("auto", api_id="auto", family="router")

        # Google (Gemini via OpenAI-compatible proxy)
        g = self.add_provider("google")
        g.add_model("gemini-3.1-pro-preview",     api_id="gemini-3.1-pro-preview", family="gemini-pro",
                    context_limit=1000000, output_limit=65536)
        g.add_model("gemini-3.1-flash-lite",      api_id="gemini-3.1-flash-lite", family="gemini-flash",
                    context_limit=1000000, output_limit=65536)

    @staticmethod
    def parse_model_id(model_id: str) -> tuple[str, str]:
        """Parse 'provider/model' → (provider_id, model_name)."""
        if "/" not in model_id:
            return ("", model_id)
        parts = model_id.split("/", 1)
        return (parts[0], parts[1])

    @staticmethod
    def is_qualified(model_id: str) -> bool:
        """Check if the model ID includes a provider prefix."""
        return "/" in model_id


# ── Convenience builder ──

def build_catalog(api_keys: dict[str, str] | None = None,
                  base_urls: dict[str, str] | None = None) -> Catalog:
    """Build a Catalog with default models + per-provider API keys/bases."""
    catalog = Catalog()
    catalog.add_default_models()

    api_keys = api_keys or {}
    base_urls = base_urls or {}

    for provider_id, key in api_keys.items():
        prov = catalog.get_provider(provider_id)
        if prov:
            prov.api_key = key

    for provider_id, url in base_urls.items():
        prov = catalog.get_provider(provider_id)
        if prov:
            prov.base_url = url

    return catalog
