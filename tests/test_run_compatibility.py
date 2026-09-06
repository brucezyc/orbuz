"""Regression tests for CLI recon context and architect configuration."""
from argparse import Namespace
import json

from orbuz.cli.main import _apply_config
from orbuz.core.orchestrator import Orchestrator
from orbuz.llm.client import LLMClient, LLMResponse


def test_recon_accepts_cli_context_and_uses_architect(tmp_path, monkeypatch):
    client = LLMClient(api_key="test-key")
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(content=json.dumps({
            "workflow": {"name": "smoke", "description": "test"},
            "plan": {"stages": [{"id": "01_write", "pattern": "pipeline",
                "agents": [{"role": "codegen-writer", "goal": "write"}]}]},
        }))

    monkeypatch.setattr(client, "chat", chat)
    plan = Orchestrator(client).recon("Write a file", project_dir=str(tmp_path),
                                     previous_context="Previous test context")
    assert calls[0]["model_tier"] == "architect"
    prompt = calls[0]["messages"][0]["content"]
    assert str(tmp_path) in prompt
    assert "Previous test context" in prompt
    assert plan["model_used"] == client.get_model_name("architect")
    client.close()


def test_architect_tier_credentials_follow_selected_model():
    client = LLMClient(models={"architect": "deepseek/deepseek-v4-flash"},
                       tier_config={"architect": {"api_key": "test-architect-key",
                                                  "api_base": "https://example.invalid/v1"}})
    resolved = client.catalog.resolve(client.get_model_name("architect"))
    assert not client.mock
    assert resolved is not None
    assert resolved.api_key == "test-architect-key"
    assert resolved.base_url == "https://example.invalid/v1"
    client.close()


def test_architect_config_preserves_cli_override():
    args = Namespace(architect_model="cli/model")
    _apply_config(args, {"architect": {"model": "config/model", "api_key": "test-key",
                                       "api_base": "https://example.invalid"}})
    assert args.architect_model == "cli/model"
    assert args.architect_api_key == "test-key"
    assert args.architect_api_base == "https://example.invalid"
