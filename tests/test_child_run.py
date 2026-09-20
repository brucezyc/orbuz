"""Running a dispatched split, merging it, and re-deriving each child's verdict.

Real Git worktrees and the real sandbox; models are scripted, so what is under test is the
protocol - who gets judged by what, and what a claim costs when it cannot be reproduced.
"""
import json
import subprocess

import pytest

from orbuz.runtime.engine import PLAN_FILE, Runtime

GOOD = 'def answer(n=1):\n    return 42 * n\n'
HARDCODED = 'def answer(n=1):\n    return 42 if n == 1 else 0\n'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class Scripted:
    def __init__(self, actions):
        self.actions = list(actions)
        self.index = 0
        self.calls = 0

    def complete(self, messages, tools, max_tokens):
        self.calls += 1
        action, args = self.actions[min(self.index, len(self.actions) - 1)]
        self.index += 1
        return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': str(self.calls), 'type': 'function',
                     'function': {'name': action, 'arguments': json.dumps(args)}}]},
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'Test')
    (root / 'answer.py').write_text('def answer(n=1):\n    return 0\n')
    (root / 'other.py').write_text('OTHER = 1\n')
    (root / 'check.py').write_text('from answer import answer\nassert answer(1) == 42\n')
    (root / 'check_other.py').write_text('from other import OTHER\nassert OTHER == 2\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def hidden(tmp_path, body):
    directory = tmp_path / 'heldout'
    directory.mkdir(exist_ok=True)
    (directory / 'check_all.py').write_text(
        'import sys\nsys.path.insert(0, "/workspace")\nfrom answer import answer\n' + body)
    return directory


def parent_contract(project, **overrides):
    spec = dict(goal='Make answer(n) return 42*n and OTHER equal 2', repository=str(project),
                writable=['answer.py', 'other.py'], context=['answer.py', 'other.py'],
                acceptance=['/usr/bin/python3', '-B', 'check.py'],
                max_calls=6, max_output_tokens=512, timeout=5,
                plan='auto', slices={'impl': ['answer.py'], 'extra': ['other.py']})
    spec.update(overrides)
    return spec


ITEMS = [
    {'id': 'fix-answer', 'objective': 'make answer return 42*n', 'writable': ['answer.py'],
     'acceptance': ['/usr/bin/python3', '-B', 'check.py'], 'slices': ['impl']},
    {'id': 'set-other', 'objective': 'set OTHER to 2', 'writable': ['other.py'],
     'acceptance': ['/usr/bin/python3', '-B', 'check_other.py'], 'slices': ['extra']},
]


def dispatched(rt, project, items=None, **overrides):
    """Parent task with a judged plan and its children created. Returns (task, children)."""
    task = rt.create(parent_contract(project, **overrides))
    rt.plan(task, Scripted([('write_file', {'path': PLAN_FILE,
                                            'content': json.dumps({'items': items or ITEMS})}),
                            ('submit', {})]))
    return task, rt.dispatch(task)['children']


def good_child(patch_path, content, acceptance):
    return Scripted([('write_file', {'path': patch_path, 'content': content}), ('submit', {})])


def test_children_run_apart_and_the_union_is_what_gets_judged(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task, children = dispatched(rt, project)
    scripts = {children[0]: good_child('answer.py', GOOD, 'check.py'),
               children[1]: good_child('other.py', 'OTHER = 2\n', 'check_other.py')}
    result = rt.run_children(task, lambda child: scripts[child], concurrency=2)

    assert result['children'] == {children[0]: 'accepted', children[1]: 'accepted'}
    assert result['merge']['status'] == 'accepted'
    assert sorted(result['merge']['applied']) == sorted(children)
    parent = rt.store.load(task)
    assert parent['evidence']['applied_children'] and not parent['evidence']['missing_children']
    assert parent['evidence']['plan_digest'] == rt.store.load(task)['plan']['digest']


def test_a_child_that_did_not_finish_blocks_acceptance(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task, children = dispatched(rt, project)
    scripts = {children[0]: good_child('answer.py', GOOD, 'check.py'),
               # writes nothing it was asked for, then claims to be done
               children[1]: Scripted([('write_file', {'path': 'other.py', 'content': 'OTHER = 1\n'}),
                                      ('submit', {})])}
    result = rt.run_children(task, lambda child: scripts[child], concurrency=2)

    assert result['merge']['status'] != 'accepted'
    assert [item['child'] for item in result['merge']['missing']] == [children[1]]
    assert 'did not finish' in rt.store.load(task)['error']


def test_reproduction_catches_a_child_that_only_satisfied_its_own_suite(project, tmp_path):
    """The child's acceptance passes; the parent's hidden suite disagrees. That is not verified."""
    rt = Runtime(tmp_path / 'state')
    task, children = dispatched(rt, project, heldout=['/usr/bin/python3', '-B', '/heldout/check_all.py'],
                                heldout_assets=[str(hidden(tmp_path, 'assert answer(0) == 0\n'))])
    scripts = {children[0]: good_child('answer.py', HARDCODED, 'check.py'),
               children[1]: good_child('other.py', 'OTHER = 2\n', 'check_other.py')}
    rt.run_children(task, lambda child: scripts[child], concurrency=2)

    report = rt.verify_children(task)
    assert report['children'][children[0]]['status'] == 'hacking_suspected'
    assert report['children'][children[1]]['status'] == 'verified'
    assert report['unverified'] == [] and report['verifier_count'] == 0


def test_unverified_children_get_exactly_one_verifier_each(project, tmp_path):
    """No held-out suite means reproduction cannot settle the claim, so a verifier is spawned."""
    rt = Runtime(tmp_path / 'state')
    task, children = dispatched(rt, project)
    scripts = {children[0]: good_child('answer.py', GOOD, 'check.py'),
               children[1]: good_child('other.py', 'OTHER = 2\n', 'check_other.py')}
    rt.run_children(task, lambda child: scripts[child], concurrency=2)

    report = rt.verify_children(task)
    assert report['unverified'] == children and report['verifier_count'] == 2
    assert 'cannot settle' in report['children'][children[0]]['why']

    verdicts = Scripted([('write_file', {'path': 'verdict.json',
                                         'content': json.dumps({'reproduced': True, 'why': 'ran it'})}),
                         ('submit', {})])
    ran = rt.verify_children(task, lambda child: verdicts)
    assert ran['verifier_count'] == 2
    assert all(v['says'] for v in ran['verifiers'] if v.get('ran'))
    assert rt.store.load(task)['verification']['unverified'] == children
