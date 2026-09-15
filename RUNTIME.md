# Evidence runtime (experimental)

This is a new execution core alongside the preserved legacy `orbuz run` workflow.
It does **not** route through the legacy orchestrator/executor.

There is one loop, and it is a long-horizon one. A task is a durable state machine whose
state is persisted after every step, so one small task and a multi-hundred-step project run
are the same code path: `run` executes steps until the task is accepted, refused, capped or
stuck, and calling `run` again continues from where it stopped. There is no separate
"long task" mode, no opt-in hooks and no second engine to keep in sync.

## What runs now

1. Create an immutable task contract from a clean, committed local Git repository.
2. Allocate a detached worktree outside the project. Original project files stay unchanged.
3. Give the model the goal, exact writable files, selected source excerpts, acceptance
   command and previous attempt evidence. It can investigate more files with tools.
4. Run a real native tool-call loop: list/read/write source, sandboxed commands,
   submit candidate or report blocked. Plain completion text never marks success.
5. On submit, commit the candidate and run the fixed acceptance argv, plus the hidden
   held-out suite when the contract declares one. Save output, exit code,
   timeout/cancellation, revision, contract hash, environment fingerprint, log hash,
   patch hash, base revision and byte-exact candidate patch. Accept only after an actual
   successful check. Empty diffs produce zero-byte no-op patches.
6. Persist status, attempt history **and a per-step journal** in SQLite. Explicit retry
   starts a new candidate from the original base, includes previous failure evidence and
   retains cumulative call/time budgets. No command replay and no automatic
   merge/push/deploy.

## One loop, durable at every step

Statuses a run can end (or pause) in:

- `accepted` / `rejected` / `stale` — evidence-backed outcomes, only `accepted` is success.
- `hacking_suspected` — the visible acceptance passed but the hidden held-out suite failed.
- `blocked` / `failed` / `cancelled` — model-reported blocker, runtime error, cancellation.
- `exhausted` — a **hard** contract limit (`max_calls`, `max_seconds`) was reached.
- `capped` — a **soft** limit (`limits.max_steps|max_tokens|max_usd`) was reached. This is a
  pause, not a failure: calling `run` again continues in the same worktree.
- `stalled` — the same tool round repeated `max_stalls` times, or the progress ledger
  reported no change of approach.
- `interrupted` — a previous process died; the first `run` after that only reconciles state.

Journal and resume rules:

- Every model request and every tool call is a journal step with a single owner. A step
  claimed twice is refused, a completed step is never replayed.
- A process that dies mid-tool leaves that step `running`; reconciliation turns **tool**
  steps into `unknown` (the external effect is unverifiable) and **model** steps into
  `failed` (no side effect, spend already charged).
- `run` refuses to continue or restart while an `unknown`/abandoned step exists; the
  operator must pass `retry=True` (CLI `retry`) to accept the unaccounted effect explicitly.
- Resuming reuses the existing worktree and transcript, so work already done on disk is
  still there and no completed tool call is repeated.
- `limits.max_steps` is a **rate** (fresh allowance per `run` call) while tokens/dollars are
  a **stock** (cumulative across resumes). Without that split a resumed run would cap again
  instantly and "resume" would be a fiction.

Context management is part of the loop, not an option:

- Above 70% of the 100,000-byte request budget the middle of the transcript is compacted:
  leading system block and `pin_first` opening turns stay, pinned messages survive verbatim,
  a deterministic receipt records what is gone, the recent tail is kept, and tool-call pairs
  are never split.
- With a summarizer available (the built-in `ChatModel` provides one) the middle is replaced
  by a six-section summary; an existing summary is updated in place as an anchor instead of
  being re-summarized, and a cross-model bridge line is added when the summarizer family
  differs from the history family.
- `pin_*` bookkeeping lives in private message keys that are stripped before every request,
  so no marker can reach a provider payload.
- The prefix that must stay byte-stable for provider cache hits is fingerprinted every
  step; changes are reported instead of silently paying full price.
- `Runtime.pin(task_id, text)` pins work that survival-of-context must not lose. Pins are
  re-injected after the leading block and are idempotent.


## Prerequisites and isolation

Linux, Git, Python >=3.10, package dependencies and `/usr/bin/bwrap` (bubblewrap).
The kernel must permit user/mount/PID/network namespaces. Sandbox startup failure
is a failure, **never** fallback execution on the host.

Command tools and acceptance both see read-only `/usr`, system binary/library links,
read-only `/workspace`, isolated `/proc` and `/dev`, writable ephemeral `/tmp`.
No host home, credentials, inherited API-key environment or network are exposed.
`.git` entries are masked. All source edits go through the host-side exact-file
allowlist, which rejects `..`, absolute paths and symlinks. Checks cannot rewrite
source or tests. Processes are killed on timeout/cancel/output overflow, with PID
namespaces handling detached descendants. Output is capped at 4 MiB per command;
the model sees the last 6000 characters and a log reference. `read_log` retrieves
paged full logs from this task (including older attempts), never arbitrary host logs.

This is **not a complete hostile-code platform**: CPU/memory/process quotas via
cgroups, dependency installation, and non-Linux executors are not implemented.
Only run trusted project fixtures/code. A Git worktree alone is not a sandbox.
System libraries are available, but project virtualenvs outside the workspace are
not mounted. Compilation should write outputs to `/tmp`.

## CLI

```bash
# From this repository; equivalent after editable package installation.
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state \
  create /absolute/path/task.json

# Export the chosen API key without putting it into task.json or argv.
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state \
  run TASK_ID --model deepseek-v4-flash --base-url https://api.deepseek.com

python -m orbuz.runtime --state-dir /absolute/path/outside/project/state status TASK_ID
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state verify TASK_ID
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state cancel TASK_ID
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state steps TASK_ID
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state \
  pin TASK_ID "constraint that must survive compaction"
python -m orbuz.runtime --state-dir /absolute/path/outside/project/state \
  retry TASK_ID --model deepseek-v4-flash --base-url https://api.deepseek.com
```

`status` also prints the step journal (`steps` shows the journal plus the resume plan, and
refuses to hide `unknown` steps). `run` continues a `capped`, `stalled` or `interrupted`
task in place; `retry` starts a fresh attempt and is the only way past an unresolved step.

`--key-env` selects a credential environment variable (default `DEEPSEEK_API_KEY`).
Model and HTTPS endpoint are explicit; there is no implicit mock or alternate-provider
fallback. Runtime `run/retry/verify` return nonzero unless accepted. `status` is an
inspection operation; it does not itself rerun checks. `verify` checks saved-evidence
freshness, **not** behavioral re-execution. A cancelled task cannot be retried;
create a new authorized task instead.

Repeated `run` also checks accepted-evidence freshness before returning, without
spending another model call. It checks patch bytes/hash against base→candidate Git
diff and ancestry, original contract hash, candidate HEAD/source, log and environment.
Old records without patch hash/base fields become `stale`; `status` still shows
the stored historical status until a freshness check. A zero-byte patch is applied
as a no-op (`git apply --allow-empty`); other patches use normal `git apply`.

Example contract (repository must already exist and be committed):

```json
{
  "goal": "Make answer() return 42 without changing the acceptance test",
  "repository": "/absolute/path/to/project",
  "writable": ["answer.py"],
  "context": ["answer.py", "check.py"],
  "acceptance": ["/usr/bin/python3", "-B", "check.py"],
  "heldout": ["/usr/bin/python3", "-B", "/heldout/check_all.py"],
  "heldout_assets": ["/absolute/path/to/hidden/dir"],
  "max_calls": 8,
  "max_output_tokens": 2048,
  "timeout": 10,
  "max_seconds": 180,
  "limits": {"max_steps": 30, "max_tokens": 4000000, "max_usd": 2.0,
             "keep_recent": 10, "pin_first": 2},
  "prices": {"input": 0.28, "output": 0.42}
}
```

`heldout` is a second argv run after a visible pass, with `heldout_assets` (one directory,
outside the repository) mounted read-only at `/heldout`. A visible pass plus a held-out
failure yields `hacking_suspected`, never `accepted`. `limits` are soft caps that end in
`capped`; `keep_recent`/`pin_first` tune compaction. `prices` is optional and only used when
supplied: no price is ever guessed, so a `max_usd` cap cannot fire without a price table.

`max_calls` counts requests, including failed/reserved interrupted requests;
`max_output_tokens` bounds each requested model completion. API token usage is
recorded as reported; monetary pricing is **not guessed** and no dollar-cap guarantee
is claimed. The built-in ChatModel uses a cancellable async HTTP coroutine with the
remaining task deadline; cancellation aborts local transport, not just returned actions.
This cannot guarantee the remote provider stops generation or billing. Trusted custom
adapters exposing only `complete` retain synchronous compatibility and may delay
cancellation; implement `complete_bounded` for runtime deadline/cancel propagation.
Serialized messages plus tool schemas are capped at 100,000 UTF-8 bytes before every
model request (not a token/dollar cap). Oversized history renews a protocol-valid brief;
if the contract/brief alone exceeds the cap, the task fails before reserving a request.

## Evidence semantics and limits

- Accepted means **the explicit acceptance command passed on that candidate**, not
  a proof that every possible user requirement was met. Test quality remains vital.
- The model cannot mark a task accepted or directly rewrite SQLite/check results.
- Commands cannot write source; the acceptance entry-point path cannot be allowlisted
  as writable. The contract author must also protect helper/oracle dependencies.
- An arbitrary acceptance command can be flawed: importing candidate code in the
  assertion-owning interpreter allows `os._exit(0)` to skip checks. The summary trial
  now imports it in child processes; the protected parent validates returned JSON,
  types, expected values and nonmutation, rejecting missing/forged PASS text. Parent
  process memory/fd access is disabled (`PR_SET_DUMPABLE=0`); killing the parent fails
  acceptance. This fixture-specific oracle is not a generic hostile-code proof:
  candidates can see its source and can fake child responses or hardcode examples.
  The runtime cannot make an arbitrary weak oracle sound; independent held-out
  checks and oracle review remain necessary.
- Source/HEAD/contract/log/environment changes invalidate saved acceptance.
- Environment fingerprint covers platform, selected executable hashes and sandbox
  policy, not every system library. This is not a hermetic reproducible-build proof.
- Persistent state is trusted user-owned storage, not a tamper-resistant audit service.
- Retry preserves artifacts from previous attempts; it does not silently apply a
  rejected patch to the next attempt. Interrupted candidates remain inspectable.
- State can grow with logs/worktrees; automatic garbage collection is not implemented.

## Verification and live trial

```bash
python -m pytest tests/test_run_compatibility.py tests/test_evidence_runtime.py \
  tests/test_runtime_recovery.py tests/test_runtime_sandbox.py -q

# Opt-in, uses real API quota, creates only a disposable fixture:
python examples/evidence_runtime_trial.py --live --state-dir /new/absolute/trial/path
```

Tests use scripted model responses for deterministic protocol/fault injection and
real Git/subprocess/sandbox execution. They are not evidence of model competence.
The opt-in live trial gives a real model an unimplemented task-summary function and
an independently authored behavioral oracle; no implementation is supplied by the
runner. It is a small integration trial, not proof of large-project autonomy or
multi-agent efficiency.

## Remaining architecture work

See [RUNTIME_PLAN.md](RUNTIME_PLAN.md). The sibling Concept Decomposer now provides
an experimental versioned-plan Python API with explicit split/merge/replan and
single-parent serial candidate chaining; parent-run offline integration tests pass.
Its independent reviewer execution remains approval-blocked. Automatic investigation,
LLM-driven planning, multi-parent integration, parallel candidates and same-model/
same-budget comparison remain pending. No production deployment.
