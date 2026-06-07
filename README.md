<p align="center">
  <h1 align="center">orbuz</h1>
  <p align="center"><strong>Multi-agent orchestration engine for research, code review, and code generation.</strong></p>
  <p align="center">
    <code>orbuz run "topic" --quality-model anthropic/claude-sonnet-4</code>
  </p>
</p>

---

Facilitates multi-agent workflows using your own LLM API key. The orchestrator decomposes a goal into stages, dispatches agents in parallel or sequence, and synthesizes results.

Built-in patterns: **fanout** (parallel agents + merge), **pipeline** (sequential chaining), **producer-reviewer** (generate → review → cycle), **codegen** (sequential codegen agents with file write + compile actions).

## Quickstart

```bash
pip install orbuz-agent-workflow

orbuz run "Impact of BIS export controls on AI chip supply chains" \
  --quality-model "anthropic/claude-sonnet-4-6" \
  --balanced-model "anthropic/claude-sonnet-4-6" \
  --cheap-model "deepseek/deepseek-v4-flash"
```

The workflow:
1. **Recon** — Analyzes topic, designs multi-stage plan
2. **Approve** — Presents the plan for review (skip with `--auto`)
3. **Execute** — Dispatches agents, routes findings, checkpoints between stages
4. **Deliver** — Output to `_workspace/{run_id}/deliver/`

Omit `--api-key` to run in mock mode.

## Code Generation

```bash
orbuz run "Build an axum+maud web server for drum transcription" \
  --project-dir ./drum-transcriber
```

Agents output structured `---actions---` blocks that the executor runs automatically:

```yaml
---actions---
- write_file: src/main.rs
  content: |
    use axum::{Router, routing::get};
  - run: cargo check
---
```

Supported actions: `write_file`, `run`, `append_file`, `delete`, `rename`.

## Agent Library

40+ built-in agents in `agents/`. Use `orbuz list agents` to browse. Agents are YAML files with: role, principles, constraints, output format, model tier, and optional MCP tool bindings.

For codegen tasks, agents tagged with `codegen` (planner → writer → compiler → reporter) are recommended.

## Model Tiers

| Tier | Use | Recommended |
|------|-----|-------------|
| `quality` | Planning, synthesis, code review | Claude Sonnet 4, DeepSeek V4 Pro |
| `balanced` | Default drafting | DeepSeek V4 Flash |
| `cheap` | Info gathering, summaries | DeepSeek V4 Flash |

Full model ID format: `<provider>/<model>` (e.g., `anthropic/claude-sonnet-4-6`, `deepseek/deepseek-v4-flash`). Provider is auto-detected from the prefix.

## Resume

Interrupted runs can be resumed:
```bash
orbuz run "..." --resume
```
Skips completed stages and continues from the last checkpoint.

## Cost Tracking

Every run shows per-agent token usage and estimated cost:
```
💰 $0.0950 | 8,250 tokens total
   deep-researcher: $0.0420 (2 calls)
   media-researcher: $0.0300 (1 calls)
```

## Configuration

Define providers in code — see `orbuz/llm/provider.py`. API keys from environment variables (e.g., `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`). Per-tier API keys can override the default:
```bash
orbuz run "..." --quality-api-key "sk-..." --quality-api-base "https://..."
```

## Architecture

```
CLI → Orchestrator (recon → plan.json)
       → Executor (fanout / pipeline / producer_reviewer / codegen)
         → Dispatcher (agent dispatch w/ MCP tools, cost tracking)
           → LLM Client (model routing, streaming, retry)
```

Output: `_workspace/{run_id}/` with per-stage summaries and a final deliverable.
