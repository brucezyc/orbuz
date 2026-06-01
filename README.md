<p align="center">
  <h1 align="center">orbuz</h1>
  <p align="center"><strong>Multi-agent orchestration engine for research, code review, and knowledge work.</strong></p>
  <p align="center">
    <code>orbuz run "topic" --quality-model claude-sonnet-4 --api-key sk-xxx</code>
  </p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> · <a href="#how-it-works">How it Works</a> · <a href="#execution-patterns">Patterns</a> · <a href="#agent-library">Agents</a>
  </p>
</p>

---

> **⚠️ Under slow dev.** The framework is functional — you can run end-to-end workflows in mock or real mode — but APIs and agent definitions may change.

orbuz takes a topic, decomposes it into stages, dispatches specialized agents in parallel, and synthesizes results. It uses your own LLM API key — no hosted service, no vendor lock-in.

Built-in patterns: **research** (fanout + pipeline), **code review** (Compound Engineering-style tiered persona selection), and **producer-reviewer** cycles.

## Quickstart

```bash
pip install orbuz-agent-workflow

# Each tier uses a qualified model ID: <provider>/<model>
# Provider is auto-detected from the prefix — keys are resolved accordingly
orbuz run "Impact of BIS export controls on AI chip supply chains" \
  --quality-model "anthropic/claude-opus-4-8" \
  --balanced-model "anthropic/claude-sonnet-4-6" \
  --cheap-model "deepseek/deepseek-v4-flash" \
  --api-key "sk-ant-..."
```

The workflow will:
1. **Recon** — Analyze the topic and design a multi-stage execution plan
2. **Approve** — Present the plan for your review
3. **Execute** — Dispatch agents in parallel, route findings between them, checkpoint between stages
4. **Deliver** — Write synthesized output to `_workspace/{run_id}/deliver/`

> **No API key?** orbuz runs in mock mode — skip `--api-key` to preview with placeholder output.

## How It Works

### Research Workflow

```
┌──────────────────────────────────────────────────────┐
│                    orbuz CLI                         │
│  orbuz run "topic" --quality-model sonnet-4 ...      │
└──────────────┬───────────────────────────────────────┘
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

### Code Review Workflow

```
orbuz run "Review current branch" --workflow code-review

── Stage 1: Scope ──
  git diff → file list + line count + diff content

── Stage 2: Persona Selection (auto, based on diff) ──
  Always-on (7):  correctness, testing, maintainability,
                  project-standards, agent-native, simplicity, reviewer
  Cross-cutting:  security (if auth), performance (if queries),
                  api-contract, reliability, adversarial, ...
  Stack-specific: swift-ios (if .swift), ...

── Stage 3: Parallel Dispatch ──
  Each persona returns structured JSON findings

── Stage 4: Merge + Dedup ──
  Dedup by (file, line, title) — higher severity wins

── Stage 5: Confidence Gating → Report ──
  P0/P1 high-confidence → report body
  Low-confidence (gated) → appendix
```

Personas are selected automatically based on diff content. An auth-related change triggers ~12 reviewers; a simple doc change triggers 7 always-on reviewers only.

## Execution Patterns

| Pattern | Description |
|---------|-------------|
| **Fanout** | Multiple agents work in parallel, sharing findings via MessageBus across rounds |
| **Pipeline** | Agents execute sequentially, each consuming the previous agent's output |
| **Producer-Reviewer** | Agent produces content, reviewer validates — cycles until PASS |
| **Code Review** | Compound Engineering-style: scope → persona selection → parallel dispatch → merge-dedup → synthesis |

## Findings Pipeline

Code review agents produce **structured findings** with a standard schema:

```json
{
  "findings": [
    {
      "severity": "P0|P1|P2|P3",
      "confidence": 0.0-1.0,
      "title": "SQL injection in user query",
      "description": "Direct string interpolation in SQL query",
      "file": "src/db/users.py",
      "line": 42,
      "why_it_matters": "Allows arbitrary SQL execution",
      "suggested_fix": "Use parameterized queries",
      "autofix_class": "safe_auto|gated_auto|manual|advisory",
      "pre_existing": false,
      "requires_verification": false
    }
  ]
}
```

- Severity: **P0** (critical) → **P3** (minor)
- autofix_class: routes findings to auto-fix, manual fix, or advisory
- Merge-dedup: deduplicates by (file, line, title), higher severity wins
- Confidence gate: findings below 0.3 are gated

## Agent Library

orbuz ships with **35 agents** organized by persona tier:

### Always-on Reviewers (7)
| Agent | Focus |
|-------|-------|
| `ce-correctness-reviewer` | Logic errors, edge cases, state bugs |
| `ce-testing-reviewer` | Coverage gaps, weak assertions, brittle tests |
| `ce-maintainability-reviewer` | Structural quality, coupling, dead code |
| `ce-project-standards-reviewer` | AGENTS.md/CLAUDE.md compliance |
| `ce-agent-native-reviewer` | Feature accessibility for agents |
| `ce-code-simplicity-reviewer` | Over-engineering, YAGNI violations |
| `code-reviewer` | General purpose line-by-line review |

### Cross-cutting Reviewers (8)
| Agent | Triggers on | Focus |
|-------|-------------|-------|
| `ce-security-reviewer` | auth, secrets, user input | OWASP Top 10, injection, XSS |
| `ce-performance-reviewer` | queries, loops, caching | N+1, allocations, async |
| `ce-api-contract-reviewer` | routes, serializers, schemas | API contracts, versioning |
| `ce-data-migration-reviewer` | migration, schema | Reversibility, rollback |
| `ce-reliability-reviewer` | retry, timeout, error handling | Circuit breakers, fallbacks |
| `ce-adversarial-reviewer` | ≥50 lines OR high-risk | Chaos engineering, abuse cases |
| `ce-previous-comments-reviewer` | existing PR comments | Prior feedback resolution |
| `ce-architecture-strategist` | 100+ lines OR architecture | SOLID, coupling, design integrity |

### Stack-specific (1)
| Agent | File extensions | Focus |
|-------|----------------|-------|
| `ce-swift-ios-reviewer` | .swift, .xcodeproj, .storyboard | SwiftUI, UIKit, Core Data |

### CE Conditional (1)
| Agent | Triggers on | Focus |
|-------|-------------|-------|
| `ce-deployment-verification-agent` | migrations + risky DDL | Deployment checklist, rollback SQL |

### Research Agents (7)
deep-researcher, official-researcher, media-researcher, background-researcher, fact-checker, competitive-intel, paper-analyst

### Content Agents (5)
writer, editor, synthesizer, documentation-writer, merge-agent

### Engineering Agents (4)
code-reviewer, debugger, test-writer, security-auditor, data-analyst

Agent definitions live in `agents/*.yaml` and are discoverable via `orbuz agents list`.

## Workflows

Pre-built workflows in `workflows/`:

| Workflow | Description |
|----------|-------------|
| `deep-research` | Research workflow (fanout research → merge → synthesis) |
| `code-review` | Compound Engineering-style multi-agent code review |

Use `--workflow <name>` to select a workflow:
```bash
orbuz run "Review auth refactor" --workflow code-review --agent-dir agents
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `orbuz run <topic>` | Start a workflow (research by default) |
| `orbuz status` | Show current workflow state |
| `orbuz stop` | Abort running workflow |
| `orbuz agents list` | List available agents in the library |

### `orbuz run` Options

| Flag | Default | Description |
|------|---------|-------------|
| `--quality-model` | required | Qualified model ID, e.g. `anthropic/claude-opus-4-8` |
| `--balanced-model` | required | Qualified model ID, e.g. `anthropic/claude-sonnet-4-6` |
| `--cheap-model` | required | Qualified model ID, e.g. `deepseek/deepseek-v4-flash` |
| `--api-key` | `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY` | API key (applied to all providers without their own key) |
| `--api-base` | provider default | API base URL override |
| `--quality-api-key` | | Per-tier API key for the quality model's provider |
| `--quality-api-base` | | Per-tier API base for the quality model's provider |
| `--balanced-api-key` | | Per-tier API key for the balanced model's provider |
| `--balanced-api-base` | | Per-tier API base for the balanced model's provider |
| `--cheap-api-key` | | Per-tier API key for the cheap model's provider |
| `--cheap-api-base` | | Per-tier API base for the cheap model's provider |
| `--workflow-name` | auto | Workflow to execute |
| `--agent-dir` | `./agents/` | Custom agent directory |

### Model IDs

Model IDs use the format `<provider>/<model>` — the provider prefix auto-selects the API format:

| Model ID | Provider | Endpoint | API Format | Tier |
|----------|----------|----------|-----------|------|
| `anthropic/claude-opus-4-8` | Anthropic | `api.anthropic.com` | `anthropic/messages` | High |
| `anthropic/claude-sonnet-4-6` | Anthropic | `api.anthropic.com` | `anthropic/messages` | Mid |
| `anthropic/claude-haiku-4-5` | Anthropic | `api.anthropic.com` | `anthropic/messages` | Low |
| `deepseek/deepseek-v4-pro` | DeepSeek | `api.deepseek.com` | `openai/completions` | High |
| `deepseek/deepseek-v4-flash` | DeepSeek | `api.deepseek.com` | `openai/completions` | Low |
| `openai/gpt-5.5` | OpenAI | `api.openai.com` | `openai/completions` | High |
| `openai/gpt-5.4-mini` | OpenAI | `api.openai.com` | `openai/completions` | Low |
| `google/gemini-3.1-pro-preview` | Google | `generativelanguage.googleapis.com` | `openai/completions` | High |
| `google/gemini-3.1-flash-lite` | Google | `generativelanguage.googleapis.com` | `openai/completions` | Low |
| `openrouter/auto` | OpenRouter | `openrouter.ai` | `openai/completions` | Router |

Provider keys are resolved from: `--<tier>-api-key` → `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` → `--api-key`.

### Catalog

orbuz ships with a built-in model catalog (`orbuz/llm/catalog.py`) that knows 6 providers and 12 default models (Opus 4.8, Sonnet 4.6, Haiku 4.5, DeepSeek V4 Pro/Flash, GPT-5.5, GPT-5.4-mini, Gemini 3.1 Pro/Flash Lite, OpenRouter). The catalog resolves provider configs (endpoint type, base URL, headers) and merges them with model-level overrides — inspired by OpenCode's plugin-based provider system.

```python
from orbuz.llm.catalog import Catalog

cat = Catalog()
cat.add_default_models()

# Resolve a model → knows it's Anthropic, uses messages API
model = cat.resolve("anthropic/claude-sonnet-4")
print(model.endpoint_type)  # "anthropic/messages"
print(model.api_id)         # "claude-sonnet-4-20250514"
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Global fallback API key |
| `ANTHROPIC_API_BASE` | Global fallback API base URL |
| `ORBUZ_API_KEY_QUALITY` | Per-tier key for quality model |
| `ORBUZ_API_BASE_QUALITY` | Per-tier base for quality model |
| `ORBUZ_API_KEY_BALANCED` | Per-tier key for balanced model |
| `ORBUZ_API_BASE_BALANCED` | Per-tier base |

Resolution: `ORBUZ_API_KEY_<TIER>` → `ANTHROPIC_API_KEY` → `DEEPSEEK_API_KEY` → `--api-key`

## Requirements

- Python 3.10+
- An LLM API key (Anthropic, DeepSeek, or any API provider)

## Design Notes

orbuz's code review system is inspired by the [Compound Engineering Plugin](https://github.com/EveryInc/compound-engineering-plugin) (18.3k ★). The model routing layer (provider catalog, endpoint types, resolution chain) is adapted from [OpenCode](https://github.com/anomalyco/opencode)'s plugin-based provider system.

## License

[MIT](LICENSE)
