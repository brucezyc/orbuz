"""Persistent task state; runtime locks serialize attempts, not cancel requests."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'runtime.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def create(self, task):
        with self.connect() as db:
            db.execute('INSERT INTO tasks(id,body) VALUES (?,?)',
                       (task['id'], json.dumps(task, allow_nan=False)))

    def load(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT body,cancelled FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown task')
        task = json.loads(row[0])
        task['cancel_requested'] = bool(row[1])
        return task

    def save(self, task):
        data = {k: v for k, v in task.items() if k != 'cancel_requested'}
        with self.connect() as db:
            db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(data, allow_nan=False), task['id']))

    def cancel(self, task_id):
        self.load(task_id)
        with self.connect() as db:
            db.execute('UPDATE tasks SET cancelled=1 WHERE id=?', (task_id,))
