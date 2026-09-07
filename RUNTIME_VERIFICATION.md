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

## Post-hardening live rerun

A second fresh real-API trial on runtime commit `5e002b5` also accepted:
- Task `c41630f4c9304f18b60339be793196cf`, DeepSeek v4 flash recorded in attempt metadata.
- 8 model requests, 15.0412 s; reported prompt/completion/total tokens: 25,971 / 2,077 / 28,048.
- Candidate `d2d6c67d45585a0a380fee914452da7d54c2a2f0`.
- `/root/yzhu/exports/orbuz-runtime-final-trial-20260907/result.json` and transcript/logs.
- Fresh evidence verification accepted; separate sandbox acceptance rerun exited 0.

## Regression commands

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m orbuz.runtime --help
```

Initial missing-module red test was observed before implementation; the first six
runtime tests subsequently passed. Latest full regression: **50 passed**. This includes
transport mocks (explicitly marked), actual SIGKILL recovery, acceptance aliases,
ignored-file evidence invalidation and scoped full-log retrieval.

Covered failure classes include false textual PASS, failed check, write scope and
symlink escapes, API error/missing credential, budget exhaustion, explicit retries,
interrupted state reconciliation, cancellation, source/log/contract modification,
deleted workspace, read-only sandbox, isolated network/host paths, output limit,
timeout and process cleanup.

## 2026-09-07 oracle correction (offline)

The old trial oracle imported the candidate in its own assertion interpreter.
Three actual sandbox regressions reproduced false exit-zero success: `os._exit(0)`,
`sys.exit(0)` and printing a fake acceptance marker before early exit. The trial now
uses child candidate processes with independent parent assertions, strict result
schema/types, expected values and protocol-failure handling. Missing JSON must not
be confused with an expected candidate ValueError on malformed input.

Eight targeted regressions pass. Both previously saved **real-model candidates**
were copied unchanged into disposable directories and rerun against the strengthened
oracle; both passed. This is offline re-verification of existing model output, not
another paid model trial and not retroactive modification of original evidence.
`/root/yzhu/exports/orbuz_saved_candidates_recheck_20260907.json` records revisions,
real sandbox exits and log paths. Generic arbitrary acceptance commands still rely
on the author's oracle quality; hardcoding/forged child responses are not eliminated.

## Request budget correction (offline)

The pre-fix probe recorded a 0.05-second task consuming roughly 0.35 seconds in a
slow synchronous adapter and an initial serialized brief of 150,749 characters.
The built-in HTTP adapter now exposes bounded requests; runtime forwards remaining
deadline/cancellation and records exhausted/cancelled states without executing late
actions. Async MockTransport probes and a real local hanging TCP server confirm
client coroutine/socket cancellation. Custom synchronous adapters remain a documented
compatibility limitation; remote generation/billing cannot be guaranteed cancelled.

Before each request, message and tool JSON is capped at 100,000 UTF-8 bytes. History
can renew into a protocol-valid brief; an oversized immutable brief fails before a
request is reserved. Tests cover initial oversize, non-ASCII text, renewed history,
status propagation and actual local socket closure. No paid calls were made.
Evidence: `/root/yzhu/exports/orbuz_request_budget_green_20260907.log`.

## Not delivered

No deployment, multi-task graph, dynamic Concept integration, parallel candidates,
automatic integration, pricing model/dollar cap, cgroups quotas or production
reliability claim. See RUNTIME.md and RUNTIME_PLAN.md for exact boundaries.
