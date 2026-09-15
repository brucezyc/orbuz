"""Threshold compaction: summarized middle, pinned survival, deterministic receipt.

Order of the compacted history is fixed:

    [leading system block] [summary] [pinned verbatim] [receipt] [recent messages]

The summary uses six fixed sections so that "what must survive" is a contract rather than
a hope. When an earlier summary exists it is fed back as an *anchor to update in place*
instead of being re-summarized (avoids summary-of-summary decay). When the summarizer's
model family differs from the family that produced the history, a one-line bridge says so.

Tool-call pairing is never split: the cutoff walks back to a boundary where every
assistant ``tool_calls`` message still has its ``tool`` results behind it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import pinning, receipts

SECTIONS = ('Intent', 'Key decisions', 'Artifacts', 'Current state', 'Next steps', 'Open questions')

SUMMARY_PROMPT = """You are a context summarization assistant. The conversation below will be \
replaced by your summary, so it must carry everything needed to continue the task.

Write the summary under these exact section headings, omitting a section only if it has no content:

## Intent
The overall goal and any standing constraints.

## Key decisions
Choices made and the reasoning, so they are not relitigated.

## Artifacts
Files, paths, identifiers, commands and APIs touched -- quote exact names.

## Current state
What is done and what is in progress right now.

## Next steps
The immediate actions still required.

## Open questions
Unresolved questions or blockers.

Focus on results, not a replay of completed actions. Respond ONLY with the summary.

<conversation>
{conversation}
</conversation>"""

INCREMENTAL_INSTRUCTION = (
    'An anchored summary from earlier compaction is provided below in <previous-summary>. '
    'Update it using the conversation above: preserve still-true details, remove stale '
    'details, and merge in new facts. Keep the same section structure.')

BRIDGE_PREFIX = 'This summary was produced by a different model than the one continuing the task.'

SUMMARY_HEADING = 'Summary of previous conversation'
DEFAULT_KEEP_RECENT = 10
DEFAULT_THRESHOLD = 0.7

_LEADING_ROLES = ('system',)


@dataclass
class CompactionResult:
    messages: list
    summarized: bool = False
    summary: str | None = None
    receipt: str | None = None
    dropped_messages: int = 0
    dropped_chars: int = 0
    strategy: str = 'SummarizingCompaction'
    dropped_keys: list = field(default_factory=list)

    @property
    def dropped_tokens_estimate(self):
        return self.dropped_chars // 4


def size_chars(messages):
    return sum(len(str(m.get('content') or '')) for m in messages)


def should_compact(messages, limit_chars, threshold=DEFAULT_THRESHOLD):
    if limit_chars <= 0:
        raise ValueError('limit_chars must be positive')
    if not 0 < threshold <= 1:
        raise ValueError('threshold must be in (0, 1]')
    return size_chars(messages) >= limit_chars * threshold


def _leading_count(messages):
    count = 0
    for message in messages:
        if message.get('role') in _LEADING_ROLES:
            count += 1
        else:
            break
    return count


def find_safe_cutoff(messages, keep_recent):
    """Index where the kept tail may start without splitting a tool-call pair."""
    if keep_recent < 0:
        raise ValueError('keep_recent must be nonnegative')
    start = max(_leading_count(messages), len(messages) - keep_recent)
    while start > 0 and messages[start].get('role') == 'tool':
        start -= 1
    while start > 0 and messages[start - 1].get('role') == 'assistant' \
            and messages[start - 1].get('tool_calls'):
        start -= 1
    return start


def summarize_prompt(messages, previous_summary=None, summarizer_model=None, history_model=None):
    conversation = '\n'.join(
        f'{m.get("role")}: {m.get("content")}' for m in messages if m.get('content'))
    parts = [SUMMARY_PROMPT.format(conversation=conversation)]
    if previous_summary:
        parts.append(INCREMENTAL_INSTRUCTION)
        parts.append(f'<previous-summary>\n{previous_summary}\n</previous-summary>')
    if summarizer_model and history_model and _family(summarizer_model) != _family(history_model):
        parts.append(BRIDGE_PREFIX)
    return '\n\n'.join(parts)


def _family(model):
    name = str(model or '')
    return name.split('/')[0].split(':')[0].strip().lower() or 'unknown'


def _safe_keep_prefix_end(messages, protect_end, leading):
    """Never let the kept prefix end in the middle of a tool-call pair."""
    while protect_end > leading:
        previous = messages[protect_end - 1]
        current = messages[protect_end] if protect_end < len(messages) else None
        if (current is not None and current.get('role') == 'tool') or \
                (previous.get('role') == 'assistant' and previous.get('tool_calls')):
            protect_end -= 1
            continue
        break
    return protect_end


def compact(messages, *, limit_chars, keep_recent=DEFAULT_KEEP_RECENT, pin_first=0,
            summarizer=None, previous_summary=None, summarizer_model=None, history_model=None,
            strategy='SummarizingCompaction', handle=None):
    """Compact *messages*. Without a summarizer this is a drop-only strategy."""
    messages = list(messages)
    leading = _leading_count(messages)
    protect_end = _safe_keep_prefix_end(
        messages, min(len(messages), leading + max(0, pin_first)), leading)
    cutoff = find_safe_cutoff(messages, keep_recent)
    if cutoff <= protect_end:
        return CompactionResult(messages=messages)
    existing_pins = pinning.collect_pinned(messages)
    middle = [m for m in messages[protect_end:cutoff] if not pinning.is_pinned(m)]
    if not middle:
        return CompactionResult(messages=messages)

    summary_text = None
    if summarizer is not None:
        summary_text = summarizer(summarize_prompt(middle, previous_summary, summarizer_model, history_model))
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise ValueError('Summarizer returned no text')
        summary_text = summary_text.strip()
        if previous_summary and _family(summarizer_model) != _family(history_model) \
                and summarizer_model and history_model:
            summary_text = f'{BRIDGE_PREFIX}\n\n{summary_text}'

    dropped_chars = size_chars(middle)
    receipt_text = receipts.format_receipt(
        dropped_messages=len(middle), dropped_tokens=dropped_chars // 4,
        by=strategy, handle=handle, has_summary=summary_text is not None)

    rebuilt = [m for m in messages[:protect_end] if not pinning.is_pinned(m)]
    if summary_text:
        rebuilt.append({'role': 'user', 'content': f'{SUMMARY_HEADING}:\n\n{summary_text}'})
    rebuilt.extend(existing_pins)
    rebuilt.append(receipts.make_receipt_message(receipt_text))
    rebuilt.extend(messages[cutoff:])
    return CompactionResult(
        messages=rebuilt, summarized=summary_text is not None, summary=summary_text,
        receipt=receipt_text, dropped_messages=len(middle), dropped_chars=dropped_chars,
        strategy=strategy, dropped_keys=[str(m.get('role')) for m in middle])
