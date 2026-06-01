"""
MCP Client — Lightweight Model Context Protocol Client
========================================================
Connects to MCP servers over stdio or HTTP/SSE transport.

MCP (Model Context Protocol) standardizes how AI tools connect.
Each MCP server exposes tools via JSON-RPC 2.0.

Supported transports:
  - stdio: spawn a subprocess, communicate via JSON lines on stdin/stdout
  - http: connect to a remote HTTP SSE endpoint

Usage:
    client = MCPClient.stdio("python", ["-m", "mcp_server_web"])
    await client.initialize()
    tools = client.list_tools()
    result = client.call_tool("web_search", {"query": "..."})
    client.close()
"""
from __future__ import annotations
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


# ── Exceptions ──

class MCPError(Exception):
    """Base MCP error."""
    pass

class MCPConnectionError(MCPError):
    """Failed to connect or initialize MCP server."""
    pass

class MCPToolNotFoundError(MCPError):
    """Requested tool not found on the server."""
    pass

class MCPToolCallError(MCPError):
    """Tool execution failed."""
    pass


# ── Data types ──

@dataclass
class MCPToolSchema:
    """Schema for an MCP tool."""
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPToolResult:
    """Result of an MCP tool call."""
    content: list[dict] = field(default_factory=list)
    is_error: bool = False
    tool_name: str = ""

    @property
    def text(self) -> str:
        """Concatenate all text content blocks."""
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    @property
    def resources(self) -> list[dict]:
        """Get resource content blocks."""
        return [b for b in self.content if b.get("type") == "resource"]


# ── JSON-RPC helpers ──

_next_id = 0

def _rpc_request(method: str, params: dict | None = None) -> dict:
    global _next_id
    _next_id += 1
    req = {
        "jsonrpc": "2.0",
        "id": _next_id,
        "method": method,
    }
    if params:
        req["params"] = params
    return req


# ── Transport base ──

class MCPTransport:
    """Base class for MCP transport layers."""
    def send(self, request: dict) -> dict:
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


# ── HTTP/SSE Transport ──

class HTTPTransport(MCPTransport):
    """
    HTTP/SSE transport for MCP.
    Uses SSE for receiving events, HTTP POST for sending requests.
    """

    def __init__(self, url: str, timeout: float = 30.0, headers: dict | None = None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}
        self._http = httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True)
        self._session_id: str | None = None

    def initialize_session(self) -> dict | None:
        """Connect to SSE endpoint and get session info."""
        # Some MCP servers use an initial SSE endpoint that returns a session endpoint
        try:
            with self._http.stream("GET", self.url, headers=self.headers) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("type") == "endpoint":
                            self._session_id = data.get("session_id")
                            return data
            return None
        except Exception as e:
            raise MCPConnectionError(f"SSE initialization failed: {e}")

    def send(self, request: dict) -> dict:
        """Send JSON-RPC request via HTTP POST."""
        url = self.url
        if self._session_id:
            url = f"{url}?session_id={self._session_id}"
        try:
            resp = self._http.post(
                url,
                json=request,
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise MCPConnectionError(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            raise MCPConnectionError(f"HTTP request failed: {e}")

    def close(self):
        self._http.close()


# ── Stdio Transport ──

class StdioTransport(MCPTransport):
    """
    Stdio transport for MCP.
    Launches a subprocess and communicates via JSON lines on stdin/stdout.
    """

    def __init__(self, command: list[str], cwd: str | None = None,
                 env: dict[str, str] | None = None, timeout: float = 30.0):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self._env = env

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=merged_env,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as e:
            raise MCPConnectionError(f"Command not found: {command[0]}. {e}")
        except Exception as e:
            raise MCPConnectionError(f"Failed to spawn process: {e}")

        self._buffer = ""

    def send(self, request: dict) -> dict:
        """Send JSON-RPC request and read response."""
        req_str = json.dumps(request, ensure_ascii=False) + "\n"
        if self._process.stdin is None or self._process.poll() is not None:
            raise MCPConnectionError("Subprocess has exited")

        try:
            self._process.stdin.write(req_str)
            self._process.stdin.flush()
        except BrokenPipeError:
            stderr = self._read_stderr()
            raise MCPConnectionError(f"Broken pipe. stderr: {stderr[:500]}")

        # Read response — JSON lines until we get our response ID
        start = time.time()
        while time.time() - start < self.timeout:
            line = self._process.stdout.readline() if self._process.stdout else ""
            if not line:
                # Process may have exited
                stderr = self._read_stderr()
                if self._process.poll() is not None:
                    raise MCPConnectionError(
                        f"Process exited with code {self._process.poll()}. stderr: {stderr[:500]}"
                    )
                continue

            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
                # Check if this response matches our request ID
                if data.get("id") == request.get("id"):
                    if "error" in data:
                        raise MCPToolCallError(f"MCP error: {data['error']}")
                    return data
            except json.JSONDecodeError:
                continue

        raise MCPConnectionError(f"Timeout waiting for response ({self.timeout}s)")

    def _read_stderr(self) -> str:
        """Read any available stderr output."""
        if self._process.stderr is None:
            return ""
        try:
            return self._process.stderr.read()
        except Exception:
            return "<error reading stderr>"

    def close(self):
        """Terminate the subprocess."""
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)


# ── MCP Client ──

class MCPClient:
    """
    Client for a single MCP server.

    Handles initialization, tool discovery, and tool calls.
    """

    def __init__(self, name: str, transport: MCPTransport,
                 description: str = ""):
        self.name = name
        self.transport = transport
        self.description = description
        self._initialized = False
        self._server_info: dict = {}
        self._capabilities: dict = {}
        self._tools: list[MCPToolSchema] | None = None
        self._tool_cache_time: float = 0
        self._tool_cache_ttl: float = 60.0  # Re-discover tools after 60s

    @classmethod
    def stdio(cls, name: str, command: list[str],
              description: str = "",
              cwd: str | None = None,
              env: dict[str, str] | None = None,
              timeout: float = 30.0) -> MCPClient:
        """Create an MCP client connected via stdio transport."""
        transport = StdioTransport(command, cwd=cwd, env=env, timeout=timeout)
        return cls(name, transport, description=description)

    @classmethod
    def http(cls, name: str, url: str,
             description: str = "",
             headers: dict[str, str] | None = None,
             timeout: float = 30.0) -> MCPClient:
        """Create an MCP client connected via HTTP/SSE transport."""
        transport = HTTPTransport(url, timeout=timeout, headers=headers)
        return cls(name, transport, description=description)

    # ── Connection lifecycle ──

    def initialize(self) -> dict:
        """Initialize the MCP session. Must be called before any tool calls."""
        req = _rpc_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "orbuz", "version": "0.1.0"},
        })
        resp = self.transport.send(req)
        result = resp.get("result", {})
        self._server_info = result.get("serverInfo", {})
        self._capabilities = result.get("capabilities", {})
        self._initialized = True

        # Send initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            self.transport.send(notif)
        except Exception:
            pass  # Notifications don't require responses

        return result

    def close(self):
        """Close the MCP connection."""
        self.transport.close()
        self._initialized = False

    def _ensure_initialized(self):
        if not self._initialized:
            raise MCPConnectionError("Client not initialized. Call .initialize() first.")

    # ── Tool discovery ──

    def list_tools(self, force_refresh: bool = False) -> list[MCPToolSchema]:
        """List available tools from the server. Cached for tool_cache_ttl."""
        self._ensure_initialized()

        now = time.time()
        if self._tools is not None and not force_refresh:
            if now - self._tool_cache_time < self._tool_cache_ttl:
                return self._tools

        req = _rpc_request("tools/list")
        resp = self.transport.send(req)
        result = resp.get("result", {})
        raw_tools = result.get("tools", [])

        self._tools = []
        for t in raw_tools:
            self._tools.append(MCPToolSchema(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))
        self._tool_cache_time = now
        return self._tools

    def get_tool(self, name: str) -> MCPToolSchema | None:
        """Get a specific tool by name."""
        for t in self.list_tools():
            if t.name == name:
                return t
        return None

    # ── Tool execution ──

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        """Call a tool on the server."""
        self._ensure_initialized()

        req = _rpc_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

        try:
            resp = self.transport.send(req)
        except MCPToolCallError:
            raise
        except Exception as e:
            raise MCPToolCallError(f"Tool call failed: {e}")

        result = resp.get("result", {})
        return MCPToolResult(
            content=result.get("content", []),
            is_error=result.get("isError", False),
            tool_name=name,
        )

    # ── Status ──

    @property
    def is_connected(self) -> bool:
        return self._initialized

    @property
    def server_info(self) -> dict:
        return self._server_info

    def __repr__(self) -> str:
        status = "connected" if self._initialized else "disconnected"
        tools = len(self._tools) if self._tools else "?"
        return f"MCPClient({self.name}, {status}, {tools} tools)"


# ── MCP Manager ──

class MCPManager:
    """
    Registry for multiple MCP clients.

    Allows agents to discover and call tools across all connected servers.
    """

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}

    def add_client(self, client: MCPClient):
        """Register an MCP client."""
        self._clients[client.name] = client

    def remove_client(self, name: str):
        """Remove and close an MCP client."""
        client = self._clients.pop(name, None)
        if client:
            client.close()

    def get_client(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    def initialize_all(self):
        """Initialize all registered clients."""
        errors = []
        for name, client in self._clients.items():
            try:
                client.initialize()
            except MCPError as e:
                errors.append(f"{name}: {e}")
        if errors:
            raise MCPConnectionError(f"Failed to initialize some clients: {'; '.join(errors)}")

    def all_tools(self, force_refresh: bool = False) -> list[tuple[str, MCPToolSchema]]:
        """
        List all tools across all connected servers.
        Returns list of (server_name, tool_schema) tuples.
        """
        result = []
        for client_name, client in self._clients.items():
            try:
                for tool in client.list_tools(force_refresh=force_refresh):
                    result.append((client_name, tool))
            except MCPError:
                continue
        return result

    def call_tool(self, server_name: str, tool_name: str,
                  arguments: dict | None = None) -> MCPToolResult:
        """Call a tool on a specific server."""
        client = self._clients.get(server_name)
        if not client:
            raise MCPToolNotFoundError(f"Unknown MCP server: {server_name}")
        return client.call_tool(tool_name, arguments)

    def call_tool_any(self, tool_name: str, arguments: dict | None = None) -> MCPToolResult:
        """
        Call a tool by name across all servers. First match wins.
        Raises MCPToolNotFoundError if no server has the tool.
        """
        for client_name, client in self._clients.items():
            if client.get_tool(tool_name):
                return client.call_tool(tool_name, arguments)
        raise MCPToolNotFoundError(f"No server has tool: {tool_name}")

    def close_all(self):
        """Close all clients."""
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def tool_count(self) -> int:
        count = 0
        for client in self._clients.values():
            try:
                count += len(client.list_tools())
            except MCPError:
                continue
        return count

    def __repr__(self) -> str:
        return f"MCPManager({self.client_count} servers, {self.tool_count} tools)"
