"""Clone an instance repository at its base commit and install its test dependencies.

Usage:
    python3 setup.py sympy__sympy-17630 [--root /root/bench]
"""
import argparse
import json
import subprocess
from pathlib import Path

from fetch import load
from repo import deps_for


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


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
    installed = []
    if not args.skip_deps:
        packages = deps_for(row['repo'])
        subprocess.run(['python3', '-m', 'pip', 'install', '--break-system-packages', '-q',
                        *packages], check=True)
        installed = packages
    print(json.dumps({'instance': args.instance_id, 'repo': row['repo'],
                      'repo_dir': str(repo), 'head': git(repo, 'rev-parse', '--short', 'HEAD'),
                      'files': len(git(repo, 'ls-files').splitlines()),
                      'installed': installed}, indent=2))


if __name__ == '__main__':
    main()
