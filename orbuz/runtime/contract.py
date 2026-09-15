"""Task contracts and version identity. No model-owned success flags."""
import hashlib
import json
import math
import platform
import shutil
import subprocess
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def git_bytes(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
         '-C', str(root), *args], stderr=subprocess.PIPE)


def git(root, *args):
    return git_bytes(root, *args).decode().strip()


def git_paths(root, *args):
    return {p.decode('utf-8', 'surrogateescape') for p in git_bytes(root, *args).split(b'\0') if p}


def candidate_patch(root, base, revision):
    return git_bytes(root, '-c', 'diff.noprefix=false', '-c', 'diff.context=3',
                     'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
                     '--no-color', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/',
                     base, revision, '--')


def relative(name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('Expected relative source path')
    p = Path(name)
    if p.is_absolute() or any(x in ('.git', '..') for x in p.parts) or str(p) == '.':
        raise ValueError('Path outside source scope')
    return p.as_posix()


def source_path(root, name):
    p = root / relative(name)
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes workspace')
    for part in [p, *p.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Symlink source paths are not supported')
    return p


def validate(spec):
    spec = dict(spec)
    allowed = {'goal', 'repository', 'writable', 'context', 'acceptance',
               'max_calls', 'max_output_tokens', 'timeout', 'max_seconds',
               'heldout', 'heldout_assets', 'heldout_patch', 'limits', 'prices'}
    if set(spec) - allowed:
        raise ValueError('Unknown contract fields')
    if not isinstance(spec.get('goal'), str) or not spec['goal'].strip() or len(spec['goal']) > 16000:
        raise ValueError('A nonempty goal of at most 16000 characters is required')
    repo = Path(spec['repository']).resolve()
    if Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise ValueError('Repository must be its Git root')
    if git(repo, 'status', '--porcelain'):
        raise ValueError('Commit or preserve source changes before creating a task')
    spec['repository'] = str(repo)
    spec['base_revision'] = git(repo, 'rev-parse', 'HEAD')
    for key in ('writable', 'context'):
        values = spec.get(key, [])
        if not isinstance(values, list):
            raise ValueError(key + ' must be a list')
        if len(values) > 128:
            raise ValueError('Too many scope/context files')
        spec[key] = list(dict.fromkeys(relative(x) for x in values))
        for name in spec[key]:
            if source_path(repo, name).is_dir():
                raise ValueError('Scope/context entries must be exact files, not directories')
    if not spec['writable']:
        raise ValueError('Explicit writable files required')
    argv = spec.get('acceptance')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or '\x00' in a for a in argv):
        raise ValueError('Nonempty acceptance argv required')
    for arg in argv:
        candidate = Path(arg)
        if candidate.is_absolute():
            if not candidate.is_relative_to('/workspace'):
                continue
            candidate = candidate.relative_to('/workspace')
        normalized = candidate.as_posix()
        # Match declared paths even before creation; inspect existing entry aliases.
        if normalized in spec['writable']:
            raise ValueError('Acceptance entry point cannot be writable')
        try:
            exists = (repo / candidate).is_file()
        except OSError:
            exists = False  # Inline code may exceed the filesystem path length.
        if exists:
            resolved = (repo / candidate).resolve()
            if resolved.is_relative_to(repo) and resolved.relative_to(repo).as_posix() in spec['writable']:
                raise ValueError('Acceptance entry point cannot be writable')
    for key, default, maximum in [('max_calls', 12, 100), ('max_output_tokens', 2048, 16384)]:
        n = spec.setdefault(key, default)
        if type(n) is not int or not 1 <= n <= maximum:
            raise ValueError('Invalid ' + key)
    for key, default, maximum in [('timeout', 30, 300), ('max_seconds', 180, 1800)]:
        n = spec.setdefault(key, default)
        if type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= maximum:
            raise ValueError('Invalid ' + key)
    _validate_heldout(spec, repo)
    _validate_limits(spec)
    return spec


def _argv(key, value):
    if not isinstance(value, list) or not value or any(
            not isinstance(a, str) or not a or '\x00' in a for a in value):
        raise ValueError(f'Nonempty {key} argv required')
    return list(value)


def _validate_heldout(spec, repo):
    """A declared held-out suite must live outside the candidate's reachable source.

    Two ways to satisfy that: ``heldout_assets`` (a directory mounted read-only at
    ``/heldout``) or ``heldout_patch`` (a diff applied to a throwaway copy of the candidate,
    the shape a real pull request has when it adds test cases to existing files).
    """
    if 'heldout' in spec:
        spec['heldout'] = _argv('heldout', spec['heldout'])
    hidden_patch = spec.get('heldout_patch')
    if hidden_patch is not None:
        if 'heldout' not in spec:
            raise ValueError('heldout_patch requires heldout: a patch with no suite does nothing')
        candidate = Path(hidden_patch)
        if not candidate.is_absolute() or not candidate.is_file():
            raise ValueError('heldout_patch must be an existing absolute file')
        candidate = candidate.resolve()
        if candidate.is_relative_to(repo):
            raise ValueError('heldout_patch must live outside the repository')
        spec['heldout_patch'] = str(candidate)
    assets = spec.get('heldout_assets')
    if assets is None:
        if 'heldout' in spec and hidden_patch is None:
            raise ValueError('heldout requires heldout_assets or heldout_patch: the hidden '
                             'material must live outside the candidate workspace')
        return
    if not isinstance(assets, list) or not assets:
        raise ValueError('heldout_assets must be a nonempty list')
    resolved = []
    for path in assets:
        candidate = Path(path)
        if not candidate.is_absolute() or not candidate.exists():
            raise ValueError('heldout_assets entries must be existing absolute paths')
        candidate = candidate.resolve()
        if candidate == repo or repo in candidate.parents:
            raise ValueError('heldout_assets must live outside the repository')
        resolved.append(str(candidate))
    spec['heldout_assets'] = resolved


def _validate_limits(spec):
    limits = spec.get('limits')
    if limits is None:
        return
    if not isinstance(limits, dict) or set(limits) - {'max_steps', 'max_tokens', 'max_usd',
                                                      'degrade', 'keep_recent', 'pin_first',
                                                      'ledger_interval'}:
        raise ValueError('Invalid limits fields')
    for key in ('max_steps', 'max_tokens'):
        n = limits.get(key)
        if n is not None and (type(n) is not int or n < 1):
            raise ValueError('Invalid limits.' + key)
    usd = limits.get('max_usd')
    if usd is not None and (not isinstance(usd, (int, float)) or usd <= 0):
        raise ValueError('Invalid limits.max_usd')
    if 'degrade' in limits:
        from .budget import DEGRADE_ORDER
        if not isinstance(limits['degrade'], (list, tuple)) or set(limits['degrade']) - set(DEGRADE_ORDER):
            raise ValueError('Invalid limits.degrade')
        limits['degrade'] = tuple(limits['degrade'])
    for key, maximum in (('keep_recent', 200), ('pin_first', 200), ('ledger_interval', 100)):
        n = limits.get(key)
        if n is not None and (type(n) is not int or not 0 <= n <= maximum):
            raise ValueError('Invalid limits.' + key)
    prices = spec.get('prices')
    if prices is not None and (not isinstance(prices, dict) or
                              set(prices) - {'input', 'output', 'cache_hit'}):
        raise ValueError('Invalid prices fields')


def environment():
    info: dict = {'platform': platform.platform(), 'sandbox_policy': 'readonly-workspace-v1'}
    for name in ('bwrap', 'python3', 'sh'):
        p = shutil.which(name, path='/usr/bin:/bin')
        info[name] = hashlib.sha256(Path(p).read_bytes()).hexdigest() if p else None
    return info
