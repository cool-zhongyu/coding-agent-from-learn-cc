"""SQLite is the durable authority; connections never cross thread boundaries."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=plain)


class SQLiteStore:
    TABLES = {"sessions", "runs", "tasks", "working_states", "memories",
              "workflow_runs", "skill_candidates", "cron_jobs", "metadata"}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table in self.TABLES:
                db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT, data TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT, dependency_id TEXT, PRIMARY KEY(task_id, dependency_id));
                CREATE TABLE IF NOT EXISTS memory_versions (
                    id INTEGER PRIMARY KEY, memory_id TEXT, data TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS workflow_journal (
                    run_id TEXT NOT NULL, step_key TEXT NOT NULL, label TEXT,
                    input_hash TEXT NOT NULL, result_json TEXT NOT NULL,
                    model TEXT, token_usage INTEGER, created_at REAL NOT NULL,
                    PRIMARY KEY(run_id, step_key));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _table(self, table):
        if table not in self.TABLES:
            raise ValueError("Unknown table")
        return table

    def put(self, table, key, value):
        with self.connect() as db:
            db.execute(f"INSERT INTO {self._table(table)} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                       (key, dumps(value), time.time()))

    def get(self, table, key, default=None):
        with self.connect() as db:
            row = db.execute(f"SELECT data FROM {self._table(table)} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, table):
        with self.connect() as db:
            rows = db.execute(f"SELECT data FROM {self._table(table)} ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, table, key):
        with self.connect() as db:
            db.execute(f"DELETE FROM {self._table(table)} WHERE id=?", (key,))

    def event(self, run_id, kind, data):
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,kind,data,created_at) VALUES(?,?,?,?)",
                       (run_id, kind, dumps(data), time.time()))
