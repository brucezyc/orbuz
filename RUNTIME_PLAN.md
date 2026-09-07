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

## First-slice hardening

The trial oracle uses child candidate processes and independent parent assertions;
this fixes demonstrated early-exit false positives, not every adversarial oracle.
Built-in HTTP requests receive remaining deadline/cancellation; serialized messages
plus tools have a 100KB UTF-8 boundary. Custom synchronous adapters retain documented
limits. Patch replay/freshness regression evidence and current Concept progress are
recorded in RUNTIME_VERIFICATION.md; do not infer them from old live-trial acceptance.

## Concept minimum integration (separate experimental API)

Sibling repository `../concept-decomposer` retains its legacy engine and adds
`src/concept_decomposer/runtime_plan.py`: immutable versioned snapshots, explicit
split/merge/replan, strict DAG checks, caller-authorized runtime contracts and
single-parent serial candidate chains. Dependency or plan changes invalidate
current bindings; historical runtime records remain inspectable.

Parent execution: 24 repository tests and 4 supplemental boundary cases passed
against the hardened runtime. See that repository's `PARENT_VERIFICATION.md`.
Its separate independent execution is approval-blocked, documented in
`RUNTIME_VERIFICATION.md`; not an independent green sign-off. The bridge is
in-memory and single-owner. Evidence refs are caller-supplied, not automatically
investigated or authenticated. No original-repository writes or automatic merge.

## Later stages (not included in first slice)

Automatic investigation and model-driven dynamic planning; independent Concept execution sign-off; persistent bridge recovery; multi-parent integration and parallel exploration; calibrated cost routing and same-model/same-budget multi-agent baseline evaluation; production resource quotas/cgroups and non-Linux runners. Do not report these complete based on single-task trials or scripted serial integration.
