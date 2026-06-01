"""
Plugin System — Lightweight Lifecycle Hooks
==============================================
Minimal plugin/hook system for orbuz. Allows extending behavior
at key lifecycle points without forking the codebase.

Hook points:
  - before_agent_run(agent_def, goal, context) -> modified context
  - after_agent_run(agent_def, result) -> None
  - before_workflow(plan) -> modified plan
  - after_workflow(result_summary) -> None
  - before_mcp_call(server, tool, args) -> modified args
  - after_mcp_call(server, tool, result) -> None

Usage:
    from orbuz.core.plugin import PluginRegistry, hook

    @hook("before_agent_run")
    def log_agent_run(agent_def, goal, context):
        print(f"Running {agent_def.name}: {goal[:50]}")
        return context  # must return context (possibly modified)
"""
from __future__ import annotations
import importlib.util
import inspect
import os
from pathlib import Path
from typing import Any, Callable


# ── Hook types ──

HOOK_POINTS = frozenset({
    "before_agent_run",    # (agent_def, goal, context) -> context
    "after_agent_run",     # (agent_def, result) -> None
    "before_workflow",     # (plan) -> plan
    "after_workflow",      # (summary) -> None
    "before_mcp_call",     # (server_name, tool_name, args) -> args
    "after_mcp_call",      # (server_name, tool_name, result) -> None
    "on_error",            # (error) -> None
})


# ── Registry ──

class PluginRegistry:
    """
    Central plugin/hook registry.

    Plugins register callback functions at specific hook points.
    """

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = {hp: [] for hp in HOOK_POINTS}

    def register(self, hook_point: str, fn: Callable):
        """Register a callback at a hook point."""
        if hook_point not in HOOK_POINTS:
            raise ValueError(f"Unknown hook point: {hook_point}. "
                             f"Valid: {sorted(HOOK_POINTS)}")
        self._hooks[hook_point].append(fn)

    def run(self, hook_point: str, *args) -> Any | None:
        """
        Run all callbacks at a hook point.

        For hooks that modify state (context, plan, args),
        the return value is passed to the next callback and returned.
        For fire-and-forget hooks, returns None.

        All hooks receive the same positional args.
        Transforming hooks must return modified first arg.
        """
        if hook_point not in self._hooks:
            return args[0] if args else None

        # Determine if this is a transforming hook (returns modified value)
        modifying_hooks = {"before_agent_run", "before_workflow", "before_mcp_call"}

        if hook_point in modifying_hooks:
            # Chain: each callback gets the previous first arg + remaining args
            result = args[0] if args else None
            rest = args[1:] if len(args) > 1 else ()
            for fn in self._hooks[hook_point]:
                try:
                    if result is not None:
                        result = fn(result, *rest)
                    else:
                        result = fn(*args)
                except Exception as e:
                    print(f"  ⚠️ Plugin hook '{hook_point}' error in {fn.__name__}: {e}")
            return result
        else:
            # Fire-and-forget: run all, ignore returns
            for fn in self._hooks[hook_point]:
                try:
                    fn(*args)
                except Exception as e:
                    print(f"  ⚠️ Plugin hook '{hook_point}' error in {fn.__name__}: {e}")
            return None

    def clear(self):
        """Remove all hooks."""
        for hp in self._hooks:
            self._hooks[hp] = []

    @property
    def hook_count(self) -> int:
        return sum(len(v) for v in self._hooks.values())

    def list_hooks(self) -> dict[str, list[str]]:
        return {
            hp: [fn.__name__ for fn in fns]
            for hp, fns in self._hooks.items() if fns
        }


# ── Global registry ──

_registry = PluginRegistry()


def get_registry() -> PluginRegistry:
    """Get the global plugin registry."""
    return _registry


def hook(hook_point: str):
    """Decorator to register a function at a hook point.

    Usage:
        @hook("before_agent_run")
        def my_hook(agent_def, goal, context):
            print(f"Running {agent_def.name}")
            return context
    """
    def decorator(fn):
        _registry.register(hook_point, fn)
        return fn
    return decorator


# ── Plugin discovery ──

def discover_plugins(plugin_dirs: list[str | Path] | None = None):
    """
    Discover and load plugins from directories.

    Scans for Python files named `plugin.py` or `orbuz_plugin.py`
    in the given directories. Each plugin file is imported, which
    registers its hooks via the @hook decorator.
    """
    dirs = plugin_dirs or []
    # Default locations
    cwd = Path.cwd()
    for d in [cwd / "plugins", cwd / ".orbuz" / "plugins",
              Path.home() / ".config" / "orbuz" / "plugins"]:
        if d.exists() and d.is_dir():
            dirs.append(d)

    loaded = 0
    for plugin_dir in dirs:
        p = Path(plugin_dir)
        if not p.exists():
            continue

        for f in p.iterdir():
            if f.is_file() and f.name in ("plugin.py", "orbuz_plugin.py"):
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"orbuz_plugin_{p.name}", f
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        loaded += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to load plugin {f}: {e}")

            elif f.is_dir() and (f / "plugin.py").exists():
                try:
                    plugin_file = f / "plugin.py"
                    spec = importlib.util.spec_from_file_location(
                        f"orbuz_plugin_{f.name}", plugin_file
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        loaded += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to load plugin {f}: {e}")

    return loaded


# ── Reset (for testing) ──

def reset():
    """Reset the global registry (for testing)."""
    _registry.clear()
