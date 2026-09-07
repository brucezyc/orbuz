"""Real sandbox checks of the trial oracle; no model/API calls."""
from pathlib import Path
import pytest
from orbuz.runtime.sandbox import execute
from examples.evidence_runtime_trial import ORACLE


@pytest.mark.parametrize('source', [
    'import os; os._exit(0)',
    'import sys; sys.exit(0)',
    'print("Acceptance: aggregation, empty input, nonmutation, malformed records passed"); import os; os._exit(0)',
    'import os, signal; os.kill(os.getppid(), signal.SIGKILL)',
    'def summarize(records): return {}',
])
def test_early_exit_or_fake_success_rejected(tmp_path, source):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'summary.py').write_text(source)
    (root / 'check.py').write_text(ORACLE)
    result = execute(root, ['/usr/bin/python3', '-B', 'check.py'], tmp_path / 'check.log', timeout=15)
    assert result['exit_code'] != 0, result


@pytest.mark.parametrize('change,accepted', [
    ('', True),
    ("\n_original = summarize\ndef summarize(records):\n    result = _original(records)\n    result['total'] = float(result['total'])\n    return result\n", False),
    ("\n_original = summarize\ndef summarize(records):\n    try: return _original(records)\n    except ValueError:\n        import os\n        os._exit(0)\n", False),
])
def test_summary_result_protocol(tmp_path, change, accepted):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'summary.py').write_text('''def summarize(records):
    if not isinstance(records, list): raise ValueError()
    out = dict(total=len(records), statuses={}, prompt_tokens=0, completion_tokens=0)
    for record in records:
        if not isinstance(record, dict): raise ValueError()
        status = record.get('status')
        if not isinstance(status, str) or not status: raise ValueError()
        out['statuses'][status] = out['statuses'].get(status, 0) + 1
        usage = record.get('usage', [])
        if not isinstance(usage, list): raise ValueError()
        for entry in usage:
            if not isinstance(entry, dict): raise ValueError()
            for key in ('prompt_tokens', 'completion_tokens'):
                value = entry.get(key, 0)
                if type(value) is not int or value < 0: raise ValueError()
                out[key] += value
    return out
''' + change)
    (root / 'check.py').write_text(ORACLE)
    result = execute(root, ['/usr/bin/python3', '-B', 'check.py'], tmp_path / 'check.log', timeout=15)
    assert (result['exit_code'] == 0) is accepted, result
    if accepted:
        assert 'Acceptance:' in result['output']
