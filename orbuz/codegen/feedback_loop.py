"""
FeedbackLoop — 编译/运行反馈闭环
=====================================
生成代码后 → 运行验证命令 → 解析错误 → 喂回 LLM → 重试。

通用设计:
  - 命令可配置（`cargo check`, `python -m pytest`, `g++ -fsyntax-only` 等）
  - 错误解析器可配置（默认: 行号 + 错误信息提取）
  - 上下文注入: 每次重试附带前次错误信息

用法:
    loop = CompileFeedbackLoop(
        command="cargo check",
        max_attempts=5,
    )
    result = loop.run(
        code_context="生成的代码",
        files_to_write={"src/main.rs": "..."},
        llm_fix_fn=lambda errors, context: "修复后的代码",
    )
"""

from __future__ import annotations
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class CompileError:
    """单个编译/运行错误。"""
    file: str
    line: int
    col: int
    level: str  # error, warning, note
    message: str
    code: str | None = None  # 错误代码段（如果可提取）


@dataclass
class FeedbackResult:
    """一次反馈闭环的结果。"""
    success: bool  # True = 通过
    output: str  # 最终命令输出
    attempt_count: int  # 实际尝试次数
    errors: list[CompileError] = field(default_factory=list)  # 最终的错误（成功时为空）
    error_summary: str = ""  # 给 LLM 的错误摘要


# ── 默认错误解析器 ──

def parse_errors_rust(output: str) -> list[CompileError]:
    """解析 cargo check / cargo build 的错误输出。"""
    errors = []
    # Rust 错误格式: error[E0308]: mismatched types
    #   --> src/main.rs:42:18
    current: CompileError | None = None
    error_re = re.compile(
        r'^\s*(error|warning|note)\[?([^\]]*)\]?:\s*(.*)',
        re.MULTILINE,
    )
    loc_re = re.compile(r'\s*-->\s*([^:]+):(\d+):(\d+)')

    lines = output.splitlines()
    for i, line in enumerate(lines):
        # Try location first
        loc_m = loc_re.match(line)
        if loc_m and current:
            current.file = loc_m.group(1)
            current.line = int(loc_m.group(2))
            current.col = int(loc_m.group(3))
            # Grab next lines as code context
            code_lines = []
            for j in range(i + 1, min(i + 5, len(lines))):
                if lines[j].strip() and not lines[j].startswith("  = ") and not lines[j].startswith("  note"):
                    code_lines.append(lines[j])
                else:
                    break
            current.code = "\n".join(code_lines[-3:]) if code_lines else ""
            # Clear current so we don't reuse it for the next error
            if current.message and current.file:
                errors.append(current)
            current = None
            continue

        err_m = error_re.match(line)
        if err_m:
            # If we had an incomplete previous error, save it
            if current and current.message and current.file:
                errors.append(current)

            level = err_m.group(1)
            code = err_m.group(2) or None
            msg = err_m.group(3)
            current = CompileError(
                file="", line=0, col=0,
                level=level, message=msg, code=code,
            )
            continue

        if current and not current.file:
            # Try inline location: file:line:col
            inline = re.match(
                r'^\s*(\S+):(\d+):(\d+):\s*(error|warning)\[?([^\]]*)\]?:\s*(.*)',
                line,
            )
            if inline:
                current.file = inline.group(1)
                current.line = int(inline.group(2))
                current.col = int(inline.group(3))
                current.level = inline.group(4)
                current.message = inline.group(6)

    # Save last error
    if current and current.message and current.file:
        errors.append(current)

    return errors


def parse_errors_python(output: str) -> list[CompileError]:
    """解析 Python traceback 错误。"""
    errors = []
    tb_re = re.compile(
        r'^\s*File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+\S+)?',
        re.MULTILINE,
    )
    err_re = re.compile(r'^(\w+(?:Error|Warning)):\s*(.*)')

    for m in tb_re.finditer(output):
        file = m.group(1)
        line = int(m.group(2))
        # Look for the next error type line after this
        col = 0
        message = ""
        level = "error"
        rest = output[m.end():]
        err_m = err_re.search(rest)
        if err_m:
            message = f"{err_m.group(1)}: {err_m.group(2)}"
        errors.append(CompileError(
            file=file, line=line, col=col,
            level=level, message=message,
        ))

    if not errors and err_re.search(output):
        # Fallback: just capture the error line
        for m in err_re.finditer(output):
            errors.append(CompileError(
                file="<unknown>", line=0, col=0,
                level="error", message=f"{m.group(1)}: {m.group(2)}",
            ))

    return errors


def parse_errors_generic(output: str) -> list[CompileError]:
    """通用错误解析器：找 file:line:col 模式。"""
    errors = []
    pattern = re.compile(r'(\S+?):(\d+):(\d+):\s*(error|warning|note):\s*(.+)')
    for m in pattern.finditer(output):
        errors.append(CompileError(
            file=m.group(1), line=int(m.group(2)),
            col=int(m.group(3)), level=m.group(4),
            message=m.group(5),
        ))

    if not errors:
        # Fallback: try just file:line pattern
        pattern2 = re.compile(r'(\S+?):(\d+):\s*(error|warning):\s*(.+)')
        for m in pattern2.finditer(output):
            errors.append(CompileError(
                file=m.group(1), line=int(m.group(2)),
                col=0, level=m.group(3),
                message=m.group(4),
            ))

    return errors


# ── 注册表 ──

_ERROR_PARSERS = {
    "rust": parse_errors_rust,
    "python": parse_errors_python,
    # cpp 和通用都用 generic
    "cpp": parse_errors_generic,
}


def register_error_parser(language: str, fn: Callable[[str], list[CompileError]]):
    _ERROR_PARSERS[language] = fn


def _pick_parser(command: str, language: str | None) -> Callable[[str], list[CompileError]]:
    """根据命令和语言选择合适的解析器。"""
    cmd_lower = command.lower()
    if "cargo" in cmd_lower:
        return parse_errors_rust
    if "pytest" in cmd_lower or "python" in cmd_lower:
        return parse_errors_python
    if language and language in _ERROR_PARSERS:
        return _ERROR_PARSERS[language]
    return parse_errors_generic


# ── 格式化错误摘要（给 LLM 看） ──

def format_errors_for_llm(errors: list[CompileError], output: str) -> str:
    """把编译错误格式化成 LLM 可以理解的形式。"""
    if not errors:
        # 没有结构化错误，返回原始输出的末尾
        lines = output.strip().splitlines()
        tail = "\n".join(lines[-30:])
        return f"命令输出（末尾 30 行）:\n```\n{tail}\n```"

    parts = [f"共 {len(errors)} 个编译/运行错误:\n"]
    for i, err in enumerate(errors[:15], 1):
        loc = f"{err.file}:{err.line}" + (f":{err.col}" if err.col else "")
        parts.append(f"{i}. [{err.level.upper()}] {loc}")
        parts.append(f"   {err.message}")
        if err.code:
            # 截断过长代码
            code = err.code[:200]
            parts.append(f"   相关代码:\n{code}")
        parts.append("")

    if len(errors) > 15:
        parts.append(f"... 还有 {len(errors) - 15} 个错误")

    # 原始输出尾部（给 LLM 更多上下文）
    lines = output.strip().splitlines()
    tail = "\n".join(lines[-20:])
    parts.append(f"命令输出尾部:\n```\n{tail}\n```")

    return "\n".join(parts)


@dataclass
class FeedbackLoop:
    """
    编译/运行反馈闭环。

    用法:
        loop = FeedbackLoop(command="cargo check", cwd="/path/to/project")
        result = loop.run()
        if not result.success:
            for attempt in range(3):
                fix = llm_call(f"修复这些错误: {result.error_summary}")
                result = loop.run()
                if result.success: break
    """
    command: str
    cwd: str | None = None
    max_attempts: int = 5
    language: str | None = None
    timeout: int = 120
    work_dir: str | None = None  # 临时工作目录（None = 原项目）

    def __post_init__(self):
        self._parser = _pick_parser(self.command, self.language)
        self._attempt = 0

    def run(self) -> FeedbackResult:
        """
        执行命令，解析输出，返回结果。

        外部调用者负责:
          1. 写文件到 work_dir
          2. 根据 result.errors 决定是否重试
        """
        self._attempt += 1
        cwd = self.work_dir or self.cwd

        try:
            proc = subprocess.run(
                self.command,
                capture_output=True, text=True, timeout=self.timeout,
                shell=True, cwd=cwd,
            )
            output = proc.stdout + "\n" + proc.stderr
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            output = f"命令超时 ({self.timeout}s)"
            exit_code = -1
        except FileNotFoundError:
            output = f"命令未找到: {self.command}"
            exit_code = -1

        errors = self._parser(output)
        error_summary = format_errors_for_llm(errors, output) if errors else ""

        return FeedbackResult(
            success=exit_code == 0 and not errors,
            output=output.strip(),
            attempt_count=self._attempt,
            errors=errors,
            error_summary=error_summary,
        )

    def should_retry(self, result: FeedbackResult) -> bool:
        """是否应该重试。"""
        if result.success:
            return False
        return self._attempt < self.max_attempts


# ── 自动闭环（包含 LLM 修复调用） ──

def auto_feedback_loop(
    command: str,
    cwd: str,
    llm_fix: Callable[[str, str], str],
    file_writer: Callable[[dict], None],
    context: str,
    max_attempts: int = 5,
    language: str | None = None,
) -> FeedbackResult:
    """
    全自动编译反馈闭环。

    参数:
        command: 验证命令
        cwd: 项目目录
        llm_fix: fn(error_summary, original_context) -> 修复后的代码/说明
        file_writer: fn(files_dict) -> None
        context: 原始上下文（给 LLM 参考）
        max_attempts: 最大尝试次数
        language: 语言（用于错误解析器选择）

    返回: 最终 FeedbackResult
    """
    loop = FeedbackLoop(
        command=command, cwd=cwd,
        max_attempts=max_attempts,
        language=language,
    )

    fix_context = context

    for attempt in range(max_attempts):
        result = loop.run()
        if result.success:
            return result

        # 喂回 LLM 修复
        fix_context = llm_fix(result.error_summary, fix_context)
        file_writer({"ALL": fix_context})  # ALL = 写入所有文件的指令
    else:
        # 最后一次尝试
        return loop.run()
