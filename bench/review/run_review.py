"""Run a code review as several independent agents, then merge what they found.

The unit of work is a review case (see make_cases.py): a submitted patch with one hidden defect,
and a scorer the reviewers cannot read. Each persona is its own task in the runtime - its own
journal, its own tokens, its own verdict - and they share nothing but the patch they review, so
"did the extra agents help" is answerable per agent instead of on average.

Personas come from the repository's own agent library: the always-on reviewers, which is the
selection rule that library already declares. A single-persona run is the baseline the fan-out
has to beat, at equal or lower cost.

Usage:
  python3 bench/review/run_review.py <case_dir> --personas all
  python3 bench/review/run_review.py <case_dir> --personas ce-correctness-reviewer
"""
import argparse
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'swebench'))
sys.path.insert(0, '/root/orbuz')

import yaml                                        # noqa: E402
from orbuz.runtime.model import ChatModel          # noqa: E402
from orbuz.runtime.engine import Runtime           # noqa: E402

SHAPE_CHECK = ("import json;d=json.load(open('{name}'));assert isinstance(d,list),'findings must "
               "be a list';print('shape ok', len(d))")


def definitions(repo):
    """Load the repository's own agent library: name, tier, archetype, principles."""
    base = Path(repo) / 'agents'
    if not (base / 'index.yaml').exists():
        base = Path(repo) / 'orbuz' / 'agents'
    index = yaml.safe_load((base / 'index.yaml').read_text())['agents']
    loaded = []
    for entry in index:
        path = base / entry['file']
        if not path.exists():
            continue
        definition = yaml.safe_load(path.read_text()) or {}
        loaded.append({'name': entry['name'], 'summary': definition.get('summary', ''),
                       'tier': definition.get('persona_tier'),
                       'archetype': definition.get('archetype'),
                       'principles': definition.get('principles') or [],
                       'constraints': definition.get('constraints') or []})
    return loaded


# The library declares what each persona is for: pick by that declaration, not by name.
# always_on means the pipeline runs it on every diff, which is the honest default fan-out.
# Researchers stay out of a code review even when they are always-on.
SELECTORS = {
    'all': lambda d: d['archetype'] == 'reviewer' and d['tier'] == 'always_on',
    'reviewers': lambda d: d['archetype'] == 'reviewer'
    and d['tier'] in ('always_on', 'cross_cutting'),
    'always-on': lambda d: d['tier'] == 'always_on',
}


def personas_of(repo, only=None):
    """The reviewers to run: a selector name, an explicit list, or the equal-cost single agent."""
    loaded = definitions(repo)
    if len(only or ()) == 1 and next(iter(only)) in SELECTORS:
        chosen = [d for d in loaded if SELECTORS[next(iter(only))](d)]
    elif not only or only == {'merged'}:
        chosen = [d for d in loaded if SELECTORS['all'](d)]
    else:
        chosen = [d for d in loaded if d['name'] in only]
    if only == {'merged'}:
        # The equal-cost baseline: one agent holding every principle the fan-out gets, with the
        # fan-out's whole budget. Without this, "more agents are better" can be bought by
        # spending more, and the comparison would say nothing about the shape.
        return [{'name': 'generalist-equal-cost',
                 'summary': f"one reviewer carrying all {len(chosen)} concerns of the fan-out",
                 'principles': [f"[{d['name']}] {x}" for d in chosen for x in d['principles']],
                 'constraints': [f"[{d['name']}] {x}" for d in chosen for x in d['constraints']]}]
    return chosen


def review_tree(case_dir, instance_repo, patch_text, mode):
    """A git repo holding base + the submitted patch, one per arm.

    One tree per arm: two arms sharing a tree means two `git apply` runs and two commits racing
    for the same index, which silently turns a comparison into a set of contract errors. A tree
    left behind by an interrupted run is rebuilt rather than trusted.
    """
    tree = case_dir / f'review-tree-{mode}'
    if tree.exists():
        head = subprocess.run(['git', '-C', str(tree), 'log', '-1', '--format=%s'],
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', str(tree), 'status', '--porcelain'],
                              capture_output=True, text=True).stdout.strip()
        if head == 'submitted patch' and not dirty:
            return tree
        shutil.rmtree(tree)
    shutil.copytree(instance_repo, tree, symlinks=True)
    subprocess.run(['git', '-C', str(tree), 'checkout', '-q', '--', '.'], check=True)
    subprocess.run(['git', '-C', str(tree), 'apply', str(case_dir / 'candidate.patch')], check=True)
    subprocess.run(['git', '-C', str(tree), '-c', 'user.email=review@example.invalid',
                    '-c', 'user.name=Review', 'commit', '-qam', 'submitted patch'], check=True)
    return tree


def contract_for(case_dir, goal, tree, persona, max_calls, max_steps=40):
    name = f"findings-{persona['name']}.json"
    text = goal + ('\n\n## Your reviewer role: ' + persona['name'] + '\n'
                   + persona['summary'] + '\n' + '\n'.join('- ' + p for p in persona['principles'])
                   + ('\nConstraints:\n' + '\n'.join('- ' + c for c in persona['constraints'])
                      if persona['constraints'] else '')
                   + '\nWrite exactly one file, ' + name + ', and then submit.')
    return {'goal': text, 'repository': str(tree), 'writable': [name], 'context': [],
            'acceptance': ['/usr/bin/python3', '-B', '-c', SHAPE_CHECK.format(name=name)],
            'heldout': ['/usr/bin/python3', '/heldout/score.py', '/workspace/' + name,
                        '/heldout/ground_truth.json'],
            'heldout_assets': [str(case_dir / 'hidden')],
            'max_calls': max_calls, 'max_output_tokens': 8192, 'timeout': 300,
            'max_seconds': 1800, 'limits': {'max_steps': max_steps, 'keep_recent': 10,
                                            'pin_first': 2}}


def merge(findings_by_persona, tolerance=3):
    """Dedup by location: one defect reported twice is one finding paid for twice."""
    merged = []
    for persona, findings in findings_by_persona.items():
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            path = str(finding.get('path') or '').lstrip('./')
            line = finding.get('line')
            same = next((e for e in merged
                         if (e['path'].endswith(path) or path.endswith(e['path']))
                         and isinstance(e['line'], int) and isinstance(line, int)
                         and abs(e['line'] - line) <= tolerance), None)
            if same is None:
                merged.append({'path': path, 'line': line, 'kind': finding.get('kind'),
                               'severity': finding.get('severity'), 'why': finding.get('why'),
                               'agents': [persona]})
            elif persona not in same['agents']:
                same['agents'].append(persona)
    return merged


def score(case_dir, findings_path):
    done = subprocess.run(['/usr/bin/python3', str(case_dir / 'hidden' / 'score.py'),
                           str(findings_path), str(case_dir / 'hidden' / 'ground_truth.json')],
                          capture_output=True, text=True)
    detail = {}
    try:
        detail = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return done.returncode == 0, detail


def run_case(case_dir, state_dir, model_name, base_url, env_file, selected, max_calls,
             max_steps=40, tag='', concurrency=6):
    meta = json.loads((case_dir / 'meta.json').read_text())
    mode = 'baseline' if len(selected) == 1 and selected[0]['name'].startswith('generalist') \
        else 'fanout'
    instance_repo = Path('/root/bench/tasks') / meta['instance'] / 'repo'
    goal = (case_dir / 'goal.md').read_text()
    tree = review_tree(case_dir, instance_repo, (case_dir / 'candidate.patch').read_text(), mode)

    # Tasks are created one at a time. A repository has one index, and two worktree adds racing
    # for it is how a parallel instrument reports contract errors instead of results.
    runtime = Runtime(state_dir)
    created, results, findings_by_persona = [], {}, {}
    for persona in selected:
        try:
            created.append((persona,
                            runtime.create(contract_for(case_dir, goal, tree, persona,
                                                        max_calls, max_steps))))
        except Exception as exc:
            # One unusable persona must not cost the other agents their results.
            results[persona['name']] = {'status': 'contract_error',
                                        'detail': f'{type(exc).__name__}: {exc}'[:200]}
            print(json.dumps({'case': case_dir.name, 'agent': persona['name'],
                              'status': 'contract_error'}), flush=True)

    # The runs themselves go in parallel: that is the point of several agents, and a runner that
    # serialises them cannot measure it. Each worker gets its own Runtime and its own client so
    # journals and connections are not shared, and the clock is recorded per agent and per arm.
    def one(item):
        persona, task = item
        worker, client = Runtime(state_dir), ChatModel(model_name, base_url, 'ORBUZ_KEY')
        begin = time.monotonic()
        try:
            outcome = worker.run(task, client)
        except Exception as exc:
            outcome = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'[:200],
                       'calls': 0, 'workspace': ''}
        finally:
            client.close()
        return persona, outcome, round(time.monotonic() - begin, 1)

    arm_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:  # noqa: SIM117
        for persona, outcome, seconds in pool.map(one, created):
            findings_path = Path(outcome.get('workspace') or '.') / f"findings-{persona['name']}.json"
            try:
                findings = json.loads(findings_path.read_text())
            except Exception:
                findings = []
            findings_by_persona[persona['name']] = findings if isinstance(findings, list) else []
            results[persona['name']] = {
                'status': outcome['status'], 'error': outcome.get('error'),
                'calls': outcome['calls'], 'seconds': seconds,
                'journal_steps': outcome.get('journal_steps'),
                'tokens': (outcome.get('budget') or {}).get('total_tokens'),
                'findings': len(findings) if isinstance(findings, list) else 0,
                'score': score(case_dir, findings_path),
            }
            print(json.dumps({'case': case_dir.name, 'agent': persona['name'],
                              **{k: results[persona['name']][k] for k in
                                 ('status', 'calls', 'seconds', 'findings', 'tokens')},
                              'hit': results[persona['name']]['score'][0]}), flush=True)
    wall_clock = round(time.monotonic() - arm_start, 1)

    merged = merge(findings_by_persona)
    merged_path = case_dir / 'merged-findings.json'
    merged_path.write_text(json.dumps(merged, indent=2))
    merged_score = score(case_dir, merged_path)
    report = {'case': case_dir.name, 'instance': meta['instance'], 'control': meta.get('control', False),
              'agents': results, 'merged': {'findings': len(merged), 'agents': len(selected),
                                            'score': merged_score},
              'cost': {'calls': sum(r.get('calls') or 0 for r in results.values()),
                       'tokens': sum(r.get('tokens') or 0 for r in results.values()),
                       'concurrency': concurrency, 'wall_clock_s': wall_clock,
                       'agent_seconds': sum(r.get('seconds') or 0 for r in results.values())},
              'hits': [n for n, r in results.items() if (r.get('score') or (False,))[0]]}
    # One file per configuration: the fan-out report and the baseline report must both survive
    # the comparison, and a shared filename silently loses whichever ran first.
    (case_dir / f'review-{mode}{tag}.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'case': case_dir.name, 'mode': mode, 'merged_findings': len(merged),
                      'merged_hit': merged_score[0], 'cost': report['cost'],
                      'agents_hit': report['hits']}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('case_dir', nargs='?')
    parser.add_argument('--repo-for-personas', default='/root/orbuz')
    parser.add_argument('--personas', default='all',
                        help='all (always-on reviewers) or a comma-separated persona list')
    parser.add_argument('--state-dir', default='/root/orbuz-state')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--env-file', default='/root/.orbuz/deepseek.env')
    parser.add_argument('--max-calls', type=int, default=15)
    parser.add_argument('--list-personas', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
                        help='build every contract and stop: catches config errors before spend')
    parser.add_argument('--max-steps', type=int, default=40,
                        help='tool steps per run: a budget arm cut short here is not a fair '
                             'comparison, whatever its call budget says')
    parser.add_argument('--tag', default='', help='suffix for the report filename')
    parser.add_argument('--concurrency', type=int, default=6,
                        help='agents run at once: the axis a serialised runner cannot measure')
    args = parser.parse_args()

    only = None if args.personas == 'all' else set(args.personas.split(','))
    available = personas_of(args.repo_for_personas, only)
    if args.list_personas:
        for persona in available:
            print(json.dumps({'name': persona['name'], 'summary': persona['summary']}))
        return
    if not available:
        raise SystemExit('No always-on reviewers found in ' + args.repo_for_personas)

    key = ''
    if Path(args.env_file).exists():
        for line in Path(args.env_file).read_text().splitlines():
            if line.startswith(('ORBUZ_KEY=', 'DEEPSEEK_API_KEY=')):
                key = line.split('=', 1)[1].strip().strip('"')
    os.environ['ORBUZ_KEY'] = key or os.environ.get('ORBUZ_KEY', '')
    if not os.environ['ORBUZ_KEY']:
        raise SystemExit('No credential: set --env-file or ORBUZ_KEY')

    if args.dry_run:
        # Contracts are validated here, in a second, instead of after the first model call.
        case_dir = Path(args.case_dir)
        meta = json.loads((case_dir / 'meta.json').read_text())
        instance_repo = Path('/root/bench/tasks') / meta['instance'] / 'repo'
        tree = review_tree(case_dir, instance_repo, (case_dir / 'candidate.patch').read_text(), 'fanout')
        goal = (case_dir / 'goal.md').read_text()
        runtime = Runtime(args.state_dir)
        for persona in available:
            spec = contract_for(case_dir, goal, tree, persona, args.max_calls)
            task = runtime.create(spec)          # create validates: this is the point of dry-run
            print(json.dumps({'agent': persona['name'], 'task': task,
                              'goal_chars': len(spec['goal']), 'writable': spec['writable'],
                              'heldout': spec['heldout'], 'assets': spec['heldout_assets'],
                              'max_calls': spec['max_calls'], 'max_seconds': spec['max_seconds']}))
        return
    run_case(Path(args.case_dir), args.state_dir, args.model, args.base_url, args.env_file,
             available, args.max_calls, args.max_steps, args.tag, args.concurrency)


if __name__ == '__main__':
    main()
