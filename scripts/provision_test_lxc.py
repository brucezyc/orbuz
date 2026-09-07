"""Provision a NEW isolated Orbuz workcell; no model calls or existing LXC writes."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

HOST = 'root@192.168.0.100'
VMID = '111'
REPO = 'https://github.com/brucezyc/orbuz.git'
BRANCH = 'feat/evidence-runtime'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST]


def remote(args, text=None, capture=False):
    return subprocess.run(SSH + [shlex.join(args)], input=text, text=True,
                          capture_output=capture, check=True, timeout=1800)


if __name__ == '__main__':
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    remote(['pvesh', 'get', '/cluster/nextid', '--vmid', VMID])
    key = Path('/root/.ssh/id_ed25519.pub').read_text()
    remote(['tee', '/tmp/orbuz-test-111.pub'], key, capture=True)
    remote(['pct', 'create', VMID,
            'local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst',
            '--hostname', 'orbuz-test', '--cores', '2', '--memory', '4096',
            '--swap', '512', '--rootfs', 'local-lvm:30',
            '--net0', 'name=eth0,bridge=vmbr0,ip=dhcp',
            '--unprivileged', '1', '--features', 'nesting=1,keyctl=1',
            '--ssh-public-keys', '/tmp/orbuz-test-111.pub', '--onboot', '0'])
    remote(['pct', 'start', VMID])
    setup = f'''set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=3 update
apt-get install -y git curl ca-certificates build-essential pkg-config libssl-dev python3 python3-venv python3-dev bubblewrap openssh-server tmux
systemctl enable --now ssh
python3 -m venv /root/venv
/root/venv/bin/pip install --upgrade pip
GIT_TERMINAL_PROMPT=0 git clone --branch {BRANCH} --single-branch {REPO} /root/orbuz
cd /root/orbuz
git checkout --detach {revision}
/root/venv/bin/pip install -e . pytest
curl --proto '=https' --tlsv1.2 --retry 3 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh
sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain stable
mkdir -p /root/projects /root/orbuz-state /root/.orbuz
chmod 700 /root/.orbuz
printf '\n. /root/venv/bin/activate\n. /root/.cargo/env\n' >> /root/.bashrc
git config --global user.name 'Orbuz Test'
git config --global user.email 'orbuz-test@local'
/root/venv/bin/pip check
/root/venv/bin/orbuz --help
/root/venv/bin/python -m orbuz.runtime --help
/root/.cargo/bin/rustc --version
ip -br -4 addr
'''
    remote(['pct', 'exec', VMID, '--', 'bash', '-s'], setup)
    # Transfer only model credentials/config, never print secret material or copy
    # SSH private keys, old projects, webhook destinations or whole environment.
    source = "from pathlib import Path; print(Path('/root/.orbuz/forge.yaml').read_text(), end='')"
    raw = remote(['pct', 'exec', '109', '--', 'python3', '-c', source], capture=True).stdout
    import yaml
    config = yaml.safe_load(raw) or {}
    allowed = {'api_key', 'api_base', 'architect', 'quality', 'balanced', 'cheap'}
    config = {k: v for k, v in config.items() if k in allowed}
    for tier in ('architect', 'quality', 'balanced', 'cheap'):
        if tier in config:
            config[tier] = {k: v for k, v in config[tier].items()
                            if k in ('model', 'api_key', 'api_base')}
    destination = "import sys,os; p='/root/.orbuz/forge.yaml'; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,sys.stdin.buffer.read()); os.close(fd)"
    remote(['pct', 'exec', VMID, '--', 'python3', '-c', destination], yaml.safe_dump(config))
    print(json.dumps({'container': VMID, 'revision': revision,
                      'config_copied': True, 'live_model_calls': 0}))
