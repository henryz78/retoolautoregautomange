import threading
import time
from contextlib import contextmanager
from typing import Dict

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from models import ApiKeyConfig, ApiKeyRuntimeState


class ApiKeyRegistry:
    def __init__(self, keys: list[ApiKeyConfig]):
        self._lock = threading.RLock()
        self.reload(keys)

    def reload(self, keys: list[ApiKeyConfig]) -> None:
        with self._lock:
            self._keys_by_secret: Dict[str, ApiKeyConfig] = {key.key: key for key in keys if key.enabled}
            self._keys_by_id: Dict[str, ApiKeyConfig] = {key.id: key for key in keys}
            self._all_keys: list[ApiKeyConfig] = list(keys)
            existing = getattr(self, "_runtime", {})
            self._runtime: Dict[str, ApiKeyRuntimeState] = {}
            for key in keys:
                runtime = existing.get(key.id) or ApiKeyRuntimeState(key_id=key.id)
                runtime.key_id = key.id
                self._runtime[key.id] = runtime

    def authenticate(self, auth: HTTPAuthorizationCredentials | None) -> ApiKeyConfig:
        if not self._keys_by_secret:
            raise HTTPException(
                status_code=503,
                detail="Service unavailable: no internal API keys configured.",
            )

        if not auth or not auth.credentials:
            raise HTTPException(
                status_code=401,
                detail="Authorization Bearer token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        key = self._keys_by_secret.get(auth.credentials)
        if not key:
            raise HTTPException(status_code=403, detail="Invalid internal API key.")
        return key

    def authenticate_scope(self, auth: HTTPAuthorizationCredentials | None, scope: str) -> ApiKeyConfig:
        key = self.authenticate(auth)
        if not key.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"Internal API key missing required scope '{scope}'.")
        return key

    def list_keys(self) -> list[ApiKeyConfig]:
        return list(self._all_keys)

    def get_by_id(self, key_id: str) -> ApiKeyConfig | None:
        return self._keys_by_id.get(key_id)

    def list_runtime_states(self) -> dict[str, ApiKeyRuntimeState]:
        with self._lock:
            return {key_id: state.model_copy(deep=True) for key_id, state in self._runtime.items()}

    def begin_request(self, key: ApiKeyConfig) -> None:
        with self._lock:
            runtime = self._runtime.setdefault(key.id, ApiKeyRuntimeState(key_id=key.id))
            limit = key.concurrency_limit
            if limit is not None and runtime.active_requests >= limit:
                raise HTTPException(
                    status_code=429,
                    detail=f"API key '{key.id}' reached concurrency limit {limit}.",
                )
            runtime.active_requests += 1
            runtime.total_requests += 1
            runtime.last_used_at = time.time()

    def end_request(self, key: ApiKeyConfig, *, success: bool) -> None:
        with self._lock:
            runtime = self._runtime.setdefault(key.id, ApiKeyRuntimeState(key_id=key.id))
            runtime.active_requests = max(runtime.active_requests - 1, 0)
            runtime.last_used_at = time.time()
            if success:
                runtime.success_requests += 1
            else:
                runtime.failed_requests += 1

    @contextmanager
    def checkout(self, key: ApiKeyConfig):
        self.begin_request(key)
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            self.end_request(key, success=succeeded)
