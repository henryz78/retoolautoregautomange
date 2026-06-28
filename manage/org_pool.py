import time
from pathlib import Path
from typing import Dict, Optional

from config import ConfigError, load_org_credentials
from models import HealthRecord, HealthState, ModelAliasConfig, OrgConfig
from retool_client import RetoolClient
from state_store import JsonStateStore


class OrgPoolError(Exception):
    pass


class OrgPool:
    def __init__(
        self,
        orgs: list[OrgConfig],
        health_store: JsonStateStore,
        cooldown_seconds: int,
        retool_client: RetoolClient,
        orgs_file_path: Path | None = None,
        allow_empty_file: bool = False,
        warning_seconds: int = 2 * 24 * 60 * 60,
    ):
        self.orgs = {org.resolved_id: org for org in orgs}
        self.health_store = health_store
        self.cooldown_seconds = cooldown_seconds
        self.retool_client = retool_client
        self.orgs_file_path = orgs_file_path
        self.allow_empty_file = allow_empty_file
        self.warning_seconds = max(int(warning_seconds), 0)
        self._last_refresh_started_at: float = 0
        self._last_refresh_completed_at: float = 0

    def _load_health(self) -> HealthState:
        state = self.health_store.load()
        changed = False
        for org in self.orgs.values():
            if org.resolved_id not in state.orgs:
                state.orgs[org.resolved_id] = HealthRecord(
                    org_id=org.resolved_id,
                    domain_name=org.domain_name,
                )
                changed = True
            else:
                state.orgs[org.resolved_id].domain_name = org.domain_name
        for org_id in list(state.orgs.keys()):
            if org_id not in self.orgs:
                del state.orgs[org_id]
                changed = True
        if changed:
            self.health_store.save(state)
        return state

    async def refresh_agents(self, model_aliases: list[ModelAliasConfig]) -> None:
        self._last_refresh_started_at = time.time()
        state = self._load_health()
        for org in self.orgs.values():
            record = state.orgs[org.resolved_id]
            if not org.enabled:
                continue
            try:
                agents = await self.retool_client.list_agents(org)
                record.discovered_agents = {agent["id"]: agent for agent in agents}
                record.agent_cache_refreshed_at = time.time()
                record.auth_failed = False
                record.last_error = None
            except Exception as exc:
                self.mark_failure(org.resolved_id, str(exc), auth_failed="401" in str(exc) or "403" in str(exc), persist=False)
                state = self._load_health()
                record = state.orgs[org.resolved_id]
                record.last_error = str(exc)
        self.health_store.save(state)
        self._last_refresh_completed_at = time.time()

    def get_org(self, org_id: str) -> Optional[OrgConfig]:
        return self.orgs.get(org_id)

    def set_org_enabled(self, org_id: str, enabled: bool) -> OrgConfig:
        org = self.orgs.get(org_id)
        if not org:
            raise OrgPoolError(f"Org '{org_id}' not found")
        org.enabled = bool(enabled)
        return org

    def reset_cooldown(self, org_id: str) -> None:
        state = self._load_health()
        record = state.orgs.get(org_id)
        if not record:
            raise OrgPoolError(f"Org '{org_id}' not found")
        record.cooldown_until = 0
        record.failure_count = 0
        record.auth_failed = False
        record.last_error = None
        self.health_store.save(state)

    def replace_orgs(self, orgs: list[OrgConfig]) -> None:
        self.orgs = {org.resolved_id: org for org in orgs}
        self._load_health()

    def reload_from_file(self) -> bool:
        if not self.orgs_file_path:
            return False
        try:
            orgs = load_org_credentials(self.orgs_file_path, allow_empty=self.allow_empty_file)
        except ConfigError:
            if self.allow_empty_file and not self.orgs_file_path.exists():
                orgs = []
            else:
                raise
        self.replace_orgs(orgs)
        return True

    def snapshot(self) -> list[dict]:
        state = self._load_health()
        now = time.time()
        records: list[dict] = []
        for org_id, org in sorted(self.orgs.items(), key=lambda item: item[0]):
            record = state.orgs[org_id]
            expires_at = org.expires_at
            is_expired = org.is_expired(now)
            is_expiring_soon = bool(expires_at and not is_expired and expires_at <= int(now + self.warning_seconds))
            records.append(
                {
                    "id": org.resolved_id,
                    "domain_name": org.domain_name,
                    "enabled": org.enabled,
                    "source_account_id": org.source_account_id,
                    "source_email": org.source_email,
                    "refreshed_at": org.refreshed_at,
                    "expires_at": expires_at,
                    "verified_models": list(org.verified_models),
                    "auth_state": org.auth_state,
                    "bundle_version": org.bundle_version,
                    "bundle_generated_at": org.bundle_generated_at,
                    "bundle_expires_at": org.bundle_expires_at,
                    "is_expired": is_expired,
                    "is_expiring_soon": is_expiring_soon,
                    "is_auth_ready": org.is_auth_ready(),
                    "last_used_at": record.last_used_at,
                    "last_success_at": record.last_success_at,
                    "last_failure_at": record.last_failure_at,
                    "failure_count": record.failure_count,
                    "cooldown_until": record.cooldown_until,
                    "auth_failed": record.auth_failed,
                    "last_error": record.last_error or org.last_error,
                    "discovered_agent_count": len(record.discovered_agents),
                    "agent_cache_refreshed_at": record.agent_cache_refreshed_at,
                }
            )
        return records

    def summary(self) -> dict:
        records = self.snapshot()
        now = time.time()
        return {
            "total_orgs": len(records),
            "enabled_orgs": sum(1 for item in records if item["enabled"]),
            "ready_orgs": sum(
                1
                for item in records
                if item["enabled"]
                and item["is_auth_ready"]
                and not item["is_expired"]
                and not item["auth_failed"]
                and item["cooldown_until"] <= now
            ),
            "expired_orgs": sum(1 for item in records if item["is_expired"]),
            "expiring_soon_orgs": sum(1 for item in records if item["is_expiring_soon"]),
            "cooldown_orgs": sum(1 for item in records if item["cooldown_until"] > now),
            "auth_failed_orgs": sum(1 for item in records if item["auth_failed"]),
            "last_refresh_started_at": self._last_refresh_started_at,
            "last_refresh_completed_at": self._last_refresh_completed_at,
        }

    def mark_success(self, org_id: str) -> None:
        state = self._load_health()
        record = state.orgs[org_id]
        record.last_used_at = time.time()
        record.last_success_at = time.time()
        record.failure_count = 0
        record.cooldown_until = 0
        record.auth_failed = False
        record.last_error = None
        self.health_store.save(state)

    def mark_failure(self, org_id: str, error: str, auth_failed: bool = False, persist: bool = True) -> None:
        state = self._load_health()
        record = state.orgs[org_id]
        record.last_failure_at = time.time()
        record.failure_count += 1
        record.last_error = error
        record.auth_failed = auth_failed
        record.cooldown_until = time.time() + self.cooldown_seconds
        if persist:
            self.health_store.save(state)

    def choose_org(
        self,
        model_alias: ModelAliasConfig,
        preferred_org_id: Optional[str] = None,
        excluded_org_ids: Optional[set[str]] = None,
    ) -> OrgConfig:
        excluded_org_ids = excluded_org_ids or set()
        now = time.time()
        state = self._load_health()

        def org_is_eligible(org: OrgConfig, record: HealthRecord) -> bool:
            if not org.enabled:
                return False
            if org.resolved_id in excluded_org_ids:
                return False
            if not org.is_auth_ready():
                return False
            if org.is_expired(now):
                return False
            if record.auth_failed:
                return False
            if record.cooldown_until > now:
                return False
            if model_alias.agent_name:
                if not any(agent.get("name") == model_alias.agent_name for agent in record.discovered_agents.values()):
                    return False
            if model_alias.model_name:
                if not any(agent.get("data", {}).get("model") == model_alias.model_name for agent in record.discovered_agents.values()):
                    return False
            return True

        if preferred_org_id:
            preferred_org = self.orgs.get(preferred_org_id)
            preferred_record = state.orgs.get(preferred_org_id)
            if preferred_org and preferred_record and org_is_eligible(preferred_org, preferred_record):
                return preferred_org

        candidates = []
        for org_id, org in self.orgs.items():
            record = state.orgs[org_id]
            if org_is_eligible(org, record):
                candidates.append((record.last_used_at, record.failure_count, org))

        if not candidates:
            raise OrgPoolError(f"No healthy orgs available for model alias '{model_alias.id}'")

        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]
