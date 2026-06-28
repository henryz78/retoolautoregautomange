import asyncio
import csv
import io
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from api_keys import ApiKeyRegistry
from audit_store import AuditStore
from config import (
    ConfigError,
    load_api_keys,
    load_managed_accounts,
    load_org_credentials,
    save_api_keys,
    save_managed_accounts,
    save_org_credentials,
)
from models import (
    AccountRefreshRequest,
    ApiKeyConfig,
    ApiKeyUpsertRequest,
    GatewayConfig,
    ManagedAccountConfig,
    ManagedAccountFingerprintResetRequest,
    ManagedAccountImportRequest,
    ManagedAccountUpsertRequest,
    OrgConfig,
)
from org_pool import OrgPool, OrgPoolError
from session_bundle import (
    SessionBundle,
    SessionBundleError,
    load_json_file,
    session_bundle_to_org_records,
    update_import_history,
)


class AdminService:
    def __init__(
        self,
        gateway_config: GatewayConfig,
        gateway_config_path: Path,
        api_keys_path: Path,
        history_output_path: Path,
        org_pool: OrgPool,
        api_key_registry: ApiKeyRegistry,
        audit_store: AuditStore,
        accounts_path: Path,
        account_state_path: Path,
        runtime_root: Path,
        gateway_refresh_callback: Callable[[], Awaitable[None]] | None = None,
        default_browser_provider: str = "cloakbrowser",
        default_max_concurrency: int = 1,
    ):
        self.gateway_config = gateway_config
        self.gateway_config_path = gateway_config_path
        self.api_keys_path = api_keys_path
        self.history_output_path = history_output_path
        self.org_pool = org_pool
        self.api_key_registry = api_key_registry
        self.audit_store = audit_store
        self.accounts_path = accounts_path
        self.account_state_path = account_state_path
        self.runtime_root = runtime_root
        self.account_jobs_root = runtime_root / "account_refresh_jobs"
        self.gateway_refresh_callback = gateway_refresh_callback
        self.default_browser_provider = self._normalize_browser_provider(default_browser_provider)
        self.default_max_concurrency = max(int(default_max_concurrency), 1)
        self.default_check_models = [alias.id for alias in gateway_config.model_aliases]
        self.supported_browser_providers = ["geekez", "obscura", "playwright", "cloakbrowser", "auto"]
        self._job_lock = threading.RLock()
        self._account_refresh_job: dict[str, Any] = {
            "id": "",
            "status": "idle",
            "requested_at": 0,
            "started_at": 0,
            "finished_at": 0,
            "mode": "refresh",
            "refresh_only": True,
            "verify_only": False,
            "ignore_cooldown": False,
            "browser_provider": self.default_browser_provider,
            "headless": False,
            "max_concurrency": self.default_max_concurrency,
            "check_models": list(self.default_check_models),
            "account_ids": [],
            "command": [],
            "command_text": "",
            "log_lines": [],
            "exit_code": None,
            "summary": {},
            "error": "",
            "org_pool_reloaded": False,
        }

    def _require_orgs_file(self) -> Path:
        if self.org_pool.orgs_file_path:
            return self.org_pool.orgs_file_path
        raise HTTPException(status_code=500, detail="Gateway orgs_file is not configured.")

    def _load_accounts(self) -> list[ManagedAccountConfig]:
        if not self.accounts_path.exists():
            return []
        try:
            return load_managed_accounts(self.accounts_path, allow_empty=True)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _save_accounts(self, accounts: list[ManagedAccountConfig]) -> None:
        save_managed_accounts(self.accounts_path, accounts)

    def _load_account_runtime_rows(self) -> dict[str, dict[str, Any]]:
        if not self.account_state_path.exists():
            return {}
        try:
            payload = load_json_file(self.account_state_path)
        except Exception:
            return {}
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}
        state_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = self._normalize_text(str(row.get("account_id") or ""))
            if key:
                state_rows[key] = row
        return state_rows

    def _get_runtime_row_for_account(
        self,
        account: ManagedAccountConfig,
        state_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        selectors = [
            account.id,
            account.expected_subdomain,
            account.email,
            f"{account.expected_subdomain}.retool.com",
        ]
        for selector in selectors:
            normalized = self._normalize_text(selector)
            if normalized and normalized in state_rows:
                return state_rows[normalized]
        return {}

    def overview(self) -> dict:
        audit_entries = self.audit_store.all().entries
        last_import = self.import_history().get("last_import")
        summary = self.org_pool.summary()
        key_runtime = self.api_key_registry.list_runtime_states()
        accounts = self._load_accounts()
        account_states = self._load_account_runtime_rows()
        ready_accounts = sum(
            1
            for account in accounts
            if account.enabled
            and str(self._get_runtime_row_for_account(account, account_states).get("auth_state") or "").strip().lower() == "ready"
        )
        summary.update(
            {
                "model_count": len(self.gateway_config.model_aliases),
                "api_key_count": len(self.api_key_registry.list_keys()),
                "active_request_count": sum(item.active_requests for item in key_runtime.values()),
                "audit_entry_count": len(audit_entries),
                "audit_success_count": sum(1 for item in audit_entries if item.success),
                "audit_failure_count": sum(1 for item in audit_entries if not item.success),
                "last_import": last_import,
                "account_count": len(accounts),
                "enabled_account_count": sum(1 for account in accounts if account.enabled),
                "ready_account_count": ready_accounts,
                "account_refresh_job": self.get_account_refresh_status(),
            }
        )
        return summary

    def list_orgs(self) -> list[dict]:
        return self.org_pool.snapshot()

    def list_accounts(self) -> list[dict]:
        state_rows = self._load_account_runtime_rows()
        records: list[dict] = []
        for account in sorted(self._load_accounts(), key=lambda item: (not item.enabled, item.expected_subdomain, item.email)):
            state = self._get_runtime_row_for_account(account, state_rows)
            records.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "expected_subdomain": account.expected_subdomain,
                    "workspace_url": f"https://{account.expected_subdomain}.retool.com/",
                    "enabled": account.enabled,
                    "notes": account.notes,
                    "browser_provider": account.browser_provider or "",
                    "fingerprint_seed": account.fingerprint_seed or "",
                    "created_at": account.created_at,
                    "updated_at": account.updated_at,
                    "password_present": bool(account.password),
                    "password_masked": self._mask_password(account.password),
                    "auth_state": state.get("auth_state") or "unknown",
                    "last_login_attempt_at": state.get("last_login_attempt_at"),
                    "last_login_success_at": state.get("last_login_success_at"),
                    "last_session_refresh_at": state.get("last_session_refresh_at"),
                    "last_verification_success_at": state.get("last_verification_success_at"),
                    "failure_count": state.get("failure_count") or 0,
                    "cooldown_until": state.get("cooldown_until") or 0,
                    "last_error": state.get("last_error") or "",
                    "browser_provider_runtime": state.get("browser_provider") or "",
                    "diagnostic_dir": state.get("diagnostic_dir") or "",
                    "final_url": state.get("final_url") or "",
                }
            )
        return records

    def upsert_account(self, request: ManagedAccountUpsertRequest) -> dict:
        accounts = self._load_accounts()
        normalized_id = self._normalize_text(request.id)
        email = self._normalize_text(request.email)
        password = self._normalize_text(request.password)
        expected_subdomain = self._normalize_subdomain(request.expected_subdomain)
        notes = str(request.notes or "").strip()
        browser_provider = self._normalize_browser_provider(request.browser_provider)
        fingerprint_seed = self._normalize_fingerprint_seed(request.fingerprint_seed)
        now = time.time()

        if not normalized_id and email and expected_subdomain:
            identity_key = self._account_identity_key(email, expected_subdomain)
            for existing in accounts:
                if self._account_identity_key(existing.email, existing.expected_subdomain) == identity_key:
                    normalized_id = existing.id
                    break

        updated = False
        saved_account: ManagedAccountConfig | None = None
        if normalized_id:
            for index, existing in enumerate(accounts):
                if existing.id != normalized_id:
                    continue
                updated = True
                merged_email = email or existing.email
                merged_password = password if password is not None else existing.password
                merged_subdomain = expected_subdomain or existing.expected_subdomain
                if not merged_email or not merged_password or not merged_subdomain:
                    raise HTTPException(status_code=400, detail="email、password、expected_subdomain 都不能为空。")
                updated_account = ManagedAccountConfig(
                    id=existing.id,
                    email=merged_email,
                    password=merged_password,
                    expected_subdomain=merged_subdomain,
                    enabled=request.enabled,
                    notes=notes,
                    browser_provider=browser_provider or existing.browser_provider,
                    fingerprint_seed=self._resolve_account_fingerprint_seed(
                        explicit_seed=fingerprint_seed,
                        existing_seed=existing.fingerprint_seed,
                        regenerate=request.regenerate_fingerprint,
                    ),
                    created_at=existing.created_at,
                    updated_at=now,
                )
                self._assert_account_unique(accounts, updated_account, ignore_id=existing.id)
                accounts[index] = updated_account
                saved_account = updated_account
                break

        if not updated:
            if not email or password is None or not expected_subdomain:
                raise HTTPException(status_code=400, detail="新建账号必须提供 email、password、expected_subdomain。")
            account_id = normalized_id or self._generate_account_id(accounts, email=email, expected_subdomain=expected_subdomain)
            saved_account = ManagedAccountConfig(
                id=account_id,
                email=email,
                password=password,
                expected_subdomain=expected_subdomain,
                enabled=request.enabled,
                notes=notes,
                browser_provider=browser_provider,
                fingerprint_seed=self._resolve_account_fingerprint_seed(
                    explicit_seed=fingerprint_seed,
                    existing_seed=None,
                    regenerate=request.regenerate_fingerprint,
                ),
                created_at=now,
                updated_at=now,
            )
            self._assert_account_unique(accounts, saved_account, ignore_id=None)
            accounts.append(saved_account)

        self._save_accounts(accounts)
        if saved_account is None:
            raise HTTPException(status_code=500, detail="Failed to save account.")
        return {
            "id": saved_account.id,
            "updated": updated,
            "email": saved_account.email,
            "expected_subdomain": saved_account.expected_subdomain,
            "enabled": saved_account.enabled,
            "notes": saved_account.notes,
            "browser_provider": saved_account.browser_provider or "",
            "fingerprint_seed": saved_account.fingerprint_seed or "",
            "password_present": bool(saved_account.password),
            "password_masked": self._mask_password(saved_account.password),
        }

    def import_accounts(self, request: ManagedAccountImportRequest) -> dict:
        imported_accounts = self._parse_account_import_content(request.content)
        if not imported_accounts:
            raise HTTPException(status_code=400, detail="没有从导入内容中解析出任何账号。")

        existing_accounts = [] if request.replace else self._load_accounts()
        merged_by_id: dict[str, ManagedAccountConfig] = {account.id: account for account in existing_accounts}
        existing_by_identity = {
            self._account_identity_key(account.email, account.expected_subdomain): account.id for account in existing_accounts
        }

        imported_count = 0
        for candidate in imported_accounts:
            target_id = candidate.id
            identity_key = self._account_identity_key(candidate.email, candidate.expected_subdomain)
            if target_id in merged_by_id:
                existing = merged_by_id[target_id]
                merged_by_id[target_id] = ManagedAccountConfig(
                    id=existing.id,
                    email=candidate.email,
                    password=candidate.password or existing.password,
                    expected_subdomain=candidate.expected_subdomain,
                    enabled=candidate.enabled,
                    notes=candidate.notes,
                    browser_provider=candidate.browser_provider or existing.browser_provider,
                    fingerprint_seed=candidate.fingerprint_seed or existing.fingerprint_seed or self._generate_fingerprint_seed(),
                    created_at=existing.created_at,
                    updated_at=time.time(),
                )
            elif identity_key in existing_by_identity:
                existing_id = existing_by_identity[identity_key]
                existing = merged_by_id[existing_id]
                merged_by_id[existing_id] = ManagedAccountConfig(
                    id=existing.id,
                    email=candidate.email,
                    password=candidate.password or existing.password,
                    expected_subdomain=candidate.expected_subdomain,
                    enabled=candidate.enabled,
                    notes=candidate.notes,
                    browser_provider=candidate.browser_provider or existing.browser_provider,
                    fingerprint_seed=candidate.fingerprint_seed or existing.fingerprint_seed or self._generate_fingerprint_seed(),
                    created_at=existing.created_at,
                    updated_at=time.time(),
                )
            else:
                if not candidate.fingerprint_seed:
                    candidate = candidate.model_copy(update={"fingerprint_seed": self._generate_fingerprint_seed()})
                merged_by_id[target_id] = candidate
                existing_by_identity[identity_key] = target_id
            imported_count += 1

        final_accounts = sorted(merged_by_id.values(), key=lambda item: (not item.enabled, item.expected_subdomain, item.email))
        self._save_accounts(final_accounts)
        return {
            "replace": bool(request.replace),
            "imported_count": imported_count,
            "total_count": len(final_accounts),
        }

    def reset_account_fingerprints(self, request: ManagedAccountFingerprintResetRequest) -> dict:
        accounts = self._load_accounts()
        selected_accounts = self._select_accounts_for_fingerprint_reset(accounts, request)
        selected_ids = {account.id for account in selected_accounts}
        now = time.time()
        final_accounts: list[ManagedAccountConfig] = []
        for account in accounts:
            if account.id not in selected_ids:
                final_accounts.append(account)
                continue
            final_accounts.append(
                account.model_copy(
                    update={
                        "fingerprint_seed": self._generate_fingerprint_seed(),
                        "updated_at": now,
                    }
                )
            )
        self._save_accounts(final_accounts)
        return {
            "updated_count": len(selected_accounts),
            "account_ids": [account.id for account in selected_accounts],
        }

    def delete_account(self, account_id: str) -> dict:
        normalized_id = self._normalize_text(account_id)
        if not normalized_id:
            raise HTTPException(status_code=400, detail="Account id is required.")
        accounts = self._load_accounts()
        remaining = [account for account in accounts if account.id != normalized_id]
        if len(remaining) == len(accounts):
            raise HTTPException(status_code=404, detail=f"Account '{normalized_id}' was not found.")
        self._save_accounts(remaining)
        return {"id": normalized_id, "deleted": True}

    async def start_account_refresh_job(self, request: AccountRefreshRequest) -> dict:
        loop = asyncio.get_running_loop()
        with self._job_lock:
            if self._account_refresh_job.get("status") in {"queued", "running"}:
                raise HTTPException(status_code=409, detail="已有账号刷新任务正在运行，请等待当前任务结束。")

        accounts = self._load_accounts()
        selected_accounts = self._select_refresh_accounts(accounts, request)
        browser_provider = self._resolve_job_browser_provider(request, selected_accounts)
        effective_headless = bool(request.headless or self._browser_provider_requires_headless(browser_provider))
        max_concurrency = max(int(request.max_concurrency or self.default_max_concurrency), 1)
        check_models = [model for model in (request.check_models or self.default_check_models) if model]
        if not check_models:
            raise HTTPException(status_code=400, detail="当前没有可用于校验的模型别名。")

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        command, temp_accounts_csv = self._build_refresh_command(
            job_id=job_id,
            selected_accounts=selected_accounts,
            request=request,
            browser_provider=browser_provider,
            max_concurrency=max_concurrency,
            check_models=check_models,
        )
        job = {
            "id": job_id,
            "status": "queued",
            "requested_at": time.time(),
            "started_at": 0,
            "finished_at": 0,
            "mode": "verify" if request.verify_only else "refresh",
            "refresh_only": bool(request.refresh_only),
            "verify_only": bool(request.verify_only),
            "ignore_cooldown": bool(request.ignore_cooldown),
            "browser_provider": browser_provider,
            "headless": effective_headless,
            "max_concurrency": max_concurrency,
            "check_models": check_models,
            "account_ids": [account.id for account in selected_accounts],
            "command": command,
            "command_text": subprocess.list2cmdline(command),
            "log_lines": [],
            "exit_code": None,
            "summary": {},
            "error": "",
            "org_pool_reloaded": False,
        }
        with self._job_lock:
            self._account_refresh_job = job

        worker = threading.Thread(
            target=self._run_refresh_job,
            args=(job_id, command, temp_accounts_csv, loop),
            daemon=True,
        )
        worker.start()
        return self.get_account_refresh_status()

    def get_account_refresh_status(self) -> dict:
        with self._job_lock:
            snapshot = dict(self._account_refresh_job)
            snapshot["log_lines"] = list(self._account_refresh_job.get("log_lines", []))
            snapshot["command"] = list(self._account_refresh_job.get("command", []))
            snapshot["check_models"] = list(self._account_refresh_job.get("check_models", []))
            snapshot["account_ids"] = list(self._account_refresh_job.get("account_ids", []))
            snapshot["summary"] = dict(self._account_refresh_job.get("summary", {}))
            return snapshot

    def update_org_enabled(self, org_id: str, enabled: bool) -> dict:
        try:
            org = self.org_pool.set_org_enabled(org_id, enabled)
            save_org_credentials(self._require_orgs_file(), list(self.org_pool.orgs.values()))
        except (OrgPoolError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": org.resolved_id, "enabled": org.enabled}

    def reset_org_cooldown(self, org_id: str) -> dict:
        try:
            self.org_pool.reset_cooldown(org_id)
        except OrgPoolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": org_id, "cooldown_reset": True}

    async def import_bundle(self, filename: str, content: bytes, allow_expired: bool = False) -> dict:
        try:
            payload = json.loads(content.decode("utf-8"))
            bundle = SessionBundle.model_validate(payload)
            imported_orgs, skipped_orgs = session_bundle_to_org_records(bundle, allow_expired=allow_expired)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid bundle encoding: {exc}") from exc
        except SessionBundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid session bundle: {exc}") from exc

        if not imported_orgs:
            raise HTTPException(status_code=400, detail="No importable org sessions found in the bundle.")

        try:
            orgs = [OrgConfig.model_validate(item) for item in imported_orgs]
            save_org_credentials(self._require_orgs_file(), orgs)
            self.org_pool.replace_orgs(orgs)
            await self.org_pool.refresh_agents(list(self.gateway_config.model_aliases))
        except (ConfigError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        update_import_history(
            self.history_output_path,
            {
                "filename": filename,
                "bundle_version": bundle.bundle_version,
                "generated_at": bundle.generated_at,
                "expires_at": bundle.expires_at,
                "imported_org_count": len(imported_orgs),
                "skipped_org_count": len(skipped_orgs),
                "output_path": str(self._require_orgs_file()),
                "allow_expired": bool(allow_expired),
            },
        )
        return {
            "imported_org_count": len(imported_orgs),
            "skipped_org_count": len(skipped_orgs),
            "skipped_orgs": skipped_orgs,
        }

    def import_history(self) -> dict:
        if not self.history_output_path.exists():
            return {"history": [], "last_import": None}
        loaded = load_json_file(self.history_output_path)
        if isinstance(loaded, dict):
            return loaded
        return {"history": [], "last_import": None}

    def list_audit_entries(self) -> list[dict]:
        return [entry.model_dump(mode="json") for entry in self.audit_store.all().entries]

    def list_api_keys(self) -> list[dict]:
        last_used_by_key: dict[str, float] = {}
        runtime_states = self.api_key_registry.list_runtime_states()
        for entry in self.audit_store.all().entries:
            if entry.api_key_id and entry.api_key_id not in last_used_by_key:
                last_used_by_key[entry.api_key_id] = entry.happened_at

        records: list[dict] = []
        for key in self.api_key_registry.list_keys():
            runtime = runtime_states.get(key.id)
            records.append(
                {
                    "id": key.id,
                    "key": key.key,
                    "enabled": key.enabled,
                    "owner": key.owner,
                    "concurrency_limit": key.concurrency_limit,
                    "scopes": key.resolved_scopes(),
                    "last_used_at": (runtime.last_used_at if runtime else None) or last_used_by_key.get(key.id),
                    "active_requests": runtime.active_requests if runtime else 0,
                    "total_requests": runtime.total_requests if runtime else 0,
                    "success_requests": runtime.success_requests if runtime else 0,
                    "failed_requests": runtime.failed_requests if runtime else 0,
                    "key_preview": self._mask_key(key.key),
                }
            )
        return records

    def upsert_api_key(self, request: ApiKeyUpsertRequest) -> dict:
        keys = self.api_key_registry.list_keys()
        key_id = self._normalize_text(request.id)
        owner = self._normalize_text(request.owner)
        provided_secret = self._normalize_text(request.key)
        updated = False
        returned_secret = None
        if not key_id:
            key_id = self._generate_key_id(keys=keys, owner=owner, scopes=request.scopes)
        for index, existing in enumerate(keys):
            if existing.id != key_id:
                continue
            updated = True
            if provided_secret is not None:
                secret = provided_secret
                returned_secret = secret
            elif request.regenerate_key:
                secret = self._generate_api_key_secret(keys)
                returned_secret = secret
            else:
                secret = existing.key
            if not secret:
                raise HTTPException(status_code=400, detail="API key secret is required for new keys.")
            keys[index] = ApiKeyConfig(
                id=key_id,
                key=secret,
                enabled=request.enabled,
                owner=owner,
                concurrency_limit=request.concurrency_limit,
                scopes=request.scopes,
            )
            break

        if not updated:
            secret = provided_secret or self._generate_api_key_secret(keys)
            keys.append(
                ApiKeyConfig(
                    id=key_id,
                    key=secret,
                    enabled=request.enabled,
                    owner=owner,
                    concurrency_limit=request.concurrency_limit,
                    scopes=request.scopes,
                )
            )
            returned_secret = secret

        self._ensure_enabled_keys(keys)

        save_api_keys(self.api_keys_path, keys)
        self.api_key_registry.reload(load_api_keys(self.api_keys_path))
        saved_key = self.api_key_registry.get_by_id(key_id)
        if not saved_key:
            raise HTTPException(status_code=500, detail=f"Saved API key '{key_id}' could not be reloaded.")
        return {
            "id": key_id,
            "updated": updated,
            "owner": saved_key.owner,
            "enabled": saved_key.enabled,
            "concurrency_limit": saved_key.concurrency_limit,
            "scopes": saved_key.resolved_scopes(),
            "key_preview": self._mask_key(saved_key.key),
            "key": returned_secret,
        }

    def delete_api_key(self, key_id: str) -> dict:
        normalized_id = self._normalize_text(key_id)
        if not normalized_id:
            raise HTTPException(status_code=400, detail="API key id is required.")
        keys = self.api_key_registry.list_keys()
        remaining = [key for key in keys if key.id != normalized_id]
        if len(remaining) == len(keys):
            raise HTTPException(status_code=404, detail=f"API key '{normalized_id}' was not found.")
        self._ensure_enabled_keys(remaining)
        save_api_keys(self.api_keys_path, remaining)
        self.api_key_registry.reload(load_api_keys(self.api_keys_path))
        return {"id": normalized_id, "deleted": True}

    def create_api_key_batch(
        self,
        *,
        owner: str,
        count: int,
        scopes: list[str] | None,
        concurrency_limit: int | None,
        enabled: bool = True,
    ) -> dict:
        normalized_owner = self._normalize_text(owner)
        if not normalized_owner:
            raise HTTPException(status_code=400, detail="Owner is required for batch key creation.")
        total = int(count)
        if total < 1 or total > 100:
            raise HTTPException(status_code=400, detail="Batch count must be between 1 and 100.")
        keys = self.api_key_registry.list_keys()
        created_items: list[dict] = []
        for _ in range(total):
            key_id = self._generate_key_id(keys=keys, owner=normalized_owner, scopes=scopes)
            secret = self._generate_api_key_secret(keys)
            config = ApiKeyConfig(
                id=key_id,
                key=secret,
                enabled=enabled,
                owner=normalized_owner,
                concurrency_limit=concurrency_limit,
                scopes=scopes,
            )
            keys.append(config)
            created_items.append(
                {
                    "id": key_id,
                    "key": secret,
                    "owner": normalized_owner,
                    "enabled": enabled,
                    "concurrency_limit": concurrency_limit,
                    "scopes": config.resolved_scopes(),
                    "key_preview": self._mask_key(secret),
                }
            )
        self._ensure_enabled_keys(keys)
        save_api_keys(self.api_keys_path, keys)
        self.api_key_registry.reload(load_api_keys(self.api_keys_path))
        return {"count": len(created_items), "items": created_items}

    def settings(self) -> dict:
        return {
            "conversation_header": self.gateway_config.conversation_header,
            "timezone": self.gateway_config.timezone,
            "allow_empty_org_pool": self.gateway_config.allow_empty_org_pool,
            "health_cooldown_seconds": self.gateway_config.health_cooldown_seconds,
            "health_refresh_interval_seconds": self.gateway_config.health_refresh_interval_seconds,
            "mapping_ttl_seconds": self.gateway_config.mapping_ttl_seconds,
            "admin_warning_days": self.gateway_config.admin_warning_days,
            "audit_history_limit": self.gateway_config.audit_history_limit,
            "orgs_file": str(self._require_orgs_file()),
            "api_keys_file": str(self.api_keys_path),
            "history_output_file": str(self.history_output_path),
            "accounts_file": str(self.accounts_path),
            "account_state_file": str(self.account_state_path),
            "default_browser_provider": self.default_browser_provider,
            "supported_browser_providers": list(self.supported_browser_providers),
            "default_max_concurrency": self.default_max_concurrency,
            "models": [alias.model_dump(mode="json") for alias in self.gateway_config.model_aliases],
        }

    def _parse_account_import_content(self, content: str) -> list[ManagedAccountConfig]:
        text = str(content or "").strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            return self._parse_accounts_from_json(text)
        return self._parse_accounts_from_csv(text)

    def _parse_accounts_from_json(self, text: str) -> list[ManagedAccountConfig]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"账号 JSON 解析失败: {exc}") from exc
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("items") or payload.get("accounts") or []
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="账号 JSON 必须是数组，或对象内包含 items/accounts 数组。")
        accounts: list[ManagedAccountConfig] = []
        existing_accounts = self._load_accounts()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise HTTPException(status_code=400, detail=f"第 {index + 1} 条账号不是对象。")
            accounts.append(self._build_account_from_payload(row, existing_accounts, line_no=index + 1))
        return accounts

    def _parse_accounts_from_csv(self, text: str) -> list[ManagedAccountConfig]:
        buffer = io.StringIO(text)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        has_header = "email" in first_line.lower() and "expected_subdomain" in first_line.lower()
        existing_accounts = self._load_accounts()
        accounts: list[ManagedAccountConfig] = []
        if has_header:
            reader = csv.DictReader(buffer)
            for index, row in enumerate(reader, start=2):
                if not any(str(value or "").strip() for value in row.values()):
                    continue
                accounts.append(self._build_account_from_payload(row, existing_accounts, line_no=index))
            return accounts

        reader = csv.reader(io.StringIO(text))
        for index, row in enumerate(reader, start=1):
            if not row or not any(str(item or "").strip() for item in row):
                continue
            if len(row) < 3:
                raise HTTPException(status_code=400, detail=f"第 {index} 行至少需要 3 列: email,password,expected_subdomain")
            payload = {
                "email": row[0],
                "password": row[1],
                "expected_subdomain": row[2],
                "enabled": row[3] if len(row) > 3 else "true",
                "notes": row[4] if len(row) > 4 else "",
                "browser_provider": row[5] if len(row) > 5 else "",
                "fingerprint_seed": row[6] if len(row) > 6 else "",
            }
            accounts.append(self._build_account_from_payload(payload, existing_accounts, line_no=index))
        return accounts

    def _build_account_from_payload(
        self,
        payload: dict[str, Any],
        existing_accounts: list[ManagedAccountConfig],
        *,
        line_no: int,
    ) -> ManagedAccountConfig:
        email = self._normalize_text(payload.get("email"))
        password = self._normalize_text(payload.get("password"))
        expected_subdomain = self._normalize_subdomain(payload.get("expected_subdomain"))
        if not email or password is None or not expected_subdomain:
            raise HTTPException(
                status_code=400,
                detail=f"第 {line_no} 行缺少必填字段，必须提供 email、password、expected_subdomain。",
            )
        enabled_raw = payload.get("enabled", True)
        enabled = self._normalize_bool(enabled_raw)
        notes = str(payload.get("notes") or "").strip()
        browser_provider = self._normalize_browser_provider(payload.get("browser_provider"))
        fingerprint_seed = self._normalize_fingerprint_seed(payload.get("fingerprint_seed")) or self._generate_fingerprint_seed()
        account_id = self._normalize_text(payload.get("id"))
        if not account_id:
            account_id = self._generate_account_id(existing_accounts, email=email, expected_subdomain=expected_subdomain)
        now = time.time()
        return ManagedAccountConfig(
            id=account_id,
            email=email,
            password=password,
            expected_subdomain=expected_subdomain,
            enabled=enabled,
            notes=notes,
            browser_provider=browser_provider,
            fingerprint_seed=fingerprint_seed,
            created_at=now,
            updated_at=now,
        )

    def _select_accounts_for_fingerprint_reset(
        self,
        accounts: list[ManagedAccountConfig],
        request: ManagedAccountFingerprintResetRequest,
    ) -> list[ManagedAccountConfig]:
        if request.refresh_all:
            selected = [account for account in accounts if account.enabled]
        else:
            requested_ids = {self._normalize_text(value) for value in request.account_ids if self._normalize_text(value)}
            if not requested_ids:
                raise HTTPException(status_code=400, detail="请至少选择一个账号，或勾选 refresh_all。")
            selected = [account for account in accounts if account.id in requested_ids]
        if not selected:
            raise HTTPException(status_code=400, detail="没有匹配到可重置指纹的账号。")
        return selected

    def _select_refresh_accounts(
        self,
        accounts: list[ManagedAccountConfig],
        request: AccountRefreshRequest,
    ) -> list[ManagedAccountConfig]:
        if request.refresh_all:
            selected = [account for account in accounts if account.enabled]
        else:
            requested_ids = {self._normalize_text(value) for value in request.account_ids if self._normalize_text(value)}
            if not requested_ids:
                raise HTTPException(status_code=400, detail="请至少选择一个账号，或勾选 refresh_all。")
            selected = [account for account in accounts if account.id in requested_ids and account.enabled]
        if not selected and not request.verify_only:
            raise HTTPException(status_code=400, detail="没有可刷新的启用账号。")
        if request.verify_only and not selected and not request.refresh_all:
            raise HTTPException(status_code=400, detail="verify-only 模式下也需要选择已导入账号。")
        return selected

    def _resolve_job_browser_provider(
        self,
        request: AccountRefreshRequest,
        selected_accounts: list[ManagedAccountConfig],
    ) -> str:
        if request.browser_provider:
            return self._normalize_browser_provider(request.browser_provider)
        providers = sorted({account.browser_provider for account in selected_accounts if account.browser_provider})
        if len(providers) == 1:
            return self._normalize_browser_provider(providers[0])
        return self.default_browser_provider

    @staticmethod
    def _browser_provider_requires_headless(value: str | None) -> bool:
        return str(value or "").strip().lower() == "cloakbrowser"

    def _build_refresh_command(
        self,
        *,
        job_id: str,
        selected_accounts: list[ManagedAccountConfig],
        request: AccountRefreshRequest,
        browser_provider: str,
        max_concurrency: int,
        check_models: list[str],
    ) -> tuple[list[str], Path | None]:
        script_path = self.gateway_config_path.parent / "scripts" / "build_org_sessions_from_accounts.py"
        if not script_path.exists():
            raise HTTPException(status_code=500, detail=f"Missing collector script: {script_path}")

        command = [
            sys.executable,
            str(script_path),
            "--gateway-config",
            str(self.gateway_config_path),
            "--output",
            str(self._require_orgs_file()),
            "--state-output",
            str(self.account_state_path),
            "--browser-provider",
            browser_provider,
            "--max-concurrency",
            str(max_concurrency),
        ]
        if request.ignore_cooldown:
            command.append("--ignore-cooldown")
        if request.headless or self._browser_provider_requires_headless(browser_provider):
            command.append("--headless")
        for model_id in check_models:
            command.extend(["--check-model", model_id])

        temp_accounts_csv: Path | None = None
        if request.verify_only:
            command.append("--verify-only")
            if not request.refresh_all:
                for account in selected_accounts:
                    command.extend(["--only-account", account.expected_subdomain])
        else:
            self.account_jobs_root.mkdir(parents=True, exist_ok=True)
            temp_accounts_csv = self.account_jobs_root / f"{job_id}-accounts.csv"
            self._write_accounts_csv(temp_accounts_csv, selected_accounts)
            command.extend(["--accounts-csv", str(temp_accounts_csv)])
            if request.refresh_only:
                command.append("--refresh-only")
        return command, temp_accounts_csv

    def _write_accounts_csv(self, path: Path, accounts: list[ManagedAccountConfig]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email", "password", "expected_subdomain", "enabled", "notes", "browser_provider", "fingerprint_seed"])
            for account in accounts:
                writer.writerow(
                    [
                        account.email,
                        account.password,
                        account.expected_subdomain,
                        "true" if account.enabled else "false",
                        account.notes,
                        account.browser_provider or "",
                        account.fingerprint_seed or "",
                    ]
                )

    def _run_refresh_job(
        self,
        job_id: str,
        command: list[str],
        temp_accounts_csv: Path | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._update_job(job_id, status="running", started_at=time.time())
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.gateway_config_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            self._finish_job(job_id, exit_code=-1, error=str(exc))
            self._cleanup_temp_file(temp_accounts_csv)
            return

        output_lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            normalized = line.rstrip()
            output_lines.append(normalized)
            self._append_job_log(job_id, normalized)
        exit_code = process.wait()
        summary = self._parse_refresh_summary(output_lines)
        org_pool_reloaded = False
        reload_error = ""
        try:
            if self.gateway_refresh_callback:
                future = asyncio.run_coroutine_threadsafe(self.gateway_refresh_callback(), loop)
                future.result(timeout=180)
                org_pool_reloaded = True
            else:
                self.org_pool.reload_from_file()
                org_pool_reloaded = True
        except Exception as exc:
            reload_error = f"刷新 org 池失败: {exc}"
            self._append_job_log(job_id, reload_error)

        error_text = reload_error
        if exit_code != 0 and not error_text:
            error_text = self._derive_refresh_error(job_id) or "账号刷新脚本返回非 0，请查看任务日志。"
        self._finish_job(
            job_id,
            exit_code=exit_code,
            error=error_text,
            summary=summary,
            org_pool_reloaded=org_pool_reloaded,
        )
        self._cleanup_temp_file(temp_accounts_csv)

    def _parse_refresh_summary(self, lines: list[str]) -> dict[str, Any]:
        patterns = {
            "processed_count": re.compile(r"^Processed\s+(\d+)\s+account\(s\)\s+this run$", re.I),
            "success_count": re.compile(r"^Successful session exports:\s+(\d+)$", re.I),
            "failure_count": re.compile(r"^Failed session exports:\s+(\d+)$", re.I),
            "skipped_count": re.compile(r"^Skipped account\(s\):\s+(\d+)$", re.I),
        }
        summary: dict[str, Any] = {}
        for line in lines:
            for key, pattern in patterns.items():
                match = pattern.match(line.strip())
                if match:
                    summary[key] = int(match.group(1))
            if line.startswith("Wrote org session pool:"):
                summary["org_output_path"] = line.split(":", 1)[1].strip()
            elif line.startswith("Wrote account runtime state:"):
                summary["account_state_path"] = line.split(":", 1)[1].strip()
            elif line.startswith("Browser provider:"):
                summary["browser_provider_runtime"] = line.split(":", 1)[1].strip()
        return summary

    def _update_job(self, job_id: str, **changes: Any) -> None:
        with self._job_lock:
            if self._account_refresh_job.get("id") != job_id:
                return
            self._account_refresh_job.update(changes)

    def _append_job_log(self, job_id: str, line: str) -> None:
        with self._job_lock:
            if self._account_refresh_job.get("id") != job_id:
                return
            log_lines = list(self._account_refresh_job.get("log_lines", []))
            log_lines.append(line)
            self._account_refresh_job["log_lines"] = log_lines[-200:]

    def _finish_job(
        self,
        job_id: str,
        *,
        exit_code: int,
        error: str,
        summary: dict[str, Any] | None = None,
        org_pool_reloaded: bool = False,
    ) -> None:
        summary = summary or {}
        success_count = int(summary.get("success_count") or 0)
        failure_count = int(summary.get("failure_count") or 0)
        skipped_count = int(summary.get("skipped_count") or 0)

        status = "succeeded"
        if exit_code != 0 or failure_count > 0:
            status = "completed_with_errors" if success_count > 0 else "failed"
        elif skipped_count > 0 and success_count == 0:
            status = "skipped"
        self._update_job(
            job_id,
            status=status,
            finished_at=time.time(),
            exit_code=exit_code,
            error=error,
            summary=summary,
            org_pool_reloaded=org_pool_reloaded,
        )

    def _derive_refresh_error(self, job_id: str) -> str:
        with self._job_lock:
            if self._account_refresh_job.get("id") != job_id:
                return ""
            account_ids = list(self._account_refresh_job.get("account_ids", []))
        if not account_ids:
            return ""

        state_rows = self._load_account_runtime_rows()
        failing_rows: list[dict[str, Any]] = []
        for account_id in account_ids:
            row = state_rows.get(str(account_id))
            if not isinstance(row, dict):
                continue
            auth_state = str(row.get("auth_state") or "").strip().lower()
            if auth_state in {"", "ready", "cooldown", "disabled"}:
                continue
            failing_rows.append(row)

        if not failing_rows:
            return ""

        summaries = [self._format_refresh_failure_row(row) for row in failing_rows[:3]]
        if len(failing_rows) == 1:
            return summaries[0]
        suffix = ""
        if len(failing_rows) > len(summaries):
            suffix = f" 等 {len(failing_rows)} 个账号失败"
        return "；".join(summaries) + suffix

    def _format_refresh_failure_row(self, row: dict[str, Any]) -> str:
        account_label = (
            self._normalize_text(row.get("account_id"))
            or self._normalize_text(row.get("expected_subdomain"))
            or self._normalize_text(row.get("email"))
            or "unknown"
        )
        auth_state = str(row.get("auth_state") or "").strip().lower()
        runtime_provider = str(row.get("browser_provider") or "").strip().lower()
        last_error = self._summarize_runtime_error(row.get("last_error"))

        if auth_state in {"captcha_blocked", "browser_unsupported"} and "obscura" in runtime_provider:
            return f"{account_label}: Obscura 当前无法兼容 Retool 登录页，请改用 GeekEZ 重新刷新。"
        if auth_state == "captcha_blocked":
            return f"{account_label}: 登录页被 Cloudflare 或 CAPTCHA 拦截。"
        if auth_state == "browser_unsupported":
            return f"{account_label}: 当前浏览器后端无法完成 Retool 登录页渲染。"
        if auth_state == "mfa_required":
            return f"{account_label}: 账号进入 MFA/二次验证流程。"
        if auth_state == "workspace_bridge_failed":
            return f"{account_label}: 登录成功，但 workspace 登录态桥接失败。"
        if auth_state == "login_required":
            return f"{account_label}: 登录失败，{last_error or '请检查账号密码或站点状态。'}"
        if last_error:
            return f"{account_label}: {last_error}"
        return f"{account_label}: 刷新失败，请查看任务日志。"

    @staticmethod
    def _summarize_runtime_error(value: Any, limit: int = 180) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3]}..."

    def _cleanup_temp_file(self, path: Path | None) -> None:
        if not path:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _assert_account_unique(
        self,
        accounts: list[ManagedAccountConfig],
        candidate: ManagedAccountConfig,
        ignore_id: str | None,
    ) -> None:
        identity_key = self._account_identity_key(candidate.email, candidate.expected_subdomain)
        for account in accounts:
            if ignore_id and account.id == ignore_id:
                continue
            if account.id == candidate.id:
                raise HTTPException(status_code=400, detail=f"账号 id 已存在: {candidate.id}")
            if self._account_identity_key(account.email, account.expected_subdomain) == identity_key:
                raise HTTPException(
                    status_code=400,
                    detail=f"账号 email/subdomain 已存在: {candidate.email} / {candidate.expected_subdomain}",
                )

    def _generate_account_id(
        self,
        accounts: list[ManagedAccountConfig],
        *,
        email: str,
        expected_subdomain: str,
    ) -> str:
        existing_ids = {account.id for account in accounts}
        base = self._slugify_fragment(expected_subdomain or email.split("@", 1)[0])
        for _ in range(32):
            candidate = base
            if candidate not in existing_ids:
                return candidate
            candidate = f"{base}-{secrets.token_hex(2)}"
            if candidate not in existing_ids:
                return candidate
        raise HTTPException(status_code=500, detail="Unable to allocate a unique account id.")

    @staticmethod
    def _account_identity_key(email: str, expected_subdomain: str) -> str:
        return f"{email.strip().lower()}|{expected_subdomain.strip().lower()}"

    @staticmethod
    def _mask_key(secret: str) -> str:
        if len(secret) <= 8:
            return "*" * len(secret)
        return f"{secret[:4]}...{secret[-4:]}"

    @staticmethod
    def _mask_password(secret: str) -> str:
        if not secret:
            return ""
        if len(secret) <= 2:
            return "*" * len(secret)
        return f"{secret[0]}{'*' * max(len(secret) - 2, 1)}{secret[-1]}"

    @staticmethod
    def _normalize_text(value: Any) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    @classmethod
    def _normalize_subdomain(cls, value: Any) -> str | None:
        normalized = cls._normalize_text(value)
        if normalized is None:
            return None
        lowered = normalized.lower().rstrip("/")
        if "://" in lowered:
            lowered = lowered.split("://", 1)[1]
        host = lowered.split("/", 1)[0]
        if host.endswith(".retool.com"):
            host = host[: -len(".retool.com")]
        return host.split(".", 1)[0].strip() or None

    def _normalize_browser_provider(self, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return self.default_browser_provider if hasattr(self, "default_browser_provider") else "geekez"
        if normalized not in {"auto", "geekez", "playwright", "obscura", "cloakbrowser"}:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 browser_provider: {normalized}，当前仅支持 obscura / geekez / playwright / cloakbrowser / auto。",
            )
        return normalized

    @staticmethod
    def _generate_fingerprint_seed() -> str:
        return secrets.token_hex(8)

    def _resolve_account_fingerprint_seed(
        self,
        *,
        explicit_seed: str | None,
        existing_seed: str | None,
        regenerate: bool,
    ) -> str:
        if regenerate:
            return self._generate_fingerprint_seed()
        if explicit_seed:
            return explicit_seed
        if existing_seed:
            return existing_seed
        return self._generate_fingerprint_seed()

    @staticmethod
    def _normalize_fingerprint_seed(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _normalize_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _slugify_fragment(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug or "account"

    @classmethod
    def _generate_key_id(cls, *, keys: list[ApiKeyConfig], owner: str | None, scopes: list[str] | None) -> str:
        existing_ids = {key.id for key in keys}
        owner_part = cls._slugify_fragment(owner or "office")
        scope_part = cls._scope_hint(scopes)
        for _ in range(32):
            candidate = f"{owner_part}-{scope_part}-{secrets.token_hex(3)}"
            if candidate not in existing_ids:
                return candidate
        raise HTTPException(status_code=500, detail="Unable to allocate a unique API key id.")

    @staticmethod
    def _generate_api_key_secret(keys: list[ApiKeyConfig]) -> str:
        existing_secrets = {key.key for key in keys}
        for _ in range(32):
            candidate = f"sk-rtg-{secrets.token_hex(20)}"
            if candidate not in existing_secrets:
                return candidate
        raise HTTPException(status_code=500, detail="Unable to allocate a unique API key secret.")

    @staticmethod
    def _scope_hint(scopes: list[str] | None) -> str:
        normalized = sorted({scope.strip().lower() for scope in (scopes or []) if scope and scope.strip()})
        if normalized == ["admin"]:
            return "admin"
        if normalized == ["inference"]:
            return "inference"
        if normalized == ["admin", "inference"]:
            return "hybrid"
        return "key"

    @staticmethod
    def _ensure_enabled_keys(keys: list[ApiKeyConfig]) -> None:
        if not any(key.enabled for key in keys):
            raise HTTPException(status_code=400, detail="At least one enabled API key must remain.")
