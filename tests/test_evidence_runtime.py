"""Real Git/check execution; scripted models test protocol, not AI ability."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orbuz.runtime.engine import Runtime


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'Test')
    (root / 'answer.py').write_text('def answer():\n    return 0\n')
    (root / 'check.py').write_text('from answer import answer\nassert answer() == 42\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def contract(project) -> dict:
    return dict(goal='Make answer return 42', repository=str(project),
                writable=['answer.py'], context=['answer.py', 'check.py'],
                acceptance=['/usr/bin/python3', '-B', 'check.py'],
                max_calls=4, max_output_tokens=512, timeout=5)


class Scripted:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = 0

    def complete(self, messages, tools, max_tokens):
        self.calls += 1
        action, args = next(self.actions)
        return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': str(self.calls), 'type': 'function', 'function': {
                'name': action, 'arguments': json.dumps(args)}}]},
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}


def test_model_submit_is_not_success(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([('submit', {})]))
    assert result['status'] == 'rejected'
    assert result['evidence']['exit_code'] != 0
    assert Path(result['evidence']['log_path']).exists()
    assert git(project, 'status', '--porcelain') == ''


def test_acceptance_persists_and_tamper_invalidates(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([
        ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
        ('submit', {})]))
    assert result['status'] == 'accepted'
    assert result['evidence']['revision']
    assert Runtime(tmp_path / 'state').verify(task)['status'] == 'accepted'
    assert 'return 0' in (project / 'answer.py').read_text()
    (Path(result['workspace']) / 'answer.py').write_text('broken')
    assert Runtime(tmp_path / 'state').verify(task)['status'] == 'stale'


def test_scope_guard_and_budget(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    spec = contract(project)
    spec['max_calls'] = 2
    task = rt.create(spec)
    model = Scripted([('write_file', {'path': 'check.py', 'content': ''}),
                      ('write_file', {'path': '../escape', 'content': 'bad'})])
    result = rt.run(task, model)
    assert result['status'] == 'exhausted'
    assert (Path(result['workspace']) / 'check.py').read_text().startswith('from answer')
    assert not (Path(result['workspace']).parent / 'escape').exists()


def test_no_acceptance_or_bad_limits_rejected(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    for update in [{'acceptance': []}, {'max_calls': 0}, {'writable': ['../bad']},
            {'timeout': float('nan')}, {'context': ['.git/config']},
            {'context': ['answer.py'] * 129}]:
        with pytest.raises(ValueError):
            rt.create(dict(contract(project), **update))


def test_cancel_and_retry_are_explicit(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    rt.cancel(task)
    model = Scripted([])
    assert rt.run(task, model)['status'] == 'cancelled'
    assert model.calls == 0
    with pytest.raises(ValueError):
        rt.run(task, model, retry=True)


@pytest.mark.parametrize('entry', ['check.py', './check.py', '/workspace/check.py'])
def test_acceptance_entry_aliases_cannot_be_writable(project, tmp_path, entry):
    rt = Runtime(tmp_path / 'state')
    spec = contract(project)
    spec['writable'] = ['answer.py', 'check.py']
    spec['acceptance'] = ['/usr/bin/python3', '-B', entry]
    with pytest.raises(ValueError, match='Acceptance entry'):
        rt.create(spec)


def test_api_failure_never_mock_success(project, tmp_path):
    class Broken:
        def complete(self, *args):
            raise RuntimeError('offline')
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    assert rt.run(task, Broken())['status'] == 'failed'
