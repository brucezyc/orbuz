"""
ShellRunner — Execute shell commands, capture structured output
===============================================================
Used by Executor's error_handler pipeline: run compile/test checks
after agent output, feed exit code + stdout back into agent context.

Pattern:
    result = ShellRunner.run("cargo check 2>&1", timeout=120)
    if result.exit_code == 0:
        # pass
    else:
        # retry agent with result.output in context
"""

import subprocess
import shlex
import re
from dataclasses import dataclass, field


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    command: str
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def output(self) -> str:
        """Combined stdout + stderr for agent context injection."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)

    def __bool__(self):
        return self.exit_code == 0 and not self.timed_out


class ShellRunner:
    """Stateless shell command runner."""

    DEFAULT_TIMEOUT = 60

    @classmethod
    def run(cls, command: str, timeout: int = 0) -> ShellResult:
        """
        Execute a shell command, return structured result.

        Supports:
          - Simple commands: 'cargo check 2>&1'
          - Pipelines:      'cargo build 2>&1 && cargo test 2>&1'
          - Heredoc:        n/a (use script files for complex logic)

        timeout: seconds (0 = DEFAULT_TIMEOUT). Raised to minimum 10s for safety.
        """
        import time

        effective_timeout = max(timeout, 10) if timeout else cls.DEFAULT_TIMEOUT
        start = time.time()

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
            elapsed = time.time() - start
            return ShellResult(
                exit_code=proc.returncode,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                command=command,
                timed_out=False,
                duration_s=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            return ShellResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {effective_timeout}s",
                command=command,
                timed_out=True,
                duration_s=elapsed,
            )

    @classmethod
    def check_condition(cls, condition: str, shell_result: ShellResult | None,
                        agent_output: str = "") -> tuple[bool, str]:
        """
        Evaluate a pass condition against a shell result and/or agent output.

        Condition syntax:
          'exit_code == 0'          → shell exited successfully
          'exit_code != 0'          → shell failed
          'contains:SUCCESS'        → agent_output contains substring
          'contains:TESTS PASSED'   → agent_output contains substring
          ''                        → always pass (no validation)
          'absent:ERROR'            → agent_output does NOT contain substring

        Returns (passed: bool, reason: str).
        """
        if not condition.strip():
            return True, "no condition (always pass)"

        # --- exit_code checks ---
        if "exit_code" in condition:
            if shell_result is None:
                return False, "exit_code condition requires a shell run"
            m = re.match(r'exit_code\s*(==|!=)\s*(\d+)', condition.strip())
            if m:
                op, expected = m.group(1), int(m.group(2))
                actual = shell_result.exit_code
                if op == "==":
                    passed = actual == expected
                else:
                    passed = actual != expected
                return passed, f"exit_code {actual} {'==' if passed else '!='} {expected}"

        # --- contains checks ---
        contains_m = re.match(r'contains:(.+)', condition.strip())
        if contains_m:
            needle = contains_m.group(1)
            haystack = agent_output or (shell_result.output if shell_result else "")
            passed = needle in haystack
            return passed, f"{'found' if passed else 'not found'} '{needle}' in output"

        # --- absent checks ---
        absent_m = re.match(r'absent:(.+)', condition.strip())
        if absent_m:
            needle = absent_m.group(1)
            haystack = agent_output or (shell_result.output if shell_result else "")
            passed = needle not in haystack
            return passed, f"{'absent' if passed else 'present'} '{needle}' in output"

        # Default: treat non-empty condition as a raw string check on agent_output
        return condition in agent_output, f"raw check: '{condition[:60]}' in output"


if __name__ == "__main__":
    # Test: successful command
    r = ShellRunner.run("echo hello")
    print(f"  ✅ echo: exit={r.exit_code}, out='{r.stdout}'")

    # Test: failing command
    r = ShellRunner.run("false")
    print(f"  ❌ false: exit={r.exit_code}")

    # Test: condition evaluation
    print(f"  condition 'exit_code == 0': {ShellRunner.check_condition('exit_code == 0', r)}")
    r2 = ShellRunner.run("echo TESTS PASSED")
    print(f"  condition 'contains:TESTS PASSED': {ShellRunner.check_condition('contains:TESTS PASSED', r2)}")

    # Test: timed out command
    import time
    r3 = ShellRunner.run("sleep 5", timeout=2)
    print(f"  ⏱  timeout: exit={r3.exit_code}, timed_out={r3.timed_out}")
