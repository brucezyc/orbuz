"""
Persona Selector — Compound Engineering-Style Layered Agent Selection
========================================================================
Selects reviewer personas for code-review workflows based on diff analysis.

Three tiers:
  always_on       → spawned on every review (correctness, testing, maintainability...)
  cross_cutting   → spawned when diff touches relevant domains (security, performance...)
  stack_specific  → spawned when diff touches specific file types (swift, cs...)
"""
from __future__ import annotations
from pathlib import Path
from orbuz.schema.agent import AgentDefinition, load_index, load_agent
from orbuz.schema.finding import PersonaTier


REVIEWER_ARCHETYPES = {"reviewer", "auditor"}
"""Only agents with these archetypes are selected for code review."""


def select_personas(
    diff_file_list: list[str] | None = None,
    diff_lines: int = 0,
    diff_content: str | None = None,
    agent_dir: str | Path | None = None,
    pr_has_comments: bool = False,
    agent_filter: str | None = None,
) -> list[AgentDefinition]:
    """
    Select reviewer personas based on diff analysis.

    Only selects agents with archetype='reviewer' or archetype='auditor'.
    Set agent_filter to a comma-separated list of agent names to override auto-selection.

    Returns a list of AgentDefinition objects ordered: always_on → cross_cutting → stack_specific.
    """
    base = Path(agent_dir) if agent_dir else Path.cwd() / "agents"
    index = load_index(base)

    # If explicit agent list provided, use it directly
    if agent_filter:
        names = [n.strip() for n in agent_filter.split(",")]
        return [load_agent(n, base) for n in names if n]

    selected: list[AgentDefinition] = []
    seen: set[str] = set()

    # ── Diff analysis ──
    file_extensions: set[str] = set()
    content_lower = (diff_content or "").lower()

    if diff_file_list:
        for fp in diff_file_list:
            ext = Path(fp).suffix.lower()
            if ext:
                file_extensions.add(ext)

    # ── Phase 1: always-on (reviewer archetype only) ──
    for entry in index.agents:
        if entry.persona_tier == "always_on" and entry.archetype in REVIEWER_ARCHETYPES:
            agent = load_agent(entry.name, base)
            if agent.name not in seen:
                selected.append(agent)
                seen.add(agent.name)

    # ── Phase 2: cross-cutting (domain match, reviewer archetype) ──
    for entry in index.agents:
        if entry.persona_tier != "cross_cutting":
            continue
        if entry.archetype not in REVIEWER_ARCHETYPES:
            continue
        agent = load_agent(entry.name, base)
        rules = agent.selection_rules

        if diff_lines < rules.min_lines:
            continue
        if diff_lines > rules.max_lines:
            continue

        if rules.diff_touches:
            keywords = [k.lower() for k in rules.diff_touches]
            matches_domain = any(kw in content_lower for kw in keywords)
            if not matches_domain and diff_file_list:
                file_lower = " ".join(f.lower() for f in diff_file_list)
                matches_domain = any(kw in file_lower for kw in keywords)
            if not matches_domain:
                continue

        if agent.name not in seen:
            selected.append(agent)
            seen.add(agent.name)

    # ── Phase 3: stack-specific (file extension match) ──
    for entry in index.agents:
        if entry.persona_tier != "stack_specific":
            continue
        if entry.archetype not in REVIEWER_ARCHETYPES:
            continue
        agent = load_agent(entry.name, base)
        rules = agent.selection_rules

        if rules.file_extensions:
            ext_set = set(e.lower() if e.startswith(".") else f".{e}".lower()
                          for e in rules.file_extensions)
            if not file_extensions.intersection(ext_set):
                continue

        if agent.name not in seen:
            selected.append(agent)
            seen.add(agent.name)

    # ── Phase 4: CE conditional (migration-specific) ──
    for entry in index.agents:
        if entry.persona_tier != "ce_conditional":
            continue
        if entry.archetype not in REVIEWER_ARCHETYPES:
            continue
        agent = load_agent(entry.name, base)
        rules = agent.selection_rules

        if rules.file_extensions:
            ext_set = set(e.lower() if e.startswith(".") else f".{e}".lower()
                          for e in rules.file_extensions)
            if not file_extensions.intersection(ext_set):
                continue

        if agent.name not in seen:
            selected.append(agent)
            seen.add(agent.name)

    return selected


def describe_team(agents: list[AgentDefinition]) -> str:
    """Pretty-print the selected reviewer team."""
    if not agents:
        return "## Review Team\n\nNo reviewers selected."

    lines = ["## Review Team"]
    tiers = {"always_on": "Always-on", "cross_cutting": "Cross-cutting",
             "stack_specific": "Stack-specific", "ce_conditional": "CE Conditional"}
    by_tier: dict[str, list[str]] = {}
    for a in agents:
        t = a.persona_tier.value if hasattr(a.persona_tier, 'value') else str(a.persona_tier)
        by_tier.setdefault(t, []).append(f"  - **{a.name}** — {a.summary[:60]}")

    for tier_key, label in tiers.items():
        if tier_key in by_tier:
            lines.append(f"\n### {label}")
            lines.extend(by_tier[tier_key])

    lines.append(f"\n**Total: {len(agents)} reviewers**")
    return "\n".join(lines)
