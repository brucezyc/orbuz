"""Loop-level tests for the unified long-horizon engine: resume, cap, held-out, stall.

Real Git worktrees and the real bubblewrap sandbox; the model is scripted (protocol, not ability).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orbuz.runtime import engine as engine_mod
from orbuz.runtime.engine import Runtime

REPO_ROOT = str(Path(__file__).resolve().parents[1])


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
    (root / 'check.py').write_text('from answer import answer\nassert answer(1) == 42\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def contract(project, **overrides) -> dict:
    spec = dict(goal='Make answer(n) return 42*n for every n >= 0', repository=str(project),
                writable=['answer.py'], context=['answer.py', 'check.py'],
                acceptance=['/usr/bin/python3', '-B', 'check.py'],
                max_calls=8, max_output_tokens=512, timeout=5)
    spec.update(overrides)
    return spec


def heldout_dir(tmp_path, body):
    directory = tmp_path / 'heldout'
    directory.mkdir(exist_ok=True)
    (directory / 'check_all.py').write_text(
        'import sys\nsys.path.insert(0, "/workspace")\nfrom answer import answer\n' + body)
    return directory


class Scripted:
    """Replays scripted (tool, args) pairs; records every message list it is handed."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.index = 0
        self.calls = 0
        self.seen = []

    def complete(self, messages, tools, max_tokens):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        action, args = self.actions[min(self.index, len(self.actions) - 1)]
        self.index += 1
        return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': str(self.calls), 'type': 'function',
                     'function': {'name': action, 'arguments': json.dumps(args)}}]},
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}


GOOD = 'def answer(n=1):\n    return 42 * n\n'
HARDCODED = 'def answer(n=1):\n    return 42 if n == 1 else 0\n'
CHECK_ALL = ('assert answer(1) == 42\nassert answer(0) == 0\n'
             'assert answer(2) == 84\nassert answer(5) == 210\n')


# ---------- held-out acceptance ----------

def test_heldout_catches_a_visible_pass(project, tmp_path):
    spec = contract(project, heldout=['/usr/bin/python3', '-B', '/heldout/check_all.py'],
                    heldout_assets=[str(heldout_dir(tmp_path, CHECK_ALL))])
    model = Scripted([('write_file', {'path': 'answer.py', 'content': HARDCODED}),
                      ('submit', {})])
    result = Runtime(tmp_path / 'state').run(Runtime(tmp_path / 'state').create(spec), model)
    suites = result['evidence']['suites']
    assert result['status'] == 'hacking_suspected'
    assert suites['visible']['exit_code'] == 0        # the candidate believed it was done
    assert suites['heldout']['exit_code'] != 0        # the hidden suite disagrees
    assert suites['heldout']['mounts'] == ['/heldout']
    assert 'held-out suite failed' in result['error']


def test_heldout_agrees_when_the_solution_is_general(project, tmp_path):
    spec = contract(project, heldout=['/usr/bin/python3', '-B', '/heldout/check_all.py'],
                    heldout_assets=[str(heldout_dir(tmp_path, CHECK_ALL))])
    model = Scripted([('write_file', {'path': 'answer.py', 'content': GOOD}), ('submit', {})])
    rt = Runtime(tmp_path / 'state')
    result = rt.run(rt.create(spec), model)
    assert result['status'] == 'accepted'
    assert result['evidence']['suites']['heldout']['exit_code'] == 0
    assert rt.verify(result['id'])['status'] == 'accepted'


def test_heldout_without_assets_or_patch_is_refused(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    with pytest.raises(ValueError, match='heldout_assets or heldout_patch'):
        rt.create(contract(project, heldout=['/usr/bin/python3', '-B', 'check.py']))


# A plain stdlib script, not pytest: the held-out suite must not need anything the
# sandbox cannot see, and the test must not depend on host-installed packages.
HIDDEN_PATCH = """diff --git a/test_hidden.py b/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/test_hidden.py
@@ -0,0 +1,5 @@
+import sys
+sys.path.insert(0, "/workspace")
+from answer import answer
+assert answer(0) == 0 and answer(5) == 210
+print("hidden ok")
"""


def hidden_patch_file(tmp_path):
    directory = tmp_path / 'hidden'
    directory.mkdir(exist_ok=True)
    path = directory / 'hidden.diff'
    path.write_text(HIDDEN_PATCH)
    return path


def test_heldout_patch_runs_on_a_scratch_copy(project, tmp_path):
    """A real PR adds test cases to the tree; the candidate workspace must stay untouched."""
    spec = contract(project, heldout=['/usr/bin/python3', '-B', 'test_hidden.py'],
                    heldout_patch=str(hidden_patch_file(tmp_path)))
    model = Scripted([('write_file', {'path': 'answer.py', 'content': HARDCODED}),
                      ('submit', {})])
    rt = Runtime(tmp_path / 'state')
    result = rt.run(rt.create(spec), model)
    assert result['status'] == 'hacking_suspected'
    staging = result['evidence']['heldout_staging']
    assert staging['patch_hash'] and Path(staging['tree']).is_dir()
    assert result['evidence']['suites']['visible']['exit_code'] == 0
    assert result['evidence']['suites']['heldout']['exit_code'] != 0
    # The candidate's own tree never received the hidden test.
    assert not (Path(result['workspace']) / 'test_hidden.py').exists()
    assert (Path(staging['tree']) / 'test_hidden.py').is_file()


def test_heldout_patch_must_live_outside_the_repository(project, tmp_path):
    inside = project / 'hidden.diff'
    inside.write_text(HIDDEN_PATCH)
    git(project, 'add', 'hidden.diff')          # a clean tree, then a disallowed location
    git(project, 'commit', '-qm', 'hidden patch in the repo')
    rt = Runtime(tmp_path / 'state')
    with pytest.raises(ValueError, match='outside the repository'):
        rt.create(contract(project, heldout=['/usr/bin/python3', '-B', 'check.py'],
                           heldout_patch=str(inside)))
    with pytest.raises(ValueError, match='requires heldout'):
        rt.create(contract(project, heldout_patch=str(hidden_patch_file(tmp_path))))


def test_changed_hidden_patch_voids_an_acceptance(project, tmp_path):
    patch_file = hidden_patch_file(tmp_path)
    spec = contract(project, heldout=['/usr/bin/python3', '-B', 'test_hidden.py'],
                    heldout_patch=str(patch_file))
    model = Scripted([('write_file', {'path': 'answer.py', 'content': GOOD}), ('submit', {})])
    rt = Runtime(tmp_path / 'state')
    task = rt.create(spec)
    result = rt.run(task, model)
    assert result['status'] == 'accepted'
    assert rt.verify(task)['status'] == 'accepted'
    patch_file.write_text(HIDDEN_PATCH.replace('answer(5) == 210', 'answer(6) == 252'))
    assert rt.verify(task)['status'] == 'stale'



# ---------- CLI ----------

def test_cli_reports_the_journal_and_accepts_a_pin(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project, limits={'max_steps': 1}))
    rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD})]))
    base = [sys.executable, '-m', 'orbuz.runtime', '--state-dir', str(tmp_path / 'state')]

    steps = subprocess.run([*base, 'steps', task], capture_output=True, text=True, cwd=REPO_ROOT)
    assert steps.returncode == 0
    payload = json.loads(steps.stdout)
    assert payload['resume_plan']['safe_to_continue'] is True
    assert any(s['name'].startswith('tool.') for s in payload['steps'])

    pinned = subprocess.run([*base, 'pin', task, 'only answer.py may change'],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert pinned.returncode == 0
    assert rt.status(task)['pins'][0]['content'] == 'only answer.py may change'

    status = subprocess.run([*base, 'status', task], capture_output=True, text=True, cwd=REPO_ROOT)
    assert status.returncode == 0
    assert json.loads(status.stdout)['journal']


def test_heldout_assets_inside_the_repository_are_refused(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    with pytest.raises(ValueError, match='outside the repository'):
        rt.create(contract(project, heldout=['/usr/bin/python3', '-B', '/heldout/x.py'],
                           heldout_assets=[str(project)]))


# ---------- budget: capped is a pause ----------

def test_capped_is_resumable_in_the_same_worktree(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    spec = contract(project, limits={'max_steps': 1})
    task = rt.create(spec)
    first = rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD})]))
    assert first['status'] == 'capped'
    assert first['budget']['steps'] == 2               # 1 model call + 1 tool call
    workspace = first['workspace']
    written = subprocess.check_output(['cat', str(first['workspace']) + '/answer.py'], text=True)
    assert written == GOOD

    second = rt.run(task, Scripted([('submit', {})]))
    assert second['status'] == 'accepted'
    assert second['workspace'] == workspace            # same worktree, not a fresh clone
    assert second['attempts'][-1]['resumed'] is True
    assert second['budget']['steps'] == 2              # fresh allowance: 1 model + 1 submit
    journal_names = [s['name'] for s in rt.journal.steps(task)]
    assert len([n for n in journal_names if n.startswith('tool.') and n.endswith('write_file')]) == 1
    assert second['steps'] == 4                        # durable total across both runs


def test_soft_token_limit_caps_and_survives_resume(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project, limits={'max_tokens': 10}))
    first = rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD})]))
    assert first['status'] == 'capped'
    assert first['budget']['total_tokens'] == 15       # money is a stock, not a rate
    assert first['calls'] == 1
    second = rt.run(task, Scripted([('submit', {})]))
    assert second['status'] == 'capped'                # the same 15 tokens still exceed the cap
    assert second['calls'] == 1                        # a capped task stops before spending more


# ---------- resume safety ----------

def test_unresolved_step_blocks_resume_until_retry(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project))
    rt.journal.claim(task, 'tool.999.0.command')       # a tool that died mid-flight
    crashed = rt.store.load(task)
    crashed['status'] = 'running'
    rt.store.save(crashed)

    recovered = rt.run(task, Scripted([('submit', {})]))
    assert recovered['status'] == 'interrupted'
    assert 'unknown' in (recovered.get('error') or '') or rt.journal.get(task, 'tool.999.0.command')['status'] == 'unknown'

    with pytest.raises(ValueError, match='Resume blocked'):
        rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD}),
                               ('submit', {})]))

    fresh = rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD}),
                                   ('submit', {})]), retry=True)
    assert fresh['status'] == 'accepted'
    assert fresh['attempts'][-1].get('resumed') is None


# ---------- stall ----------

def test_repeated_identical_tool_calls_end_as_stalled(project, tmp_path):
    rt = Runtime(tmp_path / 'state')
    repeated = ('write_file', {'path': 'answer.py', 'content': HARDCODED})
    task = rt.create(contract(project, max_calls=6))
    result = rt.run(task, Scripted([repeated] * 6))
    assert result['status'] == 'stalled'
    assert result['stall']['repeats'] >= 3
    assert 'no progress' in result['error']


# ---------- context management ----------

def test_compaction_records_a_receipt_and_reinjects_pins(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.context_mod, 'should_compact', lambda messages, limit: True)
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project, limits={'keep_recent': 2}))
    rt.pin(task, 'the candidate may only edit answer.py')
    model = Scripted([('write_file', {'path': 'answer.py', 'content': GOOD}), ('submit', {})])
    result = rt.run(task, model)

    compaction = result['attempts'][-1]['compaction']
    assert compaction['receipt'] is not None
    assert 'History before this point' in compaction['receipt']
    assert result['context']['dropped_messages'] > 0
    pin_text = 'the candidate may only edit answer.py'
    assert any(pin_text in json.dumps(messages) for messages in model.seen[1:])
    for messages in model.seen:
        assert '_longrun' not in json.dumps(messages)     # private markers never leak
    assert result['status'] == 'accepted'


# ---------- cache discipline ----------

def test_prefix_guard_reports_a_stable_prefix_across_resumes(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.context_mod, 'should_compact', lambda messages, limit: False)
    rt = Runtime(tmp_path / 'state')
    task = rt.create(contract(project, limits={'max_steps': 1}))
    first = rt.run(task, Scripted([('write_file', {'path': 'answer.py', 'content': GOOD})]))
    assert first['status'] == 'capped'
    assert first['cache']['stable'] is True
    assert first['cache']['bust'] == 0
