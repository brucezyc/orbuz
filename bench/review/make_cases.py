"""Turn a SWE-bench instance into review cases with a hidden ground truth.

A single-issue instance is not a review task: the official fix has nothing to find. Mutate the
official patch instead - flip one operator, shift one constant - and you get a patch that still
parses and still passes the tests the project runs anyway, while being wrong in exactly one
place. That place is the ground truth; the PR's own tests are what prove it matters; the
reviewer sees neither.

Every mutant is measured offline in the sandbox before it becomes a case:

    visible  (PASS_TO_PASS)                    the tests the project would run anyway
    held-out (FAIL_TO_PASS + the PR's test patch)  what proves the fix is broken

A mutant that fails held-out is a defect worth finding. A mutant that also passes visible is
the interesting kind: the project's own suite is green and the fix is still wrong.

Output per case (all of it outside the candidate's reach except candidate.patch):

    cases/<instance>/<case>/candidate.patch   what the reviewer sees
    cases/<instance>/<case>/goal.md           the issue plus the patch
    cases/<instance>/<case>/hidden/ground_truth.json   the defect (hidden)
    cases/<instance>/<case>/hidden/score.py   deterministic scorer for findings.json
    cases/<instance>/<case>/meta.json         measured exits
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'swebench'))

import fetch                      # noqa: E402
import repo as repo_mod           # noqa: E402
import runner                     # noqa: E402

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
COMPARISONS = [('==', '!='), ('!=', '=='), ('<=', '<'), ('>=', '>'), ('<', '<='), ('>', '>=')]
WORDS = [('True', 'False'), ('False', 'True'), (' and ', ' or '), (' or ', ' and ')]
NUMBER = re.compile(r'(?<![A-Za-z_.\d])(\d+)(?![A-Za-z_.\d])')

SLUG = {'==->!=': 'ne', '!=->==': 'eq', '<=-><': 'lt', '>=->>': 'gt', '<-><=': 'le',
        '>->>=': 'ge', 'True->False': 'false', 'False->True': 'true',
        ' and -> or ': 'or', ' or -> and ': 'and'}
SCORER = '''"""Score a review against the hidden defect. Deterministic, no model calls.

A finding is a hit when it names the mutated file and lands within {tolerance} lines of the
mutated line: reviewers point at ranges, and demanding an exact line would measure formatting
luck rather than detection. Everything else counts as a false positive.
"""
import json
import sys


def main():
    findings_path, truth_path = sys.argv[1], sys.argv[2]
    truth = json.load(open(truth_path))
    if truth.get('control'):
        hits = 0
    else:
        hits = 0
    try:
        findings = json.load(open(findings_path))
    except Exception as exc:
        print('unreadable findings: ' + str(exc)[:200])
        raise SystemExit(2)
    if isinstance(findings, dict):
        findings = findings.get('findings') or []
    matched = []
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict):
            continue
        path = str(finding.get('path') or '').lstrip('./')
        line = finding.get('line')
        if truth.get('control'):
            continue
        if path.endswith(truth['path']) and isinstance(line, int) \\
                and abs(line - truth['line']) <= {tolerance}:
            hits += 1
            matched.append(finding)
    print(json.dumps({{'findings': len(findings) if isinstance(findings, list) else 0,
                      'hits': hits, 'control': bool(truth.get('control')),
                      'duplicates': max(0, hits - 1)}}))
    # The visible gate passes on shape alone; this is the part a reviewer cannot see.
    if truth.get('control'):
        raise SystemExit(0 if hits == 0 and len(findings) == 0 else 1)
    raise SystemExit(0 if hits else 1)


main()
'''


def added_lines(patch_text):
    """Every added line of a patch, with the file it lands in and its new-file line number."""
    file, new_line = None, 0
    found = []
    for index, line in enumerate(patch_text.splitlines()):
        if line.startswith('+++ b/'):
            file = line[6:].strip()
            continue
        header = HUNK.match(line)
        if header:
            new_line = int(header.group(1))
            continue
        if line.startswith('+++') or line.startswith('---') or line.startswith('@@'):
            continue
        if line.startswith('+'):
            found.append({'index': index, 'file': file, 'line': new_line, 'text': line[1:],
                          'origin': 'added'})
            new_line += 1
        elif line.startswith(' '):
            # Context lines matter too: a submitted patch can be wrong about the code it sits
            # next to, and a reviewer cannot tell which line the author thinks is the fix.
            found.append({'index': index, 'file': file, 'line': new_line, 'text': line[1:],
                          'origin': 'context'})
            new_line += 1
    return found


def mutants(patch_text):
    """One-token mutations of the patch's added lines: the defect has to be findable by reading."""
    lines = patch_text.splitlines()
    out = []
    for entry in added_lines(patch_text):
        candidates = []
        for old, new in COMPARISONS + WORDS:
            if old in entry['text']:
                candidates.append((old, new))
        for number in {n for n in NUMBER.findall(entry['text'])}:
            candidates.append((number, str(int(number) + 1)))
        for old, new in candidates:
            mutated = entry['text'].replace(old, new, 1)
            if mutated == entry['text'] or not mutated.strip():
                continue
            text = '\n'.join([*lines[:entry['index']], '+' + mutated,
                              *lines[entry['index'] + 1:]]) + '\n'
            out.append({'patch': text, 'path': entry['file'], 'line': entry['line'],
                        'origin': entry['origin'], 'kind': f'{old}->{new}',
                        'before': entry['text'].rstrip(), 'after': mutated.rstrip()})
    return out


def measure(repo_dir, row, patch_text, scratch, logs):
    """Run the mutant through both suites: visible (project's own) and held-out (the PR's)."""
    work = scratch / 'mutant'
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(repo_dir, work, symlinks=True, ignore=shutil.ignore_patterns('.git'))
    runner.apply_patch(work, patch_text, 'mutant')
    files = repo_mod.test_files(row['eval_script'])
    qualified, mapped, unmapped = runner.node_map(work, files, fetch.as_list(row['PASS_TO_PASS']))
    visible = runner.sandbox_run(work, [*qualified, *mapped], logs / 'visible.log')

    hidden_tree = scratch / 'heldout'
    if hidden_tree.exists():
        shutil.rmtree(hidden_tree)
    shutil.copytree(work, hidden_tree, symlinks=True, ignore=shutil.ignore_patterns('.git'))
    runner.apply_patch(hidden_tree, row['test_patch'], 'test_patch')
    files_after = repo_mod.test_files(row['eval_script'])
    hq, hm, hunmapped = runner.node_map(hidden_tree, files_after,
                                        fetch.as_list(row['FAIL_TO_PASS']))
    heldout = runner.sandbox_run(hidden_tree, [*hq, *hm], logs / 'heldout.log')
    return {'visible_exit': visible.get('exit_code'), 'heldout_exit': heldout.get('exit_code'),
            'visible_summary': (visible.get('output') or '').strip().splitlines()[-1:],
            'heldout_summary': (heldout.get('output') or '').strip().splitlines()[-1:],
            'unmapped': {'visible': unmapped, 'heldout': hunmapped}}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('instance_id')
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--max-cases', type=int, default=6)
    parser.add_argument('--tolerance', type=int, default=3,
                        help='lines either side of the defect that still count as a hit')
    parser.add_argument('--include-control', action='store_true',
                        help='also emit the unmutated patch as a false-positive control')
    parser.add_argument('--include-loud', action='store_true',
                        help='keep mutants the visible suite already catches (default: only subtle)')
    args = parser.parse_args()

    row = fetch.load(args.instance_id, args.cache)
    repo_dir = Path(args.root) / 'tasks' / args.instance_id / 'repo'
    if not repo_dir.is_dir():
        raise SystemExit(f'Run setup.py first: {repo_dir}')
    out_root = Path(args.root) / 'review' / args.instance_id
    scratch, logs = out_root / 'scratch', out_root / 'logs'
    scratch.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    cases, rejected = [], 0
    if args.include_control:
        control_dir = out_root / 'c0-control'
        (control_dir / 'hidden').mkdir(parents=True, exist_ok=True)
        (control_dir / 'candidate.patch').write_text(row['patch'])
        (control_dir / 'goal.md').write_text(
            '# Review this candidate patch\n\nThe patch below was submitted as the fix for the\n'
            'issue. Report every defect you can defend. If the patch is sound, report nothing: a\n'
            'finding that names the wrong place costs the same as one that names nothing. Write\n'
            '`findings.json` as a list of objects with `path`, `line`, `kind`, `severity`, `why`.\n\n'
            '## Reported issue\n\n' + row['problem_statement'].strip() + '\n\n'
            '## Candidate patch\n\n```diff\n' + row['patch'].strip() + '\n```\n')
        (control_dir / 'hidden' / 'ground_truth.json').write_text(
            json.dumps({'control': True, 'note': 'the unmutated patch: any finding is a false positive'},
                       indent=2))
        (control_dir / 'hidden' / 'score.py').write_text(SCORER.format(tolerance=args.tolerance))
        (control_dir / 'meta.json').write_text(json.dumps(
            {'instance': args.instance_id, 'case': 'c0-control', 'control': True}, indent=2))
        cases.append('c0-control')
        print(json.dumps({'case': 'c0-control', 'control': True}))
    for number, mutant in enumerate(mutants(row['patch']), 1):
        if len([c for c in cases if c != 'c0-control']) >= args.max_cases:
            break
        measured = measure(repo_dir, row, mutant['patch'], scratch, logs)
        mutant['measured'] = measured
        subtle = measured['visible_exit'] == 0 and measured['heldout_exit'] not in (0, None)
        mutant['subtle'] = subtle
        if measured['heldout_exit'] in (0, None) or (not subtle and not args.include_loud):
            rejected += 1                      # the held-out suite misses it, or visible already
            continue                           # catches it: not a defect worth a reviewer
        case_id = f"m{number}-{SLUG.get(mutant['kind'], re.sub(r'[^a-z0-9]+', '', mutant['kind'].lower()))}"
        case_dir = out_root / case_id
        (case_dir / 'hidden').mkdir(parents=True, exist_ok=True)
        (case_dir / 'candidate.patch').write_text(mutant['patch'])
        (case_dir / 'goal.md').write_text(
            '# Review this candidate patch\n\n'
            'The patch below was submitted as the fix for the issue. Nothing has verified it.\n'
            'Report every defect you can defend: a wrong result, a broken edge case, a change\n'
            'that cannot do what it claims. Write your findings to `findings.json` as a list of\n'
            'objects with `path` (repository-relative), `line` (in the patched file), `kind`\n'
            '(short label), `severity` (P0-P3) and `why` (the reasoning, one or two sentences).\n'
            'A finding that names the wrong place costs the same as one that names nothing.\n\n'
            '## Reported issue\n\n' + row['problem_statement'].strip() + '\n\n'
            '## Candidate patch\n\n```diff\n' + mutant['patch'].strip() + '\n```\n')
        (case_dir / 'hidden' / 'ground_truth.json').write_text(json.dumps(
            {'path': mutant['path'], 'line': mutant['line'], 'kind': mutant['kind'],
             'before': mutant['before'], 'after': mutant['after'], 'control': False}, indent=2))
        (case_dir / 'hidden' / 'score.py').write_text(SCORER.format(tolerance=args.tolerance))
        (case_dir / 'meta.json').write_text(json.dumps(
            {'instance': args.instance_id, 'case': case_id, 'defect': {k: mutant[k] for k in
             ('path', 'line', 'kind', 'before', 'after')}, 'measured': measured,
             'subtle': subtle}, indent=2))
        cases.append(case_id)
        print(json.dumps({'case': case_id, 'defect': f"{mutant['path']}:{mutant['line']}",
                          'kind': mutant['kind'], 'visible_exit': measured['visible_exit'],
                          'heldout_exit': measured['heldout_exit'], 'subtle': subtle}))
    print(json.dumps({'instance': args.instance_id, 'cases': len(cases),
                      'rejected_mutants': rejected, 'out': str(out_root)}))


if __name__ == '__main__':
    main()
