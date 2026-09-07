"""Read-only deployment checks plus disposable offline tests; never calls a model."""
import json
from pathlib import Path
import subprocess
import sys
from provision_test_lxc import remote, VMID

local_revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
program = '''import json, os, stat, subprocess
from pathlib import Path
import yaml
import orbuz
from orbuz.runtime.sandbox import execute
p=Path('/root/.orbuz/forge.yaml')
c=yaml.safe_load(p.read_text()) or {}
print(json.dumps({'config_mode':oct(stat.S_IMODE(p.stat().st_mode)),
 'tiers':{t:{'model':(c.get(t) or {}).get('model'),
 'key_set':bool((c.get(t) or {}).get('api_key') or c.get('api_key'))}
 for t in ('architect','quality','balanced','cheap')},
 'orbuz_import':orbuz.__file__}))
workspace=Path('/root/projects/.sandbox-check'); workspace.mkdir(exist_ok=True)
r=execute(workspace,['/usr/bin/python3','-c',
 "import os; assert not os.path.exists('/root/.orbuz/forge.yaml'); assert not os.environ.get('DEEPSEEK_API_KEY'); print('SANDBOX_OK')"],
 Path('/root/orbuz-state/sandbox-check.log'))
print(json.dumps(r))
assert r['exit_code']==0 and 'SANDBOX_OK' in r['output']
'''
result = remote(['pct', 'exec', VMID, '--', '/root/venv/bin/python', '-c', program], capture=True)
print(result.stdout, flush=True)
remote(['pct', 'exec', VMID, '--', 'bash', '-c',
        'cd /root/orbuz && /root/venv/bin/python -m pytest tests -q'])
print('OFFLINE_VERIFICATION_COMPLETE', flush=True)
