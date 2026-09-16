"""Try to refute every finding a review produced: what survives is what a second agent could not break.

A review is only as good as its worst claim. The fan-out in this bench reports a defect on control
cases where there is none, so the mechanism worth testing is not more opinions but a dedicated
attempt to kill each one: every finding gets its own agent, the finding and the patch are its whole
input, and it has to write a verdict. Findings nobody could refute survive, and the case's hidden
scorer then says whether precision and recall moved.

Composes with a completed arm: it reads the findings that arm reported, so the expensive first pass
is never paid for twice.

  python3 bench/review/refute.py <case_dir> --from review-fanout-v4.json --model deepseek-flash
  python3 bench/review/refute.py <case_dir> --from review-fanout-v4.json --model deepseek-v4-pro --tag=-dear
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'swebench'))
sys.path.insert(0, '/root/orbuz')

from orbuz.runtime.engine import Runtime          # noqa: E402
from orbuz.runtime.model import ChatModel         # noqa: E402

SHAPE = ("import json;d=json.load(open('{name}'));assert isinstance(d.get('refuted'), bool),"
         "'refuted must be a boolean';print('verdict', d['refuted'])")

ASK = '''# Refute this finding

A reviewer reported the defect below in the candidate patch. Your job is to try to break the
claim, not to agree with it: check the quoted code, the semantics it depends on, and whether the
behaviour it describes can actually differ. Write `{name}` with
`{{"refuted": true|false, "why": "one or two sentences"}}`.

`refuted: true` means the claim is wrong, already handled, or cannot affect behaviour.
`refuted: false` means you tried and could not break it, and here is why it holds.

A finding you cannot break is worth keeping; agreeing cheaply is what makes a review noisy.

## Candidate patch

```diff
{patch}
```

## Reported finding

```json
{finding}
```
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('case_dir')
    parser.add_argument('--from', dest='source', default='review-fanout.json',
                        help='the arm report whose findings should be attacked')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--env-file', default='/root/.orbuz/deepseek.env')
    parser.add_argument('--state-dir', default='/root/orbuz-state')
    parser.add_argument('--max-calls', type=int, default=4)
    parser.add_argument('--max-steps', type=int, default=12)
    parser.add_argument('--concurrency', type=int, default=6)
    parser.add_argument('--tag', default='-refuted')
    args = parser.parse_args()

    case_dir = Path(args.case_dir)
    report = json.loads((case_dir / args.source).read_text())
    items = ((report.get('merged') or {}).get('items')) or []
    if not items:
        raise SystemExit(f"{args.source} carries no findings to attack. Re-run the arm with a "
                         "version of run_review.py that stores merged.items.")
    patch_text = (case_dir / 'candidate.patch').read_text()

    key = ''
    if Path(args.env_file).exists():
        for line in Path(args.env_file).read_text().splitlines():
            if line.startswith(('ORBUZ_KEY=', 'DEEPSEEK_API_KEY=')):
                key = line.split('=', 1)[1].strip().strip('"')
    os.environ['ORBUZ_KEY'] = key or os.environ.get('ORBUZ_KEY', '')
    if not os.environ['ORBUZ_KEY']:
        raise SystemExit('No credential: set --env-file or ORBUZ_KEY')

    runtime = Runtime(args.state_dir)
    created = []
    for index, finding in enumerate(items):
        name = f'verdict-{index}.json'
        contract = {
            'goal': ASK.format(name=name, patch=patch_text.strip(),
                               finding=json.dumps(finding, indent=2)),
            'repository': str(case_dir / f'review-tree-{report.get("mode", "fanout")}'),
            'writable': [name], 'context': [],
            'acceptance': ['/usr/bin/python3', '-B', '-c', SHAPE.format(name=name)],
            'max_calls': args.max_calls, 'max_output_tokens': 8192, 'timeout': 300,
            'max_seconds': 1800, 'limits': {'max_steps': args.max_steps, 'keep_recent': 8,
                                            'pin_first': 2},
        }
        if not Path(contract['repository']).is_dir():
            raise SystemExit(f"No review tree at {contract['repository']}; run the arm first")
        created.append((index, finding, runtime.create(contract)))

    def one(entry):
        index, finding, task = entry
        worker, client = Runtime(args.state_dir), ChatModel(args.model, args.base_url, 'ORBUZ_KEY')
        begin = time.monotonic()
        try:
            outcome = worker.run(task, client)
        except Exception as exc:
            outcome = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'[:200],
                       'calls': 0, 'workspace': ''}
        finally:
            client.close()
        return index, outcome, round(time.monotonic() - begin, 1)

    arm_start = time.monotonic()
    verdicts = {}
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for index, outcome, seconds in pool.map(one, created):
            path = Path(outcome.get('workspace') or '.') / f'verdict-{index}.json'
            try:
                verdict = json.loads(path.read_text())
            except Exception:
                verdict = {}
            # No verdict is not the same as "could not be refuted": an agent that died proves
            # nothing, so the two readings are reported apart instead of one being assumed.
            verdicts[index] = {'verdict_present': isinstance(verdict.get('refuted'), bool),
                               'refuted': bool(verdict.get('refuted')),
                               'why': str(verdict.get('why') or '')[:400],
                               'status': outcome['status'], 'calls': outcome.get('calls'),
                               'tokens': (outcome.get('budget') or {}).get('total_tokens'),
                               'seconds': seconds}
            print(json.dumps({'finding': index, 'refuted': verdicts[index]['refuted'],
                              'status': outcome['status'], 'calls': outcome.get('calls'),
                              'tokens': verdicts[index]['tokens'], 'seconds': seconds,
                              'why': verdicts[index]['why'][:120]}), flush=True)
    wall_clock = round(time.monotonic() - arm_start, 1)

    def survived(strict):
        keep = []
        for index, finding in enumerate(items):
            verdict = verdicts.get(index) or {}
            if verdict.get('refuted'):
                continue
            if strict and not verdict.get('verdict_present'):
                continue
            keep.append(finding)
        return keep
    survivors, survivors_loose = survived(True), survived(False)
    out = case_dir / f'refuted{args.tag}.json'
    out.write_text(json.dumps(survivors, indent=2))
    score = subprocess.run(['/usr/bin/python3', str(case_dir / 'hidden' / 'score.py'), str(out),
                            str(case_dir / 'hidden' / 'ground_truth.json')],
                           capture_output=True, text=True)
    summary = {'case': case_dir.name, 'source': args.source, 'model': args.model,
               'findings_in': len(items), 'refuted': len(items) - len(survivors_loose),
               'survived': len(survivors), 'survived_loose': len(survivors_loose),
               'unverified': sum(1 for v in verdicts.values() if not v['verdict_present']),
               'survivors_score': [score.returncode == 0,
                                   json.loads(score.stdout.strip().splitlines()[-1])],
               'verdicts': verdicts,
               'cost': {'calls': sum((v.get('calls') or 0) for v in verdicts.values()),
                        'tokens': sum((v.get('tokens') or 0) for v in verdicts.values()),
                        'wall_clock_s': wall_clock, 'concurrency': args.concurrency}}
    (case_dir / f'refute{args.tag}.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ('case', 'findings_in', 'refuted', 'survived',
                                              'survived_loose', 'unverified', 'survivors_score',
                                              'cost')}))


if __name__ == '__main__':
    main()
