# orbuz — Standalone Agent Workflow Runtime
#
#   User starts via CLI, provides an LLM API key and a topic,
#   and it autonomously performs Recon → produces a plan → executes → delivers.
#
#   Data models are fully consistent with docs/ at the project root.
#   The agent.yaml / plan.json / _workspace protocols are all shared.
#
#   ┌─────────────────────────────────────────────┐
#   │  orbuz/                                      │
#   │  ├── cli/         ← entry: orbuz run/status │
#   │  ├── core/        ← orchestration: recon → execute │
#   │  ├── agent/       ← sub-agent runtime + messages    │
#   │  ├── workspace/   ← _workspace file management      │
#   │  ├── schema/      ← Pydantic data models            │
#   │  └── ui/          ← interactive output              │
#   └─────────────────────────────────────────────┘
