"""The plan judge: which work splits may run in parallel, and why the rest may not.

Contract-level tests only (a Git fixture, no sandbox, no model). The point of the mechanism is
that independence is decided here instead of being asserted by the plan that wants it.
"""
import subprocess

import pytest

from orbuz.runtime import contract as contract_mod
from orbuz.runtime import plan as plan_mod


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


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
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_answer.py').write_text('def test_answer():\n    assert False\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def base(project, **overrides):
    spec = dict(goal='Make answer(n) return 42*n', repository=str(project),
                writable=['answer.py', 'other.py'], context=['answer.py', 'check.py'],
                acceptance=['/usr/bin/python3', '-B', 'check.py'],
                max_calls=8, max_output_tokens=512, timeout=5)
    spec.update(overrides)
    return spec


SLICES = {'diff': ['answer.py'], 'tests': ['tests'], 'other': ['other.py']}


def item(ident, writable, acceptance, slices, objective=None):
    return {'id': ident, 'objective': objective or f'objective for {ident}',
            'writable': writable, 'acceptance': acceptance, 'slices': slices}


# ---------- the split that may run in parallel ----------

def test_disjoint_work_with_distinct_evidence_dispatches_parallel(project):
    spec = contract_mod.validate(base(project, slices=SLICES, plan=[
        item('fix-answer', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff']),
        item('update-other', ['other.py'], ['/usr/bin/python3', '-B', 'check.py'], ['other']),
    ]))
    assert spec['dispatch'] == {'mode': 'parallel', 'reasons': []}
    assert [i['id'] for i in spec['plan']] == ['fix-answer', 'update-other']


def test_no_plan_keeps_the_single_agent_shape(project):
    spec = contract_mod.validate(base(project))
    assert 'dispatch' not in spec and 'plan' not in spec
    assert plan_mod.describe(spec) == {'items': 0, 'dispatch': {'mode': 'single', 'reasons': []}}


# ---------- the split that must not ----------

def test_same_evidence_same_objective_six_times_is_sequential(project):
    """The regression this mechanism exists for: N agents over one input, bought N times."""
    plan = [item(f'reviewer-{n}', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff'],
                 objective='find the defect in the patch') for n in range(6)]
    spec = contract_mod.validate(base(project, slices=SLICES, plan=plan))
    assert spec['dispatch']['mode'] == 'sequential'
    reasons = ' | '.join(spec['dispatch']['reasons'])
    assert 'writable overlap on answer.py' in reasons
    assert reasons.count('duplicate item reviewer-') == 5
    assert 'same objective over the same evidence' in reasons


def test_acceptance_naming_another_items_output_is_sequential(project):
    spec = contract_mod.validate(base(project, slices=SLICES, plan=[
        item('first', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff']),
        item('second', ['other.py'], ['/usr/bin/python3', '-B', 'answer.py'], ['other']),
    ]))
    assert spec['dispatch']['mode'] == 'sequential'
    assert "acceptance of second names first's output answer.py" in spec['dispatch']['reasons'][0]


def test_duplicate_ids_are_rejected(project):
    with pytest.raises(ValueError, match='unique'):
        contract_mod.validate(base(project, slices=SLICES, plan=[
            item('same', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff']),
            item('same', ['other.py'], ['/usr/bin/python3', '-B', 'check.py'], ['other']),
        ]))


# ---------- malformed plans fail loud ----------

def test_item_writing_outside_the_task_scope_is_rejected(project):
    with pytest.raises(ValueError, match='outside the task scope'):
        contract_mod.validate(base(project, slices=SLICES, plan=[
            item('sneak', ['check.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff']),
        ]))


def test_unknown_slice_is_rejected(project):
    with pytest.raises(ValueError, match='Unknown slice'):
        contract_mod.validate(base(project, slices=SLICES, plan=[
            item('one', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['nowhere']),
        ]))


def test_slice_path_that_does_not_exist_is_rejected(project):
    with pytest.raises(ValueError, match='Slice path does not exist'):
        contract_mod.validate(base(project, slices={'diff': ['missing.py']}, plan=[
            item('one', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff']),
        ]))


def test_plan_and_slices_require_each_other(project):
    with pytest.raises(ValueError, match='plan requires slices'):
        contract_mod.validate(base(project, plan=[
            item('one', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff'])]))
    with pytest.raises(ValueError, match='slices without a plan'):
        contract_mod.validate(base(project, slices=SLICES))


def test_item_needs_its_own_acceptance(project):
    with pytest.raises(ValueError, match='acceptance argv'):
        contract_mod.validate(base(project, slices=SLICES, plan=[
            item('one', ['answer.py'], [], ['diff'])]))


# ---------- the plan is part of the evidence, so it is part of the hash ----------

def test_plan_changes_the_contract_digest(project):
    one = contract_mod.validate(base(project, slices=SLICES, plan=[
        item('fix-answer', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'], ['diff'])]))
    two = contract_mod.validate(base(project, slices=SLICES, plan=[
        item('fix-answer', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'],
             ['diff', 'tests'])]))
    assert contract_mod.digest(one) != contract_mod.digest(two)
    assert contract_mod.digest(one) == contract_mod.digest(
        contract_mod.validate(base(project, slices=SLICES, plan=[
            item('fix-answer', ['answer.py'], ['/usr/bin/python3', '-B', 'check.py'],
                 ['diff'])])))
