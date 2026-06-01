"""
Agent Memory — Cross-Run Learning Storage
==========================================
Stores structured learnings from agent runs so that future runs can
benefit from prior knowledge. Inspired by Compound Engineering's
learnings-researcher and session-historian agents.

Usage:
    memory = AgentMemory("_workspace/.memory/index.json")
    memory.load()

    # Record what was learned
    memory.record_learning(
        agent="ce-correctness-reviewer",
        topic="input validation",
        finding="Beware of SQL injection in raw queries",
        tags=["security", "sql", "injection"],
        source_file="src/db/users.py",
    )

    # Retrieve relevant learnings for a new task
    learnings = memory.query(["sql", "security"])
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any


class AgentMemory:
    """
    Persistent cross-run memory for agent learnings.

    Data is stored as a JSON file keyed by agent name and topic.
    Each entry has:
      - agent: agent name that learned this
      - topic: what it's about
      - finding: the actual learning
      - tags: list of tags for retrieval
      - source_file: optional file reference
      - timestamp: when it was recorded
      - confidence: how reliable (0.0-1.0)
    """

    def __init__(self, path: str | Path = "_workspace/.memory/learnings.json"):
        self.path = Path(path)
        self._data: list[dict] = []
        self._loaded = False

    def load(self):
        """Load memory from disk."""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self._data = raw if isinstance(raw, list) else []
            except (json.JSONDecodeError, OSError):
                self._data = []
        else:
            self._data = []
        self._loaded = True

    def save(self):
        """Save memory to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))

    def record_learning(self, agent: str, topic: str, finding: str,
                        tags: list[str] | None = None,
                        source_file: str = "",
                        confidence: float = 0.7):
        """Record a learning from an agent run."""
        if not self._loaded:
            self.load()

        entry = {
            "agent": agent,
            "topic": topic,
            "finding": finding,
            "tags": tags or [],
            "source_file": source_file,
            "timestamp": time.time(),
            "confidence": max(0.0, min(1.0, confidence)),
        }
        self._data.append(entry)
        self.save()

    def query(self, keywords: list[str], limit: int = 10,
              min_confidence: float = 0.0) -> list[dict]:
        """
        Query memory for learnings matching any of the keywords.
        Returns entries sorted by relevance (timestamp desc, confidence desc).
        """
        if not self._loaded:
            self.load()

        if not keywords:
            return sorted(self._data, key=lambda x: x.get("timestamp", 0), reverse=True)[:limit]

        results = []
        for entry in self._data:
            if entry.get("confidence", 0) < min_confidence:
                continue
            text = (
                entry.get("topic", "") + " " +
                entry.get("finding", "") + " " +
                " ".join(entry.get("tags", []))
            ).lower()
            if any(kw.lower() in text for kw in keywords):
                results.append(entry)

        # Sort by: keyword match count (desc), confidence (desc), timestamp (desc)
        def _score(e):
            text = (e.get("topic", "") + " " + e.get("finding", "")).lower()
            matches = sum(1 for kw in keywords if kw.lower() in text)
            return (matches, e.get("confidence", 0), e.get("timestamp", 0))

        results.sort(key=_score, reverse=True)
        return results[:limit]

    def query_by_agent(self, agent: str, limit: int = 10) -> list[dict]:
        """Query learnings from a specific agent."""
        if not self._loaded:
            self.load()
        results = [
            e for e in self._data
            if e.get("agent", "").lower() == agent.lower()
        ]
        results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return results[:limit]

    def record_findings_from_run(self, agent_name: str, goal: str,
                                 findings: Any, tags: list[str] | None = None):
        """Batch-record structured findings from a code review run."""
        if not self._loaded:
            self.load()

        found_any = False
        for f in getattr(findings, "findings", findings if isinstance(findings, list) else []):
            title = getattr(f, "title", f.get("title", "")) if isinstance(f, (dict, object)) else ""
            desc = getattr(f, "description", f.get("description", "")) if isinstance(f, (dict, object)) else ""
            file = getattr(f, "file", f.get("file", "")) if isinstance(f, (dict, object)) else ""
            conf = float(getattr(f, "confidence", f.get("confidence", 0.7)) if isinstance(f, (dict, object)) else 0.7)
            if title and desc:
                self.record_learning(
                    agent=agent_name,
                    topic=goal[:100],
                    finding=f"{title}: {desc[:200]}",
                    tags=tags or [],
                    source_file=file,
                    confidence=conf,
                )
                found_any = True
        if not found_any:
            # Still record the run for context
            self.record_learning(
                agent=agent_name,
                topic=goal[:100],
                finding=f"Run completed: {goal[:100]}",
                tags=tags or [],
                confidence=0.3,
            )

    def clear(self):
        """Clear all memory."""
        self._data = []
        self.save()

    def count(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._data)

    def __repr__(self) -> str:
        return f"AgentMemory({self.path}, {self.count()} entries)"
