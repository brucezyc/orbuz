"""Clone an instance repository at its base commit and install its test dependencies.

Dependencies must be importable by the interpreter the **sandbox** runs (/usr/bin/python3),
because bubblewrap mounts only /usr read-only: a package installed into a virtualenv is
invisible there. Installing with whatever `pip` is first on PATH is the silent failure mode
this module exists to prevent - so it installs, then verifies with the sandbox interpreter.

Usage:
    python3 setup.py sympy__sympy-17630 [--root /root/bench] [--skip-deps]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from fetch import load
from repo import deps_for

SYSTEM_PYTHON = '/usr/bin/python3'
SYSTEM_SITE = '/usr/local/lib/python3.11/dist-packages'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def importable(packages, python=SYSTEM_PYTHON):
    code = ('import importlib.util as u, sys; '
            'sys.exit(1 if [n for n in sys.argv[1:] if not u.find_spec(n)] else 0)')
    done = subprocess.run([python, '-c', code, *packages], capture_output=True, text=True)
    return done.returncode == 0


def install_for_sandbox(packages):
    """Install where the sandbox can import them, then prove it with the sandbox interpreter."""
    if importable(packages):
        return 'already importable'
    attempts = [
        ('system pip', [SYSTEM_PYTHON, '-m', 'pip', 'install', '--break-system-packages', '-q',
                        *packages]),
        ('venv pip --target', [sys.executable, '-m', 'pip', 'install', '-q', '--target',
                               SYSTEM_SITE, *packages]),
    ]
    for label, command in attempts:
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0:
            continue
        if importable(packages):
            return label
    missing = [name for name in packages if not importable([name])]
    raise SystemExit(f'Cannot make {missing} importable by {SYSTEM_PYTHON} (the sandbox '
                     'interpreter). A virtualenv install does not count: /usr is all the '
                     'sandbox can see. Install system-wide, or mount a prepared virtualenv '
                     'read-only and point the suite at it.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance_id')
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--skip-deps', action='store_true')
    args = parser.parse_args()

    row = load(args.instance_id, args.cache)
    task = Path(args.root) / 'tasks' / args.instance_id
    repo = task / 'repo'
    if not repo.is_dir():
        repo.mkdir(parents=True)
        git(repo, 'init', '-q')
        git(repo, 'remote', 'add', 'origin', f"https://github.com/{row['repo']}.git")
        git(repo, 'fetch', '--depth', '1', 'origin', row['base_commit'])
        git(repo, 'checkout', '-q', 'FETCH_HEAD')
    how = 'skipped'
    if not args.skip_deps:
        how = install_for_sandbox(deps_for(row['repo']))
    print(json.dumps({'instance': args.instance_id, 'repo': row['repo'],
                      'repo_dir': str(repo), 'head': git(repo, 'rev-parse', '--short', 'HEAD'),
                      'files': len(git(repo, 'ls-files').splitlines()),
                      'deps': deps_for(row['repo']), 'deps_installed_via': how,
                      'sandbox_interpreter_ok': importable(deps_for(row['repo']))}, indent=2))


if __name__ == '__main__':
    main()
