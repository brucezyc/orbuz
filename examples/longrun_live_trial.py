"""Opt-in live long-horizon trial: cap -> resume -> accept on a real model.

Creates a disposable fixture project with a deliberate hidden suite, then drives one task
through repeated `run` calls. A soft `max_steps` makes each call stop early, so the trial
exercises exactly what a long project does: pause in `capped`, resume in the same worktree,
finish accepted with both the visible and the held-out suite green.

    python examples/longrun_live_trial.py --state-dir /tmp/longrun/state

It uses real API quota. No credential value is ever printed. Exits nonzero unless the task
ends `accepted` and the held-out suite passed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbuz.runtime.engine import Runtime            # noqa: E402
from orbuz.runtime.model import ChatModel           # noqa: E402

TERMINAL = ('accepted', 'rejected', 'hacking_suspected', 'failed', 'stalled', 'exhausted',
            'blocked', 'cancelled')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def fixture(base):
    repo, hidden = base / 'repo', base / 'heldout'
    repo.mkdir(parents=True)
    hidden.mkdir(parents=True)
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'trial@example.invalid')
    git(repo, 'config', 'user.name', 'Trial')
    (repo / 'lib.py').write_text('def total(items):\n    return 0\n')
    (repo / 'check.py').write_text('import sys\nsys.path.insert(0, ".")\nfrom lib import total\n'
                                   'assert total([1, 2, 3]) == 6\nprint("visible ok")\n')
    (hidden / 'check_all.py').write_text(
        'import sys\nsys.path.insert(0, "/workspace")\nfrom lib import total\n'
        'assert total([]) == 0\nassert total([5]) == 5\nassert total([1, 2, 3, 4]) == 10\n'
        'print("heldout ok")\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'baseline')
    return repo, hidden


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    parser.add_argument('--max-runs', type=int, default=8)
    parser.add_argument('--steps-per-run', type=int, default=1,
                        help='soft max_steps per run() call: forces cap/resume cycles')
    parser.add_argument('--keep', action='store_true', help='keep the fixture directory')
    args = parser.parse_args()

    if not os.environ.get(args.key_env):
        print(f'Absent credential environment variable: {args.key_env}')
        return 2

    base = Path(tempfile.mkdtemp(prefix='longrun-trial-'))
    repo, hidden = fixture(base)
    rt = Runtime(args.state_dir)
    task_id = rt.create(dict(
        goal='Make total(items) return the sum of the numbers in items.',
        repository=str(repo), writable=['lib.py'], context=['lib.py', 'check.py'],
        acceptance=['/usr/bin/python3', '-B', 'check.py'],
        heldout=['/usr/bin/python3', '-B', '/heldout/check_all.py'],
        heldout_assets=[str(hidden)],
        max_calls=12, max_output_tokens=1024, timeout=15,
        limits={'max_steps': args.steps_per_run, 'keep_recent': 6, 'pin_first': 1}))
    rt.pin(task_id, 'Constraint: only lib.py may change; check.py is protected.')

    model = ChatModel(args.model, args.base_url, args.key_env)
    try:
        for attempt in range(args.max_runs):
            result = rt.run(task_id, model)
            print(json.dumps({'run': attempt + 1, 'status': result['status'],
                              'calls': result['calls'], 'steps': result['steps'],
                              'tokens': (result.get('budget') or {}).get('total_tokens'),
                              'reason': result.get('error')}, ensure_ascii=False), flush=True)
            if result['status'] in TERMINAL:
                break
    finally:
        model.close()

    final = rt.status(task_id)
    verified = rt.verify(task_id)
    suites = (final.get('evidence') or {}).get('suites') or {}
    report = {
        'task': task_id,
        'status': final['status'],
        'verified': verified['status'],
        'visible_exit': (suites.get('visible') or {}).get('exit_code'),
        'heldout_exit': (suites.get('heldout') or {}).get('exit_code'),
        'heldout_mounts': (suites.get('heldout') or {}).get('mounts'),
        'attempts': [{'status': a['status'], 'resumed': bool(a.get('resumed'))}
                     for a in final['attempts']],
        'journal_steps': len(rt.journal.steps(task_id)),
        'prefix_cache': final.get('cache'),
        'pins': len(final.get('pins') or []),
        'workspace': final.get('workspace'),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    ok = (final['status'] == 'accepted' and verified['status'] == 'accepted'
          and report['heldout_exit'] == 0 and report['visible_exit'] == 0)
    print('TRIAL PASS' if ok else 'TRIAL FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
