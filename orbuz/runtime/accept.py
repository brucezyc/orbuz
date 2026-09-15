"""Acceptance: the only path to success, now with a held-out half.

Two suites, one verdict:

* ``acceptance`` (visible) -- the command the candidate can see and iterate against.
* ``heldout`` (hidden) -- composes the same behaviour end to end, lives outside the
  candidate workspace, and is mounted read-only into the sandbox when it runs.

A candidate that satisfies the visible suite and fails the held-out one is
``hacking_suspected``, not rejected-but-fine: the visible suite was gamed. Both suites
run through the same sandbox as every other command, with the candidate source mounted
read-only, so acceptance cannot be edited by the thing being judged.

Handles are resolved fail-loud: a missing or malformed handle raises instead of silently
falling back to a default run (the pattern that makes a resumed run quietly test nothing).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orbuz.runtime.sandbox import execute

ACCEPTED = 'accepted'
HACKING = 'hacking_suspected'
REJECTED = 'rejected'
FAILED = 'failed'


class MissingHandle(ValueError):
    """A durable handle was required and absent, empty, or malformed."""


def resolve_handle(value, *, kind='handle', strict=True):
    """Return a validated handle. Strict mode never falls back to a default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if strict:
            raise MissingHandle(f'Missing {kind}: refusing to fall back to a default')
        return None
    if not isinstance(value, str) or '\x00' in value:
        raise MissingHandle(f'Malformed {kind}')
    return value.strip()


def validate_argv(argv, *, kind='acceptance'):
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ValueError(f'{kind} must be a nonempty argv list of strings')
    if not argv[0].startswith('/'):
        raise ValueError(f'{kind}[0] must be an absolute path')
    return list(argv)


def assets_outside_workspace(workspace, assets):
    """Held-out assets must not live where the candidate can read them.

    A single directory is mounted read-only at ``/heldout``; the held-out argv refers to it
    as ``/heldout/<file>``. One directory keeps the mount plan deterministic.
    """
    workspace = Path(workspace).resolve()
    assets = list(assets or ())
    if len(assets) > 1:
        raise ValueError('Held-out assets must be a single directory')
    for path in assets:
        resolved = Path(path).resolve()
        if resolved == workspace or workspace in resolved.parents:
            raise ValueError('Held-out assets must live outside the candidate workspace')
        if not resolved.is_dir():
            raise ValueError('Held-out assets must be a directory (mounted at /heldout)')
    return [str(Path(p).resolve()) for p in assets]


def run_suite(workspace, argv, log_path, *, timeout, cancel=None, assets=None,
              sandbox_runner=execute):
    """Run one acceptance suite in the sandbox with optional read-only asset mounts."""
    argv = validate_argv(argv)
    mounts = [(p, '/heldout') for p in assets_outside_workspace(workspace, assets)]
    result = dict(sandbox_runner(Path(workspace), argv, Path(log_path), timeout=timeout,
                                 cancel=cancel, read_only_mounts=mounts))
    result['argv'] = argv
    result['mounts'] = [guest for _, guest in mounts]
    result['log_hash'] = hashlib.sha256(Path(result['log_path']).read_bytes()).hexdigest()
    return result


def heldout_present(spec):
    return bool(spec.get('heldout'))


def verdict(visible, heldout=None):
    """Map suite results to one status word. Absent held-out suite cannot be 'accepted' quietly."""
    if visible is None:
        return FAILED
    if visible.get('cancelled'):
        return 'cancelled'
    if visible.get('timed_out') or visible.get('exit_code') != 0:
        return REJECTED
    if heldout is None:
        return ACCEPTED
    if heldout.get('cancelled'):
        return 'cancelled'
    if heldout.get('timed_out') or heldout.get('exit_code') != 0:
        return HACKING
    return ACCEPTED


def summarize(visible, heldout=None):
    """Small JSON-safe record of both suites for evidence storage."""
    def one(result):
        if result is None:
            return None
        return {'argv': result.get('argv'), 'mounts': result.get('mounts') or [],
                'exit_code': result.get('exit_code'),
                'timed_out': result.get('timed_out'), 'cancelled': result.get('cancelled'),
                'log_path': result.get('log_path'), 'log_hash': result.get('log_hash'),
                'output': (result.get('output') or '')[-2000:]}
    return json.dumps({'visible': one(visible), 'heldout': one(heldout)}, ensure_ascii=False)
