import argparse
import asyncio
import csv
import importlib
import json
import os
import hashlib
import socket
import subprocess
import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
REPO_ROOT_DIR = PROJECT_DIR.parent
if str(REPO_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DIR))

from models import OrgConfig
from export_org_from_chrome import load_gateway_config_raw, resolve_model_checks, write_json_file
from session_bundle import DEFAULT_SESSION_BUNDLE_TTL_SECONDS, build_session_bundle
from singup import (
    GeekEZBrowserClient,
    OpenProfileResult,
    RetoolWorkspaceClient,
    acquire_workspace_authorization_payload,
    clear_current_origin_state,
    complete_login_onboarding_flow,
    ensure_page_foreground,
    format_for_log,
    get_cookie_value,
    is_login_onboarding_url,
    resolve_login_base_url,
    resolve_workspace_base_url,
    sanitize_for_log,
    sanitize_url_for_log,
    wait_for_workspace_ready,
)

DEFAULT_GEEKEZ_BROWSER_ROOT = REPO_ROOT_DIR / "GeekEZ.Browser-1.5.4-win-x64-portable"
DEFAULT_OBSCURA_WINDOWS_ROOT = PROJECT_DIR / "obscura" / "obscura-x86_64-windows"
DEFAULT_OBSCURA_LINUX_ROOT = PROJECT_DIR / "obscura" / "obscura-x86_64-linux"
DEFAULT_GEEKEZ_API_BASE = os.getenv("GEEKEZ_API_BASE", "http://127.0.0.1:12138")
DEFAULT_GEEKEZ_PROFILE_PREFIX = "retool-session"
DEFAULT_DIAGNOSTICS_ROOT = PROJECT_DIR / "runtime" / "diagnostics"


@dataclass(frozen=True)
class BrowserRuntimeConfig:
    mode: str
    browser_executable: Path | None = None
    geekez_api_base: str = ""
    geekez_profile_prefix: str = ""
    geekez_client: GeekEZBrowserClient | None = None
    obscura_executable: Path | None = None
    obscura_worker_executable: Path | None = None
    obscura_host: str = "127.0.0.1"
    obscura_stealth: bool = True
    cloakbrowser_module: Any | None = None


@dataclass
class ObscuraSessionHandle:
    process: subprocess.Popen[str]
    ws_endpoint: str
    storage_dir: Path
    port: int


@dataclass
class AccountRecord:
    email: str
    password: str
    expected_subdomain: str
    enabled: bool
    notes: str = ""
    browser_provider: str = ""
    fingerprint_seed: str = ""

    @property
    def account_id(self) -> str:
        return self.expected_subdomain or self.email

    @property
    def workspace_url(self) -> str:
        return resolve_workspace_base_url(self.expected_subdomain)

    @property
    def selector_values(self) -> set[str]:
        values = {
            self.account_id.strip().lower(),
            self.email.strip().lower(),
            self.expected_subdomain.strip().lower(),
            f"{self.expected_subdomain.strip().lower()}.retool.com",
        }
        return {value for value in values if value}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Retool org session pool from an account inventory CSV",
    )
    parser.add_argument(
        "--accounts-csv",
        default="accounts_import_template.csv",
        help="Path to account inventory CSV",
    )
    parser.add_argument(
        "--gateway-config",
        default="gateway_config.json",
        help="Path to gateway_config.json",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional output path for org session pool JSON. Defaults to gateway_config.orgs_file",
    )
    parser.add_argument(
        "--source-orgs",
        default="",
        help="Optional source org session pool JSON used by verify-only / refresh-only reads.",
    )
    parser.add_argument(
        "--profile-root",
        default="runtime/browser_profiles",
        help="Directory for persistent browser profiles used by the control plane",
    )
    parser.add_argument(
        "--state-output",
        default="runtime/account_sessions.json",
        help="Where to write account/session runtime state",
    )
    parser.add_argument(
        "--bundle-output",
        default="",
        help="Optional output path for exported session_bundle.json",
    )
    parser.add_argument(
        "--bundle-ttl-seconds",
        type=int,
        default=DEFAULT_SESSION_BUNDLE_TTL_SECONDS,
        help="TTL in seconds written into exported session bundles.",
    )
    parser.add_argument(
        "--check-model",
        action="append",
        default=[],
        help="Model alias id from gateway_config.json to verify in each org. Repeatable.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace output org session pool instead of merge-by-id/domain.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing org sessions in orgs.json without logging in again.",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="Refresh only accounts that are missing or not currently ready in saved state/org pool.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N enabled accounts after filtering. 0 means no limit.",
    )
    parser.add_argument(
        "--only-account",
        action="append",
        default=[],
        help="Only process matching account ids / emails / subdomains. Repeatable.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="Maximum concurrent account workers.",
    )
    parser.add_argument(
        "--ignore-cooldown",
        action="store_true",
        help="Ignore saved cooldown state and force a new login attempt.",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=900,
        help="Base cooldown in seconds after ordinary failures.",
    )
    parser.add_argument(
        "--blocking-cooldown-seconds",
        type=int,
        default=3600,
        help="Longer cooldown in seconds for captcha / MFA / agent-missing failures.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless",
    )
    parser.add_argument(
        "--browser-provider",
        default="auto",
        choices=["auto", "obscura", "geekez", "playwright", "cloakbrowser"],
        help="Browser backend. auto prefers GeekEZ API, then GeekEZ Browser.exe under Playwright, then Obscura. Use cloakbrowser for explicit headless CloakBrowser mode. Ordinary browsers are disabled.",
    )
    parser.add_argument(
        "--browser-executable",
        default="",
        help=(
            "Optional GeekEZ Browser.exe path or GeekEZ portable directory. "
            "Defaults to RETOOL_BROWSER_EXECUTABLE, then auto-detected GeekEZ portable. "
            "Ordinary browser executables are rejected."
        ),
    )
    parser.add_argument(
        "--diagnostics-root",
        default=str(DEFAULT_DIAGNOSTICS_ROOT),
        help="Directory where failed browser diagnostics are written.",
    )
    parser.add_argument(
        "--obscura-executable",
        default="",
        help="Optional Obscura executable path or extracted Obscura directory.",
    )
    parser.add_argument(
        "--obscura-host",
        default="127.0.0.1",
        help="Bind host used when starting Obscura CDP server. Default 127.0.0.1.",
    )
    parser.add_argument(
        "--obscura-no-stealth",
        action="store_true",
        help="Disable Obscura stealth mode.",
    )
    parser.add_argument(
        "--geekez-api-base",
        default=DEFAULT_GEEKEZ_API_BASE,
        help="GeekEZ local API base URL, default http://127.0.0.1:12138",
    )
    parser.add_argument(
        "--geekez-profile-prefix",
        default=DEFAULT_GEEKEZ_PROFILE_PREFIX,
        help="Profile name prefix when control plane creates or reuses GeekEZ profiles",
    )
    return parser.parse_args()


def resolve_output_path(args: argparse.Namespace, gateway_config_path: Path) -> Path:
    if args.output:
        return Path(args.output).resolve()

    gateway_config = load_gateway_config_raw(gateway_config_path.resolve())
    raw_orgs_file = gateway_config.get("orgs_file")
    if not raw_orgs_file:
        raise SystemExit("gateway_config.json 未配置 orgs_file，请显式传 --output")
    orgs_path = Path(str(raw_orgs_file))
    if not orgs_path.is_absolute():
        orgs_path = (gateway_config_path.resolve().parent / orgs_path).resolve()
    return orgs_path


def resolve_source_orgs_path(args: argparse.Namespace, gateway_config_path: Path) -> Path:
    if args.source_orgs:
        return Path(args.source_orgs).resolve()

    gateway_config = load_gateway_config_raw(gateway_config_path.resolve())
    raw_orgs_file = gateway_config.get("orgs_file")
    if raw_orgs_file:
        orgs_path = Path(str(raw_orgs_file))
        if not orgs_path.is_absolute():
            orgs_path = (gateway_config_path.resolve().parent / orgs_path).resolve()
        return orgs_path

    return resolve_output_path(args, gateway_config_path)


def normalize_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_fingerprint_seed(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized:
        return normalized
    return secrets.token_hex(8)


def load_accounts(csv_path: Path) -> list[AccountRecord]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"email", "password", "expected_subdomain", "enabled"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Account CSV missing required columns: {', '.join(sorted(missing))}")

        accounts: list[AccountRecord] = []
        for row in reader:
            account = AccountRecord(
                email=str(row["email"]).strip(),
                password=str(row["password"]).strip(),
                expected_subdomain=str(row["expected_subdomain"]).strip(),
                enabled=normalize_bool(row["enabled"]),
                notes=str(row["notes"]).strip(),
                browser_provider=str(row.get("browser_provider") or "").strip().lower(),
                fingerprint_seed=normalize_fingerprint_seed(row.get("fingerprint_seed")),
            )
            if not account.enabled:
                continue
            if not account.email or not account.password or not account.expected_subdomain:
                raise SystemExit(f"Invalid account row with empty required value: {row}")
            accounts.append(account)
        return accounts


def filter_accounts(accounts: list[AccountRecord], args: argparse.Namespace) -> list[AccountRecord]:
    filtered = accounts
    selectors = [str(value).strip().lower() for value in args.only_account if str(value).strip()]
    if selectors:
        selector_set = set(selectors)
        filtered = [account for account in filtered if account.selector_values & selector_set]
        if not filtered:
            raise SystemExit("No enabled accounts matched --only-account")

    if int(args.limit) > 0:
        filtered = filtered[: int(args.limit)]

    if not filtered:
        raise SystemExit("No enabled accounts left after filtering")

    return filtered


def ensure_runtime_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_existing_account_states(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}

    states: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("account_id") or "").strip()
        if key:
            states[key] = row
    return states


def merge_state_rows(previous_states: dict[str, dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = dict(previous_states)
    processed_keys: list[str] = []
    for row in new_rows:
        key = str(row.get("account_id") or "").strip()
        if not key:
            continue
        merged[key] = row
        processed_keys.append(key)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in processed_keys:
        if key in seen or key not in merged:
            continue
        rows.append(merged[key])
        seen.add(key)

    for key, row in merged.items():
        if key in seen:
            continue
        rows.append(row)
        seen.add(key)
    return rows


def derive_subdomain_from_domain_name(domain_name: str) -> str:
    normalized = str(domain_name).strip().rstrip("/")
    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    host = normalized.split("/", 1)[0]
    if host.endswith(".retool.com"):
        return host[: -len(".retool.com")]
    return host.split(".", 1)[0]


def derive_account_id_from_org_record(org: dict[str, Any]) -> str:
    for field_name in ("source_account_id", "id", "domain_name"):
        value = str(org.get(field_name) or "").strip()
        if value:
            return value
    return ""


def build_org_selector_values(org: dict[str, Any]) -> set[str]:
    domain_name = str(org.get("domain_name") or "").strip().lower()
    subdomain = derive_subdomain_from_domain_name(domain_name).strip().lower() if domain_name else ""
    values = {
        derive_account_id_from_org_record(org).strip().lower(),
        str(org.get("source_email") or "").strip().lower(),
        str(org.get("id") or "").strip().lower(),
        domain_name,
        subdomain,
        f"{subdomain}.retool.com" if subdomain else "",
    }
    return {value for value in values if value}


def build_account_record_from_org_record(org: dict[str, Any]) -> AccountRecord:
    domain_name = str(org.get("domain_name") or "").strip()
    subdomain = derive_subdomain_from_domain_name(domain_name)
    email = str(org.get("source_email") or "").strip() or f"{subdomain}@session.verify"
    return AccountRecord(
        email=email,
        password="",
        expected_subdomain=subdomain,
        enabled=True,
        notes="verify-only",
    )


def index_existing_orgs(existing_orgs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_domain: dict[str, dict[str, Any]] = {}
    for org in existing_orgs:
        if not isinstance(org, dict):
            continue
        domain_name = str(org.get("domain_name") or "").strip().lower()
        if domain_name:
            by_domain[domain_name] = org
    return by_domain


def resolve_browser_executable_in_dir(browser_root: Path) -> Path | None:
    geekez_executable = browser_root / "GeekEZ Browser.exe"
    if geekez_executable.exists():
        return geekez_executable

    nested_matches = sorted(browser_root.glob("**/GeekEZ Browser.exe"))
    if nested_matches:
        return nested_matches[0]

    return None


def resolve_obscura_executable_in_dir(obscura_root: Path) -> tuple[Path, Path | None] | None:
    platform_candidates = []
    if os.name == "nt":
        platform_candidates.extend(
            [
                obscura_root / "obscura.exe",
                obscura_root / "obscura-x86_64-windows" / "obscura.exe",
            ]
        )
    else:
        platform_candidates.extend(
            [
                obscura_root / "obscura",
                obscura_root / "obscura-x86_64-linux" / "obscura",
            ]
        )

    for candidate in platform_candidates:
        if candidate.exists():
            worker_name = "obscura-worker.exe" if candidate.suffix.lower() == ".exe" else "obscura-worker"
            worker = candidate.parent / worker_name
            return candidate.resolve(), worker.resolve() if worker.exists() else None

    nested_name = "obscura.exe" if os.name == "nt" else "obscura"
    nested_matches = sorted(obscura_root.glob(f"**/{nested_name}"))
    for candidate in nested_matches:
        if candidate.name.lower().startswith("obscura"):
            worker_name = "obscura-worker.exe" if candidate.suffix.lower() == ".exe" else "obscura-worker"
            worker = candidate.parent / worker_name
            return candidate.resolve(), worker.resolve() if worker.exists() else None
    return None


def resolve_browser_executable(raw_value: str) -> Path | None:
    env_override = os.getenv("RETOOL_BROWSER_EXECUTABLE", "").strip()
    candidate_value = raw_value.strip() or env_override

    if not candidate_value and DEFAULT_GEEKEZ_BROWSER_ROOT.exists():
        candidate_value = str(DEFAULT_GEEKEZ_BROWSER_ROOT)

    if not candidate_value:
        return None

    candidate_path = Path(candidate_value).expanduser().resolve()
    if not candidate_path.exists():
        raise SystemExit(f"Browser executable path does not exist: {candidate_path}")

    if candidate_path.is_dir():
        resolved = resolve_browser_executable_in_dir(candidate_path)
        if resolved is None:
            raise SystemExit(f"Could not find GeekEZ Browser.exe under: {candidate_path}")
        return resolved.resolve()

    if candidate_path.name.lower() != "geekez browser.exe":
        raise SystemExit(
            "Ordinary browser executable is not allowed. Pass GeekEZ Browser.exe or the GeekEZ portable directory."
        )

    return candidate_path


def resolve_obscura_executable(raw_value: str) -> tuple[Path | None, Path | None]:
    candidate_value = raw_value.strip()
    if not candidate_value:
        default_root = DEFAULT_OBSCURA_WINDOWS_ROOT if os.name == "nt" else DEFAULT_OBSCURA_LINUX_ROOT
        if default_root.exists():
            candidate_value = str(default_root)

    if not candidate_value:
        return None, None

    candidate_path = Path(candidate_value).expanduser().resolve()
    if not candidate_path.exists():
        raise SystemExit(f"Obscura executable path does not exist: {candidate_path}")

    if candidate_path.is_dir():
        resolved = resolve_obscura_executable_in_dir(candidate_path)
        if resolved is None:
            raise SystemExit(f"Could not find obscura executable under: {candidate_path}")
        return resolved

    expected_name = "obscura.exe" if os.name == "nt" else "obscura"
    if candidate_path.name.lower() != expected_name:
        raise SystemExit(f"Unsupported Obscura executable path: {candidate_path}")

    worker_name = "obscura-worker.exe" if candidate_path.suffix.lower() == ".exe" else "obscura-worker"
    worker = candidate_path.parent / worker_name
    return candidate_path, worker.resolve() if worker.exists() else None


def build_geekez_profile_name(prefix: str, account: AccountRecord) -> str:
    base_name = account.expected_subdomain or account.account_id or "retool-account"
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", base_name).strip("-_")
    sanitized_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix.strip() or DEFAULT_GEEKEZ_PROFILE_PREFIX).strip("-_")
    resolved_prefix = sanitized_prefix or DEFAULT_GEEKEZ_PROFILE_PREFIX
    resolved_suffix = sanitized or "retool-account"
    return f"{resolved_prefix}-{resolved_suffix}"


def build_obscura_storage_dir(profile_root: Path, account: AccountRecord) -> Path:
    return profile_root / f"{account.expected_subdomain}-obscura"


def build_cloakbrowser_storage_dir(profile_root: Path, account: AccountRecord) -> Path:
    seed_hint = sanitize_path_component(account.fingerprint_seed[:12] if account.fingerprint_seed else "seed")
    return profile_root / f"{account.expected_subdomain}-cloakbrowser-{seed_hint}"


def derive_cloakbrowser_fingerprint_config(account: AccountRecord) -> dict[str, Any]:
    digest = hashlib.sha256(account.fingerprint_seed.encode("utf-8")).digest()
    width = 1366 if digest[0] % 2 == 0 else 1920
    height = 768 if width == 1366 else 1080
    color_scheme = "dark" if digest[1] % 3 == 0 else "light"
    timezone = "Asia/Shanghai" if digest[2] % 2 == 0 else "Asia/Chongqing"
    locale = "zh-CN"
    hardware_concurrency = 8 if digest[3] % 2 == 0 else 16
    device_memory = 8 if digest[4] % 2 == 0 else 16
    platform_version = "15.0.0" if digest[5] % 2 == 0 else "14.0.0"
    browser_major = 136 + (digest[6] % 2)
    browser_version = f"{browser_major}.0.0.0"
    user_agent = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
    )
    args = [
        f"--fingerprint={account.fingerprint_seed}",
        "--fingerprint-platform=windows",
        "--fingerprint-platform-version=10.0.0",
        f"--fingerprint-browser-version={browser_version}",
        "--fingerprint-gpu-vendor=Google Inc. (NVIDIA)",
        "--fingerprint-gpu-renderer=ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        f"--fingerprint-hardware-concurrency={hardware_concurrency}",
        f"--fingerprint-device-memory={device_memory}",
        f"--fingerprint-screen-width={width}",
        f"--fingerprint-screen-height={height}",
        "--fingerprint-noise=true",
    ]
    return {
        "seed": account.fingerprint_seed,
        "args": args,
        "user_agent": user_agent,
        "viewport": {"width": width, "height": height},
        "locale": locale,
        "timezone": timezone,
        "color_scheme": color_scheme,
    }


def allocate_loopback_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def resolve_browser_runtime_config(args: argparse.Namespace) -> BrowserRuntimeConfig:
    provider = str(args.browser_provider).strip().lower() or "auto"
    obscura_executable, obscura_worker_executable = resolve_obscura_executable(str(args.obscura_executable or ""))
    obscura_host = str(args.obscura_host).strip() or "127.0.0.1"
    obscura_stealth = not bool(args.obscura_no_stealth)
    geekez_api_base = str(args.geekez_api_base).strip() or DEFAULT_GEEKEZ_API_BASE
    geekez_profile_prefix = str(args.geekez_profile_prefix).strip() or DEFAULT_GEEKEZ_PROFILE_PREFIX
    last_geekez_api_error: Exception | None = None
    browser_executable = resolve_browser_executable(args.browser_executable)
    if provider == "cloakbrowser":
        if not args.headless:
            raise SystemExit("CloakBrowser provider currently requires --headless.")
        try:
            cloakbrowser_module = importlib.import_module("cloakbrowser")
        except Exception as exc:
            raise SystemExit(
                "CloakBrowser provider requires Python package 'cloakbrowser'. "
                "Install it with: pip install cloakbrowser==0.4.3"
            ) from exc
        return BrowserRuntimeConfig(mode="cloakbrowser_headless", cloakbrowser_module=cloakbrowser_module)

    if provider == "obscura":
        if obscura_executable is None:
            raise SystemExit("Could not resolve Obscura executable. Ordinary browser fallback is disabled.")
        return BrowserRuntimeConfig(
            mode="obscura",
            obscura_executable=obscura_executable,
            obscura_worker_executable=obscura_worker_executable,
            obscura_host=obscura_host,
            obscura_stealth=obscura_stealth,
        )

    if provider in {"auto", "geekez"} and not args.headless:
        geekez_client = GeekEZBrowserClient(geekez_api_base)
        try:
            geekez_client.health()
        except Exception as exc:
            last_geekez_api_error = exc
        else:
            return BrowserRuntimeConfig(
                mode="geekez_api",
                geekez_api_base=geekez_api_base,
                geekez_profile_prefix=geekez_profile_prefix,
                geekez_client=geekez_client,
            )
    elif provider == "geekez" and args.headless:
        last_geekez_api_error = RuntimeError("GeekEZ API mode currently does not support --headless")

    if provider in {"auto", "geekez", "playwright"} and browser_executable is not None:
        return BrowserRuntimeConfig(mode="geekez_executable", browser_executable=browser_executable)

    if provider == "playwright":
        raise SystemExit("Could not resolve GeekEZ Browser.exe for Playwright mode. Ordinary browser fallback is disabled.")

    if provider == "auto" and obscura_executable is not None:
        return BrowserRuntimeConfig(
            mode="obscura",
            obscura_executable=obscura_executable,
            obscura_worker_executable=obscura_worker_executable,
            obscura_host=obscura_host,
            obscura_stealth=obscura_stealth,
        )

    if last_geekez_api_error is not None:
        fallback_label = (
            "GeekEZ Browser.exe or Obscura"
            if provider == "auto"
            else "GeekEZ Browser.exe"
        )
        raise SystemExit(
            f"GeekEZ API unavailable at {geekez_api_base}: {last_geekez_api_error}. "
            f"Also could not resolve {fallback_label}. Ordinary browser fallback is disabled."
        ) from last_geekez_api_error

    if provider == "geekez":
        raise SystemExit("Could not resolve GeekEZ API or GeekEZ Browser.exe. Ordinary browser fallback is disabled.")
    raise SystemExit("Could not resolve GeekEZ API, GeekEZ Browser.exe, or Obscura. Ordinary browser fallback is disabled.")


def classify_auth_state(error_text: str) -> str:
    normalized = error_text.strip().lower()
    if not normalized:
        return "unknown"

    if any(token in normalized for token in ("cloudflare", "cf-challenge", "just a moment", "captcha")):
        return "captcha_blocked"
    if any(
        token in normalized
        for token in (
            "frontend bootstrap failed",
            "entrypoint chunk",
            "cdn-cgi/challenge-platform",
            "login page shell loaded but form inputs never appeared",
        )
    ):
        return "browser_unsupported"
    if any(token in normalized for token in ("mfa", "2fa", "two-factor", "two factor", "verification code")):
        return "mfa_required"
    if "agent contract check failed" in normalized or "missing agents array" in normalized:
        return "agent_missing"
    if any(
        token in normalized
        for token in (
            "workspace auth bridge",
            "workspace 登录态尚未就绪",
            "workspace cookie jar missing",
            "auth bridge failed",
        )
    ):
        return "workspace_bridge_failed"
    if any(token in normalized for token in ("api/login failed", "login form not visible", "invalid login", "unauthorized")):
        return "login_required"
    return "login_required"


def resolve_cooldown_until(auth_state: str, failure_count: int, args: argparse.Namespace, now: int) -> int:
    if failure_count <= 0:
        return 0

    base_seconds = max(int(args.cooldown_seconds), 0)
    blocking_seconds = max(int(args.blocking_cooldown_seconds), base_seconds)
    selected_base = (
        blocking_seconds
        if auth_state in {"captcha_blocked", "mfa_required", "agent_missing", "browser_unsupported"}
        else base_seconds
    )
    if selected_base <= 0:
        return 0

    multiplier = min(max(failure_count, 1), 4)
    return now + (selected_base * multiplier)


def should_skip_due_to_cooldown(status: dict[str, Any], args: argparse.Namespace, now: int) -> bool:
    if args.ignore_cooldown:
        return False
    return parse_int(status.get("cooldown_until")) > now


def is_retry_blocked_state(auth_state: str) -> bool:
    return auth_state in {"cooldown", "disabled"}


def load_existing_orgs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else []


def merge_orgs(existing: list[dict[str, Any]], new_orgs: list[dict[str, Any]], replace: bool) -> list[dict[str, Any]]:
    if replace:
        return new_orgs

    by_key: dict[str, dict[str, Any]] = {}
    for org in existing:
        if not isinstance(org, dict):
            continue
        key = str(org.get("id") or org.get("domain_name") or "")
        if key:
            by_key[key] = org
    for org in new_orgs:
        key = str(org.get("id") or org.get("domain_name") or "")
        if key:
            by_key[key] = org
    return list(by_key.values())


async def snapshot_page(page) -> dict[str, Any]:
    return await page.evaluate(
        """() => ({
            href: location.href,
            title: document.title,
            text: (document.body?.innerText || '').slice(0, 1500),
            inputs: Array.from(document.querySelectorAll('input')).map((el, idx) => ({
                idx,
                type: el.type || '',
                name: el.name || '',
                placeholder: el.getAttribute('placeholder') || '',
                testid: el.getAttribute('data-testid') || '',
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            })).slice(0, 20),
            buttons: Array.from(document.querySelectorAll('button')).map((el, idx) => ({
                idx,
                text: (el.innerText || '').trim(),
                testid: el.getAttribute('data-testid') || '',
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            })).slice(0, 20),
        })"""
    )


def sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "unknown"


def create_debug_events() -> dict[str, list[dict[str, Any]]]:
    return {
        "console": [],
        "pageerror": [],
        "requestfailed": [],
        "httpErrors": [],
    }


def attach_page_debug_listeners(page) -> dict[str, list[dict[str, Any]]]:
    debug_events = create_debug_events()

    def append_debug_event(bucket: str, payload: dict[str, Any], limit: int = 20) -> None:
        events = debug_events.setdefault(bucket, [])
        events.append(sanitize_for_log(payload))
        if len(events) > limit:
            del events[0 : len(events) - limit]

    page.on(
        "console",
        lambda message: append_debug_event(
            "console",
            {
                "type": message.type,
                "text": message.text,
            },
            limit=30,
        ),
    )
    page.on(
        "pageerror",
        lambda exc: append_debug_event(
            "pageerror",
            {
                "message": str(exc),
            },
            limit=20,
        ),
    )
    page.on(
        "requestfailed",
        lambda request: append_debug_event(
            "requestfailed",
            {
                "url": sanitize_url_for_log(request.url),
                "method": request.method,
                "resourceType": request.resource_type,
                "failure": request.failure,
            },
            limit=30,
        ),
    )
    page.on(
        "response",
        lambda response: (
            append_debug_event(
                "httpErrors",
                {
                    "url": sanitize_url_for_log(response.url),
                    "status": response.status,
                },
                limit=30,
            )
            if response.status >= 400 and "retool.com" in response.url
            else None
        ),
    )

    setattr(page, "_retool_debug_events", debug_events)
    return debug_events


async def wait_for_geekez_cdp_ready(debug_port: int, timeout_seconds: int = 20) -> None:
    deadline = time.time() + max(timeout_seconds, 1)
    last_error: Exception | None = None
    version_url = f"http://127.0.0.1:{debug_port}/json/version"

    while time.time() < deadline:
        try:
            with urlopen(version_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and payload.get("webSocketDebuggerUrl"):
                return
            last_error = RuntimeError(f"Missing webSocketDebuggerUrl: {format_for_log(payload, limit=500)}")
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.5)

    raise RuntimeError(f"GeekEZ CDP endpoint not ready on port {debug_port}: {last_error}")


async def wait_for_obscura_cdp_ready(host: str, port: int, timeout_seconds: int = 20) -> str:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    version_url = f"http://{host}:{port}/json/version"

    while time.time() < deadline:
        try:
            with urlopen(version_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and payload.get("webSocketDebuggerUrl"):
                return str(payload["webSocketDebuggerUrl"])
            last_error = RuntimeError(f"Missing webSocketDebuggerUrl: {format_for_log(payload, limit=500)}")
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.5)

    raise RuntimeError(f"Obscura CDP endpoint not ready on {host}:{port}: {last_error}")


async def start_obscura_session(
    account: AccountRecord,
    profile_root: Path,
    browser_config: BrowserRuntimeConfig,
) -> ObscuraSessionHandle:
    if browser_config.obscura_executable is None:
        raise RuntimeError("Obscura browser mode missing obscura executable")

    storage_dir = build_obscura_storage_dir(profile_root, account)
    storage_dir.mkdir(parents=True, exist_ok=True)
    port = allocate_loopback_port(browser_config.obscura_host)

    command = [
        str(browser_config.obscura_executable),
        "serve",
        "--host",
        browser_config.obscura_host,
        "--port",
        str(port),
        "--storage-dir",
        str(storage_dir),
    ]
    if browser_config.obscura_stealth:
        command.append("--stealth")

    env = os.environ.copy()
    if browser_config.obscura_worker_executable is not None:
        existing_path = env.get("PATH", "")
        worker_dir = str(browser_config.obscura_worker_executable.parent)
        env["PATH"] = worker_dir if not existing_path else f"{worker_dir}{os.pathsep}{existing_path}"

    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    process = subprocess.Popen(
        command,
        cwd=str(browser_config.obscura_executable.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )

    try:
        ws_endpoint = await wait_for_obscura_cdp_ready(browser_config.obscura_host, port)
    except Exception:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        raise

    return ObscuraSessionHandle(
        process=process,
        ws_endpoint=ws_endpoint,
        storage_dir=storage_dir,
        port=port,
    )


async def write_failure_diagnostics(
    *,
    page,
    account: AccountRecord,
    diagnostics_root: Path,
    status: dict[str, Any],
    exc: Exception,
) -> dict[str, str]:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    account_key = sanitize_path_component(account.account_id or account.expected_subdomain or account.email)
    diagnostics_dir = diagnostics_root / f"{timestamp}-{account_key}"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    html_path = diagnostics_dir / "page.html"
    screenshot_path = diagnostics_dir / "page.png"
    metadata_path = diagnostics_dir / "debug.json"

    debug_payload: dict[str, Any] = {
        "captured_at": int(time.time()),
        "account_id": account.account_id,
        "email": account.email,
        "expected_subdomain": account.expected_subdomain,
        "fingerprint_seed": account.fingerprint_seed,
        "workspace_url": account.workspace_url,
        "browser_provider": status.get("browser_provider") or status.get("browser_mode") or "",
        "browser_executable": status.get("browser_executable") or "",
        "geekez_api_base": status.get("geekez_api_base") or "",
        "geekez_profile_id": status.get("geekez_profile_id") or "",
        "geekez_profile_name": status.get("geekez_profile_name") or "",
        "geekez_debug_port": status.get("geekez_debug_port") or "",
        "error": str(exc),
    }

    if page is not None:
        try:
            debug_payload["final_url"] = sanitize_url_for_log(page.url)
        except Exception as url_exc:
            debug_payload["final_url_error"] = str(url_exc)

        try:
            debug_payload["title"] = await page.title()
        except Exception as title_exc:
            debug_payload["title_error"] = str(title_exc)

        try:
            debug_payload["snapshot"] = sanitize_for_log(await snapshot_page(page))
        except Exception as snapshot_exc:
            debug_payload["snapshot_error"] = str(snapshot_exc)

        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception as html_exc:
            debug_payload["html_error"] = str(html_exc)

        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as screenshot_exc:
            debug_payload["screenshot_error"] = str(screenshot_exc)

        try:
            debug_payload["debug_events"] = sanitize_for_log(getattr(page, "_retool_debug_events", create_debug_events()))
        except Exception as event_exc:
            debug_payload["debug_events_error"] = str(event_exc)

    metadata_path.write_text(json.dumps(sanitize_for_log(debug_payload), ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "diagnostic_dir": str(diagnostics_dir),
        "diagnostic_html_path": str(html_path if html_path.exists() else ""),
        "diagnostic_screenshot_path": str(screenshot_path if screenshot_path.exists() else ""),
        "diagnostic_json_path": str(metadata_path),
        "final_url": str(debug_payload.get("final_url") or ""),
    }


def clear_failure_diagnostics(status: dict[str, Any]) -> None:
    for field_name in (
        "diagnostic_dir",
        "diagnostic_html_path",
        "diagnostic_screenshot_path",
        "diagnostic_json_path",
        "final_url",
    ):
        status[field_name] = ""


def should_retry_geekez_session(exc: Exception, browser_config: BrowserRuntimeConfig, attempt_index: int, max_attempts: int) -> bool:
    if browser_config.mode != "geekez_api":
        return False
    if attempt_index + 1 >= max_attempts:
        return False

    message = str(exc).strip().lower()
    return any(
        token in message
        for token in (
            "target page, context or browser has been closed",
            "context or browser has been closed",
            "browser has been closed",
            "connection closed",
            "cdp endpoint not ready",
            "econnrefused",
            "websocket",
        )
    )


async def close_browser_session(
    browser_config: BrowserRuntimeConfig,
    browser,
    context,
    opened_profile: OpenProfileResult | None,
) -> None:
    if browser_config.mode == "geekez_api":
        if browser is not None:
            await browser.close()
        if opened_profile is not None and not opened_profile.was_already_running and browser_config.geekez_client is not None:
            try:
                browser_config.geekez_client.stop_profile(opened_profile.profile_id)
            except Exception:
                pass
        return

    if context is not None:
        await context.close()


def stop_obscura_session(obscura_session: ObscuraSessionHandle | None) -> None:
    if obscura_session is None:
        return
    process = obscura_session.process
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


async def wait_for_login_form(page) -> None:
    email_locator = page.locator('input[name="email"]').first
    password_locator = page.locator('input[name="password"]').first
    try:
        await email_locator.wait_for(state="visible", timeout=60000)
        await password_locator.wait_for(state="visible", timeout=60000)
    except PlaywrightTimeoutError as exc:
        snapshot = await snapshot_page(page)
        snapshot_text = str(snapshot.get("text") or "").lower()
        html = ""
        try:
            html = (await page.content()).lower()
        except Exception:
            html = ""

        if any(token in snapshot_text or token in html for token in ("cf_clearance", "cloudflare", "/cdn-cgi/challenge-platform/")):
            raise RuntimeError(
                "Login page blocked by Cloudflare challenge under current browser backend: "
                f"{snapshot}"
            ) from exc

        if (
            "entrypointchunk" in snapshot_text
            or "entrypoint-chunk" in html
            or "src-index-" in snapshot_text
            or "src-index-" in html
        ):
            raise RuntimeError(
                "Login page frontend bootstrap failed under current browser backend; "
                "login page shell loaded but form inputs never appeared: "
                f"{snapshot}"
            ) from exc

        raise RuntimeError(f"Login form not visible: {snapshot}") from exc


async def login_account(page, account: AccountRecord) -> dict[str, Any]:
    await ensure_page_foreground(page)
    await page.goto(resolve_login_base_url(), wait_until="domcontentloaded", timeout=60000)
    await clear_current_origin_state(page)
    await page.goto("https://login.retool.com/auth/login", wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    if page.url.startswith(account.workspace_url):
        return {"status": 200, "body": {"alreadyLoggedIn": True}}

    await wait_for_login_form(page)
    await page.locator('input[name="email"]').first.fill(account.email)
    await page.locator('input[name="password"]').first.fill(account.password)

    async with page.expect_response(
        lambda resp: "/api/login" in resp.url and resp.request.method == "POST",
        timeout=60000,
    ) as login_info:
        await page.get_by_test_id("SignUp::SubmitEmailAndPassword").click()

    response = await login_info.value
    try:
        body = await response.json()
    except Exception:
        body = await response.text()
    return {"status": response.status, "body": body}


async def ensure_workspace_session(page, account: AccountRecord, login_payload: Any) -> None:
    workspace_base_url = account.workspace_url
    await page.wait_for_timeout(5000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    if not page.url.startswith(workspace_base_url):
        if is_login_onboarding_url(page.url):
            await complete_login_onboarding_flow(page)
        if not page.url.startswith(workspace_base_url):
            authorization_payload = login_payload if isinstance(login_payload, dict) else None
            if authorization_payload is None:
                authorization_payload = await acquire_workspace_authorization_payload(page)
            await wait_for_workspace_ready(page, workspace_base_url, authorization_payload=authorization_payload)

    await page.goto(workspace_base_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass


async def verify_agent_contract(page, account: AccountRecord, model_checks: list[dict[str, str]]) -> None:
    if not model_checks:
        return

    workspace_client = RetoolWorkspaceClient(page, account.workspace_url)
    agents_payload = await workspace_client.get_agents()
    agents = agents_payload.get("agents") if isinstance(agents_payload, dict) else None
    if not isinstance(agents, list):
        raise RuntimeError("Retool /api/agents 响应缺少 agents 数组")

    for check in model_checks:
        matched = False
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if check["agent_name"] and agent.get("name") != check["agent_name"]:
                continue
            if check["model_name"] and agent.get("data", {}).get("model") != check["model_name"]:
                continue
            matched = True
            break
        if not matched:
            raise RuntimeError(
                f"Agent contract check failed for alias '{check['id']}': "
                f"agent_name={check['agent_name']!r}, model_name={check['model_name']!r}"
            )


async def extract_session_record(context, account: AccountRecord) -> dict[str, Any]:
    xsrf_token = await get_cookie_value(
        context,
        account.workspace_url,
        {"xsrfToken", "xsrfTokenSameSite", "__Host-xsrfToken", "x-xsrf-token", "xsrf-token"},
    )
    access_token = await get_cookie_value(context, account.workspace_url, {"accessToken"})
    if not xsrf_token:
        raise RuntimeError(f"Workspace cookie jar missing xsrf token: {account.expected_subdomain}")
    if not access_token:
        raise RuntimeError(f"Workspace cookie jar missing accessToken: {account.expected_subdomain}")

    exported_org = OrgConfig.model_validate(
        {
            "id": account.expected_subdomain,
            "domain_name": f"{account.expected_subdomain}.retool.com",
            "x_xsrf_token": xsrf_token,
            "accessToken": access_token,
            "enabled": True,
        }
    ).model_dump(by_alias=True)
    exported_org["source_account_id"] = account.account_id
    exported_org["source_email"] = account.email
    exported_org["refreshed_at"] = int(time.time())
    return exported_org


async def verify_existing_org_session(
    playwright,
    org_record: dict[str, Any],
    model_checks: list[dict[str, str]],
    browser_config: BrowserRuntimeConfig | None,
    diagnostics_root: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    domain_name = str(org_record.get("domain_name") or "").strip()
    if not domain_name:
        raise RuntimeError("Existing org record missing domain_name")

    account = build_account_record_from_org_record(org_record)
    workspace_url = f"https://{domain_name.rstrip('/')}/"
    status: dict[str, Any] = {
        "account_id": str(org_record.get("source_account_id") or account.account_id or domain_name),
        "email": str(org_record.get("source_email") or account.email),
        "expected_subdomain": account.expected_subdomain,
        "auth_state": "unknown",
        "last_login_attempt_at": int(time.time()),
        "failure_count": 0,
        "last_error": "",
        "cooldown_until": 0,
        "profile_dir": "",
        "browser_provider": "verify_only",
        "browser_executable": "",
        "geekez_api_base": "",
        "geekez_profile_id": "",
        "geekez_profile_name": "",
        "geekez_debug_port": "",
        "geekez_profile_already_running": False,
        "last_login_success_at": 0,
        "last_session_refresh_at": parse_int(org_record.get("refreshed_at")),
        "last_verification_success_at": 0,
        "diagnostic_dir": "",
        "diagnostic_html_path": "",
        "diagnostic_screenshot_path": "",
        "diagnostic_json_path": "",
        "final_url": "",
    }

    browser = None
    context = None
    obscura_session: ObscuraSessionHandle | None = None
    page = None
    try:
        if browser_config is None:
            raise RuntimeError("verify-only requires Obscura or GeekEZ browser runtime.")

        if browser_config.mode == "obscura":
            obscura_session = await start_obscura_session(
                account=account,
                profile_root=PROJECT_DIR / "runtime" / "browser_profiles",
                browser_config=browser_config,
            )
            browser = await playwright.chromium.connect_over_cdp(obscura_session.ws_endpoint)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            status["browser_provider"] = "verify_only_obscura"
            status["browser_executable"] = str(browser_config.obscura_executable or "")
            status["profile_dir"] = str(obscura_session.storage_dir)
            status["geekez_debug_port"] = obscura_session.port
        elif browser_config.browser_executable is not None:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "executable_path": str(browser_config.browser_executable),
            }
            status["browser_provider"] = "verify_only_geekez_executable"
            status["browser_executable"] = str(browser_config.browser_executable)
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context()
        elif browser_config.mode == "cloakbrowser_headless":
            context, launch_status = await open_cloakbrowser_browser_session(
                account=account,
                profile_root=PROJECT_DIR / "runtime" / "browser_profiles",
                headless=True,
                browser_config=browser_config,
            )
            status.update(launch_status)
            status["browser_provider"] = "verify_only_cloakbrowser_headless"
        else:
            raise RuntimeError("verify-only requires Obscura, GeekEZ Browser.exe, or CloakBrowser headless. Ordinary browser fallback is disabled.")

        await context.add_cookies(
            [
                {
                    "name": "xsrfToken",
                    "value": str(org_record.get("x_xsrf_token") or ""),
                    "domain": domain_name,
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "accessToken",
                    "value": str(org_record.get("accessToken") or ""),
                    "domain": domain_name,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
        page = await context.new_page()
        attach_page_debug_listeners(page)
        await page.goto(workspace_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        await verify_agent_contract(page, account, model_checks)
        verified_org = dict(org_record)
        verified_org["enabled"] = True
        verified_org["refreshed_at"] = int(time.time())
        status["auth_state"] = "ready"
        status["last_verification_success_at"] = int(time.time())
        clear_failure_diagnostics(status)
        return verified_org, status
    except Exception as exc:
        status["failure_count"] = 1
        status["last_error"] = str(exc)
        status["auth_state"] = classify_auth_state(status["last_error"])
        status.update(
            await write_failure_diagnostics(
                page=page,
                account=account,
                diagnostics_root=diagnostics_root,
                status=status,
                exc=exc,
            )
        )
        return None, status
    finally:
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        stop_obscura_session(obscura_session)


async def open_geekez_browser_session(
    playwright,
    account: AccountRecord,
    browser_config: BrowserRuntimeConfig,
) -> tuple[Any, Any, OpenProfileResult, dict[str, Any]]:
    if browser_config.geekez_client is None:
        raise RuntimeError("GeekEZ browser mode missing geekez client")

    profile_name = build_geekez_profile_name(browser_config.geekez_profile_prefix, account)
    profile = browser_config.geekez_client.find_profile_by_name(profile_name)
    if profile is None:
        profile = browser_config.geekez_client.create_profile(profile_name)

    profile_id = str(profile.get("id") or "").strip()
    if not profile_id:
        raise RuntimeError(f"GeekEZ profile missing id: {profile_name}")

    opened_profile = browser_config.geekez_client.open_profile(profile_id)
    await wait_for_geekez_cdp_ready(opened_profile.debug_port)
    browser = await playwright.chromium.connect_over_cdp(opened_profile.cdp_endpoint)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    launch_status = {
        "browser_provider": "geekez_api",
        "geekez_api_base": browser_config.geekez_api_base,
        "geekez_profile_id": opened_profile.profile_id,
        "geekez_profile_name": opened_profile.name,
        "geekez_debug_port": opened_profile.debug_port,
        "geekez_profile_already_running": opened_profile.was_already_running,
        "profile_dir": "",
        "browser_executable": "",
    }
    return browser, context, opened_profile, launch_status


async def open_obscura_browser_session(
    playwright,
    account: AccountRecord,
    profile_root: Path,
    browser_config: BrowserRuntimeConfig,
) -> tuple[Any, Any, ObscuraSessionHandle, dict[str, Any]]:
    obscura_session = await start_obscura_session(
        account=account,
        profile_root=profile_root,
        browser_config=browser_config,
    )
    browser = await playwright.chromium.connect_over_cdp(obscura_session.ws_endpoint)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    launch_status = {
        "browser_provider": "obscura",
        "browser_executable": str(browser_config.obscura_executable or ""),
        "profile_dir": str(obscura_session.storage_dir),
        "geekez_api_base": "",
        "geekez_profile_id": "",
        "geekez_profile_name": "",
        "geekez_debug_port": obscura_session.port,
        "geekez_profile_already_running": False,
    }
    return browser, context, obscura_session, launch_status


async def open_playwright_browser_session(
    playwright,
    account: AccountRecord,
    profile_root: Path,
    headless: bool,
    browser_config: BrowserRuntimeConfig,
) -> tuple[Any, dict[str, Any]]:
    profile_dir = profile_root / account.expected_subdomain
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
    }
    launch_status = {
        "profile_dir": str(profile_dir),
        "geekez_profile_id": "",
        "geekez_profile_name": "",
        "geekez_debug_port": "",
        "geekez_profile_already_running": False,
    }

    if browser_config.mode == "geekez_executable":
        launch_options["executable_path"] = str(browser_config.browser_executable)
        launch_status["browser_provider"] = "geekez_executable"
        launch_status["browser_executable"] = str(browser_config.browser_executable)
    else:
        raise RuntimeError(f"Unsupported browser mode: {browser_config.mode}")

    context = await playwright.chromium.launch_persistent_context(**launch_options)
    return context, launch_status


async def open_cloakbrowser_browser_session(
    account: AccountRecord,
    profile_root: Path,
    headless: bool,
    browser_config: BrowserRuntimeConfig,
) -> tuple[Any, dict[str, Any]]:
    if browser_config.cloakbrowser_module is None:
        raise RuntimeError("CloakBrowser mode missing cloakbrowser module")

    fingerprint_config = derive_cloakbrowser_fingerprint_config(account)
    profile_dir = build_cloakbrowser_storage_dir(profile_root, account)
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = await browser_config.cloakbrowser_module.launch_persistent_context_async(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=fingerprint_config["args"],
        user_agent=fingerprint_config["user_agent"],
        viewport=fingerprint_config["viewport"],
        locale=fingerprint_config["locale"],
        timezone=fingerprint_config["timezone"],
        color_scheme=fingerprint_config["color_scheme"],
    )
    launch_status = {
        "browser_provider": "cloakbrowser_headless",
        "browser_executable": "cloakbrowser",
        "profile_dir": str(profile_dir),
        "fingerprint_seed": fingerprint_config["seed"],
        "fingerprint_user_agent": fingerprint_config["user_agent"],
        "fingerprint_locale": fingerprint_config["locale"],
        "fingerprint_timezone": fingerprint_config["timezone"],
        "fingerprint_viewport": f"{fingerprint_config['viewport']['width']}x{fingerprint_config['viewport']['height']}",
        "geekez_api_base": "",
        "geekez_profile_id": "",
        "geekez_profile_name": "",
        "geekez_debug_port": "",
        "geekez_profile_already_running": False,
    }
    return context, launch_status


async def process_account(
    playwright,
    account: AccountRecord,
    profile_root: Path,
    model_checks: list[dict[str, str]],
    headless: bool,
    browser_config: BrowserRuntimeConfig,
    previous_state: dict[str, Any] | None,
    args: argparse.Namespace,
    diagnostics_root: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    now = int(time.time())
    previous_failure_count = parse_int((previous_state or {}).get("failure_count"))
    previous_cooldown_until = parse_int((previous_state or {}).get("cooldown_until"))
    status: dict[str, Any] = {
        "account_id": account.account_id,
        "email": account.email,
        "expected_subdomain": account.expected_subdomain,
        "fingerprint_seed": account.fingerprint_seed,
        "auth_state": str((previous_state or {}).get("auth_state") or "unknown"),
        "last_login_attempt_at": now,
        "failure_count": previous_failure_count,
        "last_error": "",
        "cooldown_until": previous_cooldown_until,
        "profile_dir": "",
        "browser_provider": browser_config.mode,
        "browser_executable": str(browser_config.browser_executable) if browser_config.browser_executable else "",
        "geekez_api_base": browser_config.geekez_api_base,
        "geekez_profile_id": "",
        "geekez_profile_name": "",
        "geekez_debug_port": "",
        "geekez_profile_already_running": False,
        "fingerprint_user_agent": str((previous_state or {}).get("fingerprint_user_agent") or ""),
        "fingerprint_locale": str((previous_state or {}).get("fingerprint_locale") or ""),
        "fingerprint_timezone": str((previous_state or {}).get("fingerprint_timezone") or ""),
        "fingerprint_viewport": str((previous_state or {}).get("fingerprint_viewport") or ""),
        "last_login_success_at": parse_int((previous_state or {}).get("last_login_success_at")),
        "last_session_refresh_at": parse_int((previous_state or {}).get("last_session_refresh_at")),
        "last_verification_success_at": parse_int((previous_state or {}).get("last_verification_success_at")),
        "diagnostic_dir": str((previous_state or {}).get("diagnostic_dir") or ""),
        "diagnostic_html_path": str((previous_state or {}).get("diagnostic_html_path") or ""),
        "diagnostic_screenshot_path": str((previous_state or {}).get("diagnostic_screenshot_path") or ""),
        "diagnostic_json_path": str((previous_state or {}).get("diagnostic_json_path") or ""),
        "final_url": str((previous_state or {}).get("final_url") or ""),
    }
    if previous_state:
        for field_name in (
            "profile_dir",
            "browser_provider",
            "browser_executable",
            "geekez_api_base",
            "geekez_profile_id",
            "geekez_profile_name",
            "geekez_debug_port",
            "geekez_profile_already_running",
            "fingerprint_seed",
            "fingerprint_user_agent",
            "fingerprint_locale",
            "fingerprint_timezone",
            "fingerprint_viewport",
            "diagnostic_dir",
            "diagnostic_html_path",
            "diagnostic_screenshot_path",
            "diagnostic_json_path",
            "final_url",
        ):
            existing_value = previous_state.get(field_name)
            if existing_value not in (None, ""):
                status[field_name] = existing_value

    if should_skip_due_to_cooldown(status, args, now):
        status["auth_state"] = "cooldown"
        remaining_seconds = max(parse_int(status.get("cooldown_until")) - now, 0)
        status["last_error"] = (
            f"Skipped due to cooldown until {status['cooldown_until']} "
            f"({remaining_seconds}s remaining)"
        )
        return None, status

    max_browser_attempts = 2 if browser_config.mode == "geekez_api" else 1
    last_exception: Exception | None = None
    for attempt_index in range(max_browser_attempts):
        browser = None
        context = None
        opened_profile: OpenProfileResult | None = None
        obscura_session: ObscuraSessionHandle | None = None
        page = None
        try:
            if browser_config.mode == "geekez_api":
                browser, context, opened_profile, launch_status = await open_geekez_browser_session(
                    playwright=playwright,
                    account=account,
                    browser_config=browser_config,
                )
                page = context.pages[0] if context.pages else await context.new_page()
            elif browser_config.mode == "obscura":
                browser, context, obscura_session, launch_status = await open_obscura_browser_session(
                    playwright=playwright,
                    account=account,
                    profile_root=profile_root,
                    browser_config=browser_config,
                )
                page = context.pages[0] if context.pages else await context.new_page()
            elif browser_config.mode == "cloakbrowser_headless":
                context, launch_status = await open_cloakbrowser_browser_session(
                    account=account,
                    profile_root=profile_root,
                    headless=headless,
                    browser_config=browser_config,
                )
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                context, launch_status = await open_playwright_browser_session(
                    playwright=playwright,
                    account=account,
                    profile_root=profile_root,
                    headless=headless,
                    browser_config=browser_config,
                )
                page = context.pages[0] if context.pages else await context.new_page()

            attach_page_debug_listeners(page)
            status.update(launch_status)
            login_result = await login_account(page, account)
            if int(login_result["status"]) >= 400:
                raise RuntimeError(f"Retool /api/login failed: HTTP {login_result['status']} {login_result['body']}")

            status["last_login_success_at"] = int(time.time())
            status["auth_state"] = "ready"
            status["failure_count"] = 0
            status["cooldown_until"] = 0
            status["last_error"] = ""

            await ensure_workspace_session(page, account, login_result.get("body"))
            await verify_agent_contract(page, account, model_checks)
            org_record = await extract_session_record(context, account)
            status["last_session_refresh_at"] = int(time.time())
            status["last_verification_success_at"] = int(time.time())
            clear_failure_diagnostics(status)
            return org_record, status
        except Exception as exc:
            last_exception = exc
            if should_retry_geekez_session(exc, browser_config, attempt_index, max_browser_attempts):
                status["last_error"] = f"GeekEZ transient failure, retrying browser session: {exc}"
                await close_browser_session(browser_config, browser, context, opened_profile)
                stop_obscura_session(obscura_session)
                await asyncio.sleep(1.5)
                continue

            status["failure_count"] = previous_failure_count + 1
            status["last_error"] = str(exc)
            status["auth_state"] = classify_auth_state(status["last_error"])
            status["cooldown_until"] = resolve_cooldown_until(
                status["auth_state"],
                status["failure_count"],
                args,
                int(time.time()),
            )
            status.update(
                await write_failure_diagnostics(
                    page=page,
                    account=account,
                    diagnostics_root=diagnostics_root,
                    status=status,
                    exc=exc,
                )
            )
            return None, status
        finally:
            await close_browser_session(browser_config, browser, context, opened_profile)
            stop_obscura_session(obscura_session)

    if last_exception is None:
        raise RuntimeError("Unexpected empty browser attempt result")
    raise last_exception


async def process_accounts_batch(
    playwright,
    accounts: list[AccountRecord],
    profile_root: Path,
    model_checks: list[dict[str, str]],
    headless: bool,
    browser_config: BrowserRuntimeConfig,
    previous_states: dict[str, dict[str, Any]],
    args: argparse.Namespace,
    diagnostics_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    semaphore = asyncio.Semaphore(max(int(args.max_concurrency), 1))

    async def worker(account: AccountRecord) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        async with semaphore:
            return await process_account(
                playwright=playwright,
                account=account,
                profile_root=profile_root,
                model_checks=model_checks,
                headless=headless,
                browser_config=browser_config,
                previous_state=previous_states.get(account.account_id),
                args=args,
                diagnostics_root=diagnostics_root,
            )

    results = await asyncio.gather(*(worker(account) for account in accounts))
    exported_orgs: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    for org_record, status in results:
        state_rows.append(status)
        if org_record is not None:
            exported_orgs.append(org_record)
    return exported_orgs, state_rows


async def verify_orgs_batch(
    playwright,
    org_records: list[dict[str, Any]],
    model_checks: list[dict[str, str]],
    browser_config: BrowserRuntimeConfig | None,
    args: argparse.Namespace,
    diagnostics_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    semaphore = asyncio.Semaphore(max(int(args.max_concurrency), 1))

    async def worker(org_record: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        async with semaphore:
            return await verify_existing_org_session(
                playwright=playwright,
                org_record=org_record,
                model_checks=model_checks,
                browser_config=browser_config,
                diagnostics_root=diagnostics_root,
            )

    results = await asyncio.gather(*(worker(org_record) for org_record in org_records))
    exported_orgs: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    for org_record, status in results:
        state_rows.append(status)
        if org_record is not None:
            exported_orgs.append(org_record)
    return exported_orgs, state_rows


async def async_main(args: argparse.Namespace) -> None:
    accounts_csv = Path(args.accounts_csv).resolve()
    gateway_config_path = Path(args.gateway_config).resolve()
    output_path = resolve_output_path(args, gateway_config_path)
    source_orgs_path = resolve_source_orgs_path(args, gateway_config_path)
    state_output_path = Path(args.state_output).resolve()
    bundle_output_path = Path(args.bundle_output).resolve() if args.bundle_output else None
    profile_root = Path(args.profile_root).resolve()
    diagnostics_root = Path(args.diagnostics_root).resolve()
    if args.verify_only and args.refresh_only:
        raise SystemExit("--verify-only and --refresh-only cannot be used together")

    if int(args.max_concurrency) <= 0:
        raise SystemExit("--max-concurrency must be >= 1")
    if int(args.limit) < 0:
        raise SystemExit("--limit must be >= 0")

    model_checks = resolve_model_checks(gateway_config_path, args.check_model)
    previous_states = load_existing_account_states(state_output_path)
    existing_orgs = load_existing_orgs(source_orgs_path)
    existing_orgs_by_domain = index_existing_orgs(existing_orgs)

    if args.verify_only:
        verify_browser_config = resolve_browser_runtime_config(args)
        raw_orgs = existing_orgs
        if not raw_orgs:
            raise SystemExit("No existing org sessions found for --verify-only")

        filtered_orgs = raw_orgs
        selectors = [str(value).strip().lower() for value in args.only_account if str(value).strip()]
        if selectors:
            selector_set = set(selectors)
            filtered_orgs = [org for org in filtered_orgs if build_org_selector_values(org) & selector_set]
            if not filtered_orgs:
                raise SystemExit("No existing org sessions matched --only-account")
        if int(args.limit) > 0:
            filtered_orgs = filtered_orgs[: int(args.limit)]
        if not filtered_orgs:
            raise SystemExit("No org sessions left after filtering")

        async with async_playwright() as playwright:
            exported_orgs, state_rows = await verify_orgs_batch(
                playwright=playwright,
                org_records=filtered_orgs,
                model_checks=model_checks,
                browser_config=verify_browser_config,
                args=args,
                diagnostics_root=diagnostics_root,
            )

        merged_orgs = merge_orgs(existing_orgs, exported_orgs, replace=False)
        final_state_rows = merge_state_rows(previous_states, state_rows)
        browser_mode_label = "verify_only"
        browser_config = verify_browser_config
    else:
        browser_config = resolve_browser_runtime_config(args)
        accounts = filter_accounts(load_accounts(accounts_csv), args)
        if args.refresh_only:
            refresh_candidates: list[AccountRecord] = []
            for account in accounts:
                previous_state = previous_states.get(account.account_id) or {}
                existing_org = existing_orgs_by_domain.get(f"{account.expected_subdomain}.retool.com".lower())
                has_ready_state = str(previous_state.get("auth_state") or "").strip().lower() == "ready"
                has_existing_org = isinstance(existing_org, dict) and bool(existing_org.get("enabled", True))
                if has_ready_state and has_existing_org:
                    continue
                refresh_candidates.append(account)
            accounts = refresh_candidates
            if not accounts:
                print("No accounts need refresh under --refresh-only")
                return

        async with async_playwright() as playwright:
            exported_orgs, state_rows = await process_accounts_batch(
                playwright=playwright,
                accounts=accounts,
                profile_root=profile_root,
                model_checks=model_checks,
                headless=args.headless,
                browser_config=browser_config,
                previous_states=previous_states,
                args=args,
                diagnostics_root=diagnostics_root,
            )

        merged_orgs = merge_orgs(existing_orgs, exported_orgs, args.replace)
        final_state_rows = merge_state_rows(previous_states, state_rows)
        browser_mode_label = browser_config.mode

    ensure_runtime_parent(output_path)
    ensure_runtime_parent(state_output_path)
    write_json_file(output_path, merged_orgs)
    write_json_file(
        state_output_path,
        {
            "generated_at": int(time.time()),
            "account_count": len(final_state_rows),
            "success_count": len(exported_orgs),
            "failure_count": sum(
                1 for row in final_state_rows if row.get("auth_state") not in {"ready", "cooldown", "disabled"}
            ),
            "accounts": final_state_rows,
        },
    )
    if bundle_output_path is not None and exported_orgs:
        bundle = build_session_bundle(
            exported_orgs,
            verified_models=[check["id"] for check in model_checks],
            script_name="build_org_sessions_from_accounts.py",
            ttl_seconds=int(args.bundle_ttl_seconds),
        )
        write_json_file(bundle_output_path, bundle.model_dump(by_alias=True, mode="json"))

    actual_failure_count = sum(
        1 for row in final_state_rows if row.get("auth_state") not in {"ready", "cooldown", "disabled"}
    )
    skipped_count = sum(1 for row in final_state_rows if is_retry_blocked_state(str(row.get("auth_state") or "")))
    print(f"Processed {len(state_rows)} account(s) this run")
    print(f"Successful session exports: {len(exported_orgs)}")
    print(f"Failed session exports: {actual_failure_count}")
    print(f"Skipped account(s): {skipped_count}")
    print(f"Wrote org session pool: {output_path}")
    print(f"Wrote account runtime state: {state_output_path}")
    if bundle_output_path is not None and exported_orgs:
        print(f"Wrote session bundle: {bundle_output_path}")
    print(f"Max concurrency: {max(int(args.max_concurrency), 1)}")
    print(f"Browser provider: {browser_mode_label}")
    if browser_config is not None and browser_config.mode == "geekez_api":
        print(f"GeekEZ API base: {browser_config.geekez_api_base}")
        print(f"GeekEZ profile prefix: {browser_config.geekez_profile_prefix}")
    elif browser_config is not None and browser_config.mode == "cloakbrowser_headless":
        print("CloakBrowser mode: persistent headless context")
    elif browser_config is not None and browser_config.browser_executable is not None:
        print(f"GeekEZ executable: {browser_config.browser_executable}")
    if model_checks:
        print("Verified model aliases:", ", ".join(check["id"] for check in model_checks))
    if actual_failure_count > 0:
        raise SystemExit(1)


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
