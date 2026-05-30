"""
MessageBus — Inter-Agent Communication Bus
============================================
Supports three message types:
  discovery   — "I discovered X" (broadcast to relevant agents)
  request     — "I need Y" (directed to another agent or orbuz)
  directive   — "Do Z" (orbuz to an agent)

Message format follows docs/60-communication-protocol.md.
Implementation: in-memory + file persistence, supports routing by phase/round/agent.

Key interfaces:
  bus.publish(msg)              → called after agent output
  bus.route(phase)              → orbuz calls: returns agent→message mapping
  bus.pending(agent, phase)     → unread messages for an agent
"""

import json
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict


# ── Message type constants ──

DISCOVERY = "discovery"
REQUEST = "request"
DIRECTIVE = "directive"


# ── Message body ──

@dataclass
class Message:
    type: str                        # discovery / request / directive
    version: str = "1.0"
    from_: str = ""                  # sender
    to: str | None = None            # receiver (None = broadcast)
    phase: str = ""
    round: int = 1
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d

    @classmethod
    def discovery(cls, from_agent: str, phase: str, round_num: int,
                  claims: list[dict]) -> "Message":
        """Create a discovery message"""
        return cls(
            type=DISCOVERY, from_=from_agent, phase=phase, round=round_num,
            payload={"claims": claims},
        )

    @classmethod
    def request(cls, from_agent: str, to_agent: str, phase: str,
                round_num: int, action: str, target: str,
                context: str = "") -> "Message":
        """Create a request message"""
        return cls(
            type=REQUEST, from_=from_agent, to=to_agent,
            phase=phase, round=round_num,
            payload={"action": action, "target": target, "context": context},
        )

    @classmethod
    def directive(cls, to_agent: str, phase: str, round_num: int,
                  action: str, reason: str,
                  add_to_goal: str = "") -> "Message":
        """Create a directive message"""
        return cls(
            type=DIRECTIVE, from_="orbuz", to=to_agent,
            phase=phase, round=round_num,
            payload={"action": action, "reason": reason, "add_to_goal": add_to_goal},
        )


# ── Communication schema (for the agent.yaml communication field) ──

COMM_FIELD_SCHEMA = """
communication:
  publishes:
    - type: discovery
      topics: ["policy", "regulation"]
  subscribes:
    - type: request
      topics: ["fact-check"]
    - type: directive
      actions: ["refocus"]
"""


# ── Bus ──

class CommunicationSpec:
    """Agent communication capability description, parsed from agent.yaml communication field"""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.publishes: list[dict] = data.get("publishes", [])
        self.subscribes: list[dict] = data.get("subscribes", [])

    def can_publish(self, msg_type: str) -> bool:
        return any(p.get("type") == msg_type for p in self.publishes)

    def can_receive(self, msg_type: str) -> bool:
        return any(s.get("type") == msg_type for s in self.subscribes)

    def matches_topic(self, msg_type: str, topic: str) -> bool:
        """Whether the agent receives messages of a given topic"""
        for s in self.subscribes:
            if s.get("type") == msg_type and topic in s.get("topics", []):
                return True
        return False

    @property
    def publish_topics(self) -> list[str]:
        topics = []
        for p in self.publishes:
            topics.extend(p.get("topics", []))
        return topics

    def __repr__(self):
        pub = [p.get("type", "?") for p in self.publishes]
        sub = [s.get("type", "?") for s in self.subscribes]
        return f"CommSpec(publishes={pub}, subscribes={sub})"


class MessageBus:
    """
    Message bus.

    Usage:
        bus = MessageBus(workspace_dir="/path/to/run")

        # Agent publishes discovery
        bus.publish(Message.discovery("official", "01_research", 1, [
            {"statement": "...", "confidence": 0.9, "relevance": ["media"]}
        ]))

        # Agent requests information
        bus.publish(Message.request("background", "media", "01_research", 2,
                                     "verify", "ASML license status"))

        # Orbit routing
        routing = bus.route("01_research")
        for agent_name, msgs in routing.items():
            print(f"{agent_name}: {len(msgs)} relevant messages")
    """

    def __init__(self, agent_comms: dict[str, CommunicationSpec] | None = None,
                 workspace_dir: str | Path | None = None):
        self._messages: list[Message] = []
        self._comms: dict[str, CommunicationSpec] = agent_comms or {}
        self._workspace_dir = Path(workspace_dir) if workspace_dir else None

    def register_agent(self, name: str, comm_spec: CommunicationSpec):
        """Register an agent's communication capabilities"""
        self._comms[name] = comm_spec

    def publish(self, msg: Message):
        """Publish a message to the bus"""
        self._messages.append(msg)

        # Persistence
        if self._workspace_dir:
            bus_dir = self._workspace_dir / "bus"
            bus_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{msg.phase}_r{msg.round}_{msg.from_}_{msg.id}.json"
            (bus_dir / fname).write_text(json.dumps(msg.to_dict(), ensure_ascii=False, indent=2))

    # ── Query interface ──

    def by_phase(self, phase: str) -> list[Message]:
        """Read all messages for a given phase"""
        return [m for m in self._messages if m.phase == phase]

    def by_agent(self, agent_name: str) -> list[Message]:
        """Messages sent or received by a given agent"""
        return [
            m for m in self._messages
            if m.from_ == agent_name or m.to == agent_name
        ]

    def discoveries(self, phase: str) -> list[Message]:
        """All discovery messages for a given phase"""
        return [m for m in self._messages if m.phase == phase and m.type == DISCOVERY]

    def requests_for(self, agent_name: str, phase: str) -> list[Message]:
        """Request messages addressed to a specific agent"""
        return [
            m for m in self._messages
            if m.phase == phase and m.type == REQUEST and m.to == agent_name
        ]

    def directives_for(self, agent_name: str, phase: str) -> list[Message]:
        """Directive messages addressed to a specific agent"""
        return [
            m for m in self._messages
            if m.phase == phase and m.type == DIRECTIVE and m.to == agent_name
        ]

    # ── Routing (core) ──

    def route(self, phase: str) -> dict[str, list[Message]]:
        """
        Core routing method. Returns agent_name → [relevant_messages] mapping.

        Routing logic:
          1. Discovery messages: match claims' relevance field → agents
          2. Request messages: match to field → directed delivery
          3. Directive messages: match to field → directed delivery
        """
        routing: dict[str, list[Message]] = {}

        for msg in self._messages:
            if msg.phase != phase:
                continue

            if msg.type == DIRECTIVE:
                # Directive: directed delivery
                if msg.to:
                    routing.setdefault(msg.to, []).append(msg)

            elif msg.type == REQUEST:
                # Request: directed delivery
                if msg.to:
                    routing.setdefault(msg.to, []).append(msg)
                # Also deliver to orbuz (request routing)
                routing.setdefault("orbuz", []).append(msg)

            elif msg.type == DISCOVERY:
                # Discovery: match by relevance + topic
                claims = msg.payload.get("claims", [])
                for claim in claims:
                    relevant_agents = claim.get("relevance", [])
                    for agent_name in relevant_agents:
                        if agent_name in self._comms or agent_name == "all":
                            routing.setdefault(agent_name, []).append(msg)

        return routing

    def build_cross_feed(self, phase: str) -> str:
        """
        Build a "cross-agent discovery summary" for injection into the next round's context.

        Returns a markdown-formatted string:
        ```
        ## Other Agents' Findings
        - official: BIS updated the entity list...
        - media: Market split on ASML licensing...
        ```
        """
        lines = ["\n## Other Agents' Findings\n"]
        for msg in self.discoveries(phase):
            claims = msg.payload.get("claims", [])
            for claim in claims:
                stmt = claim.get("statement", "")
                conf = claim.get("confidence", "?")
                src = claim.get("source", "?")
                lines.append(f"- [{msg.from_}] {stmt} (confidence:{conf}, source:{src})")

        # Also include unhandled requests
        for msg in self._messages:
            if msg.phase == phase and msg.type == REQUEST and msg.to == "orbuz":
                lines.append(f"- [request] {msg.from_} requests: {msg.payload.get('target', '?')}")

        return "\n".join(lines)

    def pending(self, agent_name: str, phase: str) -> list[Message]:
        """All unprocessed messages (request + directive) for an agent in a given phase"""
        return (
            self.requests_for(agent_name, phase)
            + self.directives_for(agent_name, phase)
        )

    def __len__(self):
        return len(self._messages)

    def __repr__(self):
        by_type = {}
        for m in self._messages:
            by_type.setdefault(m.type, 0)
            by_type[m.type] += 1
        return f"MessageBus({len(self)} msgs: {by_type})"


if __name__ == "__main__":
    # Test
    bus = MessageBus()

    # Register agent communication capabilities
    bus.register_agent("official-researcher", CommunicationSpec({
        "publishes": [{"type": "discovery", "topics": ["policy", "regulation"]}],
        "subscribes": [{"type": "request", "topics": ["fact-check"]}],
    }))
    bus.register_agent("media-researcher", CommunicationSpec({
        "publishes": [{"type": "discovery", "topics": ["market"]}],
        "subscribes": [{"type": "request", "topics": ["find-source"]}],
    }))

    # Round 1 discoveries
    bus.publish(Message.discovery("official-researcher", "01_research", 1, [
        {"statement": "BIS updated entity list", "confidence": 0.9,
         "source": "Federal Register", "relevance": ["media-researcher"]}
    ]))
    bus.publish(Message.discovery("media-researcher", "01_research", 1, [
        {"statement": "ASML license reporting divergence", "confidence": 0.7,
         "source": "WSJ", "relevance": ["official-researcher"]}
    ]))

    print(bus)
    routing = bus.route("01_research")
    for agent, msgs in routing.items():
        print(f"  → {agent}: {len(msgs)} msgs")

    print(bus.build_cross_feed("01_research"))
