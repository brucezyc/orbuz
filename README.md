<p align="center">
  <h1 align="center">orbuz</h1>
  <p align="center"><strong>Agent orchestration workflow engine for multi-agent research and synthesis.</strong></p>
  <p align="center">
    <code>orbuz run "topic" --quality-model opus-4.8 --api-key sk-xxx</code>
  </p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> · <a href="#how-it-works">How it Works</a> · <a href="#cli-reference">CLI</a> · <a href="#agent-library">Agents</a>
  </p>
</p>

---

> **⚠️ Under slow dev.** The framework is functional — you can run end-to-end workflows in mock or real mode — but APIs and agent definitions may change. Feedback and contributions welcome.

orbuz takes a research topic, automatically decomposes it into stages, dispatches specialized agents, and synthesizes the results into a structured deliverable. It uses your own LLM API key — no hosted service, no vendor lock-in.

## Quickstart

```bash
pip install orbuz-agent-workflow

# Set your API key(s) — each tier can use a different provider
export OPENAI_API_KEY="sk-..."

# Run a research workflow
orbuz run "Impact of BIS export controls on AI chip supply chains" \
  --quality-model claude-opus-4-20250514 \
  --balanced-model claude-sonnet-4-20250514 \
  --cheap-model claude-sonnet-4-20250514
```

The workflow will:
1. **Recon** — Analyze the topic and design a multi-stage execution plan
2. **Approve** — Present the plan for your review
3. **Execute** — Dispatch agents in parallel, route findings between them, checkpoint between stages
4. **Deliver** — Write synthesized output to `_workspace/{run_id}/deliver/`

> **No API key?** orbuz runs in mock mode — skip `--api-key` to preview the full flow with placeholder output.

**Per-tier providers:**

Each model tier can use a different provider. Set per-tier API keys and bases via environment variables, or use a single key for all:

```bash
# Example: quality via Anthropic, balanced/cheap via DeepSeek
export ORBUZ_API_KEY_QUALITY="sk-ant-..."
export ORBUZ_API_BASE_QUALITY="https://api.anthropic.com/v1"
export ORBUZ_API_KEY_BALANCED="sk-ds-..."
export ORBUZ_API_BASE_BALANCED="https://api.deepseek.com/v1"

orbuz run "..." \
  --quality-model claude-opus-4-20250514 \
  --balanced-model deepseek-chat \
  --cheap-model deepseek-chat
```

Fallback: `ORBUZ_API_KEY_<TIER>` → `OPENAI_API_KEY` → `--api-key`. Same chain for `_BASE`.

## How It Works

```
┌────────────────────────────────────────────────────┐
│                    orbuz CLI                       │
│  orbuz run "topic" --quality-model opus-4.8 ...    │
└──────────────┬─────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────┐
│  Phase 1: Recon              │  ← Quality LLM analyzes topic,
│  Orchestrator → plan.json    │     selects agents, designs plan
└──────────────┬───────────────┘
               │ User approves
               ▼
┌──────────────────────────────┐
│  Phase 2: Execute            │
│  ┌── Stage 1: Fanout ────┐  │  ← Parallel agents with MessageBus
│  │  Official Researcher   │  │     claims routing between rounds
│  │  Media Researcher      │──│──→ Shared folder for cross-agent data
│  │  Background Researcher │  │
│  └─────────┬──────────────┘  │
│            ▼                 │
│  ┌── Merge ──────────────┐  │
│  │  merge-agent           │  │  ← Synthesizes parallel outputs
│  └─────────┬──────────────┘  │
│            │ Checkpoint      │  ← You decide: continue / redirect / rerun
│            ▼                 │
│  ┌── Stage 2: Pipeline ──┐  │
│  │  Synthesizer           │  │  ← Final report
│  └─────────┬──────────────┘  │
└────────────┼────────────────┘
             ▼
  _workspace/{run_id}/deliver/
```

### Execution Patterns

| Pattern | Description |
|---------|-------------|
| **Fanout** | Multiple agents work in parallel, sharing findings via MessageBus across rounds |
| **Pipeline** | Agents execute sequentially, each consuming the previous agent's output |
| **Producer-Reviewer** | Agent produces content, reviewer validates — cycles until PASS |

## CLI Reference

| Command | Description |
|---------|-------------|
| `orbuz run <topic>` | Start a workflow |
| `orbuz status` | Show current workflow state |
| `orbuz stop` | Abort running workflow |
| `orbuz agents list` | List available agents in the library |

### `orbuz run` Options

| Flag | Default | Description |
|------|---------|-------------|
| `--quality-model` | required | Orchestrator and synthesis model |
| `--balanced-model` | required | Default execution model |
| `--cheap-model` | required | Information gathering model |
| `--api-key` | `OPENAI_API_KEY` | API key (fallback for all tiers) |
| `--api-base` | `OPENAI_API_BASE` | API endpoint URL (fallback for all tiers) |

Per-tier key/base override via environment variables: `ORBUZ_API_KEY_QUALITY`, `ORBUZ_API_BASE_QUALITY`, `ORBUZ_API_KEY_BALANCED`, etc.

## Agent Library

orbuz ships with 18 built-in agents organized by expertise:

- **Researchers** — official-researcher, media-researcher, background-researcher, deep-researcher, competitive-intel, paper-analyst, fact-checker
- **Writers** — writer, editor, synthesizer, documentation-writer, merge-agent
- **Engineers** — code-reviewer, debugger, test-writer, security-auditor, data-analyst

Each agent is defined as a YAML file in `agents/` with its own system prompt, output structure, and model tier preferences. The Orchestrator selects the right agents during Recon.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | Global fallback API key |
| `OPENAI_API_BASE` | Global fallback API base URL |
| `ORBUZ_API_KEY_QUALITY` | Per-tier key for quality model |
| `ORBUZ_API_BASE_QUALITY` | Per-tier base for quality model |
| `ORBUZ_API_KEY_BALANCED` | Per-tier key for balanced model |
| `ORBUZ_API_BASE_BALANCED` | Per-tier base for balanced model |
| `ORBUZ_API_KEY_CHEAP` | Per-tier key for cheap model |
| `ORBUZ_API_BASE_CHEAP` | Per-tier base for cheap model |

Resolution order: `ORBUZ_API_KEY_<TIER>` → `OPENAI_API_KEY` → `--api-key`

## Requirements

- Python 3.10+
- An LLM API key (OpenAI, Anthropic, DeepSeek, or any OpenAI-compatible provider)

## License

[MIT](LICENSE)
