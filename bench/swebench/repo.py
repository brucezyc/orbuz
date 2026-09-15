"""Per-repo knowledge: test deps and the test files the official harness runs.

Deps are installed system-wide because the bubblewrap sandbox mounts only /usr (read-only):
anything importable inside the sandbox must live in /usr/local/lib/python3.11/dist-packages
or /usr/lib/python3/dist-packages. One repo at a time keeps versions from colliding.
"""
import re

DEPS = {
    'sympy/sympy': ['mpmath'],
    'psf/requests': ['urllib3', 'certifi', 'charset_normalizer', 'idna'],
    'pallets/flask': ['werkzeug', 'jinja2', 'click', 'itsdangerous', 'blinker'],
    'pytest-dev/pytest': ['pluggy', 'iniconfig', 'packaging'],
    'pylint-dev/pylint': ['astroid', 'dill', 'isort', 'mccabe', 'tomlkit', 'platformdirs'],
}

TEST_COMMAND = re.compile(r'^\s*(?:PYTHONWARNINGS=\S+\s+)?(?:bin/test|python -m pytest|pytest)\s+(.*)$')


def test_files(eval_script):
    """The test paths the official eval_script runs (it resets them afterwards)."""
    files = []
    for line in eval_script.splitlines():
        match = TEST_COMMAND.match(line)
        if not match:
            continue
        for token in match.group(1).split():
            if token.startswith('-'):
                continue
            if token.endswith('.py') and token not in files:
                files.append(token)
    return files


def deps_for(repo):
    if repo not in DEPS:
        raise SystemExit(f'No dependency map for {repo}; add one to bench/swebench/repo.py')
    return DEPS[repo]
