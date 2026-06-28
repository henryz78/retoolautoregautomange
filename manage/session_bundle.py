import json
import platform
import socket
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


SESSION_BUNDLE_VERSION = "1"
DEFAULT_SESSION_BUNDLE_TTL_SECONDS = 7 * 24 * 60 * 60


class SessionBundleError(Exception):
    pass


class SessionBundleGeneratedBy(BaseModel):
    tool: str = "retoolautoregautomange"
    script: str
    host: str = Field(default_factory=socket.gethostname)
    platform: str = Field(default_factory=platform.platform)
    collector_version: str = SESSION_BUNDLE_VERSION


class SessionBundleOrg(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    domain_name: str
    x_xsrf_token: str = Field(alias="x_xsrf_token")
    access_token: str = Field(alias="accessToken")
    enabled: bool = True
    source_account_id: str | None = None
    source_email: str | None = None
    refreshed_at: int = 0
    expires_at: int | None = None
    verified_models: list[str] = Field(default_factory=list)
    auth_state: str = "ready"
    last_error: str = ""

    @property
    def resolved_id(self) -> str:
        return self.id or self.domain_name


class SessionBundle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bundle_version: str = SESSION_BUNDLE_VERSION
    generated_at: int
    generated_by: SessionBundleGeneratedBy
    expires_at: int
    org_count: int = 0
    verified_models: list[str] = Field(default_factory=list)
    orgs: list[SessionBundleOrg]

    @model_validator(mode="after")
    def validate_bundle(self):
        if not self.orgs:
            raise ValueError("session bundle must contain at least one org")
        if self.org_count and self.org_count != len(self.orgs):
            raise ValueError("session bundle org_count does not match org list length")
        if not self.org_count:
            self.org_count = len(self.orgs)
        return self


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SessionBundleError(f"Session bundle file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SessionBundleError(f"Invalid JSON in session bundle file {path}: {exc}") from exc


def load_session_bundle(path: Path) -> SessionBundle:
    payload = load_json_file(path)
    try:
        return SessionBundle.model_validate(payload)
    except ValidationError as exc:
        raise SessionBundleError(f"Invalid session bundle {path}: {exc}") from exc


def build_session_bundle(
    org_records: list[dict[str, Any]],
    *,
    verified_models: list[str],
    script_name: str,
    ttl_seconds: int = DEFAULT_SESSION_BUNDLE_TTL_SECONDS,
) -> SessionBundle:
    generated_at = int(time.time())
    expires_at = generated_at + max(int(ttl_seconds), 0)
    bundle_orgs: list[SessionBundleOrg] = []
    for raw_org in org_records:
        record = dict(raw_org)
        record.setdefault("refreshed_at", generated_at)
        record.setdefault("expires_at", expires_at)
        record.setdefault("verified_models", list(verified_models))
        record.setdefault("auth_state", "ready")
        record.setdefault("last_error", "")
        bundle_orgs.append(SessionBundleOrg.model_validate(record))

    return SessionBundle(
        generated_at=generated_at,
        generated_by=SessionBundleGeneratedBy(script=script_name),
        expires_at=expires_at,
        org_count=len(bundle_orgs),
        verified_models=list(verified_models),
        orgs=bundle_orgs,
    )


def session_bundle_to_org_records(
    bundle: SessionBundle,
    *,
    allow_expired: bool = False,
    now: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current_time = now if now is not None else int(time.time())
    imported_orgs: list[dict[str, Any]] = []
    skipped_orgs: list[dict[str, Any]] = []

    for bundle_org in bundle.orgs:
        org_expires_at = bundle_org.expires_at or bundle.expires_at
        if not allow_expired and org_expires_at and org_expires_at <= current_time:
            skipped_orgs.append(
                {
                    "id": bundle_org.resolved_id,
                    "domain_name": bundle_org.domain_name,
                    "reason": "expired",
                    "expires_at": org_expires_at,
                }
            )
            continue
        if str(bundle_org.auth_state or "").strip().lower() not in {"ready", ""}:
            skipped_orgs.append(
                {
                    "id": bundle_org.resolved_id,
                    "domain_name": bundle_org.domain_name,
                    "reason": f"auth_state:{bundle_org.auth_state}",
                    "expires_at": org_expires_at,
                }
            )
            continue

        imported_orgs.append(
            {
                "id": bundle_org.resolved_id,
                "domain_name": bundle_org.domain_name,
                "x_xsrf_token": bundle_org.x_xsrf_token,
                "accessToken": bundle_org.access_token,
                "enabled": bundle_org.enabled,
                "source_account_id": bundle_org.source_account_id,
                "source_email": bundle_org.source_email,
                "refreshed_at": bundle_org.refreshed_at,
                "expires_at": org_expires_at,
                "verified_models": list(bundle_org.verified_models or bundle.verified_models),
                "auth_state": bundle_org.auth_state,
                "last_error": bundle_org.last_error,
                "bundle_version": bundle.bundle_version,
                "bundle_generated_at": bundle.generated_at,
                "bundle_expires_at": bundle.expires_at,
                "bundle_generated_by": bundle.generated_by.model_dump(mode="json"),
            }
        )

    return imported_orgs, skipped_orgs


def update_import_history(state_path: Path, record: dict[str, Any], history_limit: int = 20) -> None:
    existing: dict[str, Any]
    if state_path.exists():
        loaded = load_json_file(state_path)
        existing = loaded if isinstance(loaded, dict) else {}
    else:
        existing = {}

    history = existing.get("history")
    if not isinstance(history, list):
        history = []

    history.insert(0, record)
    existing["last_import"] = record
    existing["history"] = history[: max(int(history_limit), 1)]
    write_json_file(state_path, existing)
