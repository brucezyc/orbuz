"""The planner as a role: it runs in the same loop, and its plan is judged instead of trusted.

Real Git worktrees and the real sandbox; the model is scripted, so these test the protocol
(who decides what, what is recorded) rather than anyone's planning ability.
"""
import json
import subprocess

import pytest

from orbuz.runtime.engine import PLAN_FILE, Runtime


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class Scripted:
    """Replays scripted (tool, args) pairs."""

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
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def contract(project, **overrides):
    spec = dict(goal='Make answer(n) return 42*n', repository=str(project),
                writable=['answer.py', 'other.py'], context=['answer.py', 'check.py'],
                acceptance=['/usr/bin/python3', '-B', 'check.py'],
                max_calls=6, max_output_tokens=512, timeout=5,
                plan='auto', slices={'impl': ['answer.py'], 'extra': ['other.py']})
    spec.update(overrides)
    return spec


TWO_ITEMS = [
    {'id': 'fix-answer', 'objective': 'make answer return 42*n', 'writable': ['answer.py'],
     'acceptance': ['/usr/bin/python3', '-B', 'check.py'], 'slices': ['impl']},
    {'id': 'set-other', 'objective': 'set OTHER to 2', 'writable': ['other.py'],
     'acceptance': ['/usr/bin/python3', '-B', 'check.py'], 'slices': ['extra']},
]


def plan_actions(items):
    return [('write_file', {'path': PLAN_FILE, 'content': json.dumps({'items': items})}),
            ('submit', {})]


def test_planner_plan_is_judged_and_dispatch_builds_children(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.plan(task, Scripted(plan_actions(TWO_ITEMS)))
    assert result['status'] == 'judged' and result['planner_status'] == 'accepted'
    assert result['items'] == 2 and result['dispatch']['mode'] == 'parallel'
    assert rt.store.load(task)['planner_task'] == result['planner_task']

    dispatched = rt.dispatch(task)
    assert dispatched['mode'] == 'parallel' and len(dispatched['children']) == 2
    parent, child = rt.store.load(task), rt.store.load(dispatched['children'][0])
    assert child['contract']['writable'] == ['answer.py']
    assert child['contract']['acceptance'] == ['/usr/bin/python3', '-B', 'check.py']
    assert child['contract']['base_revision'] == parent['contract']['base_revision']
    assert 'heldout' not in child['contract']        # it judges the merged result, not one item
    assert 'plan' not in child['contract'] and 'slices' not in child['contract']


def test_same_evidence_plan_is_sequential_and_parallel_dispatch_is_refused(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    six = [dict(item, id=f'reviewer-{n}', writable=['answer.py'], slices=['impl'],
                objective='find the defect in the patch')
           for n, item in enumerate([TWO_ITEMS[0]] * 6)]
    result = rt.plan(task, Scripted(plan_actions(six)))
    assert result['dispatch']['mode'] == 'sequential'
    assert 'duplicate item reviewer-' in ' '.join(result['dispatch']['reasons'])
    with pytest.raises(ValueError, match='judged sequential'):
        rt.dispatch(task, mode='parallel')
    ordered = rt.dispatch(task, mode='sequential')
    assert len(ordered['children']) == 6 and ordered['verdict'] == 'sequential'


def test_planner_items_outside_the_task_scope_fail_loud_and_are_recorded(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    sneaky = [dict(TWO_ITEMS[0], writable=['check.py'])]
    with pytest.raises(RuntimeError, match='unusable plan'):
        rt.plan(task, Scripted(plan_actions(sneaky)))
    recorded = rt.store.load(task)['plan']
    assert recorded['status'] == 'planner_invalid' and 'outside the task scope' in recorded['reason']


def test_planner_that_never_writes_a_plan_fails_without_a_fallback(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    wandered = Scripted([('read_file', {'path': 'answer.py'})] * 10)
    with pytest.raises(RuntimeError, match='no plan'):
        rt.plan(task, wandered, max_calls=3)
    assert rt.store.load(task)['plan']['status'] == 'planner_failed'


def test_plan_verb_refuses_a_task_that_never_asked_for_one(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    spec = contract(project)
    spec.pop('plan')
    spec.pop('slices')
    task = rt.create(spec)
    with pytest.raises(ValueError, match='does not ask for a plan'):
        rt.plan(task, Scripted(plan_actions(TWO_ITEMS)))


def test_dispatch_without_a_plan_is_refused(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    with pytest.raises(ValueError, match='No judged plan'):
        rt.dispatch(task)
