import json
import threading
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from models import AuditState, ConversationState, HealthState

StateModel = TypeVar("StateModel", bound=BaseModel)


class StateStoreError(Exception):
    pass


class JsonStateStore:
    def __init__(self, path: Path, model_cls: Type[StateModel], default_factory):
        self.path = path
        self.model_cls = model_cls
        self.default_factory = default_factory
        self._lock = threading.RLock()

    def load(self) -> StateModel:
        with self._lock:
            if not self.path.exists():
                state = self.default_factory()
                self.save(state)
                return state
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return self.model_cls.model_validate(data)
            except json.JSONDecodeError as exc:
                raise StateStoreError(f"Invalid JSON state file {self.path}: {exc}") from exc
            except ValidationError as exc:
                raise StateStoreError(f"Invalid state shape in {self.path}: {exc}") from exc

    def save(self, state: StateModel) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(state.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)


def create_conversation_store(path: Path) -> JsonStateStore:
    return JsonStateStore(path, ConversationState, lambda: ConversationState())


def create_health_store(path: Path) -> JsonStateStore:
    return JsonStateStore(path, HealthState, lambda: HealthState())


def create_audit_store(path: Path) -> JsonStateStore:
    return JsonStateStore(path, AuditState, lambda: AuditState())
