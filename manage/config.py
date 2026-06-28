import json
from pathlib import Path
from typing import List

from pydantic import ValidationError

from models import ApiKeyConfig, GatewayConfig, ManagedAccountConfig, OrgConfig


class ConfigError(Exception):
    pass


def _load_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def _write_json_file(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def load_gateway_config(config_path: Path) -> GatewayConfig:
    data = _load_json_file(config_path)
    config_dir = config_path.resolve().parent

    orgs_override = data.get("orgs_file")
    if orgs_override:
        orgs_path = Path(orgs_override)
        if not orgs_path.is_absolute():
            orgs_path = (config_dir / orgs_path).resolve()
        try:
            orgs_data = _load_json_file(orgs_path)
        except ConfigError:
            if data.get("allow_empty_org_pool"):
                orgs_data = []
            else:
                raise
        if not isinstance(orgs_data, list):
            raise ConfigError(f"Org credential file must be a JSON array: {orgs_path}")
        data["orgs"] = orgs_data

    try:
        config = GatewayConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid gateway config {config_path}: {exc}") from exc

    if not config.orgs and not config.allow_empty_org_pool:
        raise ConfigError("Gateway config must define at least one org")
    if not config.model_aliases:
        raise ConfigError("Gateway config must define at least one model alias")
    return config


def resolve_relative_path(base_file: Path, target_path: str | Path | None) -> Path | None:
    if not target_path:
        return None
    path = Path(target_path)
    if path.is_absolute():
        return path.resolve()
    return (base_file.resolve().parent / path).resolve()


def load_org_credentials(config_path: Path, allow_empty: bool = False) -> List[OrgConfig]:
    data = _load_json_file(config_path)
    if not isinstance(data, list):
        raise ConfigError(f"Org credential file must be a JSON array: {config_path}")
    try:
        orgs = [OrgConfig.model_validate(item) for item in data]
    except ValidationError as exc:
        raise ConfigError(f"Invalid org credential file {config_path}: {exc}") from exc
    if not orgs and not allow_empty:
        raise ConfigError(f"Org credential file has no org entries: {config_path}")
    return orgs


def load_api_keys(config_path: Path) -> List[ApiKeyConfig]:
    data = _load_json_file(config_path)
    if isinstance(data, list) and data and isinstance(data[0], str):
        data = [{"id": f"key-{idx+1}", "key": value, "enabled": True} for idx, value in enumerate(data)]
    try:
        keys = [ApiKeyConfig.model_validate(item) for item in data]
    except ValidationError as exc:
        raise ConfigError(f"Invalid API key config {config_path}: {exc}") from exc

    enabled_keys = [key for key in keys if key.enabled]
    if not enabled_keys:
        raise ConfigError("API key config has no enabled keys")
    return keys


def load_managed_accounts(config_path: Path, allow_empty: bool = True) -> List[ManagedAccountConfig]:
    data = _load_json_file(config_path)
    if not isinstance(data, list):
        raise ConfigError(f"Managed account file must be a JSON array: {config_path}")
    try:
        accounts = [ManagedAccountConfig.model_validate(item) for item in data]
    except ValidationError as exc:
        raise ConfigError(f"Invalid managed account file {config_path}: {exc}") from exc
    if not accounts and not allow_empty:
        raise ConfigError(f"Managed account file has no account entries: {config_path}")
    return accounts


def save_org_credentials(path: Path, orgs: List[OrgConfig]) -> None:
    payload = []
    for org in orgs:
        item = org.model_dump(by_alias=True, mode="json", exclude_none=True)
        payload.append(item)
    _write_json_file(path, payload)


def save_api_keys(path: Path, keys: List[ApiKeyConfig]) -> None:
    payload = [key.model_dump(mode="json", exclude_none=True) for key in keys]
    _write_json_file(path, payload)


def save_managed_accounts(path: Path, accounts: List[ManagedAccountConfig]) -> None:
    payload = [account.model_dump(mode="json", exclude_none=True) for account in accounts]
    _write_json_file(path, payload)
