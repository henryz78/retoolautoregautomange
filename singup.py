import asyncio
import base64
import csv
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

import requests
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


TEMP_MAIL_URL = "https://vip.215.im"
EMAIL = os.getenv("RET0OL_SIGNUP_EMAIL", "")
PASSWORD = os.getenv("RET0OL_SIGNUP_PASSWORD", "")
LAST_NAME = ""
AUTO_JOIN = False

GEEKEZ_API_BASE = os.getenv("GEEKEZ_API_BASE", "http://127.0.0.1:12138")
GEEKEZ_PROFILE_ID = os.getenv("GEEKEZ_PROFILE_ID", "")
GEEKEZ_AUTO_CREATE = os.getenv("GEEKEZ_AUTO_CREATE", "1") == "1"
GEEKEZ_PROFILE_NAME = os.getenv("GEEKEZ_PROFILE_NAME", "retool-signup")
RET0OL_SIGNUP_URL = "https://login.retool.com/auth/signup?source=navbarcta"
FOLLOWUP_URL = "https://login.retool.com/auth/followup"
VERIFY_EMAIL_URL_PART = "/auth/verifyEmail"
FOLLOWUP_URL_PART = "/auth/followup"
AUTH_ROLE_URL_PART = "/auth/role"
AUTH_FAMILIARITY_URL_PART = "/auth/familiarity"
AUTH_REFERRAL_FORM_URL_PART = "/auth/referralForm"
RET0OL_CLIENT_VERSION = os.getenv("RET0OL_CLIENT_VERSION", "4.14.0-59bdefe (Build 351982)")
RET0OL_AGENT_CREATE_HAR = os.getenv(
    "RET0OL_AGENT_CREATE_HAR",
    os.path.join(os.path.dirname(__file__), "3、创建agent配置agent模型.har"),
)
RET0OL_AGENT_NAME = os.getenv("RET0OL_AGENT_NAME", "gpt5")
RET0OL_AGENT_DESCRIPTION = os.getenv("RET0OL_AGENT_DESCRIPTION", "")
RET0OL_AGENT_MODEL = os.getenv("RET0OL_AGENT_MODEL", "gpt-5.5")
RET0OL_AGENT_PROVIDER = os.getenv("RET0OL_AGENT_PROVIDER", "openai").strip().lower()
RET0OL_AGENT_TEMPERATURE = float(os.getenv("RET0OL_AGENT_TEMPERATURE", "0.3"))
RET0OL_AGENT_MAX_ITERATIONS = int(os.getenv("RET0OL_AGENT_MAX_ITERATIONS", "50"))
RET0OL_AGENT_INSTRUCTIONS = os.getenv("RET0OL_AGENT_INSTRUCTIONS", "")
RET0OL_AGENT_THINKING_ENABLED = os.getenv("RET0OL_AGENT_THINKING_ENABLED", "0") == "1"
RET0OL_AGENT_CLAUDE_NAME = os.getenv("RET0OL_AGENT_CLAUDE_NAME", "claude")
RET0OL_AGENT_CLAUDE_DESCRIPTION = os.getenv("RET0OL_AGENT_CLAUDE_DESCRIPTION", "")
RET0OL_AGENT_CLAUDE_MODEL = os.getenv("RET0OL_AGENT_CLAUDE_MODEL", "claude-sonnet-4-6")
RET0OL_AGENT_CLAUDE_PROVIDER = os.getenv("RET0OL_AGENT_CLAUDE_PROVIDER", "anthropic").strip().lower()
RET0OL_AGENT_CLAUDE_TEMPERATURE = float(os.getenv("RET0OL_AGENT_CLAUDE_TEMPERATURE", "0.3"))
RET0OL_AGENT_CLAUDE_MAX_ITERATIONS = int(os.getenv("RET0OL_AGENT_CLAUDE_MAX_ITERATIONS", "10"))
RET0OL_AGENT_CLAUDE_INSTRUCTIONS = os.getenv("RET0OL_AGENT_CLAUDE_INSTRUCTIONS", "")
RET0OL_AGENT_CLAUDE_THINKING_ENABLED = os.getenv("RET0OL_AGENT_CLAUDE_THINKING_ENABLED", "0") == "1"
RET0OL_FOLLOWUP_RELOAD_RETRIES = int(os.getenv("RET0OL_FOLLOWUP_RELOAD_RETRIES", "3"))
RET0OL_SIGNUP_MAX_ATTEMPTS = int(os.getenv("RET0OL_SIGNUP_MAX_ATTEMPTS", "2"))
RET0OL_SIGNUP_OUTPUT_CSV = os.getenv(
    "RET0OL_SIGNUP_OUTPUT_CSV",
    os.path.join(os.path.dirname(__file__), "manage", "runtime", "signup_accounts.csv"),
)
RET0OL_SIGNUP_OUTPUT_JSONL = os.getenv("RET0OL_SIGNUP_OUTPUT_JSONL", "").strip()

SENSITIVE_KEYWORDS = ("password", "token", "authorization", "cookie", "email")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT_OUTPUT_FIELDNAMES = ("email", "password", "expected_subdomain", "enabled", "notes")
ONBOARDING_OPTION_PATTERNS: dict[str, tuple[str, ...]] = {
    AUTH_ROLE_URL_PART: (r"Software Engineering",),
    AUTH_FAMILIARITY_URL_PART: (r"Advanced", r"Proficient"),
    AUTH_REFERRAL_FORM_URL_PART: (r"Web search", r"AI chatbot", r"A friend or colleague"),
}


def configure_stdio_for_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def derive_local_part_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0].strip()
    if not local_part:
        raise ValueError("无法从邮箱地址提取本地前缀")
    return local_part


def derive_subdomain_from_email(email: str) -> str:
    return derive_local_part_from_email(email)


def derive_first_name_from_email(email: str) -> str:
    return derive_local_part_from_email(email)


def build_full_name(first_name: str, last_name: str) -> str:
    parts = [first_name.strip(), last_name.strip()]
    full_name = " ".join(part for part in parts if part)
    return full_name or first_name


class RestartSignupFlowError(RuntimeError):
    """Signals that the current signup attempt should be discarded and restarted."""


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested_value in value.items():
            if any(keyword in key.lower() for keyword in SENSITIVE_KEYWORDS):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = sanitize_for_log(nested_value)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    return value


def sanitize_url_for_log(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url

    sanitized_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        sanitized_pairs.append((key, "[redacted]" if any(keyword in key.lower() for keyword in SENSITIVE_KEYWORDS) else value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized_pairs), parts.fragment))


def format_for_log(value: Any, limit: int = 500) -> str:
    sanitized = sanitize_for_log(value)
    if isinstance(sanitized, (dict, list)):
        return json.dumps(sanitized, ensure_ascii=False)[:limit]
    return str(sanitized)[:limit]


def build_followup_url_from_auth_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, parts.netloc, FOLLOWUP_URL_PART, query, parts.fragment))


def resolve_followup_url_from_signup_response(signup_body: Any) -> str | None:
    if not isinstance(signup_body, dict):
        return None

    redirect_uri = signup_body.get("redirectUri")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        return None

    return urljoin(RET0OL_SIGNUP_URL, redirect_uri.strip())


def extract_email_from_text(text: str) -> str | None:
    match = EMAIL_PATTERN.search(text or "")
    if not match:
        return None
    return match.group(0).strip()


def resolve_temp_mail_retry_delay_seconds(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    anon_quota = data.get("anonQuota")
    if not isinstance(anon_quota, dict):
        return None

    for field_name in ("retryAfterSeconds", "penaltySeconds", "nextPenaltySeconds"):
        raw_value = anon_quota.get(field_name)
        try:
            delay = int(raw_value)
        except (TypeError, ValueError):
            continue
        if delay > 0:
            return delay
    return None


def resolve_signup_password(email: str) -> str:
    configured_password = PASSWORD.strip()
    return configured_password or email


def build_fresh_profile_name(profile_name_prefix: str) -> str:
    sanitized_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", profile_name_prefix.strip() or "retool-signup")
    sanitized_prefix = sanitized_prefix.strip("-_") or "retool-signup"
    timestamp = datetime.now().strftime("%m%d-%H%M%S")
    suffix = uuid4().hex[:6]
    return f"{sanitized_prefix}-{timestamp}-{suffix}"


def resolve_workspace_base_url(subdomain: str) -> str:
    return f"https://{subdomain}.retool.com"


def resolve_login_base_url() -> str:
    parts = urlsplit(RET0OL_SIGNUP_URL)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def append_query_params(url: str, extra_params: dict[str, Any]) -> str:
    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in extra_params.items():
        if value in (None, ""):
            continue
        query_pairs.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))


def build_redirect_url_from_auth_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    redirect_uri = payload.get("redirectUri")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        return None

    base_url = urljoin(f"{resolve_login_base_url()}/", redirect_uri.strip())
    query_params: dict[str, Any] = {}
    for field_name in (
        "partialRegistrationType",
        "partialRegistrationId",
        "domain",
        "email",
        "joinToken",
        "planKey",
    ):
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            query_params[field_name] = value.strip()

    return append_query_params(base_url, query_params)


def is_login_onboarding_url(url: str) -> bool:
    parts = urlsplit(url)
    login_parts = urlsplit(resolve_login_base_url())
    return (
        parts.scheme == login_parts.scheme
        and parts.netloc == login_parts.netloc
        and parts.path.startswith("/auth/")
        and parts.path not in {FOLLOWUP_URL_PART, VERIFY_EMAIL_URL_PART}
    )


def is_workspace_url(url: str, workspace_base_url: str | None = None) -> bool:
    parts = urlsplit(url)
    if workspace_base_url:
        workspace_parts = urlsplit(workspace_base_url)
        return parts.scheme == workspace_parts.scheme and parts.netloc == workspace_parts.netloc
    return parts.netloc.endswith(".retool.com") and parts.netloc != urlsplit(resolve_login_base_url()).netloc


def resolve_onboarding_option_patterns(url: str) -> tuple[str, ...]:
    for url_part, patterns in ONBOARDING_OPTION_PATTERNS.items():
        if url_part in url:
            return patterns
    return ()


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def summarize_secret_value_for_log(value: str, preview: int = 12) -> dict[str, Any]:
    raw_value = str(value or "")
    summary: dict[str, Any] = {
        "length": len(raw_value),
        "prefix": raw_value[:preview],
        "looksLikeJwt": raw_value.count(".") == 2,
    }
    if summary["looksLikeJwt"]:
        try:
            _, payload_segment, _ = raw_value.split(".", 2)
            padding = "=" * (-len(payload_segment) % 4)
            decoded = base64.urlsafe_b64decode(f"{payload_segment}{padding}".encode("utf-8")).decode("utf-8")
            payload = parse_json_text(decoded)
            summary["jwtPayload"] = sanitize_for_log(payload)
        except Exception as exc:
            summary["jwtPayloadError"] = str(exc)
    return summary


def build_unique_agent_name(preferred_name: str, existing_names: set[str]) -> str:
    if preferred_name not in existing_names:
        return preferred_name

    suffix = 2
    while True:
        candidate = f"{preferred_name}-{suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1


def _replace_template_field(pattern: str, replacement: str, template_data: str, field_name: str) -> str:
    updated, count = re.subn(pattern, replacement, template_data, count=1)
    if count != 1:
        raise RuntimeError(f"未能在 agent templateData 中定位字段: {field_name}")
    return updated


def replace_template_string_field(template_data: str, field_name: str, value: str) -> str:
    pattern = rf'("{re.escape(field_name)}",)"(?:\\.|[^"\\])*"'
    replacement = lambda match: f"{match.group(1)}{json.dumps(value, ensure_ascii=False)}"
    updated, count = re.subn(pattern, replacement, template_data, count=1)
    if count != 1:
        raise RuntimeError(f"未能在 agent templateData 中定位字符串字段: {field_name}")
    return updated


def upsert_template_string_field(
    template_data: str,
    field_name: str,
    value: str,
    *,
    insert_after_field: str | None = None,
) -> str:
    pattern = rf'("{re.escape(field_name)}",)"(?:\\.|[^"\\])*"'
    if re.search(pattern, template_data):
        return replace_template_string_field(template_data, field_name, value)

    if not insert_after_field:
        raise RuntimeError(f"未能在 agent templateData 中插入字符串字段: {field_name}")

    insert_pattern = rf'("{re.escape(insert_after_field)}",)"(?:\\.|[^"\\])*"'
    insert_fragment = f',{json.dumps(field_name, ensure_ascii=False)},{json.dumps(value, ensure_ascii=False)}'

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(0)}{insert_fragment}"

    updated, count = re.subn(insert_pattern, repl, template_data, count=1)
    if count != 1:
        raise RuntimeError(
            f"未能在 agent templateData 中新增字段 {field_name}，缺少锚点字段: {insert_after_field}"
        )
    return updated


def replace_template_numeric_field(template_data: str, field_name: str, value: int | float) -> str:
    pattern = rf'("{re.escape(field_name)}",)-?\d+(?:\.\d+)?'
    return _replace_template_field(pattern, rf'\g<1>{value}', template_data, field_name)


def replace_template_bool_field(template_data: str, field_name: str, value: bool) -> str:
    pattern = rf'("{re.escape(field_name)}",)(true|false)'
    bool_literal = "true" if value else "false"
    return _replace_template_field(pattern, rf"\g<1>{bool_literal}", template_data, field_name)


def extract_template_string_field(template_data: str, field_name: str) -> str | None:
    match = re.search(rf'"{re.escape(field_name)}","((?:\\.|[^"\\])*)"', template_data)
    if not match:
        return None
    return json.loads(f'"{match.group(1)}"')


def extract_template_numeric_field(template_data: str, field_name: str) -> int | float | None:
    match = re.search(rf'"{re.escape(field_name)}",(-?\d+(?:\.\d+)?)', template_data)
    if not match:
        return None
    raw_value = match.group(1)
    if "." in raw_value:
        return float(raw_value)
    return int(raw_value)


def extract_template_bool_field(template_data: str, field_name: str) -> bool | None:
    match = re.search(rf'"{re.escape(field_name)}",(true|false)', template_data)
    if not match:
        return None
    return match.group(1) == "true"


def load_agent_create_seed_payload() -> dict[str, Any]:
    try:
        with open(RET0OL_AGENT_CREATE_HAR, "r", encoding="utf-8") as har_file:
            har_payload = json.load(har_file)
    except OSError as exc:
        raise RuntimeError(f"无法读取 agent 创建 HAR 文件: {RET0OL_AGENT_CREATE_HAR}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"agent 创建 HAR 不是有效 JSON: {RET0OL_AGENT_CREATE_HAR}") from exc

    entries = har_payload.get("log", {}).get("entries", [])
    for entry in entries:
        request = entry.get("request", {})
        if request.get("method") != "POST":
            continue
        if urlsplit(str(request.get("url") or "")).path != "/api/workflow":
            continue

        post_text = request.get("postData", {}).get("text")
        if not isinstance(post_text, str) or not post_text.strip():
            raise RuntimeError("agent 创建 HAR 中的 /api/workflow 请求缺少 postData.text")

        payload = parse_json_text(post_text)
        if not isinstance(payload, dict):
            raise RuntimeError("agent 创建 HAR 中的 /api/workflow 请求体格式异常")
        return payload

    raise RuntimeError("agent 创建 HAR 中未找到 POST /api/workflow 请求")


def resolve_agent_provider(ai_settings: Any, provider: str) -> tuple[str, str, str]:
    if not isinstance(ai_settings, dict):
        raise RuntimeError("aiSettings 响应格式异常")

    if provider == "openai":
        resource_name = ai_settings.get("assistOpenAIResourceName")
        provider_id = "retoolAIBuiltIn::openAI"
        provider_name = "openAI"
    elif provider == "anthropic":
        resource_name = ai_settings.get("assistAnthropicResourceName")
        provider_id = "retoolAIBuiltIn::anthropic"
        provider_name = "anthropic"
    else:
        raise RuntimeError(f"不支持的 agent provider: {provider}")

    if not isinstance(resource_name, str) or not resource_name.strip():
        raise RuntimeError(f"aiSettings 中未返回 provider 资源名: {provider}")

    return provider_id, provider_name, resource_name.strip()


def resolve_agent_root_folder_id(agents_metadata: Any) -> int:
    if not isinstance(agents_metadata, dict):
        raise RuntimeError("agentsMetadata 响应格式异常")

    folders = agents_metadata.get("agentFolders")
    if not isinstance(folders, list) or not folders:
        raise RuntimeError("agentsMetadata 中缺少 agentFolders")

    for folder in folders:
        if isinstance(folder, dict) and folder.get("systemFolder") and folder.get("folderType") == "agent":
            folder_id = folder.get("id")
            if isinstance(folder_id, int):
                return folder_id

    first_folder = folders[0]
    if isinstance(first_folder, dict) and isinstance(first_folder.get("id"), int):
        return first_folder["id"]

    raise RuntimeError("未能从 agentsMetadata 中解析 agent folderId")


def collect_existing_agent_names(agents_metadata: Any) -> set[str]:
    if not isinstance(agents_metadata, dict):
        return set()
    agents = agents_metadata.get("agentsMetadata")
    if not isinstance(agents, list):
        return set()
    names: set[str] = set()
    for agent in agents:
        if isinstance(agent, dict):
            name = agent.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


def build_agent_template_data(
    template_data: str,
    *,
    provider_id: str,
    provider_name: str,
    provider_resource_name: str,
    instructions: str,
    model: str,
    temperature: float,
    max_iterations: int,
    thinking_enabled: bool,
) -> str:
    updated = template_data
    updated = replace_template_string_field(updated, "providerId", provider_id)
    updated = replace_template_string_field(updated, "providerName", provider_name)
    updated = replace_template_string_field(updated, "model", model)
    updated = upsert_template_string_field(
        updated,
        "providerResourceName",
        provider_resource_name,
        insert_after_field="model",
    )
    updated = replace_template_string_field(updated, "instructions", instructions)
    updated = replace_template_numeric_field(updated, "temperature", temperature)
    updated = replace_template_numeric_field(updated, "maxIterations", max_iterations)
    updated = replace_template_bool_field(updated, "thinkingEnabled", thinking_enabled)
    return updated


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str
    model: str
    provider: str
    temperature: float
    max_iterations: int
    instructions: str
    thinking_enabled: bool


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def parse_agent_configs_from_env() -> list[AgentConfig]:
    configured_names_raw = os.getenv("RET0OL_AGENT_NAMES", "").strip()
    if configured_names_raw:
        configured_names = [name.strip() for name in configured_names_raw.split(",") if name.strip()]
        if not configured_names:
            raise RuntimeError("RET0OL_AGENT_NAMES 已设置，但未解析出有效 agent 名称")
        configs: list[AgentConfig] = []
        for raw_name in configured_names:
            env_key = re.sub(r"[^A-Za-z0-9]+", "_", raw_name).strip("_").upper()
            if not env_key:
                raise RuntimeError(f"无法从 agent 名称生成环境变量前缀: {raw_name}")
            configs.append(
                AgentConfig(
                    name=os.getenv(f"RET0OL_AGENT_{env_key}_NAME", raw_name).strip() or raw_name,
                    description=os.getenv(f"RET0OL_AGENT_{env_key}_DESCRIPTION", "").strip(),
                    model=os.getenv(f"RET0OL_AGENT_{env_key}_MODEL", "").strip(),
                    provider=os.getenv(f"RET0OL_AGENT_{env_key}_PROVIDER", "").strip().lower(),
                    temperature=parse_float_env(f"RET0OL_AGENT_{env_key}_TEMPERATURE", 0.3),
                    max_iterations=parse_int_env(f"RET0OL_AGENT_{env_key}_MAX_ITERATIONS", 10),
                    instructions=os.getenv(f"RET0OL_AGENT_{env_key}_INSTRUCTIONS", ""),
                    thinking_enabled=parse_bool_env(f"RET0OL_AGENT_{env_key}_THINKING_ENABLED", False),
                )
            )
        for config in configs:
            if not config.model:
                raise RuntimeError(f"agent 配置缺少 model: {config.name}")
            if not config.provider:
                raise RuntimeError(f"agent 配置缺少 provider: {config.name}")
        return configs

    return [
        AgentConfig(
            name=RET0OL_AGENT_NAME.strip() or "gpt5",
            description=RET0OL_AGENT_DESCRIPTION,
            model=RET0OL_AGENT_MODEL.strip() or "gpt-5.5",
            provider=RET0OL_AGENT_PROVIDER or "openai",
            temperature=RET0OL_AGENT_TEMPERATURE,
            max_iterations=RET0OL_AGENT_MAX_ITERATIONS,
            instructions=RET0OL_AGENT_INSTRUCTIONS,
            thinking_enabled=RET0OL_AGENT_THINKING_ENABLED,
        ),
        AgentConfig(
            name=RET0OL_AGENT_CLAUDE_NAME.strip() or "claude",
            description=RET0OL_AGENT_CLAUDE_DESCRIPTION,
            model=RET0OL_AGENT_CLAUDE_MODEL.strip() or "claude-sonnet-4-6",
            provider=RET0OL_AGENT_CLAUDE_PROVIDER or "anthropic",
            temperature=RET0OL_AGENT_CLAUDE_TEMPERATURE,
            max_iterations=RET0OL_AGENT_CLAUDE_MAX_ITERATIONS,
            instructions=RET0OL_AGENT_CLAUDE_INSTRUCTIONS,
            thinking_enabled=RET0OL_AGENT_CLAUDE_THINKING_ENABLED,
        ),
    ]


@dataclass
class OpenProfileResult:
    profile_id: str
    name: str
    debug_port: int
    was_already_running: bool = False

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"


@dataclass(frozen=True)
class SignupSuccessResult:
    email: str
    password: str
    subdomain: str
    workspace_url: str
    created_at: str
    agents: list[dict[str, Any]]

    def to_account_csv_row(self) -> dict[str, str]:
        return {
            "email": self.email,
            "password": self.password,
            "expected_subdomain": self.subdomain,
            "enabled": "true",
            "notes": f"generated_by=singup.py created_at={self.created_at}",
        }

    def to_jsonl_record(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "password": self.password,
            "expected_subdomain": self.subdomain,
            "workspace_url": self.workspace_url,
            "created_at": self.created_at,
            "agents": self.agents,
        }


def resolve_signup_output_csv_path() -> Path:
    return Path(RET0OL_SIGNUP_OUTPUT_CSV).expanduser().resolve()


def resolve_signup_output_jsonl_path() -> Path | None:
    if not RET0OL_SIGNUP_OUTPUT_JSONL:
        return None
    return Path(RET0OL_SIGNUP_OUTPUT_JSONL).expanduser().resolve()


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_csv_row(path: Path, row: dict[str, str]) -> None:
    ensure_parent_directory(path)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACCOUNT_OUTPUT_FIELDNAMES)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    ensure_parent_directory(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.write("\n")


def persist_signup_output(
    result: SignupSuccessResult,
    *,
    csv_path: Path | None = None,
    jsonl_path: Path | None = None,
) -> dict[str, str]:
    resolved_csv_path = csv_path or resolve_signup_output_csv_path()
    append_csv_row(resolved_csv_path, result.to_account_csv_row())

    resolved_jsonl_path = jsonl_path if jsonl_path is not None else resolve_signup_output_jsonl_path()
    if resolved_jsonl_path is not None:
        append_jsonl_record(resolved_jsonl_path, result.to_jsonl_record())

    return {
        "csvPath": str(resolved_csv_path),
        "jsonlPath": str(resolved_jsonl_path) if resolved_jsonl_path is not None else "",
    }


def normalize_debug_port(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def extract_debug_port(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    for key in ("remoteDebugPort", "remote port", "debugPort"):
        port = normalize_debug_port(payload.get(key))
        if port is not None:
            return port

    nested_profile = payload.get("profile")
    if isinstance(nested_profile, dict):
        return extract_debug_port(nested_profile)

    return None


class GeekEZBrowserClient:
    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")

    @staticmethod
    def _format_api_payload(data: Any) -> str:
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)[:500]
        return str(data)[:500]

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.api_base}{path}"
        try:
            resp = requests.request(method=method, url=url, json=payload, params=params, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError(f"GeekEZ API 请求失败: {method} {path} -> {exc}") from exc

        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text

        return resp.status_code, data

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        status_code, data = self._request(method, path, payload=payload, params=params)

        if allow_not_found and status_code == 404:
            return None

        if status_code >= 400:
            raise RuntimeError(
                f"GeekEZ API 失败: {method} {path} -> HTTP {status_code}: "
                f"{self._format_api_payload(data)}"
            )

        if not isinstance(data, dict):
            raise RuntimeError(
                f"GeekEZ API 失败: {method} {path} -> 返回数据格式异常: {type(data).__name__}"
            )

        if not data.get("success"):
            raise RuntimeError(f"GeekEZ API 失败: {method} {path} -> {self._format_api_payload(data)}")

        return data

    def health(self) -> None:
        self.call("GET", "/api/status")

    def list_profiles(self) -> list[dict[str, Any]]:
        data = self.call("GET", "/api/profiles")
        if not data or not isinstance(data.get("profiles"), list):
            raise RuntimeError("GeekEZ API 返回的 profiles 列表格式异常")
        return data["profiles"]

    def get_profile(self, id_or_name: str) -> dict[str, Any] | None:
        encoded = quote(id_or_name, safe="")
        data = self.call("GET", f"/api/profiles/{encoded}", allow_not_found=True)
        if data is None:
            return None
        profile = data.get("profile")
        if not isinstance(profile, dict):
            raise RuntimeError("GeekEZ API 返回的 profile 数据格式异常")
        return profile

    def find_profile_by_name(self, profile_name: str) -> dict[str, Any] | None:
        for profile in self.list_profiles():
            if profile.get("name") == profile_name:
                profile_id = profile.get("id")
                if isinstance(profile_id, str) and profile_id:
                    detailed = self.get_profile(profile_id)
                    if detailed is not None:
                        return detailed
                return profile
        return None

    def create_profile(self, profile_name: str) -> dict[str, Any]:
        data = self.call(
            "POST",
            "/api/profiles",
            payload={"name": profile_name, "proxyStr": "Direct"},
        )
        profile = data.get("profile") if data else None
        if not isinstance(profile, dict):
            raise RuntimeError("GeekEZ API 创建 profile 后未返回有效 profile 数据")
        return profile

    def open_profile(self, id_or_name: str) -> OpenProfileResult:
        encoded = quote(id_or_name, safe="")
        data = self.call("GET", f"/api/open/{encoded}")
        if data is None:
            raise RuntimeError("GeekEZ open 接口返回为空")

        profile_id = str(data.get("profileId") or id_or_name)
        profile_name = str(data.get("name") or id_or_name)
        debug_port = extract_debug_port(data)
        was_already_running = str(data.get("message") or "").strip().lower() == "already running"

        profile = self.get_profile(profile_id)
        if profile is not None:
            profile_id = str(profile.get("id") or profile_id)
            profile_name = str(profile.get("name") or profile_name)
            debug_port = debug_port or extract_debug_port(profile)

        if debug_port is None:
            raise RuntimeError("GeekEZ 已启动环境，但未返回远程调试端口。请先在 GeekEZ Browser 设置中启用远程调试。")

        return OpenProfileResult(
            profile_id=profile_id,
            name=profile_name,
            debug_port=debug_port,
            was_already_running=was_already_running,
        )

    def stop_profile(self, id_or_name: str) -> None:
        encoded = quote(id_or_name, safe="")
        self.call("POST", f"/api/profiles/{encoded}/stop")


class RetoolWorkspaceClient:
    def __init__(self, page, workspace_base_url: str):
        self.page = page
        self.workspace_base_url = workspace_base_url.rstrip("/")

    async def get_xsrf_token(self) -> str:
        cookies = await self.page.context.cookies([self.workspace_base_url])
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            if name.lower() in {"x-xsrf-token", "xsrf-token", "xsrftoken", "__host-xsrftoken"}:
                value = str(cookie.get("value") or "").strip()
                if value:
                    return value
        raise RuntimeError("当前 workspace 登录态中未找到 XSRF cookie")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        response = await self.page.context.request.fetch(
            f"{self.workspace_base_url}{path}",
            method=method,
            params=params,
            data=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": self.workspace_base_url,
                "referer": f"{self.workspace_base_url}/",
                "x-retool-client-version": RET0OL_CLIENT_VERSION,
                "x-xsrf-token": await self.get_xsrf_token(),
            },
        )
        body_text = await response.text()

        body = parse_json_text(body_text)
        if response.status >= 400:
            raise RuntimeError(
                f"Retool workspace API 失败: {method} {path} -> HTTP {response.status}: "
                f"{format_for_log(body, limit=1000)}"
            )
        return body

    async def get_ai_settings(self) -> Any:
        return await self.request("GET", "/api/aiSettings")

    async def get_agents_metadata(self) -> Any:
        return await self.request("GET", "/api/agents/agentsMetadata")

    async def get_environments(self) -> Any:
        return await self.request("GET", "/api/environments")

    async def create_workflow(self, payload: dict[str, Any]) -> Any:
        return await self.request("POST", "/api/workflow", payload=payload)

    async def save_workflow(self, workflow_id: str, new_workflow_data: dict[str, Any]) -> Any:
        return await self.request("POST", f"/api/workflow/{workflow_id}", payload={"newWorkflowData": new_workflow_data})

    async def release_workflow(
        self,
        workflow_id: str,
        workflow_save_id: str,
        *,
        name: str = "0.0.2",
        description: str = "",
        additional_workflows: list[Any] | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            "/api/workflowRelease",
            payload={
                "workflowId": workflow_id,
                "workflowSaveId": workflow_save_id,
                "name": name,
                "description": description,
                "additionalWorkflows": additional_workflows or [],
            },
        )

    async def get_agents(self) -> Any:
        return await self.request("GET", "/api/agents")


async def get_cookie_value(context, url: str, cookie_names: set[str]) -> str | None:
    cookies = await context.cookies([url])
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if name in cookie_names:
            value = str(cookie.get("value") or "").strip()
            if value:
                return value
    return None


async def dump_login_auth_state(page) -> dict[str, Any]:
    login_base_url = resolve_login_base_url()
    cookies = await page.context.cookies([login_base_url])
    cookie_summaries = []
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        cookie_summaries.append(
            {
                "name": name,
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "httpOnly": bool(cookie.get("httpOnly")),
                "secure": bool(cookie.get("secure")),
                "sameSite": cookie.get("sameSite"),
                "valueSummary": summarize_secret_value_for_log(value) if value else {"length": 0},
            }
        )

    storage_snapshot = await page.evaluate(
        """() => {
            const summarizeValue = (value) => {
                const raw = String(value || '');
                return {
                    length: raw.length,
                    prefix: raw.slice(0, 12),
                    looksLikeJwt: raw.split('.').length === 3,
                };
            };

            const localStorageEntries = [];
            for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                if (!key) continue;
                localStorageEntries.push({
                    name: key,
                    valueSummary: summarizeValue(localStorage.getItem(key) || ''),
                });
            }

            const sessionStorageEntries = [];
            for (let index = 0; index < sessionStorage.length; index += 1) {
                const key = sessionStorage.key(index);
                if (!key) continue;
                sessionStorageEntries.push({
                    name: key,
                    valueSummary: summarizeValue(sessionStorage.getItem(key) || ''),
                });
            }

            return {
                href: location.href,
                title: document.title,
                localStorageEntries,
                sessionStorageEntries,
            };
        }"""
    )

    state = {
        "cookies": cookie_summaries,
        "storage": storage_snapshot,
    }
    print("login auth state:", format_for_log(state, limit=4000))
    return state


async def bridge_workspace_auth_from_login(
    page,
    workspace_base_url: str,
    authorization_payload: Any | None = None,
) -> None:
    if authorization_payload is None:
        obtain_result = await page.evaluate(
            """async ({ clientVersion }) => {
                const response = await fetch('/api/obtainAuthorizationToken', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        accept: 'application/json',
                        'content-type': 'application/json',
                        'x-retool-client-version': clientVersion,
                    },
                });
                return {
                    status: response.status,
                    bodyText: await response.text(),
                };
            }""",
            {"clientVersion": RET0OL_CLIENT_VERSION},
        )
        authorization_payload = parse_json_text(str(obtain_result.get("bodyText") or ""))
        print("obtainAuthorizationToken status:", obtain_result.get("status"))
        print("obtainAuthorizationToken body:", format_for_log(authorization_payload, limit=1000))
        if int(obtain_result.get("status") or 0) >= 400:
            await dump_login_auth_state(page)
            raise RuntimeError(
                "Retool workspace auth bridge 失败: POST /api/obtainAuthorizationToken -> "
                f"HTTP {obtain_result.get('status')}: "
                f"{format_for_log(authorization_payload, limit=1000)}"
            )

    if not isinstance(authorization_payload, dict):
        raise RuntimeError("Retool obtainAuthorizationToken 返回格式异常")

    redirect_url = build_redirect_url_from_auth_payload(authorization_payload)
    auth_url = authorization_payload.get("authUrl")
    authorization_token = authorization_payload.get("authorizationToken")
    if redirect_url and (not isinstance(auth_url, str) or not auth_url.strip()):
        print("login auth redirect:", sanitize_url_for_log(redirect_url))
        await page.goto(redirect_url, wait_until="domcontentloaded", timeout=60000)
        return

    if not isinstance(auth_url, str) or not auth_url.strip():
        raise RuntimeError("obtainAuthorizationToken 响应缺少 authUrl")
    if not isinstance(authorization_token, str) or not authorization_token.strip():
        raise RuntimeError("obtainAuthorizationToken 响应缺少 authorizationToken")

    resolved_auth_url = urljoin(f"{workspace_base_url}/", auth_url.strip())
    login_xsrf_token = await get_cookie_value(
        page.context,
        resolve_login_base_url(),
        {"xsrfToken", "__Host-xsrfToken"},
    )
    bridge_result = await page.evaluate(
        """async ({ authUrl, authorizationToken, xsrfToken, clientVersion }) => {
            const headers = {
                accept: 'application/json',
                'content-type': 'application/json',
                'x-retool-client-version': clientVersion,
            };
            if (xsrfToken) {
                headers['x-xsrf-token'] = xsrfToken;
            }
            const response = await fetch(authUrl, {
                method: 'POST',
                credentials: 'include',
                headers,
                body: JSON.stringify({ authorizationToken }),
            });

            return {
                status: response.status,
                bodyText: await response.text(),
            };
        }""",
        {
            "authUrl": resolved_auth_url,
            "authorizationToken": authorization_token,
            "xsrfToken": login_xsrf_token or "",
            "clientVersion": RET0OL_CLIENT_VERSION,
        },
    )
    body = parse_json_text(str(bridge_result.get("bodyText") or ""))
    if int(bridge_result.get("status") or 0) >= 400:
        await dump_login_auth_state(page)
        raise RuntimeError(
            "Retool workspace auth bridge 失败: POST /api/auth/saveAuth -> "
            f"HTTP {bridge_result.get('status')}: "
            f"{format_for_log(body, limit=1000)}"
        )

    if isinstance(body, dict):
        raw_redirect_uri = body.get("redirectUri")
        if isinstance(raw_redirect_uri, str) and raw_redirect_uri.strip():
            workspace_redirect_url = urljoin(resolved_auth_url, raw_redirect_uri.strip())
            print("workspace auth redirect:", sanitize_url_for_log(workspace_redirect_url))
            await page.goto(workspace_redirect_url, wait_until="domcontentloaded", timeout=60000)
            return

    print(f"workspace auth bridged: {sanitize_url_for_log(resolved_auth_url)}")


def resolve_target_profile(client: GeekEZBrowserClient) -> tuple[dict[str, Any], bool]:
    if GEEKEZ_PROFILE_ID:
        profile = client.get_profile(GEEKEZ_PROFILE_ID)
        if profile is not None:
            return profile, False
        raise ValueError(f"未找到配置的 GeekEZ profile: {GEEKEZ_PROFILE_ID}")

    if not GEEKEZ_AUTO_CREATE:
        raise ValueError("未配置 GEEKEZ_PROFILE_ID，且 GEEKEZ_AUTO_CREATE 未开启，无法为本次运行创建全新 profile")

    fresh_profile_name = build_fresh_profile_name(GEEKEZ_PROFILE_NAME)
    return client.create_profile(fresh_profile_name), True


async def wait_for_json_response(page, url_part: str, action, timeout_ms: int = 30000):
    async with page.expect_response(
        lambda resp: url_part in resp.url and resp.request.resource_type in {"fetch", "xhr"},
        timeout=timeout_ms,
    ) as response_info:
        await action()
    resp = await response_info.value
    body = await read_response_body_safe(resp)
    return resp, body


async def read_response_body_safe(resp) -> Any:
    content_type = resp.headers.get("content-type", "")
    try:
        if "json" in content_type:
            try:
                return await resp.json()
            except Exception:
                return await resp.text()
        return await resp.text()
    except Exception as exc:
        return f"<body unavailable: {type(exc).__name__}: {exc}>"


async def dump_page_debug(page, label: str) -> None:
    debug_url = sanitize_url_for_log(page.url)
    debug_title = await page.title()
    debug_runtime = await page.evaluate(
        """() => ({
            readyState: document.readyState,
            href: location.href,
            visibilityState: document.visibilityState,
            hasRoot: !!document.querySelector('#root'),
            hasNextRoot: !!document.querySelector('#__next'),
            text: (document.body?.innerText || '').slice(0, 1200),
        })"""
    )
    debug_text = debug_runtime.get("text") if isinstance(debug_runtime, dict) else ""
    debug_html = await page.content()
    debug_inputs = await page.evaluate(
        """() => Array.from(document.querySelectorAll('input')).map((el, idx) => ({
            idx,
            type: el.type || '',
            name: el.getAttribute('name') || '',
            placeholder: el.getAttribute('placeholder') || '',
            testid: el.getAttribute('data-testid') || '',
            value: el.value || ''
        }))"""
    )
    debug_buttons = await page.evaluate(
        """() => Array.from(document.querySelectorAll('button')).map((el, idx) => ({
            idx,
            type: el.getAttribute('type') || '',
            text: (el.innerText || '').trim(),
            testid: el.getAttribute('data-testid') || ''
        }))"""
    )
    debug_events = getattr(page, "_retool_debug_events", None)
    print(f"{label} debug url:", debug_url)
    print(f"{label} debug title:", debug_title)
    print(f"{label} debug runtime:", format_for_log(debug_runtime, limit=1200))
    print(f"{label} debug text:", json.dumps(debug_text, ensure_ascii=False))
    print(f"{label} debug inputs:", json.dumps(debug_inputs, ensure_ascii=False))
    print(f"{label} debug buttons:", json.dumps(debug_buttons, ensure_ascii=False))
    if debug_events:
        print(f"{label} debug events:", format_for_log(debug_events, limit=3000))
    print(f"{label} debug html:", json.dumps(debug_html[:1200], ensure_ascii=False))


async def is_blank_page(page) -> bool:
    snapshot = await page.evaluate(
        """() => {
            const body = document.body;
            if (!body) {
                return { text: '', html: '' };
            }

            const clone = body.cloneNode(true);
            clone.querySelectorAll('script,style,noscript').forEach((el) => el.remove());

            return {
                text: (body.innerText || '').trim(),
                html: (clone.innerHTML || '').replace(/\\s+/g, ' ').trim(),
            };
        }"""
    )
    if not isinstance(snapshot, dict):
        return False
    text = str(snapshot.get("text") or "").strip()
    html = str(snapshot.get("html") or "").strip()
    return not text and not html


async def ensure_page_foreground(page) -> None:
    try:
        await page.bring_to_front()
    except Exception as exc:
        message = str(exc).strip().lower()
        unsupported_bring_to_front = (
            "bringtofront" in message
            and (
                "unknown page method" in message
                or "not supported" in message
            )
        )
        if not unsupported_bring_to_front:
            raise
    try:
        await page.evaluate("() => window.focus()")
    except Exception:
        pass
    await page.wait_for_timeout(1000)


async def wait_for_page_ready(page, extra_delay_ms: int = 2000) -> None:
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(extra_delay_ms)


async def maybe_handle_verify_email_step(page, full_name: str) -> bool:
    if VERIFY_EMAIL_URL_PART not in page.url:
        return False

    await ensure_page_foreground(page)
    await wait_for_page_ready(page)
    body_text = await page.evaluate("() => document.body.innerText || ''")
    normalized_text = body_text.lower()
    create_org_button = page.get_by_role(
        "button",
        name=re.compile("create a new organization", re.IGNORECASE),
    ).first
    request_join_button = page.get_by_test_id("Login::VerifyEmailPage_submitButton").first
    full_name_input = page.locator('input[placeholder="Grace Hopper"], input[type="text"]').first

    if "create a new organization" not in normalized_text and "request to join" not in normalized_text:
        return False

    if await full_name_input.count() > 0:
        await full_name_input.fill(full_name)

    if AUTO_JOIN:
        action_button = request_join_button
        action_label = "request to join existing team"
    else:
        action_button = create_org_button
        action_label = "create a new organization"

    if await action_button.count() == 0:
        raise RuntimeError("verifyEmail 页面存在待处理分支，但未找到对应操作按钮")

    print(f"verifyEmail branch detected, action: {action_label}")
    fallback_followup_url = build_followup_url_from_auth_url(page.url)
    await action_button.click()
    await page.wait_for_timeout(1500)

    if not AUTO_JOIN:
        confirm_button = page.get_by_role("button", name=re.compile("^ok$", re.IGNORECASE)).first
        if await confirm_button.count() > 0:
            print("verifyEmail confirmation accepted")
            await confirm_button.click()
            await page.wait_for_timeout(3000)

    if await has_followup_form(page):
        return True
    try:
        await page.wait_for_url(f"**{FOLLOWUP_URL_PART}**", timeout=5000)
    except PlaywrightTimeoutError:
        if await has_followup_form(page):
            return True
        if VERIFY_EMAIL_URL_PART in page.url:
            print(f"verifyEmail branch fallback: {sanitize_url_for_log(fallback_followup_url)}")
            await page.goto(fallback_followup_url, wait_until="domcontentloaded", timeout=60000)
    return True


def resolve_retry_followup_url(current_url: str, preferred_followup_url: str | None) -> str:
    if FOLLOWUP_URL_PART in current_url:
        return current_url
    if preferred_followup_url and FOLLOWUP_URL_PART in preferred_followup_url:
        return preferred_followup_url
    if preferred_followup_url:
        return build_followup_url_from_auth_url(preferred_followup_url)
    return build_followup_url_from_auth_url(current_url)


async def wait_for_followup_ready(
    page,
    full_name: str,
    preferred_followup_url: str | None = None,
    retries: int = 2,
) -> None:
    for attempt in range(retries + 1):
        await ensure_page_foreground(page)
        try:
            await page.wait_for_url(
                lambda current_url: FOLLOWUP_URL_PART in current_url or VERIFY_EMAIL_URL_PART in current_url,
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            pass

        if await has_followup_form(page):
            await wait_for_page_ready(page)
            if await has_followup_form(page):
                return

        if FOLLOWUP_URL_PART in page.url:
            await wait_for_page_ready(page)
            if await has_followup_form(page):
                return

        if VERIFY_EMAIL_URL_PART in page.url and not await is_blank_page(page):
            handled = await maybe_handle_verify_email_step(page, full_name)
            if handled:
                if await has_followup_form(page):
                    await wait_for_page_ready(page)
                    if await has_followup_form(page):
                        return

                if FOLLOWUP_URL_PART in page.url:
                    await wait_for_page_ready(page)
                    if await has_followup_form(page):
                        return

        if attempt < retries:
            followup_snapshot = await page.evaluate(
                """() => ({
                    href: location.href,
                    readyState: document.readyState,
                    visibilityState: document.visibilityState,
                    text: (document.body?.innerText || '').slice(0, 400),
                    inputCount: document.querySelectorAll('input').length,
                    buttonCount: document.querySelectorAll('button').length,
                })"""
            )
            print(
                f"followup retry snapshot {attempt + 1}/{retries + 1}:",
                format_for_log(followup_snapshot, limit=1200),
            )
            if await is_blank_page(page):
                print(f"followup page blank, retry {attempt + 1}/{retries + 1}: {FOLLOWUP_URL}")
                target_url = preferred_followup_url or FOLLOWUP_URL
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            elif VERIFY_EMAIL_URL_PART in page.url:
                print(f"verifyEmail page still pending, retry {attempt + 1}/{retries + 1}: reload current page")
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            else:
                retry_followup_url = resolve_retry_followup_url(page.url, preferred_followup_url)
                if retry_followup_url:
                    print(
                        "followup page not ready, "
                        f"retry {attempt + 1}/{retries + 1}: goto followup url "
                        f"{sanitize_url_for_log(retry_followup_url)}"
                    )
                    await page.goto(retry_followup_url, wait_until="domcontentloaded", timeout=60000)
                else:
                    print(f"followup page not ready, retry {attempt + 1}/{retries + 1}: reload current page")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
            continue

        for reload_attempt in range(RET0OL_FOLLOWUP_RELOAD_RETRIES):
            print(
                "followup page still broken after goto retries, "
                f"reload attempt {reload_attempt + 1}/{RET0OL_FOLLOWUP_RELOAD_RETRIES}"
            )
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await ensure_page_foreground(page)
            await wait_for_page_ready(page)

            if await has_followup_form(page):
                return

            if VERIFY_EMAIL_URL_PART in page.url and not await is_blank_page(page):
                handled = await maybe_handle_verify_email_step(page, full_name)
                if handled and await has_followup_form(page):
                    await wait_for_page_ready(page)
                    if await has_followup_form(page):
                        return

        await dump_page_debug(page, "followup-transition")
        raise RestartSignupFlowError("注册成功后 followup 页面反复刷新仍未恢复，丢弃本轮并从头重试")


async def wait_for_testid_attached(page, testid: str, timeout_ms: int = 30000):
    locator = page.get_by_test_id(testid)
    await locator.first.wait_for(state="attached", timeout=timeout_ms)
    return locator


async def wait_for_signup_form(page, retries: int = 2):
    email_selectors = [
        'input[name="email"]',
        'input[placeholder="Work email"]',
        'input[type="text"]',
    ]
    password_selectors = [
        'input[name="password"]',
        'input[placeholder="Password"]',
        'input[type="password"]',
    ]

    for attempt in range(retries + 1):
        await ensure_page_foreground(page)
        await page.wait_for_timeout(2000)

        email_selector = None
        for selector in email_selectors:
            if await page.locator(selector).count():
                email_selector = selector
                break

        password_selector = None
        for selector in password_selectors:
            if await page.locator(selector).count():
                password_selector = selector
                break

        if email_selector and password_selector:
            return email_selector, password_selector

        if attempt < retries:
            await page.reload(wait_until="domcontentloaded", timeout=60000)

    await dump_page_debug(page, "signup")
    raise RuntimeError("未找到注册页输入框，请先确认 GeekEZ Browser 已正常打开并完成必要验证")


async def wait_for_followup_form(page, retries: int = 2):
    for attempt in range(retries + 1):
        await ensure_page_foreground(page)
        try:
            first_name_locator = await wait_for_testid_attached(page, "SignUp::FullNameInput", timeout_ms=10000)
            org_name_locator = await wait_for_testid_attached(page, "SignUp::OrgNameInput", timeout_ms=10000)
            return first_name_locator, org_name_locator
        except Exception:
            if attempt < retries:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                continue
            await dump_page_debug(page, "followup")
            raise


async def has_followup_form(page) -> bool:
    full_name_count = await page.get_by_test_id("SignUp::FullNameInput").count()
    org_name_count = await page.get_by_test_id("SignUp::OrgNameInput").count()
    return full_name_count > 0 and org_name_count > 0


async def snapshot_onboarding_page(page) -> dict[str, Any]:
    return await page.evaluate(
        """() => ({
            href: location.href,
            readyState: document.readyState,
            headingTexts: Array.from(document.querySelectorAll('h1,h2'))
                .map((el) => (el.innerText || el.textContent || '').trim())
                .filter(Boolean)
                .slice(0, 5),
            labelTexts: Array.from(document.querySelectorAll('label'))
                .map((el) => (el.innerText || el.textContent || '').trim())
                .filter(Boolean)
                .slice(0, 20),
            buttonTexts: Array.from(document.querySelectorAll('button'))
                .map((el) => ({
                    text: (el.innerText || el.textContent || '').trim(),
                    disabled: !!el.disabled,
                }))
                .slice(0, 10),
            bodyText: (document.body?.innerText || '').slice(0, 1600),
        })"""
    )


async def complete_login_onboarding_step(page) -> bool:
    if is_workspace_url(page.url):
        return False
    if not is_login_onboarding_url(page.url):
        return False

    initial_url = page.url
    option_patterns = resolve_onboarding_option_patterns(initial_url)
    if not option_patterns:
        return False

    await ensure_page_foreground(page)
    await wait_for_page_ready(page)
    if is_workspace_url(page.url):
        return False
    if not is_login_onboarding_url(page.url):
        return False
    print("login onboarding page:", sanitize_url_for_log(page.url))

    for option_pattern in option_patterns:
        option_locator = page.get_by_text(re.compile(option_pattern, re.IGNORECASE)).first
        if await option_locator.count() == 0:
            continue

        await option_locator.click()
        await page.wait_for_timeout(800)
        continue_button = page.get_by_role("button", name=re.compile("^continue$", re.IGNORECASE)).first
        if await continue_button.count() == 0:
            raise RuntimeError("login onboarding 页面缺少 Continue 按钮")

        is_enabled = await continue_button.is_enabled()
        print(f"login onboarding option selected: {option_pattern} (continue enabled={is_enabled})")
        if not is_enabled:
            continue

        await continue_button.click()
        await page.wait_for_timeout(1500)
        return True

    onboarding_snapshot = await snapshot_onboarding_page(page)
    print("login onboarding snapshot:", format_for_log(onboarding_snapshot, limit=2000))
    raise RuntimeError(f"未能自动完成 login onboarding 页面: {sanitize_url_for_log(page.url)}")


async def complete_login_onboarding_flow(page, max_steps: int = 6) -> None:
    for step in range(max_steps):
        if is_workspace_url(page.url):
            return
        if not is_login_onboarding_url(page.url):
            return

        completed = await complete_login_onboarding_step(page)
        if not completed:
            return

        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        print(f"login onboarding progressed to: {sanitize_url_for_log(page.url)}")

    if is_workspace_url(page.url):
        return
    if is_login_onboarding_url(page.url):
        await dump_page_debug(page, "login-onboarding")
        raise RuntimeError("login onboarding 页面链路超过预期步数，未能进入 workspace bridge 阶段")


async def acquire_workspace_authorization_payload(page, retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    last_status: int | None = None
    last_body: Any = None

    for attempt in range(retries + 1):
        if is_workspace_url(page.url):
            raise RuntimeError("当前页面已进入 workspace 域名，不需要再获取 login-domain authorization payload")
        result = await page.evaluate(
            """async ({ clientVersion }) => {
                const response = await fetch('/api/obtainAuthorizationToken', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        accept: 'application/json',
                        'content-type': 'application/json',
                        'x-retool-client-version': clientVersion,
                    },
                });
                return {
                    status: response.status,
                    bodyText: await response.text(),
                };
            }""",
            {"clientVersion": RET0OL_CLIENT_VERSION},
        )
        status = int(result.get("status") or 0)
        body = parse_json_text(str(result.get("bodyText") or ""))
        print(f"obtainAuthorizationToken attempt {attempt + 1}/{retries + 1}:", status, format_for_log(body, limit=1000))

        if status < 400 and isinstance(body, dict):
            return body

        last_status = status
        last_body = body
        last_error = RuntimeError(
            "Retool obtainAuthorizationToken 失败: "
            f"HTTP {status}: {format_for_log(body, limit=1000)}"
        )
        if attempt < retries and is_login_onboarding_url(page.url):
            progressed = await complete_login_onboarding_step(page)
            if progressed:
                await page.wait_for_timeout(3000)
                continue
        if attempt < retries:
            await page.wait_for_timeout(2000)

    await dump_login_auth_state(page)
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        "Retool obtainAuthorizationToken 失败: "
        f"HTTP {last_status}: {format_for_log(last_body, limit=1000)}"
    )


async def choose_available_org_name(page, preferred_subdomain: str) -> str:
    locator = page.get_by_test_id("SignUp::OrgNameInput").first
    await locator.fill(preferred_subdomain)
    await page.wait_for_timeout(1500)

    continue_button = page.get_by_test_id("SignUp::SubmitOrgCreateForm").first
    button_enabled = await continue_button.is_enabled()
    validation_snapshot = await page.evaluate(
        """() => ({
            bodyText: (document.body?.innerText || '').slice(0, 2000),
            orgValue: document.querySelector('[data-testid="SignUp::OrgNameInput"]')?.value || '',
            inputAriaInvalid: document.querySelector('[data-testid="SignUp::OrgNameInput"]')?.getAttribute('aria-invalid') || '',
            disabledButtons: Array.from(document.querySelectorAll('button')).map((el) => ({
                text: (el.innerText || '').trim(),
                disabled: !!el.disabled,
                testid: el.getAttribute('data-testid') || '',
            })),
        })"""
    )
    print("org validation snapshot:", format_for_log(validation_snapshot, limit=1800))
    if button_enabled:
        return preferred_subdomain

    suggestion_snapshot = await page.evaluate(
        """() => Array.from(document.querySelectorAll('button, [role="button"], span, div'))
            .map((el) => (el.innerText || '').trim())
            .filter((text) => text && text.endsWith('Add'))
            .slice(0, 10)"""
    )
    print("org suggestions raw:", format_for_log(suggestion_snapshot, limit=1200))
    suggestions = []
    candidate_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,62}$")
    suggestion_sources: list[str] = []

    body_text = validation_snapshot.get("bodyText") if isinstance(validation_snapshot, dict) else None
    if isinstance(body_text, str) and body_text.strip():
        suggestion_sources.append(body_text)

    if isinstance(suggestion_snapshot, list):
        suggestion_sources.extend(str(raw) for raw in suggestion_snapshot if str(raw).strip())

    for source in suggestion_sources:
        for line in source.splitlines():
            value = line.strip()
            if not value.endswith("Add"):
                continue
            candidate = value.removesuffix("Add").strip()
            if candidate_pattern.fullmatch(candidate) and candidate not in suggestions:
                suggestions.append(candidate)

    print("org suggestions parsed:", format_for_log(suggestions, limit=800))

    for candidate in suggestions:
        await locator.fill(candidate)
        await page.wait_for_timeout(1500)
        if await continue_button.is_enabled():
            print(f"org name fallback selected: {candidate}")
            return candidate

    raise RuntimeError(f"组织子域不可用，且未找到可用候选。初始值: {preferred_subdomain}")


async def read_temp_mail_hero_text(page) -> str:
    return await page.evaluate(
        """() => {
            const hero = document.querySelector('#hero-mail-card');
            if (hero) {
                return hero.innerText || '';
            }
            return document.body ? (document.body.innerText || '') : '';
        }"""
    )


async def snapshot_temp_mail_page(page) -> dict[str, Any]:
    return await page.evaluate(
        """() => ({
            href: location.href,
            readyState: document.readyState,
            title: document.title,
            heroText: document.querySelector('#hero-mail-card')?.innerText || '',
            buttonTexts: Array.from(document.querySelectorAll('button'))
                .map((el) => ({
                    text: (el.innerText || '').trim(),
                    disabled: !!el.disabled,
                }))
                .slice(0, 10),
            bodyText: (document.body?.innerText || '').slice(0, 1200),
        })"""
    )


async def clear_current_origin_state(page) -> None:
    await page.evaluate(
        """async () => {
            try { localStorage.clear(); } catch {}
            try { sessionStorage.clear(); } catch {}

            try {
                const cookieNames = document.cookie
                    .split(';')
                    .map((part) => part.trim().split('=')[0])
                    .filter(Boolean);
                for (const name of cookieNames) {
                    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
                    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=${location.hostname}`;

                    const domainParts = location.hostname.split('.');
                    while (domainParts.length > 1) {
                        const domain = `.${domainParts.join('.')}`;
                        document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=${domain}`;
                        domainParts.shift();
                    }
                }
            } catch {}

            try {
                if ('caches' in window) {
                    const keys = await caches.keys();
                    await Promise.all(keys.map((key) => caches.delete(key)));
                }
            } catch {}

            try {
                if ('serviceWorker' in navigator) {
                    const registrations = await navigator.serviceWorker.getRegistrations();
                    await Promise.all(registrations.map((registration) => registration.unregister()));
                }
            } catch {}

            try {
                if (indexedDB.databases) {
                    const dbs = await indexedDB.databases();
                    await Promise.all(
                        dbs.map(
                            (db) =>
                                db.name
                                    ? new Promise((resolve) => {
                                          const request = indexedDB.deleteDatabase(db.name);
                                          request.onsuccess = () => resolve(true);
                                          request.onerror = () => resolve(false);
                                          request.onblocked = () => resolve(false);
                                      })
                                    : Promise.resolve(false)
                        )
                    );
                }
            } catch {}
        }"""
    )


async def clear_temp_mail_homepage_state(page) -> None:
    await clear_current_origin_state(page)


async def acquire_temp_email(page) -> str:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            await page.goto(TEMP_MAIL_URL, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as exc:
            last_error = exc
            error_text = str(exc)
            is_retryable = "ERR_ABORTED" in error_text or "frame was detached" in error_text.lower()
            if not is_retryable or attempt == 1:
                raise RuntimeError(f"打开 215.im 首页失败: {error_text}") from exc
            print(f"215.im homepage navigation retry {attempt + 1}/2: {error_text}")
            await page.wait_for_timeout(1500)
    else:
        raise RuntimeError(f"打开 215.im 首页失败: {last_error}")

    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass

        title = await page.title()
        content = await page.content()
        if "Just a moment" in title or "cf-challenge" in content.lower():
            raise RuntimeError("215.im 首页命中 Cloudflare/风控挑战，无法自动提取临时邮箱")

        generate_button = page.get_by_role("button", name="生成邮箱地址")
        hero_text = await read_temp_mail_hero_text(page)
        existing_address = extract_email_from_text(hero_text)
        if await generate_button.count() == 0:
            if existing_address:
                print("215.im homepage restored existing inbox; clearing site state for a fresh address")
                await clear_temp_mail_homepage_state(page)
                await page.goto(TEMP_MAIL_URL, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeoutError:
                    pass
                generate_button = page.get_by_role("button", name="生成邮箱地址")
                hero_text = await read_temp_mail_hero_text(page)
                existing_address = extract_email_from_text(hero_text)

            if await generate_button.count() == 0:
                if existing_address:
                    print("215.im homepage reusing active inbox")
                    print("temporary email acquired")
                    return existing_address
                raise RuntimeError("215.im 首页未找到“生成邮箱地址”按钮")

        hero_input = page.locator("#hero-prefix-input")
        if await hero_input.count() > 0:
            await hero_input.fill("")

        body: Any = None
        for bridge_attempt in range(4):
            try:
                async with page.expect_response(
                    lambda resp: "/api/temp-inbox" in resp.url and resp.request.method == "POST",
                    timeout=60000,
                ) as response_info:
                    await generate_button.click()
            except PlaywrightTimeoutError:
                page_snapshot = await snapshot_temp_mail_page(page)
                print(
                    "215.im bridge timeout snapshot:",
                    format_for_log(page_snapshot, limit=2000),
                )
                fallback_address = extract_email_from_text(str(page_snapshot.get("heroText") or ""))
                if not fallback_address:
                    fallback_address = extract_email_from_text(str(page_snapshot.get("bodyText") or ""))
                if fallback_address:
                    print("215.im bridge timeout fallback to DOM address")
                    print("temporary email acquired")
                    return fallback_address
                if bridge_attempt < 3:
                    print(f"215.im bridge timeout, reload and retry ({bridge_attempt + 1}/4)")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=30000)
                    except PlaywrightTimeoutError:
                        pass
                    continue
                raise

            response = await response_info.value
            body = await read_response_body_safe(response)
            if response.status < 400:
                break

            is_rate_limited = (
                response.status == 429
                and isinstance(body, dict)
                and body.get("errorCode") == "rate_limit_temporarily_blocked"
            )
            retry_delay_seconds = resolve_temp_mail_retry_delay_seconds(body)
            if is_rate_limited and bridge_attempt < 3 and retry_delay_seconds:
                print(
                    "215.im temp inbox rate limited, "
                    f"retrying in {retry_delay_seconds}s "
                    f"(attempt {bridge_attempt + 1}/4)"
                )
                await page.wait_for_timeout(retry_delay_seconds * 1000)
                continue

            raise RuntimeError(
                "215.im 首页临时邮箱桥接接口返回失败，"
                f"status={response.status}, body={format_for_log(body, limit=800)}"
            )
        else:
            raise RuntimeError("215.im 首页临时邮箱桥接接口重试后仍未成功")

        address = None
        if isinstance(body, dict):
            payload = body.get("data") if isinstance(body.get("data"), dict) else body
            raw_address = payload.get("address") if isinstance(payload, dict) else None
            if isinstance(raw_address, str) and "@" in raw_address:
                address = raw_address.strip()

        if not address:
            hero_text = await read_temp_mail_hero_text(page)
            address = extract_email_from_text(hero_text)

        if not address:
            raise RuntimeError(
                "未能从 215.im 提取临时邮箱地址，"
                f"接口响应: {format_for_log(body, limit=800)}"
            )

        print("temporary email acquired")
        return address
    except Exception:
        raise


async def wait_for_workspace_ready(
    page,
    workspace_base_url: str,
    authorization_payload: Any | None = None,
    retries: int = 3,
) -> None:
    workspace_client = RetoolWorkspaceClient(page, workspace_base_url)

    for attempt in range(retries + 1):
        print(f"打开 workspace: {workspace_base_url} (attempt {attempt + 1}/{retries + 1})")
        if attempt == 0 and not page.url.startswith(workspace_base_url):
            await bridge_workspace_auth_from_login(page, workspace_base_url, authorization_payload=authorization_payload)
        await ensure_page_foreground(page)
        await page.goto(workspace_base_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        title = await page.title()
        content = await page.content()
        if "Just a moment" in title or "cf-challenge" in content.lower():
            raise RuntimeError("workspace 页面命中 Cloudflare/风控挑战，无法继续创建 agent")

        try:
            await workspace_client.get_xsrf_token()
            await workspace_client.get_ai_settings()
            return
        except Exception as exc:
            if attempt >= retries:
                await dump_page_debug(page, "workspace-bootstrap")
                raise RuntimeError(f"workspace 登录态尚未就绪: {exc}") from exc
            print(f"workspace bootstrap retry {attempt + 1}/{retries + 1}: {exc}")
            await page.wait_for_timeout(3000)


async def create_and_configure_agent(
    page,
    workspace_base_url: str,
    agent_config: AgentConfig,
) -> dict[str, Any]:
    workspace_client = RetoolWorkspaceClient(page, workspace_base_url)
    ai_settings = await workspace_client.get_ai_settings()
    agents_metadata = await workspace_client.get_agents_metadata()
    environments = await workspace_client.get_environments()

    environments_list = environments.get("environments") if isinstance(environments, dict) else None
    if not isinstance(environments_list, list) or not environments_list:
        raise RuntimeError("workspace environments 未初始化完成，暂时不能创建 agent")

    provider_id, provider_name, provider_resource_name = resolve_agent_provider(ai_settings, agent_config.provider)
    folder_id = resolve_agent_root_folder_id(agents_metadata)
    existing_names = collect_existing_agent_names(agents_metadata)
    preferred_name = agent_config.name.strip() or "agent"
    agent_name = build_unique_agent_name(preferred_name, existing_names)

    seed_payload = load_agent_create_seed_payload()
    create_payload = json.loads(json.dumps(seed_payload))
    create_payload["name"] = agent_name
    create_payload["description"] = agent_config.description
    create_payload["folderId"] = folder_id

    print(
        "creating agent:",
        json.dumps(
            {
                "name": agent_name,
                "folderId": folder_id,
                "provider": provider_name,
                "model": agent_config.model,
                "maxIterations": agent_config.max_iterations,
            },
            ensure_ascii=False,
        ),
    )
    created_workflow = await workspace_client.create_workflow(create_payload)
    if not isinstance(created_workflow, dict):
        raise RuntimeError("创建 agent 后返回数据格式异常")

    workflow_id = created_workflow.get("id")
    template_data = created_workflow.get("templateData")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise RuntimeError("创建 agent 后未返回 workflowId")
    if not isinstance(template_data, str) or not template_data:
        raise RuntimeError("创建 agent 后未返回 templateData")

    updated_template_data = build_agent_template_data(
        template_data,
        provider_id=provider_id,
        provider_name=provider_name,
        provider_resource_name=provider_resource_name,
        instructions=agent_config.instructions,
        model=agent_config.model,
        temperature=agent_config.temperature,
        max_iterations=agent_config.max_iterations,
        thinking_enabled=agent_config.thinking_enabled,
    )

    workflow_data = json.loads(json.dumps(created_workflow))
    workflow_data["name"] = agent_name
    workflow_data["description"] = agent_config.description
    workflow_data["folderId"] = folder_id
    workflow_data["templateData"] = updated_template_data

    print(f"saving agent workflow: {workflow_id}")
    saved_workflow = await workspace_client.save_workflow(workflow_id, workflow_data)
    if not isinstance(saved_workflow, dict):
        raise RuntimeError("保存 agent 配置后返回数据格式异常")

    workflow_save_id = saved_workflow.get("saveId")
    if not isinstance(workflow_save_id, str) or not workflow_save_id.strip():
        raise RuntimeError("保存 agent 配置后未返回 workflow saveId")

    print(f"releasing agent workflow: {workflow_id}")
    release_info = await workspace_client.release_workflow(
        workflow_id,
        workflow_save_id.strip(),
    )
    if not isinstance(release_info, dict):
        raise RuntimeError("发布 agent 后返回数据格式异常")

    final_template_data = saved_workflow.get("templateData")
    final_summary = {
        "workspace": workspace_base_url,
        "workflowId": workflow_id,
        "releaseId": release_info.get("id"),
        "name": saved_workflow.get("name") or agent_name,
        "folderId": saved_workflow.get("folderId") or folder_id,
        "model": extract_template_string_field(str(final_template_data or ""), "model"),
        "providerId": extract_template_string_field(str(final_template_data or ""), "providerId"),
        "providerName": extract_template_string_field(str(final_template_data or ""), "providerName"),
        "temperature": extract_template_numeric_field(str(final_template_data or ""), "temperature"),
        "maxIterations": extract_template_numeric_field(str(final_template_data or ""), "maxIterations"),
        "thinkingEnabled": extract_template_bool_field(str(final_template_data or ""), "thinkingEnabled"),
    }
    print("agent created:", json.dumps(sanitize_for_log(final_summary), ensure_ascii=False))
    return final_summary


async def create_and_configure_agents(page, workspace_base_url: str) -> list[dict[str, Any]]:
    agent_configs = parse_agent_configs_from_env()
    if not agent_configs:
        raise RuntimeError("未解析出任何 agent 配置")

    summaries: list[dict[str, Any]] = []
    for agent_config in agent_configs:
        summary = await create_and_configure_agent(page, workspace_base_url, agent_config)
        summaries.append(summary)
    return summaries


async def run_signup_flow(cdp_endpoint: str) -> SignupSuccessResult:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            debug_events: dict[str, list[dict[str, Any]]] = {
                "console": [],
                "pageerror": [],
                "requestfailed": [],
                "httpErrors": [],
            }
            setattr(page, "_retool_debug_events", debug_events)

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
                        "url": request.url,
                        "method": request.method,
                        "resourceType": request.resource_type,
                        "failure": request.failure,
                    },
                    limit=20,
                ),
            )
            page.on(
                "response",
                lambda response: (
                    append_debug_event(
                        "httpErrors",
                        {
                            "url": response.url,
                            "status": response.status,
                        },
                        limit=30,
                    )
                    if response.status >= 400 and "retool.com" in response.url
                    else None
                ),
            )
            email = EMAIL.strip() or await acquire_temp_email(page)
            password = resolve_signup_password(email)
            subdomain = derive_subdomain_from_email(email)
            first_name = derive_first_name_from_email(email)
            full_name = build_full_name(first_name, LAST_NAME)

            if EMAIL.strip():
                print("signup email source: configured env")
            else:
                print("signup email source: vip.215.im homepage")

            print(f"打开注册页: {RET0OL_SIGNUP_URL}")
            await ensure_page_foreground(page)
            await page.goto(resolve_login_base_url(), wait_until="domcontentloaded", timeout=60000)
            await clear_current_origin_state(page)
            await page.goto(RET0OL_SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            title = await page.title()
            print("page title:", title)

            # 如果被 Cloudflare / 风控卡住，这里先明确暴露
            content = await page.content()
            if "Just a moment" in content or "cf-challenge" in content.lower():
                raise RuntimeError("页面命中 Cloudflare/风控挑战，当前 GeekEZ Browser 窗口未绕过验证")

            email_selector, password_selector = await wait_for_signup_form(page)

            await page.fill(email_selector, email)
            await page.fill(password_selector, password)

            continue_button = page.get_by_test_id("SignUp::SubmitEmailAndPassword")
            if await continue_button.count() == 0:
                raise RuntimeError("未找到 Continue 按钮")

            print("提交 /api/signup")
            signup_resp, signup_body = await wait_for_json_response(
                page,
                "/api/signup",
                lambda: continue_button.click(),
                timeout_ms=60000,
            )
            print("signup status:", signup_resp.status)
            print("signup body:", format_for_log(signup_body))
            preferred_followup_url = resolve_followup_url_from_signup_response(signup_body)
            if preferred_followup_url:
                print("signup redirectUri:", sanitize_url_for_log(preferred_followup_url))
                try:
                    await page.wait_for_url(
                        lambda current_url: FOLLOWUP_URL_PART in current_url or VERIFY_EMAIL_URL_PART in current_url,
                        timeout=5000,
                    )
                except PlaywrightTimeoutError:
                    print(f"signup redirect fallback goto: {sanitize_url_for_log(preferred_followup_url)}")
                    await page.goto(preferred_followup_url, wait_until="domcontentloaded", timeout=60000)
            else:
                preferred_followup_url = None

            await wait_for_followup_ready(page, full_name, preferred_followup_url=preferred_followup_url)
            print("followup url:", sanitize_url_for_log(page.url))
            full_name_locator, org_name_locator = await wait_for_followup_form(page)
            if await full_name_locator.count() == 0 or await org_name_locator.count() == 0:
                raise RuntimeError("未找到 followup 表单输入框")
            await full_name_locator.fill(full_name)
            selected_subdomain = await choose_available_org_name(page, subdomain)

            continue_button_followup = page.get_by_test_id("SignUp::SubmitOrgCreateForm")
            if await continue_button_followup.count() == 0:
                raise RuntimeError("未找到 followup Continue 按钮")

            print("等待 /api/user/changeName")
            print("等待 /api/organization/admin/initializeOrganization")
            async with AsyncExitStack() as stack:
                change_name_info = await stack.enter_async_context(
                    page.expect_response(
                        lambda resp: "/api/user/changeName" in resp.url,
                        timeout=60000,
                    )
                )
                init_org_info = await stack.enter_async_context(
                    page.expect_response(
                        lambda resp: "/api/organization/admin/initializeOrganization" in resp.url,
                        timeout=60000,
                    )
                )
                await continue_button_followup.click()

            change_name_resp = await change_name_info.value
            init_org_resp = await init_org_info.value

            print("changeName status:", change_name_resp.status)
            change_name_body = await read_response_body_safe(change_name_resp)
            print("changeName body:", format_for_log(change_name_body))

            print("initializeOrganization status:", init_org_resp.status)
            init_org_body = await read_response_body_safe(init_org_resp)
            print("initializeOrganization body:", format_for_log(init_org_body))

            await page.wait_for_timeout(3000)
            await complete_login_onboarding_flow(page)
            workspace_base_url = resolve_workspace_base_url(selected_subdomain)
            authorization_payload = None
            if not page.url.startswith(workspace_base_url):
                authorization_payload = await acquire_workspace_authorization_payload(page)
            await wait_for_workspace_ready(page, workspace_base_url, authorization_payload=authorization_payload)
            agent_summaries = await create_and_configure_agents(page, workspace_base_url)

            print("final url:", sanitize_url_for_log(page.url))
            print(f"SUCCESS_SUBDOMAIN: {selected_subdomain}.retool.com")
            print("SUCCESS_AGENTS:", json.dumps(sanitize_for_log(agent_summaries), ensure_ascii=False))
            return SignupSuccessResult(
                email=email,
                password=password,
                subdomain=selected_subdomain,
                workspace_url=workspace_base_url,
                created_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                agents=agent_summaries,
            )
        finally:
            await browser.close()


async def async_main() -> SignupSuccessResult:
    client = GeekEZBrowserClient(GEEKEZ_API_BASE)
    client.health()
    print("GeekEZ local API ok:", GEEKEZ_API_BASE)

    last_restart_error: Exception | None = None
    for attempt in range(RET0OL_SIGNUP_MAX_ATTEMPTS):
        if attempt > 0:
            print(f"restart signup flow from scratch: attempt {attempt + 1}/{RET0OL_SIGNUP_MAX_ATTEMPTS}")

        profile, created = resolve_target_profile(client)
        profile_id = str(profile.get("id") or "")
        profile_name = str(profile.get("name") or "")
        if not profile_id:
            raise RuntimeError("GeekEZ profile 缺少 id，无法继续")

        if created:
            print(
                "created fresh GeekEZ profile:",
                json.dumps({"id": profile_id, "name": profile_name}, ensure_ascii=False),
            )
        else:
            print(
                "reuse pinned GeekEZ profile:",
                json.dumps({"id": profile_id, "name": profile_name}, ensure_ascii=False),
            )

        opened = client.open_profile(profile_id)
        print("profile opened:")
        print(
            json.dumps(
                {
                    "profileId": opened.profile_id,
                    "name": opened.name,
                    "debugPort": opened.debug_port,
                    "cdpEndpoint": opened.cdp_endpoint,
                    "alreadyRunning": opened.was_already_running,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        try:
            result = await run_signup_flow(opened.cdp_endpoint)
            output_paths = persist_signup_output(result)
            print(
                "signup output appended:",
                json.dumps(
                    {
                        "csvPath": output_paths["csvPath"],
                        "jsonlPath": output_paths["jsonlPath"] or None,
                        "email": result.email,
                        "expectedSubdomain": result.subdomain,
                    },
                    ensure_ascii=False,
                ),
            )
            return result
        except RestartSignupFlowError as exc:
            last_restart_error = exc
            if attempt + 1 >= RET0OL_SIGNUP_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"followup 渲染异常，已重开 {RET0OL_SIGNUP_MAX_ATTEMPTS} 轮仍失败"
                ) from exc
            print(f"restartable signup failure: {exc}")
        finally:
            if opened.was_already_running:
                print("GeekEZ profile was already running; skip stop request")
            else:
                try:
                    client.stop_profile(opened.profile_id)
                    print(
                        "GeekEZ profile stop requested; profile retained for later inspection:",
                        json.dumps({"id": opened.profile_id, "name": opened.name}, ensure_ascii=False),
                    )
                except Exception as exc:
                    print("GeekEZ profile stop failed:", exc)

    if last_restart_error is not None:
        raise RuntimeError("signup flow exhausted restart attempts") from last_restart_error


def main() -> None:
    configure_stdio_for_logging()
    try:
        asyncio.run(async_main())
    except PlaywrightTimeoutError as exc:
        print("Playwright timeout:", exc)
        sys.exit(1)
    except Exception as exc:
        print("ERROR:", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
