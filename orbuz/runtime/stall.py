"""Stall detection: three questions, counted, with a bounded response.

Every ``interval`` steps the loop asks the model three booleans about the run:
is the request satisfied, is progress being made, is it going in circles. The answers are
*not* a completion signal -- acceptance is still the runtime's job -- they only decide
whether to re-plan or stop spending.

Unproductive answers accumulate: no auto-reset, because silently forgetting a stall is
how a loop spins until the budget dies. ``max_stalls`` consecutive unproductive answers
stop the run; one short of that, the caller is told to re-plan.
"""
from __future__ import annotations

import json

QUESTIONS = ('is_request_satisfied', 'is_progress_being_made', 'is_in_loop')

LEDGER_PROMPT = (
    'Assess the run so far. Reply with a single JSON object and nothing else, with exactly '
    'these boolean fields:\n'
    '{"is_request_satisfied": bool, "is_progress_being_made": bool, "is_in_loop": bool}\n'
    'is_request_satisfied: the stated goal is achieved and verified.\n'
    'is_progress_being_made: the last steps advanced the goal.\n'
    'is_in_loop: the same actions or conclusions are repeating.\n'
    'This assessment is advisory: only the runtime acceptance check decides success.')


def parse_ledger(raw):
    """Parse the ledger reply. Missing or non-boolean fields are an error, not a guess."""
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw or '').strip()
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            raise ValueError('Ledger reply is not a JSON object')
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f'Ledger reply is not valid JSON: {exc}') from None
    missing = [q for q in QUESTIONS if q not in data]
    if missing:
        raise ValueError('Ledger reply missing fields: ' + ', '.join(missing))
    parsed = {}
    for question in QUESTIONS:
        value = data[question]
        if not isinstance(value, bool):
            raise ValueError(f'Ledger field {question} must be a boolean')
        parsed[question] = value
    return parsed


class StallDetector:
    def __init__(self, max_stalls=3, interval=5):
        if type(max_stalls) is not int or max_stalls < 1:
            raise ValueError('max_stalls must be a positive int')
        if type(interval) is not int or interval < 1:
            raise ValueError('interval must be a positive int')
        self.max_stalls = max_stalls
        self.interval = interval
        self.stalls = 0
        self.asked = 0
        self.history = []

    def due(self, step_index):
        return step_index > 0 and step_index % self.interval == 0

    def observe(self, ledger):
        """Return 'done' | 'continue' | 'replan' | 'stop'."""
        if not isinstance(ledger, dict) or set(ledger) != set(QUESTIONS):
            raise ValueError('Ledger must carry exactly the three questions')
        self.asked += 1
        self.history.append(dict(ledger))
        if ledger['is_request_satisfied']:
            return 'done'
        if not ledger['is_progress_being_made'] or ledger['is_in_loop']:
            self.stalls += 1
        if self.stalls >= self.max_stalls:
            return 'stop'
        if self.stalls >= self.max_stalls - 1:
            return 'replan'
        return 'continue'

    def snapshot(self):
        return {'asked': self.asked, 'stalls': self.stalls, 'max_stalls': self.max_stalls,
                'interval': self.interval, 'history': self.history}
