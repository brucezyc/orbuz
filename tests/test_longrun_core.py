"""Unit tests for the long-horizon core: journal, pinning, receipts, context, budget, stall.

These are pure-mechanism tests (no Git, no sandbox). The loop-level behaviour is in
tests/test_longrun_loop.py.
"""
import pytest

from orbuz.runtime import accept as acceptance
from orbuz.runtime import context as ctx
from orbuz.runtime import pinning, receipts
from orbuz.runtime.budget import DEGRADE_ORDER, Budget, Limits, PrefixGuard
from orbuz.runtime.journal import CAPPED, Journal
from orbuz.runtime.stall import QUESTIONS, StallDetector, parse_ledger


# ---------- journal ----------

def test_journal_claim_is_single_owner(tmp_path):
    j = Journal(tmp_path / 'state')
    assert j.claim('run', 'step.a') is True
    assert j.claim('run', 'step.a') is False       # already claimed: no replay
    j.complete('run', 'step.a')
    assert j.claim('run', 'step.a') is False       # finished: still no replay
    with pytest.raises(ValueError, match='already complete'):
        j.complete('run', 'step.a')
    assert j.get('run', 'step.a')['status'] == 'complete'


def test_journal_terminal_states_and_resume_plan(tmp_path):
    j = Journal(tmp_path / 'state')
    j.claim('run', 'ok'); j.complete('run', 'ok')
    j.claim('run', 'bad'); j.fail('run', 'bad', 'boom')
    j.claim('run', 'capped'); j.cap('run', 'capped', 'budget')
    j.claim('run', 'unknown')
    plan = j.resume_plan('run')
    assert plan['done'] == ['run:ok:1']
    assert plan['retryable'] == ['run:bad:1']
    assert plan['capped'] == ['run:capped:1']
    assert plan['abandoned'] == ['run:unknown:1']
    assert plan['safe_to_continue'] is False       # an unresolved step blocks resume
    j.mark_unknown('run', 'unknown', 'effect unconfirmed')
    plan = j.resume_plan('run')
    assert plan['unknown'] == ['run:unknown:1'] and plan['abandoned'] == []
    assert plan['safe_to_continue'] is False


def test_journal_rejects_unknown_step_and_replayed_completion(tmp_path):
    j = Journal(tmp_path / 'state')
    with pytest.raises(ValueError, match='Unknown step'):
        j.complete('run', 'never-claimed')
    with pytest.raises(ValueError, match='Invalid step key'):
        j.claim('', 'x')


def test_journal_prune_and_delete_keep_live_steps(tmp_path):
    import time as _time
    j = Journal(tmp_path / 'state')
    j.claim('run', 'old'); j.complete('run', 'old')
    j.claim('run', 'live')
    assert j.prune(older_than_seconds=0, now=_time.time() + 60) == 1
    assert j.get('run', 'live')['status'] == 'running'
    assert j.delete_run('run') == 1
    assert j.steps('run') == []


def test_journal_shares_one_database_file(tmp_path):
    from orbuz.runtime.store import Store
    store = Store(tmp_path / 'state')
    j = Journal.in_database(store.path)
    j.claim('run', 'step')
    assert j.path == store.path
    assert j.get('run', 'step')['status'] == 'running'


# ---------- pinning ----------

def test_pin_survives_compaction_by_reinjection():
    messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'old work'}]
    pinned = pinning.pin('writable scope: answer.py, never touch tests')
    messages.append(pinned)
    compacted = [{'role': 'system', 'content': 'sys'},
                 {'role': 'user', 'content': 'Summary of previous conversation:\n\n## Intent\nx'}]
    rebuilt = pinning.reinject_pinned(messages, compacted)
    assert rebuilt[2] == pinned                      # placed after the leading block
    assert pinning.reinject_pinned(messages, rebuilt) == rebuilt    # idempotent


def test_private_markers_never_reach_the_model():
    messages = [pinning.pin('keep me'), receipts.make_receipt_message('[History before this point (1 messages, ~2 tokens) x]')]
    assert pinning.has_private_keys(messages)
    cleaned = pinning.sanitize_for_model(messages)
    assert not pinning.has_private_keys(cleaned)
    assert cleaned[0]['content'] == 'keep me'


# ---------- receipts ----------

def test_receipt_is_deterministic_and_truthful():
    first = receipts.format_receipt(dropped_messages=3, dropped_tokens=120, by='S', handle='run-1')
    second = receipts.format_receipt(dropped_messages=3, dropped_tokens=120, by='S', handle='run-1')
    assert first == second                            # no timestamp, byte-stable
    assert 'summarized by S' in first and 'run-1' in first
    dropped = receipts.format_receipt(dropped_messages=3, dropped_tokens=120, by='S', has_summary=False)
    assert 'dropped by S' in dropped and 'Persisted run handle' not in dropped


def test_receipt_validation_and_dedupe():
    with pytest.raises(ValueError):
        receipts.format_receipt(dropped_messages=-1, dropped_tokens=0, by='S')
    messages = [receipts.make_receipt_message('[History before this point (1 messages, ~1 tokens) a]'),
                {'role': 'user', 'content': 'work'},
                receipts.make_receipt_message('[History before this point (2 messages, ~2 tokens) b]')]
    assert receipts.count_receipts(messages) == 2
    deduped = receipts.dedupe_receipts(messages)
    assert receipts.count_receipts(deduped) == 1
    assert deduped[-1]['content'].endswith('b]')


# ---------- context ----------

def conversation(size=40, words=200):
    messages = [{'role': 'system', 'content': 'sys'}]
    for index in range(size):
        messages.append({'role': 'user', 'content': f'turn {index} ' + 'x' * words})
        messages.append({'role': 'assistant', 'content': f'ack {index}'})
    return messages


def test_threshold_and_drop_only_receipt():
    messages = conversation()
    assert ctx.should_compact(messages, 10_000) is True
    assert ctx.should_compact([{'role': 'user', 'content': 'tiny'}], 10_000) is False
    result = ctx.compact(messages, limit_chars=10_000, keep_recent=4)
    assert result.summarized is False
    assert result.dropped_messages > 0
    assert result.receipt.startswith(receipts.PREFIX) and 'dropped by' in result.receipt
    assert receipts.count_receipts(result.messages) == 1
    assert result.messages[0] == messages[0]
    assert any('turn 39' in (m.get('content') or '') for m in result.messages)   # recent tail kept


def test_pin_first_and_explicit_pins_are_kept():
    messages = conversation()
    pinned = pinning.pin('load bearing fact')
    messages.insert(3, pinned)
    result = ctx.compact(messages, limit_chars=10_000, keep_recent=2, pin_first=2)
    contents = [m.get('content') for m in result.messages]
    assert messages[1]['content'] in contents      # pin_first keeps the opening turns
    assert messages[2]['content'] in contents
    assert any(m == pinned for m in result.messages)


def test_summarizer_gets_sections_and_anchor_and_bridge():
    prompts = []

    def summarizer(prompt):
        prompts.append(prompt)
        return '## Intent\nship it'

    messages = conversation()
    first = ctx.compact(messages, limit_chars=10_000, keep_recent=2, summarizer=summarizer)
    assert first.summarized is True
    for section in ctx.SECTIONS:
        assert f'## {section}' in prompts[0]
    ctx.compact(messages, limit_chars=10_000, keep_recent=2, summarizer=summarizer,
                previous_summary=first.summary)
    assert ctx.INCREMENTAL_INSTRUCTION in prompts[1]
    ctx.compact(messages, limit_chars=10_000, keep_recent=2, summarizer=summarizer,
                summarizer_model='anthropic/claude', history_model='deepseek/v4')
    assert ctx.BRIDGE_PREFIX in prompts[2]
    assert first.messages[1]['content'].startswith(ctx.SUMMARY_HEADING)


def test_summarizer_failure_is_loud():
    with pytest.raises(ValueError, match='no text'):
        ctx.compact(conversation(), limit_chars=10_000, keep_recent=2, summarizer=lambda p: '   ')


def test_tool_call_pairs_are_never_split():
    messages = [{'role': 'system', 'content': 'sys'}]
    messages += [{'role': 'user', 'content': 'x' * 500} for _ in range(8)]
    messages.append({'role': 'assistant', 'content': None,
                     'tool_calls': [{'id': '1', 'function': {'name': 'command', 'arguments': '{}'}}]})
    messages.append({'role': 'tool', 'tool_call_id': '1', 'content': 'result'})
    cutoff = ctx.find_safe_cutoff(messages, keep_recent=1)
    assert messages[cutoff]['role'] != 'tool'
    assert not (messages[cutoff - 1].get('role') == 'assistant' and messages[cutoff - 1].get('tool_calls'))


# ---------- budget ----------

def test_soft_budget_caps_and_degrades_once():
    limits = Limits(max_steps=4)
    budget = Budget(limits)
    assert budget.check().action == 'continue'
    budget.steps = 2
    decision = budget.check()
    assert decision.action == 'degrade' and decision.degrade_step == DEGRADE_ORDER[0]
    budget.take(decision.degrade_step)
    budget.calls, budget.steps = 1, 3
    assert budget.check().degrade_step == DEGRADE_ORDER[1]
    budget.steps = 4
    assert budget.check().action == 'cap'
    with pytest.raises(ValueError, match='already applied'):
        budget.take(DEGRADE_ORDER[0])


def test_no_soft_limits_never_caps_and_money_is_not_guessed():
    budget = Budget(Limits())
    budget.steps = budget.calls = 10_000
    assert budget.check().action == 'continue'
    budget.observe({'prompt_tokens': 1000, 'completion_tokens': 10,
                    'prompt_cache_hit_tokens': 900, 'prompt_cache_miss_tokens': 100})
    assert budget.usd is None                       # no prices supplied: no invented cost
    assert budget.cache_hit_ratio == 0.9
    priced = Budget(Limits(max_usd=1.0), prices={'input': 1.0, 'output': 2.0})
    priced.observe({'prompt_tokens': 1_000_000, 'completion_tokens': 0})
    assert priced.usd == 1.0 and priced.check().action == 'cap'


def test_prefix_guard_flags_only_real_changes():
    guard = PrefixGuard()
    tools = [{'function': {'name': 'read_file'}}]
    system = {'role': 'system', 'content': 'frozen prefix'}
    brief = {'role': 'user', 'content': '{"goal": "x"}'}
    first = guard.observe([system, brief], tools)
    assert first['stable'] is True and first['prefix_chars'] > 0
    # A growing transcript is append-only: it must never be reported as a cache bust.
    grown = guard.observe([system, brief, {'role': 'user', 'content': 'work'},
                           {'role': 'assistant', 'content': 'ack'}], tools)
    assert grown['stable'] is True and guard.busts == []
    # A trailing volatile note is expected to change every step and is not part of the prefix.
    noted = guard.observe([system, brief, {'role': 'user', 'content': 'Situation: {"calls": 3}'}], tools)
    assert noted['stable'] is True and guard.busts == []
    changed = guard.observe([system, {'role': 'user', 'content': '{"goal": "x", "ts": 1}'}], tools)
    assert changed['stable'] is False and changed['bust'] == 1 and len(guard.busts) == 1
    retooled = guard.observe([system, brief], [{'function': {'name': 'command'}}])
    assert retooled['stable'] is False and len(guard.busts) == 2


# ---------- stall ----------

def test_ledger_parsing_is_strict():
    ledger = parse_ledger('{"is_request_satisfied": false, "is_progress_being_made": true, "is_in_loop": false}')
    assert ledger == {'is_request_satisfied': False, 'is_progress_being_made': True, 'is_in_loop': False}
    with pytest.raises(ValueError, match='missing fields'):
        parse_ledger('{"is_request_satisfied": true}')
    with pytest.raises(ValueError, match='must be a boolean'):
        parse_ledger({'is_request_satisfied': 'yes', 'is_progress_being_made': True, 'is_in_loop': False})
    with pytest.raises(ValueError):
        parse_ledger('not json at all')


def test_stall_escalates_then_stops():
    detector = StallDetector(max_stalls=3, interval=5)
    assert detector.due(5) is True and detector.due(4) is False
    stuck = {'is_request_satisfied': False, 'is_progress_being_made': False, 'is_in_loop': True}
    assert detector.observe(stuck) == 'continue'
    assert detector.observe(stuck) == 'replan'
    assert detector.observe(stuck) == 'stop'
    satisfied = {'is_request_satisfied': True, 'is_progress_being_made': True, 'is_in_loop': False}
    assert StallDetector().observe(satisfied) == 'done'


# ---------- acceptance helpers ----------

def test_handle_resolution_is_fail_loud():
    assert acceptance.resolve_handle(' run-1 ') == 'run-1'
    with pytest.raises(acceptance.MissingHandle):
        acceptance.resolve_handle(None)
    with pytest.raises(acceptance.MissingHandle):
        acceptance.resolve_handle('   ')
    assert acceptance.resolve_handle(None, strict=False) is None


def test_verdict_matrix():
    ok = {'exit_code': 0, 'timed_out': False, 'cancelled': False}
    bad = {'exit_code': 1, 'timed_out': False, 'cancelled': False}
    timeout = {'exit_code': None, 'timed_out': True, 'cancelled': False}
    assert acceptance.verdict(ok) == 'accepted'
    assert acceptance.verdict(ok, ok) == 'accepted'
    assert acceptance.verdict(ok, bad) == acceptance.HACKING       # visible passed, held-out failed
    assert acceptance.verdict(bad, ok) == 'rejected'
    assert acceptance.verdict(timeout, ok) == 'rejected'
    assert acceptance.verdict(ok, timeout) == acceptance.HACKING


def test_heldout_assets_must_live_outside_the_workspace(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    inside = workspace / 'hidden.py'
    inside.write_text('x')
    with pytest.raises(ValueError, match='outside the candidate workspace'):
        acceptance.assets_outside_workspace(workspace, [inside])
    outside = tmp_path / 'heldout'
    outside.mkdir()
    assert acceptance.assets_outside_workspace(workspace, [outside]) == [str(outside)]
    with pytest.raises(ValueError, match='nonempty'):
        acceptance.validate_argv([])
    with pytest.raises(ValueError, match='absolute'):
        acceptance.validate_argv(['python3', 'x.py'])
