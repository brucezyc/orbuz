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
    bound = client._bound_ids["architect"]
    resolved = client.catalog.resolve(bound)
    assert not client.mock
    assert resolved is not None
    assert resolved.api_key == "test-architect-key"
    assert resolved.base_url == "https://example.invalid/v1"
    client.close()


def test_missing_higher_tiers_fall_downward():
    client = LLMClient(
        models={"balanced": "gpt-5.6-sol", "cheap": "Qwen/Qwen3.8-27B"},
        tier_config={
            "balanced": {"api_key": "k-bal", "api_base": "https://example.invalid/v1"},
            "cheap": {"api_key": "k-cheap", "api_base": "https://example.invalid/v1"},
        },
    )
    assert client.resolve_tier("architect") == "balanced"
    assert client.resolve_tier("quality") == "balanced"
    assert client.get_model_name("architect") == "gpt-5.6-sol"
    resolved = client.catalog.resolve(client._bound_ids["balanced"])
    assert resolved is not None
    assert resolved.api_key == "k-bal"
    assert not client.mock
    client.close()


def test_unqualified_model_binds_own_credentials():
    client = LLMClient(
        models={"quality": "gpt-6-astra"},
        tier_config={"quality": {"api_key": "k-q", "api_base": "https://example.invalid/v1"}},
    )
    bound = client._bound_ids["quality"]
    resolved = client.catalog.resolve(bound)
    assert bound == "tier-quality/gpt-6-astra"
    assert resolved is not None
    assert resolved.api_id == "gpt-6-astra"
    assert resolved.api_key == "k-q"
    assert resolved.base_url == "https://example.invalid/v1"
    assert not client.mock
    client.close()


def test_failed_call_falls_to_next_configured_tier():
    client = LLMClient(
        models={"quality": "bad-model", "balanced": "ok-model"},
        tier_config={
            "quality": {"api_key": "k-q", "api_base": "https://example.invalid/v1"},
            "balanced": {"api_key": "k-b", "api_base": "https://example.invalid/v1"},
        },
    )
    seen = []

    def fake_direct(model_id, *args, **kwargs):
        seen.append(model_id)
        if "bad-model" in model_id:
            return LLMResponse(content="", success=False, error="channel failed")
        return LLMResponse(content="OK", success=True, model=model_id)

    client._chat_direct = fake_direct
    resp = client.chat("quality", system="s", messages=[{"role": "user", "content": "hi"}])
    assert resp.success
    assert resp.content == "OK"
    assert seen == ["tier-quality/bad-model", "tier-balanced/ok-model"]
    client.close()


def test_unknown_provider_prefix_keeps_full_api_id():
    client = LLMClient(
        models={"cheap": "Qwen/Qwen3.8-27B"},
        tier_config={"cheap": {"api_key": "k-c", "api_base": "https://example.invalid/v1"}},
    )
    bound = client._bound_ids["cheap"]
    resolved = client.catalog.resolve(bound)
    assert bound == "tier-cheap/Qwen/Qwen3.8-27B"
    assert resolved is not None
    assert resolved.api_id == "Qwen/Qwen3.8-27B"
    assert resolved.api_key == "k-c"
    client.close()


def test_architect_config_preserves_cli_override():
    args = Namespace(architect_model="cli/model")
    _apply_config(args, {"architect": {"model": "config/model", "api_key": "test-key",
                                       "api_base": "https://example.invalid"}})
    assert args.architect_model == "cli/model"
    assert args.architect_api_key == "test-key"
    assert args.architect_api_base == "https://example.invalid"


def test_cli_run_reaches_executor(tmp_path, monkeypatch):
    from orbuz.cli.main import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["orbuz", "run", "test", "--auto",
                                    "--project-dir", str(tmp_path)])
    monkeypatch.setattr(Orchestrator, "recon", lambda self, **kwargs: {
        "workflow": {"name": "cli-regression"}, "recon_summary": {},
        "plan": {"stages": []},
    })
    main()
    status = json.loads((tmp_path / "_workspace/current/status.json").read_text())
    assert status["state"] == "completed"


def test_previous_context_reads_saved_summary(tmp_path):
    from orbuz.cli.main import _load_previous_context_fast
    assert _load_previous_context_fast(tmp_path) == ""
    summary = tmp_path / "_workspace/run/summary.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("Previous summary")
    assert _load_previous_context_fast(tmp_path) == "Previous summary"
