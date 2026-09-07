"""Linux 沙箱的真实执行测试；依赖可用的 bubblewrap/user namespaces。"""

import errno
import json
import os
from pathlib import Path
import socket
import time
import uuid

import pytest

from orbuz.runtime import sandbox
from orbuz.runtime.sandbox import execute


@pytest.fixture
def paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("original\n")
    return workspace, tmp_path / "run.log"


def run_python(paths, code, **kwargs):
    return execute(paths[0], ["/usr/bin/python3", "-c", code], paths[1], **kwargs)


def test_execute_real_python(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_path = tmp_path / "command.log"
    result = execute(
        workspace,
        ["/usr/bin/python3", "-c", "print('sandbox-python-ok')"],
        log_path,
    )
    assert result == {
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "output": "sandbox-python-ok\n",
        "log_path": str(log_path),
    }
    assert log_path.read_text() == "sandbox-python-ok\n"


def test_read_only_workspace_system_and_writable_tmp(paths):
    result = run_python(paths, """
import errno, os
from pathlib import Path
assert os.getcwd() == '/workspace'
assert Path('source.py').read_text() == 'original\\n'
for path in ('source.py', 'new.txt', '/usr/orbuz-write-test', '/new-file'):
    try:
        Path(path).write_text('changed')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES), (path, exc)
    else:
        raise AssertionError(path)
Path('/tmp/scratch').write_text('temporary')
assert Path('/tmp/scratch').read_text() == 'temporary'
print('read-only-ok')
""")
    assert result['exit_code'] == 0, result
    assert paths[0].joinpath('source.py').read_text() == 'original\n'
    assert not paths[0].joinpath('new.txt').exists()


def test_host_paths_environment_and_capabilities_hidden(paths, monkeypatch):
    monkeypatch.setenv('ORBUZ_TEST_SECRET', 'must-not-leak')
    monkeypatch.setenv('PYTHONPATH', '/root/host-only')
    outside = paths[0].parent / 'host-secret'
    outside.write_text('host-only-sentinel')
    (paths[0] / 'escape').symlink_to(outside)
    result = run_python(paths, f"""
import os
from pathlib import Path
for path in ('/root', '/home', '/etc', {str(outside)!r}, '/workspace/escape'):
    assert not Path(path).exists(), path
assert 'ORBUZ_TEST_SECRET' not in os.environ
assert 'PYTHONPATH' not in os.environ
assert os.environ['PATH'] == '/usr/bin:/bin'
status = Path('/proc/self/status').read_text()
for line in status.splitlines():
    if line.startswith(('CapEff:', 'CapPrm:', 'CapBnd:')):
        assert int(line.split()[1], 16) == 0, line
assert 'NoNewPrivs:\\t1' in status
mapping = Path('/proc/self/uid_map').read_text().split()
assert int(mapping[2]) == 1
print('isolation-ok')
""")
    assert result['exit_code'] == 0, result


@pytest.mark.parametrize('kind', ['directory', 'file', 'nested', 'symlink'])
def test_git_metadata_hidden(paths, kind):
    git = paths[0] / '.git'
    if kind == 'nested':
        git = paths[0] / 'submodule' / '.git'
        git.parent.mkdir()
    if kind in ('directory', 'nested'):
        git.mkdir()
        (git / 'config').write_text('secret-git-config')
    elif kind == 'file':
        git.write_text('gitdir: /root/private-gitdir\n')
    else:
        git.symlink_to('/root/private-gitdir')
    result = run_python(paths, """
from pathlib import Path
for path in Path('/workspace').rglob('.git'):
    if path.is_dir():
        assert list(path.iterdir()) == []
    else:
        assert path.read_bytes() == b''
print('git-hidden')
""")
    if kind == 'symlink':
        assert result['exit_code'] is None
        assert 'failed closed' in result['output']
    else:
        assert result['exit_code'] == 0, result
    assert git.is_symlink() or git.exists()


def test_no_host_network_or_abstract_unix_socket(paths):
    # 只访问本机监听端口，不发起外网请求；同时检验抽象 Unix socket 隔离。
    with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
        tcp.bind(('127.0.0.1', 0))
        tcp.listen()
        address = '\0orbuz-sandbox-' + uuid.uuid4().hex
        unix.bind(address)
        unix.listen()
        result = run_python(paths, f"""
import socket
from pathlib import Path
assert Path('/proc/self/ns/net').readlink().as_posix() != {os.readlink('/proc/self/ns/net')!r}
for family, address in [(socket.AF_INET, {tcp.getsockname()!r}), (socket.AF_UNIX, {address!r})]:
    with socket.socket(family) as sock:
        sock.settimeout(0.2)
        assert sock.connect_ex(address) != 0
print('network-isolated')
""")
    assert result['exit_code'] == 0, result


def test_complete_log_and_unicode_tail(paths):
    result = run_python(paths, "import sys; print('证据' * 5000); print('stderr-marker', file=sys.stderr)")
    assert result['exit_code'] == 0
    log = paths[1].read_text()
    assert 'stderr-marker' in log
    assert log.count('证据') == 5000
    assert result['output'] == log[-6000:]


def test_output_limit_kills_writer(paths):
    started = time.monotonic()
    result = run_python(paths, "import os; data=b'x'*65536\nwhile True: os.write(1,data)", timeout=5)
    assert result['exit_code'] != 0
    assert not result['timed_out']
    assert not result['cancelled']
    assert time.monotonic() - started < 3
    assert paths[1].stat().st_size == 4 * 1024 * 1024
    assert result['output'] == 'x' * 6000


def matching_processes(token):
    matches = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if token.encode() in (entry / 'cmdline').read_bytes():
                matches.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return matches


@pytest.mark.parametrize('mode', ['timeout', 'cancel', 'exit'])
def test_cleanup_includes_setsid_descendants(paths, mode):
    token = 'orbuz-child-' + uuid.uuid4().hex
    code = f"""
import os, signal, time
# {token}
if os.fork() == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('descendant-started', flush=True)
    while True:
        time.sleep(1)
time.sleep({0.1 if mode == 'exit' else 20})
"""
    started = time.monotonic()
    options = {'timeout': 0.4 if mode == 'timeout' else 5}
    if mode == 'cancel':
        options['cancel'] = lambda: time.monotonic() - started > 0.4
    result = run_python(paths, code, **options)
    assert time.monotonic() - started < 3, result
    assert 'descendant-started' in result['output'], result
    assert result['timed_out'] == (mode == 'timeout'), result
    assert result['cancelled'] == (mode == 'cancel'), result
    if mode == 'exit':
        assert result['exit_code'] == 0, result
    else:
        assert result['exit_code'] != 0, result
    deadline = time.monotonic() + 1
    while matching_processes(token) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert matching_processes(token) == []


def test_closed_output_does_not_bypass_timeout(paths):
    result = run_python(paths, 'import os,time; os.close(1); os.close(2); time.sleep(20)', timeout=0.2)
    assert result['timed_out']
    assert result['exit_code'] != 0


def test_pre_cancel_does_not_launch(paths, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('pre-cancel launched a process')
    monkeypatch.setattr(sandbox.subprocess, 'Popen', forbidden)
    result = run_python(paths, "print('should-not-run')", cancel=lambda: True)
    assert result['cancelled']
    assert result['exit_code'] is None
    assert result['output'] == ''


@pytest.mark.parametrize('error', [FileNotFoundError('bwrap missing'), PermissionError('namespace denied')])
def test_launcher_failure_is_fail_closed(paths, monkeypatch, error):
    calls = []
    def denied(command, **kwargs):
        calls.append(command)
        raise error
    monkeypatch.setattr(sandbox.subprocess, 'Popen', denied)
    result = run_python(paths, "print('unsafe-fallback')")
    assert len(calls) == 1
    assert calls[0][0] == '/usr/bin/bwrap'
    assert result['exit_code'] is None
    assert 'failed closed' in result['output']
    assert 'unsafe-fallback' not in result['output']


def test_shell_is_explicit_and_still_read_only(paths):
    result = execute(paths[0], ['/bin/sh', '-c', 'printf nope > source.py'], paths[1])
    assert result['exit_code'] != 0
    assert paths[0].joinpath('source.py').read_text() == 'original\n'


def test_exit_status_preserved(paths):
    result = run_python(paths, 'import sys; print("failed"); sys.exit(7)')
    assert result['exit_code'] == 7
    assert result['output'] == 'failed\n'


def test_reject_log_inside_workspace_or_linked_to_source(paths):
    source = paths[0] / 'source.py'
    with pytest.raises(ValueError):
        execute(paths[0], ['/bin/true'], source)
    paths[1].symlink_to(source)
    with pytest.raises(ValueError):
        execute(paths[0], ['/bin/true'], paths[1])
    paths[1].unlink()
    os.link(source, paths[1])
    with pytest.raises(ValueError):
        execute(paths[0], ['/bin/true'], paths[1])
    assert source.read_text() == 'original\n'
