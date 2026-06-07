"""
Codegen tools — tool schemas + dispatch for orbuz sub-agents.

Sub-agents use native OpenAI function calling instead of
custom ---actions--- blocks. This module provides:

  1. TOOL_SCHEMAS  — list of OpenAI-format tool definitions
  2. dispatch()    — execute a tool call by name + args
  3. Hermes fallback — unknown tools route to Hermes if available

Usage:
    from orbuz.codegen.tools import TOOL_SCHEMAS, dispatch

    # Pass schemas to LLM
    resp = llm.chat(messages=msgs, tools=TOOL_SCHEMAS)

    # Execute tool calls
    for tc in resp.tool_calls:
        result = dispatch(tc["function"]["name"],
                          json.loads(tc["function"]["arguments"]))
        messages.append({"role": "tool",
                         "tool_call_id": tc["id"],
                         "content": result})
"""
from __future__ import annotations
import json
import os
import subprocess
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Tool Schemas (OpenAI function-calling format) ──

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write content to a file, completely replacing existing content. "
            "Creates parent directories automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute file path"
                },
                "content": {
                    "type": "string",
                    "description": "Complete file content to write"
                },
            },
            "required": ["path", "content"],
        },
    },
}

TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": (
            "Execute a shell command in the project directory. "
            "Returns stdout, stderr, and exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default: 120)",
                    "default": 120,
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (default: project root)"
                },
            },
            "required": ["command"],
        },
    },
}

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a text file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read"
                },
                "max_length": {
                    "type": "integer",
                    "description": "Max characters to return (default: 8000)",
                    "default": 8000,
                },
            },
            "required": ["path"],
        },
    },
}

# All tool schemas for codegen sub-agents
TOOL_SCHEMAS: list[dict] = [
    WRITE_FILE_SCHEMA,
    TERMINAL_SCHEMA,
    READ_FILE_SCHEMA,
]

# ── Dispatch ──

# Default project path for terminal commands (overridable per-call)
_default_project_path: str | None = None


def set_default_project_path(path: str):
    """Set the default project root for terminal commands."""
    global _default_project_path
    _default_project_path = path


def dispatch(
    tool_name: str,
    args: dict[str, Any],
    project_path: str | None = None,
) -> str:
    """Execute a tool call and return the result as a string.

    Falls back to Hermes's handle_function_call for unknown tools.
    """
    pp = project_path or _default_project_path or os.getcwd()

    if tool_name == "write_file":
        return _write_file(args, pp)
    elif tool_name == "terminal":
        return _terminal(args, pp)
    elif tool_name == "read_file":
        return _read_file(args, pp)
    else:
        return _hermes_fallback(tool_name, args)


# ── Built-in tool handlers ──


def _write_file(args: dict, project_path: str) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return json.dumps({"error": "path is required"})
    fp = Path(project_path) / path if not Path(path).is_absolute() else Path(path)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return json.dumps({"ok": True, "path": str(fp), "bytes": len(content)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _terminal(args: dict, project_path: str) -> str:
    cmd = args.get("command", "")
    if not cmd:
        return json.dumps({"error": "command is required"})
    timeout = args.get("timeout", 120)
    workdir = args.get("workdir") or project_path
    try:
        sp = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=workdir, timeout=timeout,
        )
        return json.dumps({
            "exit_code": sp.returncode,
            "stdout": sp.stdout[-8000:],
            "stderr": sp.stderr[-4000:],
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _read_file(args: dict, project_path: str) -> str:
    path = args.get("path", "")
    max_len = args.get("max_length", 8000)
    if not path:
        return json.dumps({"error": "path is required"})
    fp = Path(project_path) / path if not Path(path).is_absolute() else Path(path)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {fp}"})
    try:
        content = fp.read_text()
        truncated = len(content) > max_len
        if truncated:
            content = content[:max_len] + "\n... [truncated]"
        return json.dumps({
            "content": content,
            "path": str(fp),
            "bytes": len(content),
            "truncated": truncated,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Hermes fallback ──

_HERMES_AVAILABLE: bool | None = None


def _hermes_available() -> bool:
    """Check if Hermes's tool dispatch is importable (lazy, cached)."""
    global _HERMES_AVAILABLE
    if _HERMES_AVAILABLE is not None:
        return _HERMES_AVAILABLE
    try:
        import model_tools  # noqa: F401
        _HERMES_AVAILABLE = True
    except ImportError:
        try:
            import sys
            # Check common Hermes locations
            hermes_paths = [
                "/usr/local/lib/hermes-agent",
                "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages",
                os.path.expanduser("~/.local/lib/python*/site-packages"),
            ]
            for hp in hermes_paths:
                if "*" in hp:
                    from glob import glob
                    for match in glob(hp):
                        sys.path.insert(0, match)
                else:
                    sys.path.insert(0, hp)
            import model_tools  # noqa: F401
            _HERMES_AVAILABLE = True
        except ImportError:
            _HERMES_AVAILABLE = False
    return _HERMES_AVAILABLE


def _hermes_fallback(tool_name: str, args: dict) -> str:
    """Route an unknown tool call to Hermes's handle_function_call."""
    if not _hermes_available():
        return json.dumps({
            "error": (
                f"Unknown tool '{tool_name}'. "
                f"Available tools: write_file, terminal, read_file"
            ),
        })

    try:
        from model_tools import handle_function_call
        result = handle_function_call(
            function_name=tool_name,
            function_args=args,
        )
        return result
    except Exception as e:
        logger.warning("Hermes fallback failed for %s: %s", tool_name, e)
        return json.dumps({
            "error": f"Tool '{tool_name}' not available (Hermes fallback failed: {e})",
        })
