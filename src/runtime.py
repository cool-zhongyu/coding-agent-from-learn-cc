"""Infrastructure only: no imports of loops, tool dispatchers or teams."""
import threading
import uuid
from dataclasses import dataclass
from src.config import WORKDIR, client
from src.permissions import PermissionPolicy
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore


@dataclass
class AgentRuntime:
    client: object
    db: SQLiteStore
    vectors: VectorStore
    permissions: PermissionPolicy
    session_id: str
    redis: object = None

    @classmethod
    def from_env(cls):
        runtime = cls(client, SQLiteStore(WORKDIR / ".agent" / "agent.db"),
                      VectorStore(), PermissionPolicy(), "session_" + uuid.uuid4().hex)
        runtime.db.put("sessions", runtime.session_id, {"id": runtime.session_id})
        return runtime


_runtime = None
_lock = threading.Lock()


def get_runtime():
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = AgentRuntime.from_env()
        return _runtime
