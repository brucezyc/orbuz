"""Crash-safe step journal: monotonic step state, idempotency keys, no silent replay.

State machine (monotonic; terminal states never change):

    running --complete--> complete
            --fail------> failed
            --cap-------> capped      (budget exhausted: resumable, NOT a failure)
            --unknown---> unknown     (external effect whose outcome cannot be confirmed)

A step key is ``run:name:attempt``. A retry is a *new* key, never a replay of an
existing one, so ``claim`` returning False always means "someone already did this".
``unknown`` steps are surfaced by ``resume_plan`` and never re-run automatically.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

RUNNING = 'running'
COMPLETE = 'complete'
FAILED = 'failed'
CAPPED = 'capped'
UNKNOWN = 'unknown'

TERMINAL = (COMPLETE, FAILED, CAPPED, UNKNOWN)

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
    key         TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    step_index  INTEGER,
    status      TEXT NOT NULL,
    started_at  REAL,
    finished_at REAL,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id, step_index, started_at);
"""


class Journal:
    """Step state for one state directory. One table, one connection at a time."""

    def __init__(self, state_dir, filename='journal.sqlite3'):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / filename
        with self.connect() as db:
            db.executescript(SCHEMA)

    @classmethod
    def in_database(cls, db_path):
        """Journal living in an existing database file (one storage, many tables)."""
        journal = cls.__new__(cls)
        journal.root = Path(db_path).resolve().parent
        journal.path = Path(db_path).resolve()
        with journal.connect() as db:
            db.executescript(SCHEMA)
        return journal

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def key(run_id, name, attempt=1):
        if not run_id or not name or attempt < 1:
            raise ValueError('Invalid step key parts')
        return f'{run_id}:{name}:{attempt}'

    def claim(self, run_id, name, attempt=1, step_index=None):
        """Atomically start a step. False means it was already claimed or finished."""
        key = self.key(run_id, name, attempt)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM steps WHERE key=?', (key,)).fetchone()
            if row is not None:
                db.execute('COMMIT')
                return False
            db.execute('INSERT INTO steps(key,run_id,name,attempt,step_index,status,started_at)'
                       ' VALUES (?,?,?,?,?,?,?)',
                       (key, run_id, name, attempt, step_index, RUNNING, time.time()))
            db.execute('COMMIT')
            return True

    def _finish(self, key, status, result=None, error=None):
        if status not in TERMINAL:
            raise ValueError('Not a terminal step status')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM steps WHERE key=?', (key,)).fetchone()
            if row is None:
                db.execute('COMMIT')
                raise ValueError('Unknown step')
            if row[0] in TERMINAL:
                db.execute('COMMIT')
                raise ValueError(f'Step already {row[0]}')
            db.execute('UPDATE steps SET status=?, finished_at=?, result=?, error=? WHERE key=?',
                       (status, time.time(), result, error, key))
            db.execute('COMMIT')

    def complete(self, run_id, name, result=None, attempt=1):
        self._finish(self.key(run_id, name, attempt), COMPLETE, result=result)

    def fail(self, run_id, name, error, attempt=1):
        self._finish(self.key(run_id, name, attempt), FAILED, error=str(error))

    def cap(self, run_id, name, reason, attempt=1):
        """Budget exhausted mid-step: resumable, not a failure."""
        self._finish(self.key(run_id, name, attempt), CAPPED, error=str(reason))

    def mark_unknown(self, run_id, name, note, attempt=1):
        """An external effect happened but its outcome cannot be confirmed."""
        self._finish(self.key(run_id, name, attempt), UNKNOWN, error=str(note))

    def get(self, run_id, name, attempt=1):
        with self.connect() as db:
            row = db.execute('SELECT key,status,started_at,finished_at,result,error FROM steps'
                             ' WHERE key=?', (self.key(run_id, name, attempt),)).fetchone()
        if row is None:
            return None
        return dict(zip(('key', 'status', 'started_at', 'finished_at', 'result', 'error'), row))

    def steps(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT key,name,attempt,step_index,status,started_at,finished_at,'
                              'result,error FROM steps WHERE run_id=? ORDER BY started_at', (run_id,)).fetchall()
        return [dict(zip(('key', 'name', 'attempt', 'step_index', 'status', 'started_at',
                          'finished_at', 'result', 'error'), row)) for row in rows]

    def resume_plan(self, run_id):
        """What may run next. Unknown steps are reported, never auto-replayed."""
        buckets = {COMPLETE: [], FAILED: [], CAPPED: [], UNKNOWN: [], RUNNING: []}
        for step in self.steps(run_id):
            buckets.setdefault(step['status'], []).append(step['key'])
        return {'done': buckets[COMPLETE], 'retryable': buckets[FAILED], 'capped': buckets[CAPPED],
                'unknown': buckets[UNKNOWN], 'abandoned': buckets[RUNNING],
                'safe_to_continue': not buckets[UNKNOWN] and not buckets[RUNNING]}

    def delete_run(self, run_id):
        with self.connect() as db:
            return db.execute('DELETE FROM steps WHERE run_id=?', (run_id,)).rowcount

    def prune(self, older_than_seconds, now=None):
        """Drop finished steps older than a window. Running steps are never pruned."""
        cutoff = (now if now is not None else time.time()) - older_than_seconds
        with self.connect() as db:
            return db.execute('DELETE FROM steps WHERE status!=? AND finished_at IS NOT NULL'
                              ' AND finished_at<?', (RUNNING, cutoff)).rowcount
