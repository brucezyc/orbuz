"""
CLI Printer — Formatted Output
"""
import json


def print_plan(plan: dict):
    """Display plan.json to the user (first level: summary)"""
    rs = plan.get("recon_summary", {})
    print("\n" + "=" * 60)
    print(f"🔍 Recon complete — {plan.get('workflow', {}).get('description', '')}")
    print("=" * 60)
    print(f"  Complexity: {rs.get('complexity', '?')}")
    print(f"  Estimated: ~{rs.get('estimated_total_seconds', '?')}s / ~{rs.get('estimated_total_tokens', '?')} tokens")

    if rs.get("key_findings"):
        print(f"\n  Key Findings:")
        for f in rs["key_findings"]:
            print(f"    • {f}")

    stages = plan.get("plan", {}).get("stages", [])
    print(f"\n  Split into {len(stages)} Stage(s):")
    for s in stages:
        agents = s.get("agents", [])
        roles = ", ".join(a["role"] for a in agents)
        dep = ""
        if s.get("depends_on"):
            dep = f" (depends on: {', '.join(s['depends_on'])})"
        print(f"    Stage {s.get('id', '?')} ({s.get('pattern', '?')}): {roles}{dep}")
        for a in agents:
            rationale = a.get("rationale", "")
            if rationale:
                print(f"      ├ {a['role']} — {rationale}")
            else:
                print(f"      ├ {a['role']}")
        if s.get("merge", {}).get("enabled"):
            print(f"      └ → merge: {s['merge'].get('agent_role', '?')}")

    if plan.get("agent_registry_updates"):
        print(f"\n  🆕 New Agents Created: {len(plan['agent_registry_updates'])}")
        for u in plan["agent_registry_updates"]:
            print(f"      {u.get('name', '?')} — {u.get('summary', '')}")

    if plan.get("alternatives_considered"):
        print(f"\n  Alternatives Considered:")
        for alt in plan["alternatives_considered"]:
            print(f"    • {alt.get('pattern', '?')}: {alt.get('description', '')}")
            print(f"      → Rejected: {alt.get('rejected_reason', '')}")


def print_checkpoint(event: dict):
    """Display checkpoint information"""
    summary = event.get("summary", {})
    findings = summary.get("key_findings", []) if summary else []

    print(f"\n──  ⏸ Checkpoint ──")
    print(f"Phase {event.get('stage_id', '?')} ({event.get('stage_name', '')}) complete")

    if findings:
        print(f"\n  Key Findings:")
        for f in findings:
            print(f"    • {f}")

    if "errors" in (summary or {}):
        for e in summary["errors"]:
            print(f"  ⚠️  {e}")

    print(f"\n  Next phase: {event.get('next_stage', '?')}")


def print_progress(event: dict):
    """Display progress information"""
    stage = event.get("stage", "")
    msg = event.get("msg", "")
    if event.get("round"):
        print(f"  📡 Stage {stage} Round {event['round']}: {event.get('agents', 0)} agents")
    if event.get("cycle"):
        suffix = f" → {event.get('verdict', '')}" if event.get("verdict") else ""
        print(f"  🔄 Cycle {event['cycle']}{suffix}")
    if msg:
        print(f"  {msg}")
