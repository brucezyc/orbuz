"""Run visible / held-out suites of a SWE-bench instance inside the orbuz sandbox.

The sandbox has no network and mounts the workspace read-only, so this is the only honest way
to know whether a task is runnable offline at all.

Usage:
    python3 runner.py <instance_id> --suite visible|heldout|both [--root /root/bench]
                       [--orbuz /root/orbuz] [--patched] [--json result.json]
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/root/orbuz')

from fetch import as_list, load          # noqa: E402
from repo import test_files              # noqa: E402


def apply_patch(root, text, name):
    path = Path(root) / f'{name}.diff'
    path.write_text(text if text.endswith('\n') else text + '\n')
    done = subprocess.run(['git', 'apply', str(path)], cwd=root, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f'patch {name} failed: {done.stderr.strip()[:300]}')
    return path


def node_map(root, files, names):
    """Map bare test names to file::name node ids by reading the patched working tree."""
    mapping, unmapped = {}, []
    for name in names:
        hit = next((f for f in files if f'def {name}(' in (Path(root) / f).read_text()), None)
        (mapping.setdefault(hit, []).append(name) if hit else unmapped.append(name))
    return mapping, unmapped


def sandbox_run(workspace, nodes, log_path, orbuz='/root/orbuz', timeout=900):
    sys.path.insert(0, orbuz)
    from orbuz.runtime.sandbox import execute
    argv = ['/usr/bin/python3', '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *nodes]
    return execute(Path(workspace), argv, Path(log_path), timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance_id')
    parser.add_argument('--suite', choices=('visible', 'heldout', 'both'), default='both')
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--orbuz', default='/root/orbuz')
    parser.add_argument('--patched', action='store_true',
                        help='prepare (and keep) a scratch copy with the hidden tests applied')
    parser.add_argument('--gold', action='store_true',
                        help='with --patched: also apply the upstream fix (green baseline check)')
    parser.add_argument('--json')
    args = parser.parse_args()

    row = load(args.instance_id, args.cache)
    repo_dir = Path(args.root) / 'tasks' / args.instance_id / 'repo'
    if not repo_dir.is_dir():
        raise SystemExit(f'Not set up: {repo_dir} (run setup.py first)')
    files = test_files(row['eval_script'])

    work = repo_dir
    if args.patched:
        work = Path(args.root) / 'tasks' / args.instance_id / 'heldout'
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(repo_dir, work, symlinks=True)
        apply_patch(work, row['test_patch'], 'test_patch')
        if args.gold:
            apply_patch(work, row['patch'], 'gold_patch')
    logs = Path(args.root) / 'tasks' / args.instance_id / 'logs'
    logs.mkdir(parents=True, exist_ok=True)

    report = {'instance': args.instance_id, 'repo': row['repo'], 'patched': args.patched,
              'test_files': files, 'suites': {}}
    for suite in (('visible', 'heldout') if args.suite == 'both' else (args.suite,)):
        names = as_list(row['PASS_TO_PASS'] if suite == 'visible' else row['FAIL_TO_PASS'])
        mapping, unmapped = node_map(work, files, names)
        nodes = [f'{f}::{n}' for f, group in mapping.items() for n in group]
        if not nodes:
            report['suites'][suite] = {'nodes': 0, 'exit': None, 'unmapped': unmapped,
                                       'note': 'no matching test file in this tree'}
            continue
        result = sandbox_run(work, nodes, logs / f'{suite}.log', orbuz=args.orbuz)
        tail = [line for line in (result.get('output') or '').splitlines()
                if 'passed' in line or 'failed' in line or 'error' in line]
        report['suites'][suite] = {'nodes': len(nodes), 'exit': result.get('exit_code'),
                                   'summary': tail[-1] if tail else '',
                                   'unmapped': unmapped, 'log': str(logs / f'{suite}.log')}
    print(json.dumps(report, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
