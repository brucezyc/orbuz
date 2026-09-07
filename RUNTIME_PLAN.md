# Evidence runtime implementation

Status: first executable slice implemented and exercised; legacy runtime is preserved, not repaired.
Actual results: [RUNTIME_VERIFICATION.md](RUNTIME_VERIFICATION.md).

## Accepted direction

Goals/context/attempts/evidence replace role/stage-driven execution. Model proposes actions; deterministic runtime owns isolation, limits and acceptance. Task decomposition follows acquisition of facts and is revisable, not a fixed full-project DAG. Artifacts and source versions, not conversation summaries, are authoritative.

## First executable slice

- Explicit task contract: goal, committed repository, relative writable files, relevant context files, immutable acceptance argv, bounded calls/output/time.
- SQLite task/attempt/evidence state outside target repository; Git detached worktrees isolate candidates. No automatic merge/push/deploy.
- Fresh working context: contract + source references + previous failure evidence. Tools: list/read/write files, sandboxed command execution, submit candidate, report blocked.
- Linux bubblewrap: read-only candidate source; only scratch tmp writable in commands; no network, host credentials or host home. Host file tools enforce exact write scope and reject symlink/path traversal. Unsupported sandbox means fail closed.
- Submission creates a candidate commit; runtime executes unchanged acceptance in sandbox, saves actual output and binds evidence to revision, contract and environment. Status never follows model completion text.
- Cancellation, finite budgets, interrupted-attempt reconciliation and explicit retry. No blind replay of external effects; external-effect tools do not exist in this slice.
- CLI via `python -m orbuz.runtime`; JSON task input initially, plus run/status/retry/cancel/verify commands. User-supplied acceptance is required for now; model-authored acceptance would not be an independent oracle.

## Verification

First red/green test of false-success rejection; real temporary Git repositories and subprocess checks. Regression: failing check, candidate modification invalidates evidence, protected file/path escape denied, timeout/cancel, no-key/API failure, tool budget exhaustion, persisted restart. Real model trial only inside a disposable fixture, limited calls/output/time; no production code changes and no fabricated model responses. Test doubles are clearly identified as deterministic runtime tests.

## Later stages (not included in first slice)

Dynamic Concept Decomposer (investigate/split/merge/replan with versioned contracts); dependency invalidation and integration across multiple tasks; parallel exploration; calibrated cost routing and same-model/same-budget multi-agent baseline evaluation; production resource quotas/cgroups and non-Linux runners. Do not report these complete based on a single-task trial.
