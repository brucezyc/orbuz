#!/usr/bin/env python3
"""orbuz integration test — verifies all new modules work together."""
import os, sys, tempfile
sys.path.insert(0, os.path.expanduser("~/yzhu/repos/dev/orbuz"))

print("=== orbuz integration test ===")

from orbuz.llm.client import LLMClient, _estimate_cost as estimate_cost, LLMResponse
from orbuz.schema.agent import AgentDefinition, MCPSpec, OutputContract, OutputSpec
from orbuz.schema.finding import Finding, FindingSet, Severity
from orbuz.core.dispatcher import Dispatcher, DispatcherResult, merge_dedup_findings
from orbuz.core.eval import evaluate_agent, evaluate_output, evaluate_finding_set
from orbuz.agent.memory import AgentMemory
from orbuz.mcp.client import MCPManager, MCPClient, MCPToolSchema, MCPToolResult, MCPError
from orbuz.core.plugin import PluginRegistry, get_registry, hook, discover_plugins, HOOK_POINTS
from orbuz.core.executor import CostTracker

# 1. LLM client
client = LLMClient(mock=True)
resp = client.chat("quality", "You are a helpful assistant.", [{"role": "user", "content": "Hello"}])
assert resp.success
print(f"1. LLM client: OK (cost=${resp.cost_usd:.6f})")

# 2. Streaming
chunks = []
resp2 = client.chat("balanced", "test", stream=True, on_chunk=lambda c: chunks.append(c))
assert resp2.success
print(f"2. Streaming: OK ({len(chunks)} chunks captured)")

# 3. Cost estimation
cost = estimate_cost("claude-opus-4-8", 1000, 500)
assert cost > 0
print(f"3. Cost estimation: OK (${cost:.4f})")

# 4. MCP manager
mgr = MCPManager()
assert mgr.client_count == 0
print(f"4. MCP manager: OK ({mgr.client_count} clients)")

# 5. MCP schema
schema = MCPToolSchema(name="web_search", description="Search the web", input_schema={})
assert schema.name == "web_search"
print(f"5. MCP schema: OK")

# 6. Agent memory
path = os.path.join(tempfile.gettempdir(), "test_mem.json")
mem = AgentMemory(path)
mem.record_learning("test-agent", "testing", "This is a test learning", tags=["test"])
assert mem.count() == 1
results = mem.query(["test"])
assert len(results) == 1
mem.clear()
os.remove(path)
print(f"6. Agent memory: OK")

# 7. Output eval
r = evaluate_output("This is a substantive research output with analysis.")
assert r.passed
assert r.score >= 0.9
print(f"7. Output eval: OK (score={r.score:.2f})")

# 8. Findings eval
fs = FindingSet(findings=[
    Finding(title="Bug", severity=Severity.P1, file="a.py", confidence=0.9, description="Test"),
])
r2 = evaluate_finding_set(fs)
assert r2.score >= 0.9
print(f"8. Findings eval: OK")

# 9. Plugin system
reg = get_registry()
captured = []
@hook("after_agent_run")
def test_hook(agent_def, result):
    captured.append(agent_def.name)
reg.run("after_agent_run", AgentDefinition(name="test-agent", description="Test"), DispatcherResult(success=True))
assert len(captured) == 1
# Reset global registry after test
reg.clear()
print(f"9. Plugin system: OK")

# 10. Cost tracker
ct = CostTracker()
ct.record("agent-a", 1000, 500, 0.015)
s = ct.summary()
assert s["total_cost_usd"] == 0.015
print(f"10. Cost tracker: OK (${s['total_cost_usd']})")

# 11. MCPSpec in agent
spec = MCPSpec(tool="web_search", params={"query": "test"}, required=False)
agent_mcp = AgentDefinition(name="mcp-agent", description="Uses MCP", mcp_tools=[spec])
assert len(agent_mcp.mcp_tools) == 1
print(f"11. Agent MCP config: OK")

# 12. Dispatch with MCP (graceful no-server)
d = Dispatcher(client, mcp_manager=mgr)
result3 = d.run_agent(agent_mcp, "Research topic", context="some context")
assert result3.success
print(f"12. Dispatch with MCP: OK (cost=${result3.cost_usd:.6f})")

# 13. Structured output mode
agent_reviewer = AgentDefinition(
    name="test-reviewer",
    description="A code reviewer",
    output_contract=OutputContract(produces_findings=True, required_fields=["severity", "title"]),
)
result4 = d.run_agent(agent_reviewer, "Review this code", context="def foo(): pass", require_structured_findings=True)
assert result4.success
print(f"13. Structured output: OK")

# 14. DispatcherResult with cost_usd
dr = DispatcherResult(success=True, output="test", cost_usd=0.042)
assert dr.cost_usd == 0.042
print(f"14. DispatcherResult cost: OK (${dr.cost_usd})")

# 15. Agent eval
r5 = evaluate_agent("test", "Good output", fs)
assert r5.passed
print(f"15. Agent eval: OK (score={r5.score:.2f})")

print()
print("=== All 15 tests passed! ===")
