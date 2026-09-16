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


def personas_of(repo, only=None):
    """The repository's always-on reviewers, in the order its index declares them."""
    index = yaml.safe_load((Path(repo) / 'agents' / 'index.yaml').read_text())['agents']
    if only == {'merged'}:
        # The equal-cost baseline: one agent holding every always-on concern, run with the
        # budget the whole fan-out gets. Without this, "more agents are better" can be bought
        # by spending more, and the comparison would say nothing about the shape.
        always = [e for e in index]
        principles, constraints = [], []
        for entry in always:
            path = Path(repo) / 'agents' / entry['file']
            if not path.exists():
                continue
            definition = yaml.safe_load(path.read_text()) or {}
            if definition.get('persona_tier') != 'always_on':
                continue
            principles += [f"[{entry['name']}] {p}" for p in (definition.get('principles') or [])]
            constraints += [f"[{entry['name']}] {c}" for c in (definition.get('constraints') or [])]
        return [{'name': 'generalist-equal-cost',
                 'summary': f'one reviewer carrying all {len(always)} always-on concerns',
                 'principles': principles, 'constraints': constraints}]
    chosen = []
    for entry in index:
        path = Path(repo) / 'agents' / entry['file']
        if not path.exists():
            continue
        definition = yaml.safe_load(path.read_text()) or {}
        if definition.get('persona_tier') != 'always_on':
            continue
        if only and entry['name'] not in only:
            continue
        chosen.append({'name': entry['name'], 'summary': definition.get('summary', ''),
                       'principles': definition.get('principles') or [],
                       'constraints': definition.get('constraints') or []})
    return chosen


def review_tree(case_dir, instance_repo, patch_text):
    """A git repo holding base + the submitted patch: the reviewers read this, never write it."""
    tree = case_dir / 'review-tree'
    if tree.exists():
        return tree
    shutil.copytree(instance_repo, tree, symlinks=True)
    subprocess.run(['git', '-C', str(tree), 'checkout', '-q', '--', '.'], check=True)
    patch = case_dir / 'candidate.patch'
    subprocess.run(['git', '-C', str(tree), 'apply', str(patch)], check=True)
    subprocess.run(['git', '-C', str(tree), '-c', 'user.email=review@example.invalid',
                    '-c', 'user.name=Review', 'commit', '-qam', 'submitted patch'], check=True)
    return tree


def contract_for(case_dir, goal, tree, persona, max_calls):
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
            'max_calls': max_calls, 'max_output_tokens': 4096, 'timeout': 300,
            'max_seconds': 1200, 'limits': {'max_steps': 8, 'keep_recent': 10, 'pin_first': 2}}


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


def run_case(case_dir, state_dir, model_name, base_url, env_file, selected, max_calls):
    meta = json.loads((case_dir / 'meta.json').read_text())
    instance_repo = Path('/root/bench/tasks') / meta['instance'] / 'repo'
    goal = (case_dir / 'goal.md').read_text()
    tree = review_tree(case_dir, instance_repo, (case_dir / 'candidate.patch').read_text())
    runtime = Runtime(state_dir)
    model = ChatModel(model_name, base_url, 'ORBUZ_KEY')
    results, findings_by_persona = {}, {}
    try:
        for persona in selected:
            try:
                spec = runtime.create(contract_for(case_dir, goal, tree, persona, max_calls))
            except Exception as exc:
                # One unusable persona must not cost the other agents their results.
                results[persona['name']] = {'status': 'contract_error',
                                            'detail': f'{type(exc).__name__}: {exc}'[:200]}
                print(json.dumps({'case': case_dir.name, 'agent': persona['name'],
                                  'status': 'contract_error'}), flush=True)
                continue
            outcome = runtime.run(spec, model)
            findings_path = Path(outcome['workspace']) / f"findings-{persona['name']}.json"
            try:
                findings = json.loads(findings_path.read_text())
            except Exception:
                findings = []
            findings_by_persona[persona['name']] = findings if isinstance(findings, list) else []
            results[persona['name']] = {
                'status': outcome['status'], 'calls': outcome['calls'],
                'journal_steps': outcome.get('journal_steps'),
                'tokens': (outcome.get('budget') or {}).get('total_tokens'),
                'findings': len(findings) if isinstance(findings, list) else 0,
                'score': score(case_dir, findings_path),
            }
            print(json.dumps({'case': case_dir.name, 'agent': persona['name'],
                              **{k: results[persona['name']][k] for k in
                                 ('status', 'calls', 'findings', 'tokens')},
                              'hit': results[persona['name']]['score'][0]}), flush=True)
    finally:
        model.close()

    merged = merge(findings_by_persona)
    merged_path = case_dir / 'merged-findings.json'
    merged_path.write_text(json.dumps(merged, indent=2))
    merged_score = score(case_dir, merged_path)
    report = {'case': case_dir.name, 'instance': meta['instance'], 'control': meta.get('control', False),
              'agents': results, 'merged': {'findings': len(merged), 'agents': len(selected),
                                            'score': merged_score},
              'cost': {'calls': sum(r.get('calls') or 0 for r in results.values()),
                       'tokens': sum(r.get('tokens') or 0 for r in results.values())},
              'hits': [n for n, r in results.items() if (r.get('score') or (False,))[0]]}
    # One file per configuration: the fan-out report and the baseline report must both survive
    # the comparison, and a shared filename silently loses whichever ran first.
    mode = 'baseline' if len(selected) == 1 and selected[0]['name'].startswith('generalist') \
        else 'fanout'
    (case_dir / f'review-{mode}.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'case': case_dir.name, 'mode': mode, 'merged_findings': len(merged),
                      'merged_hit': merged_score[0], 'cost': report['cost'],
                      'agents_hit': [n for n, r in results.items() if r['score'][0]]}))
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

    run_case(Path(args.case_dir), args.state_dir, args.model, args.base_url, args.env_file,
             available, args.max_calls)


if __name__ == '__main__':
    main()
