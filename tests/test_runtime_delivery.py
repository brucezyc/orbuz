"""Offline delivery regressions using real Git patches and sandbox acceptance."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from orbuz.runtime.contract import digest
from orbuz.runtime.engine import Runtime


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Delivery Test')
    git(root, 'config', 'user.email', 'delivery@example.invalid')
    (root / 'answer.txt').write_bytes(b'first\nold\n\n\n')
    (root / 'payload.bin').write_bytes(b'\x00\xffold\r\n')
    (root / 'delete.txt').write_bytes(b'delete me\n')
    (root / 'check.py').write_text('print("delivery fixture checked")\n')
    (root / '.gitignore').write_text('ignored.txt\n')
    for name in ['答案.txt', 'line\nbreak.txt', ' trailing \n']:
        (root / name).write_text('old\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    rt = Runtime(tmp_path / 'state')
    task_id = rt.create(dict(
        goal='Exercise delivery serialization, not model ability', repository=str(root),
        writable=['answer.txt', 'payload.bin', 'delete.txt', 'new.txt', 'ignored.txt',
                  '答案.txt', 'line\nbreak.txt', ' trailing \n'],
        context=[], acceptance=['/usr/bin/python3', '-B', 'check.py'], max_calls=3))
    return rt, task_id, root


def accept(delivery, changes):
    rt, task_id, root = delivery
    task = rt.status(task_id)
    attempt_dir = rt.store.root / task_id / 'delivery'
    attempt_dir.mkdir(parents=True)
    workspace = attempt_dir / 'source'
    git(root, 'worktree', 'add', '--detach', str(workspace), task['contract']['base_revision'])
    for name, content in changes.items():
        if content is None:
            (workspace / name).unlink()
        else:
            (workspace / name).write_bytes(content)
    task.update(workspace=str(workspace), status='running')
    rt.accept(task, attempt_dir, lambda: False, 10)
    rt.store.save(task)
    assert task['status'] == 'accepted'
    return task


def replay(delivery, task, tmp_path):
    _, _, root = delivery
    replay_root = tmp_path / 'replay'
    git(root, 'worktree', 'add', '--detach', str(replay_root), task['contract']['base_revision'])
    patch = Path(task['evidence']['patch_path'])
    # Empty tree changes are deliberately a zero-byte patch, applied as a no-op.
    empty = ['--allow-empty'] if not patch.read_bytes() else []
    git(replay_root, 'apply', *empty, '--check', str(patch))
    git(replay_root, 'apply', *empty, '--index', str(patch))
    assert git(replay_root, 'write-tree') == git(root, 'rev-parse', task['evidence']['revision'] + '^{tree}')


@pytest.mark.parametrize('changes', [
    {'answer.txt': b'first\nnew\n\n\n'},
    {'answer.txt': b'first\r\nnew\r\n\r\n'},
    {'payload.bin': b'\x00\xfeNEW\r\n\x80'},
    {'new.txt': b'new\n\n', 'delete.txt': None, 'ignored.txt': b'forced add\n'},
    {},
], ids=['trailing-blank-context', 'crlf', 'binary', 'add-delete-ignored', 'empty'])
def test_patch_replays_exact_candidate_tree(delivery, tmp_path, changes):
    task = accept(delivery, changes)
    replay(delivery, task, tmp_path)
    evidence = task['evidence']
    patch = Path(evidence['patch_path']).read_bytes()
    assert evidence['patch_hash'] == hashlib.sha256(patch).hexdigest()
    assert evidence['base_revision'] == task['contract']['base_revision']
    if not changes:
        assert patch == b''
    assert delivery[0].verify(task['id'])['status'] == 'accepted'


@pytest.mark.parametrize('name', ['答案.txt', 'line\nbreak.txt', ' trailing \n'])
@pytest.mark.parametrize('operation', ['modify', 'delete', 'new'])
def test_nul_paths_are_exact_and_replayable(delivery, tmp_path, name, operation):
    rt, task_id, root = delivery
    if operation == 'new':
        (root / name).unlink()
        git(root, 'add', '-u')
        git(root, 'commit', '-qm', 'remove before task')
        # Create a new contract on this committed base, not a mutated old contract.
        spec = rt.status(task_id)['contract'].copy()
        spec.pop('base_revision')
        task_id = rt.create(spec)
        delivery = rt, task_id, root
    task = accept(delivery, {name: None if operation == 'delete' else b'new\n'})
    replay(delivery, task, tmp_path)
    assert rt.verify(task_id)['status'] == 'accepted'


@pytest.mark.parametrize('tamper', ['replace', 'remove', 'rehash', 'base', 'revision', 'unrelated'])
def test_patch_hash_and_revision_relation_invalidate(delivery, tamper):
    rt, task_id, root = delivery
    task = accept(delivery, {'answer.txt': b'first\nnew\n\n\n'})
    evidence = task['evidence']
    patch = Path(evidence['patch_path'])
    if tamper == 'remove':
        patch.unlink()
    elif tamper in ('replace', 'rehash'):
        patch.write_bytes(b'not a patch\n')
        if tamper == 'rehash':
            evidence['patch_hash'] = hashlib.sha256(patch.read_bytes()).hexdigest()
    elif tamper == 'base':
        evidence['base_revision'] = evidence['revision']
    elif tamper == 'revision':
        workspace = Path(task['workspace'])
        git(workspace, 'commit', '--allow-empty', '-qm', 'later revision')
        evidence['revision'] = git(workspace, 'rev-parse', 'HEAD').decode().strip()
        (workspace / 'answer.txt').write_bytes(b'different\n')
        git(workspace, 'add', '.')
        git(workspace, 'commit', '-qm', 'different tree')
        evidence['revision'] = git(workspace, 'rev-parse', 'HEAD').decode().strip()
    else:
        # Same tree and same patch, but a root commit unrelated to the task base.
        tree = git(root, 'rev-parse', evidence['revision'] + '^{tree}').decode().strip()
        unrelated = git(root, 'commit-tree', tree, '-m', 'unrelated root').decode().strip()
        git(Path(task['workspace']), 'reset', '--hard', unrelated)
        evidence['revision'] = unrelated
    rt.store.save(task)
    assert rt.verify(task_id)['status'] == 'stale'


class NoCalls:
    def complete(self, *args):
        pytest.fail('Repeated run must not call a model')


@pytest.mark.parametrize('tamper', ['source', 'ignored', 'patch', 'contract', 'log', 'missing-hash'])
@pytest.mark.parametrize('cancelled', [False, True])
def test_repeated_run_checks_freshness_without_spending(delivery, tamper, cancelled):
    rt, task_id, _ = delivery
    task = accept(delivery, {'answer.txt': b'new\n'})
    if tamper in ('source', 'ignored'):
        name = 'answer.txt' if tamper == 'source' else 'ignored.txt'
        (Path(task['workspace']) / name).write_bytes(b'stale\n')
    elif tamper == 'patch':
        Path(task['evidence']['patch_path']).write_bytes(b'')
    elif tamper == 'log':
        Path(task['evidence']['log_path']).write_bytes(b'changed log')
    elif tamper == 'contract':
        task['contract']['goal'] = 'altered goal'
    else:
        task['evidence'].pop('patch_hash', None)
    rt.store.save(task)
    if cancelled:
        rt.cancel(task_id)
    # status is documented historical inspection, not freshness verification.
    assert rt.status(task_id)['status'] == 'accepted'
    result = Runtime(rt.store.root).run(task_id, NoCalls())
    assert result['status'] == 'stale'
    assert result['calls'] == task['calls']
    assert result['attempts'] == task['attempts']
    assert rt.status(task_id)['status'] == 'stale'


def test_repeated_valid_run_keeps_acceptance(delivery):
    rt, task_id, _ = delivery
    accept(delivery, {})
    assert rt.run(task_id, NoCalls())['status'] == 'accepted'


def test_contract_base_cannot_be_rebound_with_evidence(delivery):
    rt, task_id, _ = delivery
    task = accept(delivery, {'answer.txt': b'new\n'})
    task['contract']['base_revision'] = task['evidence']['revision']
    task['evidence']['base_revision'] = task['evidence']['revision']
    task['evidence']['contract_hash'] = digest(task['contract'])
    patch = Path(task['evidence']['patch_path'])
    patch.write_bytes(b'')
    task['evidence']['patch_hash'] = hashlib.sha256(b'').hexdigest()
    rt.store.save(task)
    assert rt.verify(task_id)['status'] == 'stale'


def test_run_submits_unicode_path(delivery):
    rt, task_id, _ = delivery

    class Scripted:
        actions = iter([('write_file', {'path': '答案.txt', 'content': 'new\n'}), ('submit', {})])

        def complete(self, *args):
            name, arguments = next(self.actions)
            return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': name, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]}}

    assert rt.run(task_id, Scripted())['status'] == 'accepted'
