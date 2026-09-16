"""Run a sprint: consecutive instances of one repository over a single carried line of work.

Single instances measure whether a fix lands. Long-horizon failures only appear when work
carries forward: a fix that breaks the next instance's base, a cherry-pick that will not apply,
a transcript that outgrows what the model can hold. A sprint is the cheapest honest test of
that axis, because it adds exactly one git operation between two otherwise identical tasks.

Per instance, in commit order:

1. check out that instance's base commit and cherry-pick the commit the previous task produced
   (the candidate's own work, accepted or not) - a conflict here is the composite-error signal;
2. build the contract against the carried repository (make_contract re-runs preflight, so a
   carry that breaks the visible suite is caught before any model budget is spent);
3. run the task;
4. commit whatever the task left in its workspace and carry that commit into the next instance.

The candidate's work is carried even when it was rejected: a sprint measures what the agent
does to its own future base, not what a perfect agent would do.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def git(root, *args, check=True):
    done = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if check and done.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {root}: {done.stderr.strip()[:300]}")
    return done


def candidates(cache, repo, limit):
    """Instances of one repository, oldest first, so a sprint walks the project's timeline."""
    rows = [row for row in json.loads(Path(cache).read_text()) if row['repo'] == repo]
    if not rows:
        raise SystemExit(f'No instances for {repo} in {cache}')
    rows.sort(key=lambda row: row['created_at'])
    return rows[:limit]


def last_json(text):
    """The final JSON object a run prints; earlier lines are phase records."""
    depth, start = 0, None
    found = None
    for index, char in enumerate(text):
        if char == '{':
            if depth == 0:
                start = index
            depth += 1
        elif char == '}' and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    found = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    pass
    return found


def sprint(args):
    sprint_dir = Path(args.root) / 'sprints' / f"{Path(args.repo).name}-{args.stamp}"
    repo_dir = sprint_dir / 'repo'
    sprint_dir.mkdir(parents=True, exist_ok=True)
    if not repo_dir.is_dir():
        first = Path(args.root) / 'tasks' / candidates(args.cache, args.repo, 1)[0]['instance_id'] / 'repo'
        if not first.is_dir():
            raise SystemExit(f'Run setup.py for the first instance first: {first}')
        git(args.root, 'clone', '-q', str(first), str(repo_dir))
        git(repo_dir, 'config', 'user.email', 'sprint@example.invalid')
        git(repo_dir, 'config', 'user.name', 'Sprint')

    selected = candidates(args.cache, args.repo, args.instances)
    records, carry_commit = [], None
    for index, row in enumerate(selected, 1):
        base = row['base_commit']
        record = {'instance': row['instance_id'], 'base_commit': base[:10], 'index': index}
        git(repo_dir, 'checkout', '-q', '--detach', base)
        git(repo_dir, 'reset', '-q', '--hard', base)
        if carry_commit:
            picked = git(repo_dir, 'cherry-pick', '-x', carry_commit, check=False)
            if picked.returncode:
                git(repo_dir, 'cherry-pick', '--abort', check=False)
                record['carry'] = 'conflict'
                record['detail'] = picked.stderr.strip().splitlines()[-1][:200] if picked.stderr.strip() else ''
                records.append(record)
                break
            record['carry'] = 'carried'
        else:
            record['carry'] = 'first'

        contract = sprint_dir / f'{row["instance_id"]}.contract.json'
        built = subprocess.run(
            [sys.executable, str(HERE / 'make_contract.py'), row['instance_id'],
             '--cache', args.cache, '--root', args.root, '--repo', str(repo_dir),
             '--out', str(contract), '--max-calls', str(args.max_calls),
             '--max-seconds', str(args.max_seconds), '--steps-per-run', str(args.steps_per_run)],
            capture_output=True, text=True)
        if built.returncode:
            record['status'] = 'unusable_carry'
            record['detail'] = (built.stderr.strip() or built.stdout.strip())[:250]
            records.append(record)
            break
        record['contract'] = json.loads(last_json(built.stdout) or '{}')

        if args.dry_run:
            record['status'] = 'dry_run'
            records.append(record)
            continue

        launched = subprocess.run(
            [sys.executable, str(HERE / 'run_task.py'), str(contract),
             '--state-dir', args.state_dir, '--model', args.model, '--base-url', args.base_url,
             '--env-file', args.env_file, '--runs', str(args.runs)],
            capture_output=True, text=True)
        phase_log = sprint_dir / f'{row["instance_id"]}.run.log'
        phase_log.write_text(launched.stdout + launched.stderr)
        final = last_json(launched.stdout) or {}
        record.update({'status': final.get('status'), 'verified': final.get('verified'),
                       'task': final.get('task'), 'calls': final.get('calls'),
                       'journal_steps': final.get('journal_steps'),
                       'visible_exit': final.get('visible_exit'),
                       'heldout_exit': final.get('heldout_exit'),
                       'budget': final.get('budget'), 'log': str(phase_log)})

        workspace = final.get('workspace')
        if not workspace or not Path(workspace).is_dir():
            record['carry'] = 'lost: no workspace to carry'
            records.append(record)
            break
        git(workspace, 'add', '-A')
        committed = git(workspace, '-c', 'user.email=sprint@example.invalid',
                        '-c', 'user.name=Sprint', 'commit', '-qm',
                        f'sprint {row["instance_id"]}: {final.get("status")}', check=False)
        if committed.returncode and 'nothing to commit' not in (committed.stdout + committed.stderr):
            record['carry'] = 'lost: could not commit workspace'
            records.append(record)
            break
        carry_commit = git(workspace, 'rev-parse', 'HEAD').stdout.strip()
        record['carry_commit'] = carry_commit[:10]
        records.append(record)

        report = {'sprint': sprint_dir.name, 'repo': args.repo, 'instances': records,
                  'carry_commit': carry_commit,
                  'totals': {'accepted': sum(r.get('status') == 'accepted' for r in records),
                             'calls': sum(r.get('calls') or 0 for r in records),
                             'tokens': sum((r.get('budget') or {}).get('total_tokens') or 0
                                           for r in records)}}
        (sprint_dir / 'sprint.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'index': index, **{k: record.get(k) for k in
              ('instance', 'carry', 'status', 'visible_exit', 'heldout_exit', 'calls',
               'journal_steps', 'carry_commit')}}, ensure_ascii=False), flush=True)

    report = {'sprint': sprint_dir.name, 'repo': args.repo, 'instances': records,
              'carry_commit': carry_commit,
              'totals': {'accepted': sum(r.get('status') == 'accepted' for r in records),
                         'calls': sum(r.get('calls') or 0 for r in records),
                         'tokens': sum((r.get('budget') or {}).get('total_tokens') or 0
                                       for r in records)}}
    (sprint_dir / 'sprint.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report['totals'], ensure_ascii=False))
    print(json.dumps({'sprint': sprint_dir.name, 'report': str(sprint_dir / 'sprint.json')}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default='sympy/sympy')
    parser.add_argument('--instances', type=int, default=3)
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--state-dir', default='/root/orbuz-state')
    parser.add_argument('--stamp', default='sprint')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--env-file', default='/root/.orbuz/deepseek.env')
    parser.add_argument('--runs', type=int, default=6)
    parser.add_argument('--max-calls', type=int, default=30)
    parser.add_argument('--max-seconds', type=float, default=1500)
    parser.add_argument('--steps-per-run', type=int, default=6)
    parser.add_argument('--list', action='store_true', help='show the selected instances and stop')
    parser.add_argument('--dry-run', action='store_true', help='carry and build contracts, run nothing')
    args = parser.parse_args()

    if args.list:
        for row in candidates(args.cache, args.repo, args.instances):
            print(json.dumps({'instance': row['instance_id'], 'created_at': row['created_at'],
                              'base_commit': row['base_commit'][:10],
                              'difficulty': row.get('difficulty'),
                              'f2p': len(row.get('FAIL_TO_PASS') or []),
                              'p2p': len(row.get('PASS_TO_PASS') or [])}))
        return
    sprint(args)


if __name__ == '__main__':
    main()
