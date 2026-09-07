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


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
         '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()


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
               'max_calls', 'max_output_tokens', 'timeout', 'max_seconds'}
    if set(spec) - allowed:
        raise ValueError('Unknown contract fields')
    if not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
        raise ValueError('A goal is required')
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
        if not Path(arg).is_absolute() and (repo / arg).is_file() and Path(arg).as_posix() in spec['writable']:
            raise ValueError('Acceptance entry point cannot be writable')
    for key, default, maximum in [('max_calls', 12, 100), ('max_output_tokens', 2048, 16384)]:
        n = spec.setdefault(key, default)
        if type(n) is not int or not 1 <= n <= maximum:
            raise ValueError('Invalid ' + key)
    for key, default, maximum in [('timeout', 30, 300), ('max_seconds', 180, 1800)]:
        n = spec.setdefault(key, default)
        if type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= maximum:
            raise ValueError('Invalid ' + key)
    return spec


def environment():
    info: dict = {'platform': platform.platform(), 'sandbox_policy': 'readonly-workspace-v1'}
    for name in ('bwrap', 'python3', 'sh'):
        p = shutil.which(name, path='/usr/bin:/bin')
        info[name] = hashlib.sha256(Path(p).read_bytes()).hexdigest() if p else None
    return info
