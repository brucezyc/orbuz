"""Turn a SWE-bench instance into review cases with a hidden ground truth.

A single-issue instance is not a review task: the official fix has nothing to find. Mutate the
fix instead - flip one operator, shift one constant - and you get a patch that still parses and
still passes the tests the project runs anyway, while being wrong in exactly one place. That
place is the ground truth; the PR's own tests are what prove it matters; the reviewer sees
neither.

Mutations are applied to the *tree*, never to the patch text. A patch whose context lines no
longer match the file cannot be applied at all, and a defect the reviewer is meant to find has
to be a real difference in the submitted code - so: apply the fix, commit it, edit one line, and
let git write the candidate patch against the base commit.

Every mutant is measured offline in the sandbox before it becomes a case:

    visible  (PASS_TO_PASS)                        the tests the project runs anyway
    held-out (FAIL_TO_PASS + the PR's test patch)  what proves the fix is broken

Output per case (only candidate.patch and goal.md are the reviewer's to read):

    cases/<instance>/<case>/candidate.patch           what the reviewer sees
    cases/<instance>/<case>/goal.md                   the issue plus the patch
    cases/<instance>/<case>/hidden/ground_truth.json  the defect
    cases/<instance>/<case>/hidden/score.py           deterministic scorer for findings.json
    cases/<instance>/<case>/meta.json                 measured exits
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'swebench'))

import fetch                      # noqa: E402
import repo as repo_mod           # noqa: E402
import runner                     # noqa: E402

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
COMPARISONS = [('==', '!='), ('!=', '=='), ('<=', '<'), ('>=', '>'), ('<', '<='), ('>', '>=')]
WORDS = [('True', 'False'), ('False', 'True'), (' and ', ' or '), (' or ', ' and '),
         ('is not None', 'is None'), ('is None', 'is not None'),
         ('startswith', 'endswith'), ('endswith', 'startswith'),
         ('min(', 'max('), ('max(', 'min('),
         (' + 1', ' - 1'), (' - 1', ' + 1'), ('* -1', '* 1')]
NUMBER = re.compile(r'(?<![A-Za-z_.\d])(\d+)(?![A-Za-z_.\d])')
SLUG = {'==->!=': 'ne', '!=->==': 'eq', '<=-><': 'lt', '>=->>': 'gt', '<-><=': 'le', '>->>=': 'ge',
        'True->False': 'false', 'False->True': 'true'}

SCORER = '''"""Score a review against the hidden defect. Deterministic, no model calls.

A finding is a hit when it names the mutated file and lands within {tolerance} lines of the
mutated line: reviewers point at ranges, and demanding an exact line would measure formatting
luck rather than detection. Everything else - and every finding on a control case - is a false
positive, because on a control there is nothing to find.
"""
import json
import sys


def main():
    findings_path, truth_path = sys.argv[1], sys.argv[2]
    truth = json.load(open(truth_path))
    try:
        findings = json.load(open(findings_path))
    except Exception as exc:
        print('unreadable findings: ' + str(exc)[:200])
        raise SystemExit(2)
    if isinstance(findings, dict):
        findings = findings.get('findings') or []
    findings = findings if isinstance(findings, list) else []
    control = bool(truth.get('control'))
    hits = 0
    for finding in findings:
        if not isinstance(finding, dict) or control:
            continue
        path = str(finding.get('path') or '').lstrip('./')
        line = finding.get('line')
        if path.endswith(truth['path']) and isinstance(line, int) \\
                and abs(line - truth['line']) <= {tolerance}:
            hits += 1
    print(json.dumps({{'findings': len(findings), 'hits': hits, 'control': control,
                       'duplicates': max(0, hits - 1)}}))
    if control:
        raise SystemExit(0 if not findings else 1)
    raise SystemExit(0 if hits else 1)


main()
'''


def hunk_lines(patch_text):
    """Every line inside the fix's hunks, with the file it lands in and its patched line number."""
    file, new_line = None, 0
    found = []
    for line in patch_text.splitlines():
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
            found.append({'file': file, 'line': new_line, 'text': line[1:], 'origin': 'added'})
            new_line += 1
        elif line.startswith(' '):
            found.append({'file': file, 'line': new_line, 'text': line[1:], 'origin': 'context'})
            new_line += 1
    return found


def mutations_of(entry):
    """One-token mutations of one line: a defect a reader could find, not a rewrite."""
    out = []
    for old, new in COMPARISONS + WORDS:
        if old in entry['text']:
            out.append((old, new))
    for number in {n for n in NUMBER.findall(entry['text'])}:
        out.append((number, str(int(number) + 1)))
    return out


def submitted_tree(repo_dir, patch_text, destination):
    """A clean copy holding the fix as a commit, so any later line edit diffs against the base."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(repo_dir, destination, symlinks=True)
    reset(destination)
    runner.apply_patch(destination, patch_text, 'gold')
    subprocess.run(['git', '-C', str(destination), '-c', 'user.email=case@example.invalid',
                    '-c', 'user.name=Case', 'commit', '-qam', 'submitted fix'], check=True)
    return destination


def reset(tree):
    """Back to the submitted fix: the mutation and any test patch are undone, nothing else."""
    subprocess.run(['git', '-C', str(tree), 'reset', '-q', '--hard', 'HEAD'], check=True)
    subprocess.run(['git', '-C', str(tree), 'clean', '-qfd'], check=True)


def write_line(tree, entry, mutated):
    path = Path(tree) / entry['file']
    lines = path.read_text().splitlines(keepends=True)
    lines[entry['line'] - 1] = mutated + ('\n' if lines[entry['line'] - 1].endswith('\n') else '')
    path.write_text(''.join(lines))


def candidate_patch(tree):
    """The submitted fix as it now stands: git diff against the commit before it."""
    return subprocess.run(['git', '-C', str(tree), 'diff', 'HEAD~1'],
                          capture_output=True, text=True, check=True).stdout


def measure(tree, row, logs):
    """Both suites against the mutant tree: the project's own, and the PR's held-out tests."""
    files = repo_mod.test_files(row['eval_script'])
    qualified, mapped, unmapped = runner.node_map(tree, files, fetch.as_list(row['PASS_TO_PASS']))
    visible = runner.sandbox_run(tree, [*qualified, *mapped], logs / 'visible.log')
    runner.apply_patch(tree, row['test_patch'], 'test_patch')
    files_after = repo_mod.test_files(row['eval_script'])
    hq, hm, hunmapped = runner.node_map(tree, files_after, fetch.as_list(row['FAIL_TO_PASS']))
    heldout = runner.sandbox_run(tree, [*hq, *hm], logs / 'heldout.log')
    reset(tree)
    return {'visible_exit': visible.get('exit_code'), 'heldout_exit': heldout.get('exit_code'),
            'visible_summary': (visible.get('output') or '').strip().splitlines()[-1:],
            'heldout_summary': (heldout.get('output') or '').strip().splitlines()[-1:],
            'unmapped': {'visible': unmapped, 'heldout': hunmapped}}


def write_case(out_root, case_id, patch_text, goal, meta, tolerant):
    case_dir = out_root / case_id
    (case_dir / 'hidden').mkdir(parents=True, exist_ok=True)
    (case_dir / 'candidate.patch').write_text(patch_text)
    (case_dir / 'goal.md').write_text(goal)
    (case_dir / 'hidden' / 'ground_truth.json').write_text(json.dumps(meta['truth'], indent=2))
    (case_dir / 'hidden' / 'score.py').write_text(SCORER.format(tolerance=tolerant))
    (case_dir / 'meta.json').write_text(json.dumps(meta, indent=2))
    return case_dir


REVIEW_ASK = ('# Review this candidate patch\n\n'
              'The patch below was submitted as the fix for the issue. Nothing has verified it.\n'
              'Report every defect you can defend: a wrong result, a broken edge case, a change\n'
              'that cannot do what it claims. Write your findings to `findings.json` as a list of\n'
              'objects with `path` (repository-relative), `line` (in the patched file), `kind`\n'
              '(short label), `severity` (P0-P3) and `why` (the reasoning, one or two sentences).\n'
              'A finding that names the wrong place costs the same as one that names nothing.\n'
              'If the patch is sound, report nothing.\n\n'
              '## Reported issue\n\n{issue}\n\n## Candidate patch\n\n```diff\n{patch}\n```\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('instance_id', nargs='?')
    parser.add_argument('--root', default='/root/bench')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--max-cases', type=int, default=5)
    parser.add_argument('--tolerance', type=int, default=3,
                        help='lines either side of the defect that still count as a hit')
    parser.add_argument('--subtle-only', action='store_true',
                        help='keep only mutants the visible suite misses (the interesting kind)')
    parser.add_argument('--include-control', action='store_true',
                        help='also emit the unmutated fix as a false-positive control')
    args = parser.parse_args()
    if not args.instance_id:
        raise SystemExit('Usage: make_cases.py <instance_id> [--max-cases N]')

    row = fetch.load(args.instance_id, args.cache)
    repo_dir = Path(args.root) / 'tasks' / args.instance_id / 'repo'
    if not repo_dir.is_dir():
        raise SystemExit(f'Run setup.py first: {repo_dir}')
    out_root = Path(args.root) / 'review' / args.instance_id
    logs = out_root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    tree = submitted_tree(repo_dir, row['patch'], out_root / 'tree')

    cases, rejected = [], 0
    if args.include_control:
        goal = REVIEW_ASK.format(issue=row['problem_statement'].strip(), patch=row['patch'].strip())
        write_case(out_root, 'c0-control', row['patch'], goal,
                   {'instance': args.instance_id, 'case': 'c0-control', 'control': True,
                    'truth': {'control': True,
                              'note': 'the unmutated fix: any finding is a false positive'}},
                   args.tolerance)
        cases.append('c0-control')
        print(json.dumps({'case': 'c0-control', 'control': True}), flush=True)

    entries = hunk_lines(row['patch'])
    print(json.dumps({'instance': args.instance_id, 'mutatable_lines': len(entries),
                      'hunks': sorted({e['file'] for e in entries})}), flush=True)
    for number, entry in enumerate(entries, 1):
        if len([c for c in cases if c != 'c0-control']) >= args.max_cases:
            break
        for old, new in mutations_of(entry):
            mutated = entry['text'].replace(old, new, 1)
            if mutated == entry['text'] or not mutated.strip():
                continue
            write_line(tree, entry, mutated)
            patch_text = candidate_patch(tree)
            if not patch_text.strip():
                reset(tree)
                continue
            measured = measure(tree, row, logs)
            subtle = measured['visible_exit'] == 0 and measured['heldout_exit'] not in (0, None)
            if measured['heldout_exit'] in (0, None) or (args.subtle_only and not subtle):
                rejected += 1          # the held-out suite does not react: not a defect
                continue
            kind = f'{old}->{new}'
            case_id = f'm{number}-{SLUG.get(kind) or re.sub(r"[^a-z0-9]+", "", kind.lower())}'
            if (out_root / case_id).exists():
                case_id += f'-{abs(hash(mutated)) % 1000}'
            goal = REVIEW_ASK.format(issue=row['problem_statement'].strip(),
                                     patch=patch_text.strip())
            write_case(out_root, case_id, patch_text, goal,
                       {'instance': args.instance_id, 'case': case_id,
                        'defect': {'path': entry['file'], 'line': entry['line'], 'kind': kind,
                                   'origin': entry['origin'], 'before': entry['text'].rstrip(),
                                   'after': mutated.rstrip()},
                        'measured': measured, 'subtle': subtle,
                        'truth': {'path': entry['file'], 'line': entry['line'], 'kind': kind,
                                  'control': False}},
                       args.tolerance)
            cases.append(case_id)
            print(json.dumps({'case': case_id, 'defect': f"{entry['file']}:{entry['line']}",
                              'kind': kind, 'origin': entry['origin'],
                              'visible_exit': measured['visible_exit'],
                              'heldout_exit': measured['heldout_exit'], 'subtle': subtle}),
                  flush=True)
            break                                     # one mutant per source line is enough
    print(json.dumps({'instance': args.instance_id,
                      'cases': len([c for c in cases if c != 'c0-control']),
                      'control': 'c0-control' in cases, 'rejected_mutants': rejected,
                      'out': str(out_root)}))


if __name__ == '__main__':
    main()
