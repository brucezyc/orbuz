# Evidence runtime: first slice verification

Date: 2026-09-07. New code is on a local feature branch; legacy runtime is preserved.

## Actual trial

- Provider/model: DeepSeek `deepseek-v4-flash`, real HTTPS API (no mock).
- Disposable task: implement aggregation/validation of runtime task status and token usage records.
- Starting source: `raise NotImplementedError`; test oracle authored independently before the call.
- Model tool sequence: read, read, write, command, write, command, submit.
- Model requests: 6; elapsed runtime: 14.3404 s.
- API-reported token totals: prompt 18,772; completion 2,007; total 20,779.
- First check exposed an implementation defect; the model revised its source and reran it.
- Runtime acceptance exited 0. Separate post-run sandbox recheck also exited 0.
- Six independent held-out edge cases not supplied in the visible oracle also passed
  in the sandbox (`holdout_check.py`, `holdout.log` in the trial directory).
- Accepted candidate revision: `306d49fb6826223b4b10cedc090973b686453eef`.
- Task: `f80d1e36a8d342599c35cdf246a3a3a5`.
- Actual artifacts: `/root/yzhu/exports/orbuz-runtime-trial-20260907/`.
  `result.json`, `trial.json`, `independent-recheck.log`, and per-attempt transcript,
  candidate source/commit/patch and acceptance log are retained there, not in this repo.
- Original fixture source remains unimplemented. Candidate has not been merged.

These results establish a small real model/tool/verification loop. They do not
establish multi-task orchestration, broad coding competence, or cost/quality gains
over a single agent. This slice *is itself* a single-task agent baseline.

## Regression commands

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m orbuz.runtime --help
```

Initial missing-module red test was observed before implementation; the first six
runtime tests subsequently passed. The final regression count is recorded in the
local commit/closeout report, including additional independent-review regressions.

Covered failure classes include false textual PASS, failed check, write scope and
symlink escapes, API error/missing credential, budget exhaustion, explicit retries,
interrupted state reconciliation, cancellation, source/log/contract modification,
deleted workspace, read-only sandbox, isolated network/host paths, output limit,
timeout and process cleanup.

## Not delivered

No deployment, multi-task graph, dynamic Concept integration, parallel candidates,
automatic integration, pricing model/dollar cap, cgroups quotas or production
reliability claim. See RUNTIME.md and RUNTIME_PLAN.md for exact boundaries.
