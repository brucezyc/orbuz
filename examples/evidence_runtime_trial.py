"""Opt-in real model trial. Fixture/oracle are authored here; solution only by model.

Run with --live, explicit --state-dir and DEEPSEEK_API_KEY in environment.
No production project is modified and no candidate is merged automatically.
"""
import argparse
import json
import subprocess
from pathlib import Path

from orbuz.runtime.engine import Runtime
from orbuz.runtime.model import ChatModel


SEED = '''def summarize(records):
    raise NotImplementedError("Implement the task contract")
'''
ORACLE = '''import ctypes
import json
import subprocess
import sys

# Keep candidate imports outside the assertion-owning interpreter. Linux parent
# memory/fd access is denied; killing this parent fails acceptance, never passes.
if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
    raise RuntimeError("Cannot protect oracle parent")
CHILD = """import json, sys
records = json.loads(sys.stdin.read())
from summary import summarize
try:
    result = summarize(records)
except ValueError:
    response = {'error': 'ValueError'}
else:
    response = {'result': result, 'after': records}
print(json.dumps(response, allow_nan=False))
"""

def summarize(records):
    proc = subprocess.run([sys.executable, '-B', '-c', CHILD],
                          input=json.dumps(records), text=True, capture_output=True,
                          timeout=2, close_fds=True)
    assert proc.returncode == 0, ('candidate child failed', proc.returncode)
    # Exit zero without a complete machine-readable result is failure.
    try:
        response = json.loads(proc.stdout)
    except ValueError as exc:
        raise AssertionError('candidate returned no valid JSON result') from exc
    if response == {'error': 'ValueError'}:
        raise ValueError()
    assert isinstance(response, dict) and set(response) == {'result', 'after'}
    assert response['after'] == records, 'input mutation'
    result = response['result']
    assert isinstance(result, dict) and set(result) == {'total', 'statuses', 'prompt_tokens', 'completion_tokens'}
    for key in ('total', 'prompt_tokens', 'completion_tokens'):
        assert type(result[key]) is int and result[key] >= 0
    assert isinstance(result['statuses'], dict)
    assert all(isinstance(key, str) and key and type(value) is int and value > 0
               for key, value in result['statuses'].items())
    return result

r = summarize([
    {"status": "accepted", "usage": [{"prompt_tokens": 10, "completion_tokens": 4}]},
    {"status": "rejected", "usage": [{"prompt_tokens": 7, "completion_tokens": 3}]},
    {"status": "accepted", "usage": []},
    {"status": "failed"},
])
assert r == {"total": 4, "statuses": {"accepted": 2, "rejected": 1, "failed": 1},
             "prompt_tokens": 17, "completion_tokens": 7}, r
assert summarize([]) == {"total": 0, "statuses": {}, "prompt_tokens": 0, "completion_tokens": 0}
original = [{"status": "pending", "usage": [{"prompt_tokens": 2}]}]
import copy
before = copy.deepcopy(original)
assert summarize(original)["completion_tokens"] == 0
assert original == before
for bad in [None, {}, [None], [{}], [{"status": 1}], [{"status": "x", "usage": None}],
            [{"status": "x", "usage": [{"prompt_tokens": -1}]}],
            [{"status": "x", "usage": [{"prompt_tokens": True}]}],
            [{"status": "x", "usage": [{"completion_tokens": 1.5}]}]]:
    try:
        summarize(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(("must reject invalid input", bad))
print("Acceptance: aggregation, empty input, nonmutation, malformed records passed")
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--model', default='deepseek-v4-flash')
    args = parser.parse_args()
    root = args.state_dir.resolve()
    repo = root / 'fixture'
    repo.mkdir(parents=True, exist_ok=False)
    (repo / 'summary.py').write_text(SEED)
    (repo / 'check.py').write_text(ORACLE)
    for argv in [['init', '-q'], ['config', 'user.name', 'Orbuz Trial'],
                 ['config', 'user.email', 'trial@localhost'], ['add', '.'],
                 ['commit', '-qm', 'Unimplemented trial with independent oracle']]:
        subprocess.run(['git', '-C', str(repo), *argv], check=True)
    spec = {'repository': str(repo), 'goal':
            'Implement summary.py summarize(records) for Orbuz task records. Input must be a list '
            'of dicts, each with a nonempty string status and optional usage list of dicts. '
            'Return total record count, statuses histogram, sum of prompt_tokens and completion_tokens '
            'across usage entries. Missing usage or token fields mean zero. Token values must be '
            'nonnegative integers, not bools. All malformed inputs raise ValueError. Do not mutate '
            'input. Use only Python standard library. Investigate source and run checks before submit.',
            'writable': ['summary.py'], 'context': ['summary.py', 'check.py'],
            'acceptance': ['/usr/bin/python3', '-B', 'check.py'],
            'max_calls': 8, 'max_output_tokens': 2048, 'timeout': 10, 'max_seconds': 180}
    rt = Runtime(root / 'state')
    task = rt.create(spec)
    (root / 'trial.json').write_text(json.dumps({'task_id': task, 'contract': spec}, indent=2))
    model = ChatModel(args.model, 'https://api.deepseek.com')
    try:
        result = rt.run(task, model)
    finally:
        model.close()
    (root / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result.get(k) for k in ['id', 'status', 'calls', 'elapsed_s', 'usage', 'error', 'workspace', 'evidence']}, indent=2))
    return 0 if result['status'] == 'accepted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
