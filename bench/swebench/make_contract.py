"""Build an orbuz contract from a SWE-bench Verified instance.

Mapping (kept deliberately free of localisation leakage):

* ``goal``          <- the upstream problem statement
* ``writable``      <- every non-test module **in the package the tests exercise**, not the
                       files the gold patch happened to touch. Using those would hand the
                       candidate the localisation answer.
* ``acceptance``    <- the PASS_TO_PASS node ids (tests already present at the base commit)
* ``heldout``       <- the FAIL_TO_PASS node ids, plus ``heldout_patch`` = the PR's test_patch,
                       so the hidden tests run on a throwaway copy of the candidate.
* ``context``       <- empty on purpose: the model must explore with its tools, exactly as it
                       would on an unfamiliar repository.

Usage:
    python3 make_contract.py sympy__sympy-17630 --root /root/bench [--out contract.json]
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

from fetch import as_list, load
from repo import test_files

MAX_WRITABLE = 128          # contract limit: writable entries are exact files
PYTEST = ['/usr/bin/python3', '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider']


def node_ids(root, files, names):
    """Resolve test names to runnable node ids.

    The dataset mixes two shapes: full ids that already carry a file and a class
    (``test_requests.py::RequestsTestCase::test_x``) and bare names that must be located in
    the files the eval_script runs. Both are supported; bare names that cannot be located are
    reported rather than silently dropped.
    """
    resolved, unmapped, mapping = [], [], {}
    for name in names:
        if '::' in name:
            resolved.append(name)
            continue
        hit = next((f for f in files if f'def {name}(' in (Path(root) / f).read_text()), None)
        if hit:
            mapping.setdefault(hit, []).append(name)
        else:
            unmapped.append(name)
    resolved += [f'{f}::{n}' for f, group in mapping.items() for n in group]
    return resolved, unmapped


def hidden_tree(repo, test_patch_text, destination):
    """Copy the base tree, apply the PR test patch, so hidden test names can be located.

    The hidden tests do not exist at the base commit - that is what makes them hidden.
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(repo, destination, symlinks=True, ignore=shutil.ignore_patterns('.git'))
    patch_file = destination.parent / 'locate_test_patch.diff'
    patch_file.write_text(test_patch_text if test_patch_text.endswith('\n')
                          else test_patch_text + '\n')
    applied = subprocess.run(['git', 'apply', '-p1', str(patch_file)], cwd=str(destination),
                             capture_output=True, text=True)
    if applied.returncode != 0:
        raise SystemExit('test_patch did not apply while locating hidden tests: ' +
                         applied.stderr.strip()[:300])
    return destination


def preflight(repo, nodes, orbuz='/root/orbuz', timeout=300):
    """Refuse to build a task whose visible suite cannot pass offline at the base commit.

    Two ways an instance is unusable here: its tests need a network the sandbox does not have,
    or its code predates the interpreter we run (an old sympy importing collections.Mapping
    fails on python 3.11). Neither is visible until the tests actually run, and finding out
    after a model has spent a budget is waste.
    """
    import sys
    sys.path.insert(0, orbuz)
    from orbuz.runtime.sandbox import execute
    logs = Path(repo).parent / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    result = execute(Path(repo), ['/usr/bin/python3', '-B', '-m', 'pytest', '-q',
                                  '-p', 'no:cacheprovider', *nodes],
                     logs / 'preflight.log', timeout=timeout)
    return result, logs / 'preflight.log'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance_id')
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--out')
    parser.add_argument('--max-calls', type=int, default=40)
    parser.add_argument('--max-seconds', type=float, default=1500)
    parser.add_argument('--steps-per-run', type=int, default=6)
    args = parser.parse_args()

    row = load(args.instance_id, args.cache)
    task = Path(args.root) / 'tasks' / args.instance_id
    repo = task / 'repo'
    if not repo.is_dir():
        raise SystemExit(f'Run setup.py first: {repo}')

    files = test_files(row['eval_script'])
    visible, missing_visible = node_ids(repo, files, as_list(row['PASS_TO_PASS']))
    if not visible:
        raise SystemExit('No visible node ids resolved')
    located = hidden_tree(repo, row['test_patch'], task / 'locate')
    files_after = test_files(row['eval_script'])
    hidden, missing_hidden = node_ids(located, files_after, as_list(row['FAIL_TO_PASS']))
    if not hidden:
        raise SystemExit('No hidden node ids resolved; are the FAIL_TO_PASS names in '
                         + ', '.join(files_after))

    # Scope the writable allowlist to the package the tests exercise. Two shapes:
    # tests inside the package (sympy/matrices/expressions/tests/...) or a flat tests/ dir
    # (requests/tests/...). Never the gold patch's files - that would name the answer.
    repo_name = Path(row['repo']).name
    package_dirs = sorted({str(Path(f).parent.parent) for f in files} - {'.', ''})
    if not package_dirs and (Path(repo) / repo_name).is_dir():
        package_dirs = [repo_name]          # flat tests/ dir: the package named after the repo
    if not package_dirs:
        raise SystemExit('Cannot infer which package to scope from: ' + ', '.join(files))
    writable = []
    for directory in package_dirs:
        base = repo / directory
        for path in sorted(base.rglob('*.py')):
            rel = path.relative_to(repo).as_posix()
            if 'tests/' in rel or rel.startswith('test_') or '/test_' in rel:
                continue
            if rel not in writable:
                writable.append(rel)
    if not writable:
        raise SystemExit('No writable modules found for ' + ', '.join(package_dirs))
    if len(writable) > MAX_WRITABLE:
        raise SystemExit(f'{len(writable)} writable modules exceeds the contract limit of '
                         f'{MAX_WRITABLE}; narrow the package scope explicitly rather than '
                         'letting the list be truncated')

    result, log_path = preflight(repo, visible)
    if result.get('exit_code') != 0:
        lines = [line for line in (result.get('output') or '').splitlines() if line.strip()]
        raise SystemExit('Unusable instance: the visible suite does not pass offline at the base '
                         'commit (exit ' + str(result.get('exit_code')) + '). Last output: '
                         + (lines[-1][:300] if lines else '') + '\nLog: ' + str(log_path)
                         + '\nDo not spend a model budget on it.')

    hidden_dir = task / 'hidden'
    hidden_dir.mkdir(exist_ok=True)
    patch_file = hidden_dir / 'test_patch.diff'
    patch_file.write_text(row['test_patch'] if row['test_patch'].endswith('\n')
                          else row['test_patch'] + '\n')

    contract = {
        'goal': row['problem_statement'],
        'repository': str(repo),
        'writable': writable,
        'context': [],
        'acceptance': [*PYTEST, *visible],
        'heldout': [*PYTEST, *hidden],
        'heldout_patch': str(patch_file),
        'max_calls': args.max_calls,
        'max_output_tokens': 8192,
        'timeout': 300,
        'max_seconds': args.max_seconds,
        'limits': {'max_steps': args.steps_per_run, 'keep_recent': 10, 'pin_first': 2},
    }
    out = Path(args.out) if args.out else task / 'contract.json'
    out.write_text(json.dumps(contract, indent=2))
    print(json.dumps({'instance': row['instance_id'],
                      'preflight_visible_exit': result.get('exit_code'),
                      'contract': str(out),
                      'hidden_node_ids': len(hidden),
                      'hidden_patch': str(patch_file),
                      'writable_modules': len(writable),
                      'package_dirs': package_dirs,
                      'visible_nodes': len(visible),
                      'unmapped_visible': missing_visible,
                      'unmapped_hidden': missing_hidden,
                      'goal_chars': len(row['problem_statement'])}, indent=2))
    print('\nHidden node ids were located in a throwaway copy with the PR test patch applied.')


if __name__ == '__main__':
    main()
