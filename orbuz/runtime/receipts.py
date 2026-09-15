"""Compaction receipts: a deterministic note saying "your memory here is secondhand".

After a strategy crosses a compaction boundary, the surviving history no longer covers
everything that happened. The receipt states that plainly, names the strategy and the
size of what is gone, and - when a transcript handle is available - points at the
persisted run so the model can re-read instead of confabulating.

No timestamp is included: the text is a pure function of its arguments, so its bytes
are deterministic and testable.
"""
from __future__ import annotations

MARKER = 'longrun.receipt.v1'
PREFIX = '[History before this point'
PRIVATE_KEY = '_longrun'


def format_receipt(*, dropped_messages, dropped_tokens, by, handle=None, has_summary=True):
    """Render the standard receipt. Deterministic: same inputs, same bytes."""
    for name, value in (('dropped_messages', dropped_messages), ('dropped_tokens', dropped_tokens)):
        if type(value) is not int or value < 0:
            raise ValueError(f'{name} must be a nonnegative int')
    if not by or not isinstance(by, str):
        raise ValueError('A strategy name is required')
    if has_summary:
        core = (f'was summarized by {by}. The summary above is secondhand; '
                're-verify critical facts against primary sources.')
    else:
        core = (f'was dropped by {by}. That context is no longer in the window; '
                're-verify critical facts against primary sources.')
    transcript = f' Persisted run handle: {handle}.' if handle else ''
    return f'{PREFIX} ({dropped_messages} messages, ~{dropped_tokens} tokens) {core}{transcript}]'


def make_receipt_message(text):
    return {'role': 'user', 'content': text, PRIVATE_KEY: {'receipt': MARKER}}


def is_receipt(message):
    if not isinstance(message, dict):
        return False
    meta = message.get(PRIVATE_KEY)
    if isinstance(meta, dict) and meta.get('receipt') == MARKER:
        return True
    return isinstance(message.get('content'), str) and message['content'].startswith(PREFIX)


def count_receipts(messages):
    return sum(1 for m in messages if is_receipt(m))


def dedupe_receipts(messages):
    """Keep only the most recent receipt: older boundaries are implied by it."""
    seen = False
    out = []
    for message in reversed(messages):
        if is_receipt(message):
            if seen:
                continue
            seen = True
        out.append(message)
    return list(reversed(out))
