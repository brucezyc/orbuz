"""只读 evidence 执行沙箱；任何隔离失败都不回退到宿主执行。"""

from __future__ import annotations

import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Callable


MAX_LOG_BYTES = 4 * 1024 * 1024
OUTPUT_CHARS = 6000
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "TMPDIR": "/tmp"}


def _command(workspace: Path, argv: list[str], empty_fd: int,
             read_only_mounts: tuple = ()) -> list[str]:
    # 不搜索调用者 PATH，避免把恶意 bwrap 当作隔离器执行。
    command = [
        "/usr/bin/bwrap",
        "--unshare-user", "--unshare-net", "--unshare-pid",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        "--cap-drop", "ALL", "--clearenv",
    ]
    for key, value in _ENV.items():
        command.extend(["--setenv", key, value])
    # usr-merge 主机保留系统链接，而不是把链接目标重复挂载。
    for name in ("usr", "bin", "lib", "lib64"):
        path = Path("/") / name
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to("/usr"):
                raise ValueError(f"Unsupported system symlink: {path}")
            command.extend(["--symlink", os.readlink(path), str(path)])
        elif path.is_dir():
            command.extend(["--ro-bind", str(path), str(path)])
        elif name in ("usr", "bin"):
            raise ValueError(f"Missing system directory: {path}")
    command.extend([
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--ro-bind", str(workspace), "/workspace",
    ])
    # Held-out acceptance assets: read-only, outside /workspace, single-level guest path so
    # the mount point always exists without relying on bwrap creating parents.
    for host, guest in read_only_mounts:
        command.extend(["--ro-bind", host, guest])
    # .git 既可能是目录，也可能是指向宿主 worktree gitdir 的文本文件。
    # 不跟随 .git 符号链接：挂载目标会解析链接，直接拒绝比误遮蔽更安全。
    for root, directories, files in os.walk(workspace, followlinks=False):
        if ".git" not in directories and ".git" not in files:
            continue
        git_path = Path(root) / ".git"
        if git_path.is_symlink():
            raise ValueError("Symlink .git is not supported by the sandbox")
        destination = str(Path("/workspace") / git_path.relative_to(workspace))
        if git_path.is_dir():
            directories.remove(".git")
            command.extend(["--tmpfs", destination, "--remount-ro", destination])
        else:
            command.extend(["--ro-bind-data", str(empty_fd), destination])
    command.extend(["--chdir", "/workspace", "--remount-ro", "/", "--", *argv])
    return command


def _kill_group(process: subprocess.Popen) -> None:
    # 新 PID namespace 的 init 一并死亡，setsid/double-fork 也不能留下守护进程。
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(
    workspace: Path,
    argv: list[str],
    log_path: Path,
    timeout: float = 30,
    cancel: Callable[[], bool] | None = None,
    read_only_mounts: list[tuple[str, str]] | None = None,
) -> dict:
    """合并 stdout/stderr 保存至宿主日志；超时/取消/日志超限均杀组并 wait。

    exit_code=None 表示启动前失败或取消；隔离器失败保留非零退出码。
    日志超限保留前 4MiB 并终止，output 始终是已保存日志的末 6000 字。
    workspace 必须是父 runtime 准备的源码目录，而非宿主根目录。
    read_only_mounts 为 (宿主路径, 沙箱绝对路径) 列表，用于只读挂载验收资产；
    目标不能落在 /workspace 内，宿主路径必须存在。
    """
    workspace = Path(workspace).resolve(strict=True)
    log_path = Path(log_path).absolute()
    if not workspace.is_dir() or workspace == Path("/"):
        raise ValueError("workspace must be a source directory, not host root")
    if log_path.resolve().is_relative_to(workspace):
        raise ValueError("log_path must be outside the read-only workspace")
    if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
        raise ValueError("argv must be a nonempty list of strings without NUL")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    mounts = []
    for entry in read_only_mounts or ():
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("read_only_mounts entries must be (host, guest) pairs")
        host, guest = entry
        if not isinstance(host, str) or not isinstance(guest, str) or "\0" in host or "\0" in guest:
            raise ValueError("Invalid read-only mount")
        if not guest.startswith("/") or guest == "/" or guest.startswith("/workspace"):
            raise ValueError("read-only mount guest path must be absolute and outside /workspace")
        mounts.append((str(Path(host).resolve(strict=True)), guest))

    result = {"exit_code": None, "timed_out": False, "cancelled": False,
              "output": "", "log_path": str(log_path)}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 不允许最终日志路径链接到源文件/凭据；日志 FD 不传给沙箱。
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    process = None
    tail = bytearray()
    written = 0
    with os.fdopen(log_fd, "wb") as log:
        if os.fstat(log.fileno()).st_nlink != 1:
            raise ValueError("log_path must not be hard-linked")
        log.truncate(0)

        def record(data: bytes) -> bool:
            nonlocal written
            accepted = data[:MAX_LOG_BYTES - written]
            log.write(accepted)
            written += len(accepted)
            tail.extend(accepted)
            del tail[:-OUTPUT_CHARS * 4]
            return len(accepted) < len(data)

        if cancel is not None and cancel():
            result["cancelled"] = True
        else:
            try:
                with open(os.devnull, "rb") as empty:
                    command = _command(workspace, argv, empty.fileno(), tuple(mounts))
                    started = time.monotonic()
                    process = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, env=_ENV, cwd="/",
                        start_new_session=True, close_fds=True,
                        pass_fds=(empty.fileno(),),
                    )
                assert process.stdout is not None
                os.set_blocking(process.stdout.fileno(), False)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if cancel is not None and cancel():
                            result["cancelled"] = True
                            _kill_group(process)
                            break
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            result["timed_out"] = True
                            _kill_group(process)
                            break
                        events = selector.select(min(0.05, remaining))
                        if events:
                            data = os.read(process.stdout.fileno(), 65536)
                            if not data:
                                # 子进程可主动关闭输出但仍在运行，不能把 EOF 当退出。
                                selector.unregister(process.stdout)
                            elif record(data):
                                _kill_group(process)
                                break
                        if process.poll() is not None:
                            break
                # 正常退出也清理后台后代，PID namespace 提供跨 session 的兜底。
                _kill_group(process)
                result["exit_code"] = process.wait()
                # 回收后管道不再有写者，保存终止前已经写入管道的数据。
                while written < MAX_LOG_BYTES:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    if record(data):
                        break
            except (OSError, ValueError) as exc:
                record(f"Sandbox failed closed: {exc}\n".encode("utf-8"))
            finally:
                if process is not None:
                    _kill_group(process)
                    result["exit_code"] = process.wait()
                    if process.stdout is not None:
                        process.stdout.close()
    result["output"] = tail.decode("utf-8", errors="replace")[-OUTPUT_CHARS:]
    return result
