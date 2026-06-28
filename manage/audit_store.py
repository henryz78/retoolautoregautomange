from models import AuditEntry, AuditState
from state_store import JsonStateStore


class AuditStore:
    def __init__(self, store: JsonStateStore, history_limit: int):
        self._store = store
        self._history_limit = max(int(history_limit), 1)

    def append(self, entry: AuditEntry) -> AuditEntry:
        state = self._store.load()
        state.entries.insert(0, entry)
        state.entries = state.entries[: self._history_limit]
        self._store.save(state)
        return entry

    def all(self) -> AuditState:
        return self._store.load()
