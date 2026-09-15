# SWE-bench Verified as a long-horizon task source

Real tasks with a natural visible/held-out split: `PASS_TO_PASS` are tests that already exist
in the base commit, `FAIL_TO_PASS` are tests the upstream PR added and that the candidate never
sees. A visible pass with a held-out failure is exactly the `hacking_suspected` case the runtime
is built to catch.

**Run this on the orbuz workcell (LXC 111), not on 104.** 104 is for authoring and driving only.

## Commands

```bash
cd /root/orbuz && git pull            # scripts live in the orbuz repo
python3 bench/swebench/fetch.py sympy__sympy-17630 --json /root/bench/data/one.json
python3 bench/swebench/setup.py sympy__sympy-17630        # clone @base_commit + install deps
python3 bench/swebench/runner.py sympy__sympy-17630 --suite both
python3 bench/swebench/runner.py sympy__sympy-17630 --suite heldout --patched   # hidden tests applied
```

## Why system-wide dependencies

The bubblewrap sandbox mounts `/usr` read-only and nothing else, so a package is importable
inside the sandbox only if it lives in `/usr/local/lib/python3.11/dist-packages` (or
`/usr/lib/python3/dist-packages`). On the workcell `python3` resolves to the orbuz virtualenv,
where a plain `pip install` lands **invisibly for the sandbox** - `setup.py` therefore installs
and then verifies through `/usr/bin/python3`, failing loudly if the package is not importable
there. Fallback when the system interpreter has no pip: `~/venv/bin/pip install --target
/usr/local/lib/python3.11/dist-packages <pkgs>`. Install one repo at a time; when two instances
need conflicting versions, mount a prepared virtualenv read-only instead.

## Verified facts (2026-09-15, measured on 111)

- Sandbox runs the real sympy suite offline: 20 passed, no network.
- `sympy__sympy-17630` (difficulty 1-4 hours): 19 P2P nodes green at base; the 2 FAIL_TO_PASS
  names do not exist at base; after the PR `test_patch` they fail; after the gold patch
  P2P + FAIL_TO_PASS = 21 passed.
- Run **node ids**, never whole files: unrelated tests in the same file fail in a bare
  environment and the official harness also selects nodes.
