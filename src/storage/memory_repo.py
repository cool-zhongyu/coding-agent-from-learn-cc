import json
import time


class MemoryRepository:
    def __init__(self, db):
        self.db = db

    def upsert(self, record):
        key = record["filename"]
        with self.db.connect() as conn:
            old = conn.execute("SELECT data FROM memories WHERE id=?", (key,)).fetchone()
            if old:
                conn.execute("INSERT INTO memory_versions(memory_id,data,created_at) VALUES(?,?,?)",
                             (key, old[0], time.time()))
            conn.execute("INSERT INTO memories VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                         (key, json.dumps(record, ensure_ascii=False), time.time()))

    def all(self):
        return self.db.all("memories")
