"""Bound serialized model input, including tools and UTF-8 source text."""
import json

MAX_REQUEST_BYTES = 100_000


def request_size(messages, tools):
    return len(json.dumps({'messages': messages, 'tools': tools},
                          ensure_ascii=False).encode('utf-8'))


def bounded_context(messages, tools, renew):
    if request_size(messages, tools) <= MAX_REQUEST_BYTES:
        return messages
    # Keep a protocol-valid fresh brief, never an orphaned tool result.
    fresh = renew()
    recent = messages[-1].get('content') or ''
    if recent:
        fresh.append({'role': 'user', 'content':
                      'Context renewed; source and task logs retained. Recent result: ' + recent[:6000]})
    if request_size(fresh, tools) > MAX_REQUEST_BYTES:
        raise ValueError('Serialized model request exceeds input budget')
    return fresh
