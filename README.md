<p align="center">
  <h1 align="center">orbuz</h1>
  <p align="center"><strong>Agent orchestration workflow engine for multi-agent research and synthesis.</strong></p>
  <p align="center">
    <code>orbuz run "topic" --quality-model gpt-4o --api-key sk-xxx</code>
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

# Set your API key
export OPENAI_API_KEY="sk-..."

# Run a research workflow
orbuz run "Impact of BIS export controls on AI chip supply chains" \
  --quality-model gpt-4o \
  --balanced-model gpt-4o-mini \
  --cheap-model gpt-4o-mini
```

The workflow will:
1. **Recon** — Analyze the topic and design a multi-stage execution plan
2. **Approve** — Present the plan for your review
3. **Execute** — Dispatch agents in parallel, route findings between them, checkpoint between stages
4. **Deliver** — Write synthesized output to `_workspace/{run_id}/deliver/`

> **No API key?** orbuz runs in mock mode — skip `--api-key` to preview the full flow with placeholder output.

**Custom API endpoints:**

```bash
# DeepSeek, Together, Groq, vLLM, Ollama — any OpenAI-compatible API
export OPENAI_API_BASE="https://api.deepseek.com/v1"
orbuz run "..." --quality-model deepseek-chat --balanced-model deepseek-chat --cheap-model deepseek-chat
```

## How It Works

```
┌────────────────────────────────────────────────────┐
│                    orbuz CLI                       │
│  orbuz run "topic" --quality-model gpt-4o ...      │
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
| `--api-key` | `OPENAI_API_KEY` | API key |
| `--api-base` | `OPENAI_API_BASE` | API endpoint URL |
| `--workflow-name` | auto | Name for this run |
| `--agent-dir` | `./agents/` | Custom agent library directory |

## Agent Library

orbuz ships with 18 built-in agents organized by expertise:

- **Researchers** — official-researcher, media-researcher, background-researcher, deep-researcher, competitive-intel, paper-analyst, fact-checker
- **Writers** — writer, editor, synthesizer, documentation-writer, merge-agent
- **Engineers** — code-reviewer, debugger, test-writer, security-auditor, data-analyst

Each agent is defined as a YAML file in `agents/` with its own system prompt, output structure, and model tier preferences. The Orchestrator selects the right agents during Recon.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | API key (also accepts `--api-key`) |
| `OPENAI_API_BASE` | API base URL, default `https://api.openai.com/v1` |

## Requirements

- Python 3.10+
- An LLM API key (OpenAI, DeepSeek, or any OpenAI-compatible provider)

## License

[MIT](LICENSE)
