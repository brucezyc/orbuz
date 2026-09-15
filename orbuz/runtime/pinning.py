"""Pinned content: work that every compaction strategy must preserve verbatim.

A pin is a harness-side convention, not a model instruction: the marker lives in a
private key on the message dict and is stripped before any model request
(:func:`sanitize_for_model`), so the model never sees the bookkeeping.

Strategies do not have to be careful; :func:`reinject_pinned` re-inserts anything a
compaction dropped, after the leading system/summary block, so pinned state sits near
the top of the surviving history.
"""
from __future__ import annotations

MARKER = 'longrun.pin.v1'
PRIVATE_KEY = '_longrun'


def pin(text, **extra):
    """Return a message whose content compaction must never drop."""
    if not isinstance(text, str) or not text:
        raise ValueError('Pinned content must be a nonempty string')
    meta = {'pin': MARKER}
    meta.update(extra)
    return {'role': 'user', 'content': text, PRIVATE_KEY: meta}


def is_pinned(message):
    return bool(isinstance(message, dict)
                and isinstance(message.get(PRIVATE_KEY), dict)
                and message[PRIVATE_KEY].get('pin') == MARKER)


def collect_pinned(messages):
    return [m for m in messages if is_pinned(m)]


def sanitize_for_model(messages):
    """Strip private bookkeeping so no marker key can reach a provider payload."""
    cleaned = []
    for message in messages:
        if isinstance(message, dict) and PRIVATE_KEY in message:
            message = {k: v for k, v in message.items() if k != PRIVATE_KEY}
        cleaned.append(message)
    return cleaned


def has_private_keys(messages):
    return any(isinstance(m, dict) and PRIVATE_KEY in m for m in messages)


def _leading_block_len(messages):
    """Count leading system messages plus an immediately following summary message."""
    count = 0
    for message in messages:
        role = message.get('role')
        if role == 'system':
            count += 1
            continue
        if role == 'user' and isinstance(message.get('content'), str) \
                and message.get('content', '').startswith('Summary of previous conversation'):
            count += 1
        break
    return count


def reinject_pinned(original, compacted):
    """Make the surviving history carry every pin from *original*, once, near the top.

    Pins already present in *compacted* are moved, not duplicated, so repeated calls are
    idempotent and a pin can never be dropped by a strategy that kept a stale copy.
    """
    pinned = collect_pinned(original)
    if not pinned:
        return list(compacted)
    surviving = [message for message in compacted if not is_pinned(message)]
    at = _leading_block_len(surviving)
    return [*surviving[:at], *pinned, *surviving[at:]]
