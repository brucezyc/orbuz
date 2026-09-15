"""Failure/recovery regressions against real source and subprocesses."""
import json
import subprocess
from pathlib import Path

import pytest

from test_evidence_runtime import Scripted, contract, git, project
from orbuz.runtime.engine import Runtime


def test_failed_attempt_can_retry_with_cumulative_budget(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    assert rt.run(task, Scripted([('submit', {})]))['status'] == 'rejected'
    result = rt.run(task, Scripted([
        ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
        ('submit', {})]), retry=True)
    assert result['status'] == 'accepted'
    assert result['calls'] == 3
    assert [a['status'] for a in result['attempts']] == ['rejected', 'accepted']
    assert result['attempts'][0]['evidence']['exit_code'] != 0


def test_running_record_reconciles_without_replay(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    data = rt.status(task)
    data['status'] = 'running'
    data['calls'] = 1
    data['attempts'] = [{'status': 'running', 'workspace': 'interrupted fixture'}]
    rt.store.save(data)
    model = Scripted([])
    result = Runtime(tmp_path / 'state').run(task, model)
    assert result['status'] == 'interrupted'
    assert model.calls == 0


def test_context_is_bounded_and_keeps_failure_diagnostics(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([('submit', {})]))
    result['attempts'].append({'workspace': result['workspace'], 'status': 'running'})
    brief = json.loads(rt.context(result)[1]['content'])
    # The brief holds only what never changes mid-task (the cached prefix); earlier-attempt
    # diagnostics ride in the trailing situation note so a resume cannot bust that prefix.
    situation = json.loads(rt.situation(result)['content'].removeprefix('Situation: '))
    assert 'AssertionError' in situation['prior_attempts'][0]['evidence']['output']
    assert 'prior_attempts' not in brief and 'remaining_calls' not in brief
    assert len(json.dumps(situation)) < 100000


def test_real_process_crash_reconciles_without_replay(project, tmp_path):
    import sys
    import time
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    marker = tmp_path / 'request-started'
    script = '''import sys,time
from pathlib import Path
from orbuz.runtime.engine import Runtime
class Waiting:
    def complete(self, *args):
        Path(sys.argv[3]).write_text('reserved')
        time.sleep(30)
Runtime(sys.argv[1]).run(sys.argv[2], Waiting())
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path / 'state'), task, str(marker)])
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        with pytest.raises(ValueError, match='already running'):
            rt.run(task, Scripted([]))
        proc.kill()
        proc.wait(timeout=5)
        result = rt.run(task, Scripted([]))
        assert result['status'] == 'interrupted'
        assert result['calls'] == 1
        assert result['elapsed_s'] > 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_full_logs_are_retrievable_but_other_tasks_are_not(project, tmp_path):
    from orbuz.runtime.tools import dispatch
    taskroot = tmp_path / 'task'
    current = taskroot / 'current'
    old = taskroot / 'previous'
    current.mkdir(parents=True)
    old.mkdir()
    logfile = old / 'acceptance.log'
    logfile.write_text('BEGIN' + 'x' * 10000 + 'END')
    result = dispatch('read_log', {'path': str(logfile), 'offset': 0, 'limit': 5},
                      project, contract(project), current / 'tool.log', lambda: False, 10)
    assert result['content'] == 'BEGIN'
    with pytest.raises(ValueError, match='outside this task'):
        dispatch('read_log', {'path': '/root/.hermes/.env'}, project,
                 contract(project), current / 'tool.log', lambda: False, 10)
    with pytest.raises(ValueError, match='outside this task'):
        dispatch('read_log', {'path': str(tmp_path / 'other' / 'acceptance.log')}, project,
                 contract(project), current / 'tool.log', lambda: False, 10)


def test_read_symlink_escape_denied(project, tmp_path):
    (project / 'escape').symlink_to('/root/.hermes/.env')
    git(project, 'add', 'escape')
    git(project, 'commit', '-qm', 'symlink fixture')
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([('read_file', {'path': 'escape'}), ('blocked', {'reason': 'stop'})]))
    transcript = json.loads((Path(result['workspace']).parent / 'transcript.json').read_text())
    outputs = [m['content'] for m in transcript if m['role'] == 'tool']
    assert 'escapes workspace' in outputs[0]


def test_text_pass_does_not_accept(project, tmp_path):
    class Text:
        def complete(self, *args):
            return {'message': {'role': 'assistant', 'content': 'PASS: everything done'}}
    rt = Runtime(tmp_path / 'state')
    spec = contract(project)
    spec['max_calls'] = 1
    task = rt.create(spec)
    assert rt.run(task, Text())['status'] == 'exhausted'


def test_logs_and_contract_tamper_invalidate(project, tmp_path):
    for tamper in ('log', 'contract'):
        rt = Runtime(tmp_path / tamper)
        task = rt.create(contract(project))
        result = rt.run(task, Scripted([
            ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
            ('submit', {})]))
        if tamper == 'log':
            Path(result['evidence']['log_path']).write_text('invented')
        else:
            result['contract']['goal'] = 'changed goal'
            rt.store.save(result)
        assert rt.verify(task)['status'] == 'stale'


def test_cancel_during_model_response_prevents_submit(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    class Cancelling(Scripted):
        def complete(self, messages, tools, max_tokens):
            rt.cancel(task)
            return super().complete(messages, tools, max_tokens)
    result = rt.run(task, Cancelling([('submit', {})]))
    assert result['status'] == 'cancelled'
    assert 'evidence' not in result


def test_deleted_workspace_invalidates_without_exception(project, tmp_path):
    import shutil
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([
        ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
        ('submit', {})]))
    shutil.rmtree(result['workspace'])
    assert rt.verify(task)['status'] == 'stale'


def test_ignored_candidate_file_is_versioned(project, tmp_path):
    (project / '.gitignore').write_text('new.py\n')
    git(project, 'add', '.gitignore')
    git(project, 'commit', '-qm', 'ignore fixture')
    rt = Runtime(tmp_path / 'state')
    spec = contract(project)
    spec['writable'].append('new.py')
    result = rt.run(rt.create(spec), Scripted([
        ('write_file', {'path': 'new.py', 'content': 'VALUE = 42\n'}),
        ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
        ('submit', {})]))
    assert result['status'] == 'accepted'
    assert 'new.py' in git(Path(result['workspace']), 'ls-files').splitlines()


def test_ignored_injected_file_invalidates_evidence(project, tmp_path):
    (project / '.gitignore').write_text('injected.py\n')
    git(project, 'add', '.gitignore')
    git(project, 'commit', '-qm', 'ignore fixture')
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    result = rt.run(task, Scripted([
        ('write_file', {'path': 'answer.py', 'content': 'def answer():\n    return 42\n'}),
        ('submit', {})]))
    assert result['status'] == 'accepted'
    (Path(result['workspace']) / 'injected.py').write_text('changed environment')
    assert rt.verify(task)['status'] == 'stale'


def test_cli_and_missing_key_fail_closed(project, tmp_path):
    import os
    import sys
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(contract(project)))
    cmd = [sys.executable, '-m', 'orbuz.runtime', '--state-dir', str(tmp_path / 'state')]
    p = subprocess.run(cmd + ['create', str(path)], cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    task = json.loads(p.stdout)['id']
    p = subprocess.run(cmd + ['run', task, '--model', 'unused', '--base-url', 'https://example.invalid',
                             '--key-env', 'NONEXISTENT_ORBUZ_TEST_KEY'],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 2
    assert 'Missing credential' in p.stdout
    assert Runtime(tmp_path / 'state').status(task)['calls'] == 0
