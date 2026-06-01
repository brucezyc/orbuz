"""
OracleValidator — 数值/结果验证器
=======================================
生成代码后 → 运行 benchmark → 对比预期输出 → 标记偏差。

通用设计:
  - 基准命令可配置（`cargo bench`, `python bench.py`, `./run_tests` 等）
  - 输出解析器可配置（默认: 数值表格、pass/fail 行）
  - 预期值可通过 YAML 文件或运行时的 callable 提供
  - 偏差报告格式化为 LLM 友好的上下文

用法:
    oracle = OracleValidator(
        command="cargo test --bench iv -- --exact iv_bench",
        expected=ExpectedValues(file="expected.yaml"),
        cwd="/path/to/project",
    )
    result = oracle.run()
    if result.mismatches:
        # 喂回 LLM
        fix = llm_call(f"修复这些偏差: {result.report}")
"""

from __future__ import annotations
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ExpectedValue:
    """单个预期值。"""
    name: str  # 测量的名称，如 "explicit_iv.mean_err"
    value: float
    tolerance: float = 1e-9  # 绝对容差
    relative_tolerance: float = 1e-9  # 相对容差


@dataclass
class Mismatch:
    """单个偏差。"""
    name: str
    expected: float
    actual: float
    abs_error: float
    rel_error: float
    within_tolerance: bool = False


@dataclass
class OracleResult:
    """验证结果。"""
    success: bool  # True = 所有检查通过
    mismatches: list[Mismatch] = field(default_factory=list)
    total_checks: int = 0
    passed: int = 0
    raw_output: str = ""
    report: str = ""  # 格式化后的报告（给 LLM 看）+ 原始输出

    @property
    def accuracy(self) -> float:
        """通过率 0.0-1.0。"""
        if self.total_checks == 0:
            return 1.0
        return self.passed / self.total_checks


# ── 预期值来源 ──

@dataclass
class ExpectedValues:
    """
    预期值的来源。

    支持:
      - file="expected.yaml": 从 YAML 文件加载
      - dict={"explicit_iv.mean_err": 3.86e-11}: 硬编码字典
      - records=[ExpectedValue(...)]: 直接传入
    """
    file: str | None = None
    dict: dict[str, float] | None = None
    records: list[ExpectedValue] | None = None

    def resolve(self) -> list[ExpectedValue]:
        if self.records:
            return self.records
        if self.dict:
            return [
                ExpectedValue(name=k, value=v)
                for k, v in self.dict.items()
            ]
        if self.file:
            path = Path(self.file).expanduser()
            if path.exists():
                try:
                    import yaml
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    records = []
                    for k, v in data.items():
                        if isinstance(v, dict):
                            records.append(ExpectedValue(
                                name=k, value=v["value"],
                                tolerance=v.get("tolerance", 1e-9),
                                relative_tolerance=v.get("relative_tolerance", 1e-9),
                            ))
                        else:
                            records.append(ExpectedValue(name=k, value=float(v)))
                    return records
                except Exception:
                    pass
        return []


# ── 默认输出解析器 ──

def parse_numeric_table(output: str) -> dict[str, float]:
    """
    从 benchmark 输出中提取 key=value 数值对。

    支持格式:
      - "mean_err: 3.86e-11"
      - "explicit_iv.mean_err = 3.86e-11"
      - "| explicit_iv | 0.51 μs | 3.86e-11 |"
      - JSON 行: {"name": "explicit_iv", "mean_err": 3.86e-11}
    """
    results = {}

    # JSON 行模式
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            results[k] = float(v)
            except Exception:
                pass

    # key: value 模式
    kv_pattern = re.compile(
        r'([a-zA-Z_][a-zA-Z0-9_.]*)\s*[:=]\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)'
    )
    for m in kv_pattern.finditer(output):
        key = m.group(1).strip()
        val = float(m.group(2))
        # 跳过明显不是数值的
        if key not in results:
            results[key] = val

    # 表格模式: | name | value1 | value2 |
    table_pattern = re.compile(r'\|\s*(\S+)\s*\|\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)')
    for m in table_pattern.finditer(output):
        key = f"table.{m.group(1)}"
        if key not in results:
            results[key] = float(m.group(2))

    return results


def parse_pass_fail(output: str) -> dict[str, float]:
    """
    从测试输出中提取 pass/fail 计数。

    支持:
      - "PASS: 10, FAIL: 0"
      - "10 passed, 0 failed"
      - "ok / FAILED"
    """
    results = {}

    passed = re.search(r'(\d+)\s+passed', output, re.IGNORECASE)
    failed = re.search(r'(\d+)\s+failed', output, re.IGNORECASE)
    if passed:
        results["tests.passed"] = float(passed.group(1))
    if failed:
        results["tests.failed"] = float(failed.group(1))

    return results


# ── 验证 ──

def check_value(expected: ExpectedValue, actual: float) -> Mismatch:
    """校验单个值是否在容差范围内。"""
    abs_error = abs(actual - expected.value)

    if expected.value != 0:
        rel_error = abs_error / abs(expected.value)
    else:
        rel_error = abs_error

    within = (
        abs_error <= expected.tolerance
        or rel_error <= expected.relative_tolerance
    )

    return Mismatch(
        name=expected.name,
        expected=expected.value,
        actual=actual,
        abs_error=abs_error,
        rel_error=rel_error,
        within_tolerance=within,
    )


# ── 报告格式化 ──

def format_mismatches(mismatches: list[Mismatch], total_checks: int) -> str:
    """把偏差格式化为 LLM 友好的报告。"""
    if not mismatches:
        return f"所有 {total_checks} 项检查通过 ✅"

    parts = [f"偏差报告: {len(mismatches)}/{total_checks} 项未通过\n"]

    # 严重的放在前面
    sorted_ms = sorted(mismatches, key=lambda m: m.abs_error, reverse=True)

    for m in sorted_ms:
        status = "✅" if m.within_tolerance else "❌"
        parts.append(
            f"{status} {m.name}: "
            f"预期={m.expected:.6e}, 实际={m.actual:.6e}, "
            f"绝对误差={m.abs_error:.6e}, 相对误差={m.rel_error:.6e}"
        )

    return "\n".join(parts)


# ── 主类 ──

class OracleValidator:
    """
    数值/结果验证器。

    用法:
        oracle = OracleValidator(
            command="cargo test --bench iv",
            expected=ExpectedValues(dict={
                "explicit_iv.mean_err": 3.86e-11,
            }),
        )
        result = oracle.run()
        if not result.success:
            # 偏差报告已包含在 result.report 中
            print(result.report)
    """
    def __init__(
        self,
        command: str,
        expected: ExpectedValues,
        cwd: str | None = None,
        parsers: list[Callable[[str], dict[str, float]]] | None = None,
        timeout: int = 180,
        extra_parsers: list[Callable[[str], dict[str, float]]] | None = None,
    ):
        self.command = command
        self.expected = expected
        self.cwd = cwd
        self._parsers = list(parsers or [parse_numeric_table, parse_pass_fail])
        if extra_parsers:
            self._parsers.extend(extra_parsers)
        self.timeout = timeout

    def run(self) -> OracleResult:
        """运行基准命令并验证输出。"""
        try:
            proc = subprocess.run(
                self.command,
                capture_output=True, text=True, timeout=self.timeout,
                shell=True, cwd=self.cwd,
            )
            output = proc.stdout + "\n" + proc.stderr
        except subprocess.TimeoutExpired:
            output = f"命令超时 ({self.timeout}s)"
        except FileNotFoundError:
            output = f"命令未找到: {self.command}"

        # 解析输出为数值
        extracted: dict[str, float] = {}
        for parser in self._parsers:
            extracted.update(parser(output))

        # 加载预期值
        expected_records = self.expected.resolve()

        # 校验
        mismatches: list[Mismatch] = []
        total = len(expected_records)
        passed = 0

        for exp in expected_records:
            actual = extracted.get(exp.name)
            if actual is None:
                # 尝试去除前缀匹配
                for k, v in extracted.items():
                    if k.endswith(exp.name) or exp.name.endswith(k):
                        actual = v
                        break

            if actual is None:
                mismatches.append(Mismatch(
                    name=exp.name,
                    expected=exp.value,
                    actual=float("nan"),
                    abs_error=float("inf"),
                    rel_error=float("inf"),
                ))
                continue

            m = check_value(exp, actual)
            if m.within_tolerance:
                passed += 1
            mismatches.append(m)

        # 生成报告
        report = format_mismatches(mismatches, total)
        report += "\n\n--- 原始输出 ---\n"
        report += output[:3000]  # 截断原始输出

        return OracleResult(
            success=passed == total and len(mismatches) == total,
            mismatches=[m for m in mismatches if not m.within_tolerance],
            total_checks=total,
            passed=passed,
            raw_output=output,
            report=report,
        )
