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
        "description": "Read a text file with line numbers. Supports offset and limit for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to project root or absolute)"
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default: 1)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return (default: 500, max: 2000)",
                },
            },
            "required": ["path"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch",
        "description": (
            "Targeted find-and-replace edit in a file. Use fuzzy matching so "
            "minor whitespace/indentation differences won't break it. "
            "Returns a unified diff. Safe for surgical changes without rewriting "
            "the entire file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to project root or absolute)"
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find and replace. Include surrounding context for uniqueness."
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text"
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Search file contents with regex, or find files by name. "
            "Uses ripgrep internally — faster than grep."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for in file contents, or glob pattern (e.g. '*.rs') to find files by name"
                },
                "target": {
                    "type": "string",
                    "enum": ["content", "files"],
                    "description": "'content' to search inside files, 'files' to find files by name"
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default: project root)"
                },
                "file_glob": {
                    "type": "string",
                    "description": "Filter by file pattern (e.g. '*.rs' to only search Rust files)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 50)",
                },
            },
            "required": ["pattern"],
        },
    },
}

# All tool schemas for codegen sub-agents
TOOL_SCHEMAS: list[dict] = [
    WRITE_FILE_SCHEMA,
    TERMINAL_SCHEMA,
    READ_FILE_SCHEMA,
    PATCH_SCHEMA,
    SEARCH_SCHEMA,
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
    elif tool_name == "patch":
        return _patch(args, pp)
    elif tool_name == "search_files":
        return _search_files(args, pp)
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
    offset = args.get("offset", 1)
    limit = args.get("limit", 500)
    if not path:
        return json.dumps({"error": "path is required"})
    fp = Path(project_path) / path if not Path(path).is_absolute() else Path(path)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {fp}"})
    try:
        lines = fp.read_text().splitlines(keepends=True)
        total = len(lines)
        start = max(0, (offset or 1) - 1)
        end = min(total, start + (limit or 500))
        selected = lines[start:end]
        # Format with line numbers
        out_lines = []
        for i, line in enumerate(selected, start=start + 1):
            out_lines.append(f"{i}|{line.rstrip()}")
        content = "\n".join(out_lines)
        return json.dumps({
            "content": content,
            "path": str(fp),
            "total_lines": total,
            "lines_shown": len(selected),
            "offset": start + 1,
            "truncated": end < total,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _patch(args: dict, project_path: str) -> str:
    """Targeted find-and-replace edit in a file."""
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    if not path or not old_string:
        return json.dumps({"error": "path and old_string are required"})
    fp = Path(project_path) / path if not Path(path).is_absolute() else Path(path)
    if not fp.exists():
        return json.dumps({"error": f"File not found: {fp}"})
    try:
        content = fp.read_text()
        if old_string not in content:
            # Try fuzzy: show close match locations
            import difflib
            lines = content.splitlines()
            closest = difflib.get_close_matches(old_string, lines, n=3, cutoff=0.5)
            hints = [f"  near line: {l[:80]}" for l in closest] if closest else []
            return json.dumps({
                "error": "old_string not found in file",
                "hints": hints or ["Use read_file to see the actual content first"],
            })
        new_content = content.replace(old_string, new_string, 1)
        fp.write_text(new_content)
        return json.dumps({
            "ok": True,
            "path": str(fp),
            "bytes_changed": len(old_string),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _search_files(args: dict, project_path: str) -> str:
    """Search file contents with regex or find files by name (uses ripgrep)."""
    pattern = args.get("pattern", "")
    target = args.get("target", "content")
    path = args.get("path") or project_path
    file_glob = args.get("file_glob", "")
    limit = min(args.get("limit", 50), 200)
    if not pattern:
        return json.dumps({"error": "pattern is required"})
    spath = Path(project_path) / path if not Path(str(path)).is_absolute() else Path(str(path))
    spath_str = str(spath)
    try:
        if target == "files":
            # Find files by glob pattern
            from glob import glob
            matches = []
            for f in sorted(glob(f"{spath_str}/**/{pattern}", recursive=True)):
                if Path(f).is_file():
                    st = Path(f).stat()
                    matches.append(f"  {Path(f).relative_to(spath_str)}  ({st.st_size} bytes)")
                    if len(matches) >= limit:
                        break
            return json.dumps({"ok": True, "target": "files", "matches": matches, "count": len(matches)})
        else:
            # Content search via ripgrep
            cmd_parts = ["rg", "-n", "--no-heading"]
            if file_glob:
                cmd_parts.extend(["-g", file_glob])
            cmd_parts.extend([pattern, spath_str])
            sp = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=30
            )
            if sp.returncode == 0:
                lines = sp.stdout.strip().split("\n")[:limit]
                return json.dumps({"ok": True, "target": "content", "matches": lines, "count": len(lines)})
            elif sp.returncode == 1:
                return json.dumps({"ok": True, "target": "content", "matches": [], "count": 0, "note": "No matches found"})
            else:
                return json.dumps({"error": f"rg failed: {sp.stderr[:500]}"})
    except FileNotFoundError:
        # rg not installed, fall back to grep
        try:
            cmd_parts = ["grep", "-rn", pattern, spath_str]
            if file_glob:
                cmd_parts.extend(["--include", file_glob])
            sp = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30)
            lines = sp.stdout.strip().split("\n")[:limit] if sp.stdout.strip() else []
            return json.dumps({"ok": True, "target": "content", "matches": lines, "count": len(lines)})
        except Exception as e:
            return json.dumps({"error": f"search failed: {e}"})
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
