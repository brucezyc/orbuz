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


def candidates(cache, repo, limit, window='newest'):
    """Pick a window of one repository's instances and walk it forward in time.

    A sprint always runs oldest-to-newest, but the window matters: this environment runs
    python 3.11, so a repository's earliest instances (2016-2017 sympy) are not usable at all -
    their code predates the interpreter. ``newest`` takes the most recent instances and still
    walks them in ascending order, which is the window a sprint can actually run in.
    """
    rows = [row for row in json.loads(Path(cache).read_text()) if row['repo'] == repo]
    if not rows:
        raise SystemExit(f'No instances for {repo} in {cache}')
    rows.sort(key=lambda row: row['created_at'])
    chosen = rows[:limit] if window == 'oldest' else rows[-limit:]
    return chosen


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
    selected = candidates(args.cache, args.repo, args.instances, args.window)
    if not repo_dir.is_dir():
        # Seed from upstream, not from a per-instance clone: those carry only the single
        # commit their instance needs, so a sprint could not reach the other bases.
        git(Path(args.root), 'clone', '-q', f'https://github.com/{args.repo}.git', str(repo_dir))
        git(repo_dir, 'config', 'user.email', 'sprint@example.invalid')
        git(repo_dir, 'config', 'user.name', 'Sprint')
    records, carry_commit = [], None

    def close():
        """Write what happened so far: a sprint that stops early is a result, not a crash."""
        (sprint_dir / 'sprint.json').write_text(json.dumps(
            {'sprint': sprint_dir.name, 'repo': args.repo, 'instances': records,
             'carry_commit': carry_commit,
             'totals': {'accepted': sum(r.get('status') == 'accepted' for r in records),
                        'calls': sum(r.get('calls') or 0 for r in records),
                        'tokens': sum((r.get('budget') or {}).get('total_tokens') or 0
                                      for r in records)}}, indent=2))

    for index, row in enumerate(selected, 1):
        base = row['base_commit']
        record = {'instance': row['instance_id'], 'base_commit': base[:10], 'index': index}
        if git(repo_dir, 'cat-file', '-e', f'{base}^{{commit}}', check=False).returncode:
            # A base can be outside whatever the repository already has; upstream serves it.
            git(repo_dir, 'fetch', '-q', 'origin', base)
        git(repo_dir, 'checkout', '-q', '--detach', base)
        git(repo_dir, 'reset', '-q', '--hard', base)
        start_red = False
        if carry_commit:
            picked = git(repo_dir, 'cherry-pick', '-x', carry_commit, check=False)
            if picked.returncode:
                # Do not abort. A conflict between the agent's own work and the project's own
                # history is the long-horizon situation itself; aborting would erase the very
                # thing a sprint exists to produce. Hand the conflicted tree to the next task
                # and let the verdict say whether the agent can clean up its own mess.
                record['conflict_paths'] = git(repo_dir, 'diff', '--name-only',
                                               '--diff-filter=U').stdout.split()
                record['detail'] = next((line for line in picked.stderr.splitlines()
                                         if line.startswith('CONFLICT')), '')[:200]
                git(repo_dir, 'add', '-A')
                handed = git(repo_dir, '-c', 'user.email=sprint@example.invalid',
                             '-c', 'user.name=Sprint', 'commit', '-qm',
                             f'carry with unresolved conflict from {row["instance_id"]}',
                             check=False)
                record['carry'] = ('conflict_handed_over' if not handed.returncode
                                   else 'conflict_left_in_worktree')
                start_red = True
            else:
                record['carry'] = 'carried'
        else:
            record['carry'] = 'first'

        contract = sprint_dir / f'{row["instance_id"]}.contract.json'
        build = [sys.executable, str(HERE / 'make_contract.py'), row['instance_id'],
                 '--cache', args.cache, '--root', args.root, '--repo', str(repo_dir),
                 '--out', str(contract), '--max-calls', str(args.max_calls),
                 '--max-seconds', str(args.max_seconds),
                 '--steps-per-run', str(args.steps_per_run)]
        if start_red:
            # A carried tree that starts red is the agent's own mess, not an unusable instance:
            # skip the environment gate and let it try.
            build.append('--no-preflight')
        built = subprocess.run(build, capture_output=True, text=True)
        if built.returncode:
            # Distinguish "this instance cannot run in this environment" from "the work
            # carried from earlier tasks broke this base": the first is an environment fact,
            # the second is the composite-error signal a sprint exists to measure.
            clean = sprint_dir / f'clean-{index}'
            git(repo_dir, 'worktree', 'add', '-q', '--detach', str(clean), base)
            clean_built = subprocess.run(
                [sys.executable, str(HERE / 'make_contract.py'), row['instance_id'],
                 '--cache', args.cache, '--root', args.root, '--repo', str(clean),
                 '--out', str(sprint_dir / f'{row["instance_id"]}.clean.contract.json'),
                 '--max-calls', str(args.max_calls), '--max-seconds', str(args.max_seconds),
                 '--steps-per-run', str(args.steps_per_run)],
                capture_output=True, text=True)
            if clean_built.returncode:
                # The instance cannot run here whatever we did; skip it and keep the sprint
                # alive rather than reporting an environment fact as a sprint outcome.
                record['status'] = 'unusable_instance'
                record['detail'] = (built.stderr.strip() or built.stdout.strip())[-250:]
                records.append(record)
                continue
            # The carry is what broke this base: that is the thing under test, not a reason to
            # stop. Rebuild without the gate and run the task on the broken base.
            record['carried_preflight'] = 'failed'
            rebuilt = subprocess.run([*build, '--no-preflight'], capture_output=True, text=True)
            if rebuilt.returncode:
                record['status'] = 'contract_failed'
                record['detail'] = rebuilt.stderr.strip()[-250:]
                records.append(record)
                continue
            built = rebuilt
            close()
        record['contract'] = last_json(built.stdout) or {}

        start_head = git(repo_dir, 'rev-parse', 'HEAD').stdout.strip()   # base, or base + carry

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
            close()
            break
        # Carry a tree, not the workspace's HEAD. Two traps: a task that changed nothing
        # leaves HEAD at the base commit, and carrying that would replay upstream history into
        # the next instance; and a commit whose parent is the previous carry makes the wrong
        # commit the merge base, which silently drops the work carried before it. A commit
        # whose parent is this instance's own base holds the full cumulative work, and a
        # cherry-pick of it onto the next base three-way merges against exactly that base.
        git(workspace, 'add', '-A')
        tree = git(workspace, 'write-tree').stdout.strip()
        if tree == git(repo_dir, 'rev-parse', f'{start_head}^{{tree}}').stdout.strip():
            # Nothing new. If a carry was already applied this instance, its commit is the
            # carrier (its parent is this base, so the next cherry-pick merges correctly).
            # If there was no carry, there is nothing to carry and the base commit must not be
            # passed on: it is upstream history, and cherry-picking it fabricates a conflict.
            carry_commit = None if start_head == base else start_head
        else:
            carry_commit = git(workspace, '-c', 'user.email=sprint@example.invalid',
                               '-c', 'user.name=Sprint', 'commit-tree', tree, '-p', base,
                               '-m', f'carry {row["instance_id"]}: {final.get("status")}'
                               ).stdout.strip()
        record['carry_commit'] = carry_commit[:10] if carry_commit else None
        record['carry_parent'] = base[:10]
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
    parser.add_argument('--window', choices=('newest', 'oldest'), default='newest',
                        help='which slice of the timeline to sprint over (walked oldest-first)')
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
        for row in candidates(args.cache, args.repo, args.instances, args.window):
            print(json.dumps({'instance': row['instance_id'], 'created_at': row['created_at'],
                              'base_commit': row['base_commit'][:10],
                              'difficulty': row.get('difficulty'),
                              'f2p': len(row.get('FAIL_TO_PASS') or []),
                              'p2p': len(row.get('PASS_TO_PASS') or [])}))
        return
    sprint(args)


if __name__ == '__main__':
    main()
