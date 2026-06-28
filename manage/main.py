import os
import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from admin_service import AdminService
from api_keys import ApiKeyRegistry
from config import ConfigError, load_api_keys, load_gateway_config, resolve_relative_path
from conversation_store import ConversationStore
from audit_store import AuditStore
from models import (
    AccountRefreshRequest,
    ApiKeyBatchCreateRequest,
    ApiKeyDeleteRequest,
    ApiKeyUpsertRequest,
    AnthropicMessagesRequest,
    BundleImportRequest,
    ChatCompletionRequest,
    ManagedAccountDeleteRequest,
    ManagedAccountFingerprintResetRequest,
    ManagedAccountImportRequest,
    ManagedAccountUpsertRequest,
    ResponsesRequest,
    ToggleEnabledRequest,
)
from org_pool import OrgPool
from retool_client import RetoolClient
from service import (
    GatewayService,
    error_stream_generator,
    stream_responses_api_response,
    stream_text_response,
)
from state_store import create_audit_store, create_conversation_store, create_health_store


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RETOOL_GATEWAY_CONFIG", BASE_DIR / "gateway_config.json"))
API_KEYS_PATH = Path(os.environ.get("RETOOL_GATEWAY_API_KEYS", BASE_DIR / "api_keys.json"))
CONVERSATIONS_PATH = Path(os.environ.get("RETOOL_GATEWAY_CONVERSATIONS", BASE_DIR / "runtime" / "conversations.json"))
HEALTH_PATH = Path(os.environ.get("RETOOL_GATEWAY_HEALTH", BASE_DIR / "runtime" / "health.json"))
AUDIT_PATH = Path(os.environ.get("RETOOL_GATEWAY_AUDIT", BASE_DIR / "runtime" / "audit.json"))
IMPORT_HISTORY_PATH = Path(os.environ.get("RETOOL_GATEWAY_IMPORT_HISTORY", BASE_DIR / "runtime" / "session_bundle_imports.json"))
ACCOUNTS_PATH = Path(os.environ.get("RETOOL_GATEWAY_ACCOUNTS", BASE_DIR / "accounts.json"))
ACCOUNT_STATE_PATH = Path(os.environ.get("RETOOL_GATEWAY_ACCOUNT_STATE", BASE_DIR / "runtime" / "account_sessions.json"))
RUNTIME_ROOT = Path(os.environ.get("RETOOL_GATEWAY_RUNTIME_ROOT", BASE_DIR / "runtime"))
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"
DEFAULT_BROWSER_PROVIDER = os.environ.get("RETOOL_GATEWAY_BROWSER_PROVIDER", "cloakbrowser")
DEFAULT_ACCOUNT_MAX_CONCURRENCY = int(os.environ.get("RETOOL_GATEWAY_ACCOUNT_MAX_CONCURRENCY", "1"))


gateway_config = load_gateway_config(CONFIG_PATH)
api_key_registry = ApiKeyRegistry(load_api_keys(API_KEYS_PATH))
conversation_state_store = create_conversation_store(CONVERSATIONS_PATH)
health_state_store = create_health_store(HEALTH_PATH)
audit_state_store = create_audit_store(AUDIT_PATH)
conversation_store = ConversationStore(conversation_state_store, gateway_config.mapping_ttl_seconds)
audit_store = AuditStore(audit_state_store, gateway_config.audit_history_limit)
retool_client = RetoolClient(
    timeout_seconds=gateway_config.request_timeout_seconds,
    poll_interval_seconds=gateway_config.poll_interval_seconds,
    poll_max_attempts=gateway_config.poll_max_attempts,
    timezone=gateway_config.timezone,
)
org_pool = OrgPool(
    orgs=gateway_config.orgs,
    health_store=health_state_store,
    cooldown_seconds=gateway_config.health_cooldown_seconds,
    retool_client=retool_client,
    orgs_file_path=resolve_relative_path(CONFIG_PATH, gateway_config.orgs_file),
    allow_empty_file=gateway_config.allow_empty_org_pool,
    warning_seconds=gateway_config.admin_warning_days * 24 * 60 * 60,
)
gateway_service = GatewayService(
    model_aliases=gateway_config.model_aliases,
    org_pool=org_pool,
    conversation_store=conversation_store,
    retool_client=retool_client,
    audit_store=audit_store,
    debug_mode=DEBUG_MODE,
)
admin_service = AdminService(
    gateway_config=gateway_config,
    gateway_config_path=CONFIG_PATH,
    api_keys_path=API_KEYS_PATH,
    history_output_path=IMPORT_HISTORY_PATH,
    org_pool=org_pool,
    api_key_registry=api_key_registry,
    audit_store=audit_store,
    accounts_path=ACCOUNTS_PATH,
    account_state_path=ACCOUNT_STATE_PATH,
    runtime_root=RUNTIME_ROOT,
    gateway_refresh_callback=gateway_service.startup,
    default_browser_provider=DEFAULT_BROWSER_PROVIDER,
    default_max_concurrency=DEFAULT_ACCOUNT_MAX_CONCURRENCY,
)

app = FastAPI(title="Retool OpenAI Gateway")
security = HTTPBearer(auto_error=False)
health_refresh_task: asyncio.Task | None = None


@app.middleware("http")
async def debug_request_probe(request: Request, call_next):
    if DEBUG_MODE:
        try:
            body_bytes = await request.body()
            body_text = body_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            body_text = f"<failed to read body: {exc}>"
        interesting_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {
                "authorization",
                "content-type",
                "x-api-key",
                "anthropic-version",
                "anthropic-beta",
                "user-agent",
            }
        }
        print(f"[DEBUG] {request.method} {request.url.path}")
        print(f"[DEBUG] headers={interesting_headers}")
        print(f"[DEBUG] body={body_text[:2000]}")
    return await call_next(request)


async def authenticate_client(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
):
    return api_key_registry.authenticate(auth)


async def authenticate_gateway_client(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    if auth and auth.credentials:
        return api_key_registry.authenticate(auth)
    if x_api_key:
        synthetic_auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials=x_api_key)
        return api_key_registry.authenticate(synthetic_auth)
    return api_key_registry.authenticate(auth)


async def authenticate_admin(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
):
    return api_key_registry.authenticate_scope(auth, "admin")

async def refresh_org_health_forever():
    interval_seconds = max(int(gateway_config.health_refresh_interval_seconds), 30)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await gateway_service.startup()
        except Exception as exc:
            print(f"[WARN] background org health refresh failed: {exc}")


@app.on_event("startup")
async def startup():
    global health_refresh_task
    print("Starting Retool OpenAI gateway...")
    await gateway_service.startup()
    health_refresh_task = asyncio.create_task(refresh_org_health_forever())
    print("Gateway initialized.")


@app.on_event("shutdown")
async def shutdown():
    global health_refresh_task
    if health_refresh_task:
        health_refresh_task.cancel()
        try:
            await health_refresh_task
        except asyncio.CancelledError:
            pass
        health_refresh_task = None


@app.get("/v1/models")
async def list_v1_models(_: Annotated[object, Depends(authenticate_client)]):
    return gateway_service.get_models_list_response()


@app.get("/models")
async def list_models_no_auth():
    return gateway_service.get_models_list_response()


@app.get("/debug")
async def get_debug(enable: bool | None = Query(default=None)):
    global DEBUG_MODE
    if enable is not None:
        DEBUG_MODE = enable
        gateway_service.debug_mode = enable
    return {"debug_mode": DEBUG_MODE}


@app.get("/healthz")
async def healthz():
    overview = admin_service.overview()
    payload = {
        "status": "ok",
        "ready": overview["ready_orgs"] > 0,
        "org_count": len(org_pool.orgs),
        "model_count": len(gateway_service.model_aliases),
        "summary": overview,
    }
    return payload


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    api_key=Depends(authenticate_client),
    conversation_id: str | None = Header(default=None, alias=gateway_config.conversation_header),
):
    conversation_id = gateway_service.resolve_conversation_id(request, conversation_id)
    api_key_registry.begin_request(api_key)
    success = False
    try:
        result = await gateway_service.chat_completion(request, api_key, conversation_id)
        if request.stream:
            async def wrapped_stream():
                try:
                    async for chunk in stream_text_response(result, request.model):
                        yield chunk
                finally:
                    api_key_registry.end_request(api_key, success=True)

            success = True
            return StreamingResponse(
                wrapped_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        success = True
        return result
    finally:
        if not request.stream or not success:
            api_key_registry.end_request(api_key, success=success)


@app.head("/")
async def gateway_root_head():
    return {}


@app.head("/v1")
async def anthropic_gateway_head():
    return {}


@app.post("/v1/responses")
async def openai_responses(
    request: ResponsesRequest,
    api_key=Depends(authenticate_gateway_client),
    conversation_id: str | None = Header(default=None, alias=gateway_config.conversation_header),
):
    resolved_conversation_id = (
        conversation_id
        or request.conversation_id
        or f"responses-{request.model}-{os.urandom(8).hex()}"
    )
    chat_request = gateway_service.convert_responses_request(request, resolved_conversation_id)
    api_key_registry.begin_request(api_key)
    success = False
    try:
        result = await gateway_service.chat_completion(chat_request, api_key, resolved_conversation_id)
        if isinstance(result, str):
            content = result
            model_id = chat_request.model
        else:
            content = str(result.choices[0].message.content)
            model_id = result.model

        if request.stream:
            async def wrapped_responses_stream():
                try:
                    async for chunk in stream_responses_api_response(
                        model_id=model_id,
                        content=content,
                        request=request,
                    ):
                        yield chunk
                finally:
                    api_key_registry.end_request(api_key, success=True)

            success = True
            return StreamingResponse(
                wrapped_responses_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        success = True
        return gateway_service.build_responses_response(model_id=model_id, content=content, request=request)
    finally:
        if not request.stream or not success:
            api_key_registry.end_request(api_key, success=success)


@app.post("/v1/messages")
@app.post("/v1/v1/messages")
async def anthropic_messages(
    request: AnthropicMessagesRequest,
    api_key=Depends(authenticate_gateway_client),
    conversation_id: str | None = Header(default=None, alias=gateway_config.conversation_header),
):
    resolved_conversation_id = (
        conversation_id
        or (request.metadata or {}).get("conversation_id")
        or f"anthropic-{request.model}-{os.urandom(8).hex()}"
    )
    chat_request = gateway_service.convert_anthropic_messages_request(request, resolved_conversation_id)
    api_key_registry.begin_request(api_key)
    success = False
    try:
        result = await gateway_service.chat_completion(chat_request, api_key, resolved_conversation_id)
        if isinstance(result, str):
            content = result
            model_id = chat_request.model
        else:
            content = str(result.choices[0].message.content)
            model_id = result.model
        success = True
        return gateway_service.build_anthropic_messages_response(model_id=model_id, content=content)
    finally:
        api_key_registry.end_request(api_key, success=success)


@app.get("/admin", response_class=HTMLResponse)
async def admin_console():
    return HTMLResponse(build_admin_console_html())


@app.get("/admin/api/overview")
async def admin_overview(_: Annotated[object, Depends(authenticate_admin)]):
    return admin_service.overview()


@app.get("/admin/api/orgs")
async def admin_orgs(_: Annotated[object, Depends(authenticate_admin)]):
    return {"items": admin_service.list_orgs()}


@app.get("/admin/api/accounts")
async def admin_accounts(_: Annotated[object, Depends(authenticate_admin)]):
    return {
        "items": admin_service.list_accounts(),
        "refresh_job": admin_service.get_account_refresh_status(),
    }


@app.post("/admin/api/accounts")
async def admin_accounts_upsert(
    request: ManagedAccountUpsertRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.upsert_account(request)


@app.post("/admin/api/accounts/import")
async def admin_accounts_import(
    request: ManagedAccountImportRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.import_accounts(request)


@app.post("/admin/api/accounts/delete")
async def admin_accounts_delete(
    request: ManagedAccountDeleteRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.delete_account(request.id)


@app.post("/admin/api/accounts/reset-fingerprint")
async def admin_accounts_reset_fingerprint(
    request: ManagedAccountFingerprintResetRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.reset_account_fingerprints(request)


@app.post("/admin/api/accounts/refresh")
async def admin_accounts_refresh(
    request: AccountRefreshRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return await admin_service.start_account_refresh_job(request)


@app.get("/admin/api/accounts/refresh-status")
async def admin_accounts_refresh_status(_: Annotated[object, Depends(authenticate_admin)]):
    return admin_service.get_account_refresh_status()


@app.post("/admin/api/orgs/{org_id}/enabled")
async def admin_org_enabled(
    org_id: str,
    request: ToggleEnabledRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.update_org_enabled(org_id, request.enabled)


@app.post("/admin/api/orgs/{org_id}/cooldown/reset")
async def admin_org_cooldown_reset(
    org_id: str,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.reset_org_cooldown(org_id)


@app.post("/admin/api/session-bundles/import")
async def admin_import_bundle(
    request: BundleImportRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return await admin_service.import_bundle(
        filename=request.filename,
        content=request.content.encode("utf-8"),
        allow_expired=request.allow_expired,
    )


@app.post("/admin/api/health/refresh")
async def admin_refresh_health(_: Annotated[object, Depends(authenticate_admin)]):
    await gateway_service.startup()
    return {"refreshed": True, "summary": admin_service.overview()}


@app.get("/admin/api/imports")
async def admin_import_history(_: Annotated[object, Depends(authenticate_admin)]):
    return admin_service.import_history()


@app.get("/admin/api/audit")
async def admin_audit(_: Annotated[object, Depends(authenticate_admin)]):
    return {"items": admin_service.list_audit_entries()}


@app.get("/admin/api/api-keys")
async def admin_api_keys(_: Annotated[object, Depends(authenticate_admin)]):
    return {"items": admin_service.list_api_keys()}


@app.post("/admin/api/api-keys")
async def admin_api_keys_upsert(
    request: ApiKeyUpsertRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.upsert_api_key(request)


@app.post("/admin/api/api-keys/delete")
async def admin_api_keys_delete(
    request: ApiKeyDeleteRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.delete_api_key(request.id)


@app.post("/admin/api/api-keys/batch")
async def admin_api_keys_batch(
    request: ApiKeyBatchCreateRequest,
    _: Annotated[object, Depends(authenticate_admin)],
):
    return admin_service.create_api_key_batch(
        owner=request.owner,
        count=request.count,
        scopes=request.scopes,
        concurrency_limit=request.concurrency_limit,
        enabled=request.enabled,
    )


@app.get("/admin/api/settings")
async def admin_settings(_: Annotated[object, Depends(authenticate_admin)]):
    return admin_service.settings()


def ensure_example_files():
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.write_text(
        """{
  "conversation_header": "X-Conversation-ID",
  "timezone": "Asia/Shanghai",
  "orgs_file": "orgs.json",
  "allow_empty_org_pool": true,
  "request_timeout_seconds": 120,
  "poll_interval_seconds": 1.0,
  "poll_max_attempts": 300,
  "mapping_ttl_seconds": 604800,
  "health_cooldown_seconds": 300,
  "health_refresh_interval_seconds": 300,
  "admin_warning_days": 2,
  "audit_history_limit": 500,
  "models": [
    {
      "id": "gpt-5.5",
      "owned_by": "openai",
      "agent_name": "gpt5",
      "model_name": "gpt-5.5",
      "display_name": "Retool pooled GPT-5.5"
    },
    {
      "id": "claude-sonnet-4-6",
      "owned_by": "anthropic",
      "agent_name": "claude",
      "model_name": "claude-sonnet-4-6",
      "display_name": "Retool pooled Claude Sonnet 4.6"
    }
  ]
}
""",
        encoding="utf-8",
    )
    orgs_path = BASE_DIR / "orgs.example.json"
    if not orgs_path.exists():
        orgs_path.write_text(
            """[
  {
    "id": "example-org",
    "domain_name": "your-domain.retool.com",
    "x_xsrf_token": "your-xsrf-token",
    "accessToken": "your-access-token",
    "enabled": true
  }
]
""",
            encoding="utf-8",
        )
    if not API_KEYS_PATH.exists():
        API_KEYS_PATH.write_text(
            """[
  {
    "id": "office-default",
    "key": "sk-example-internal-key",
    "enabled": true,
    "owner": "office",
    "scopes": ["inference", "admin"]
  }
]
""",
            encoding="utf-8",
        )


def build_admin_console_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Retool Gateway Admin</title>
  <style>
    :root {
      --bg: #f4efe6;
      --panel: rgba(255, 250, 243, 0.94);
      --panel-strong: #fff8ef;
      --ink: #1d1914;
      --muted: #6a6258;
      --line: rgba(29, 25, 20, 0.12);
      --accent: #c0551a;
      --accent-soft: #f4d8bf;
      --ok: #1b7f55;
      --warn: #b56b00;
      --bad: #aa2f1f;
      --shadow: 0 16px 40px rgba(77, 44, 19, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(240, 192, 134, 0.42), transparent 30%),
        radial-gradient(circle at top right, rgba(197, 85, 26, 0.18), transparent 24%),
        linear-gradient(180deg, #f7f3eb 0%, #f2ece2 55%, #efe6d8 100%);
      min-height: 100vh;
    }
    .wrap {
      max-width: 1500px;
      margin: 0 auto;
      padding: 28px 20px 44px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.25fr 0.75fr;
      gap: 18px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 18px;
      backdrop-filter: blur(10px);
    }
    .hero-title {
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -0.02em;
      margin: 0 0 8px;
    }
    .hero-sub {
      color: var(--muted);
      line-height: 1.6;
      margin: 0;
      max-width: 760px;
    }
    .quick-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 18px;
    }
    .metric {
      padding: 14px;
      border-radius: 18px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
    }
    .metric .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
    .metric .value { font-size: 28px; font-weight: 800; margin-top: 8px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: #fff5eb;
      border: 1px solid rgba(192, 85, 26, 0.18);
      font-size: 13px;
    }
    .toast {
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 1000;
      max-width: min(420px, calc(100vw - 32px));
      padding: 12px 14px;
      border-radius: 16px;
      background: #1f4f32;
      color: #f3fbf5;
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity 0.18s ease, transform 0.18s ease;
    }
    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }
    .tag, .pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 8px;
      margin: 0 6px 6px 0;
      border-radius: 999px;
      background: var(--accent-soft);
      font-size: 12px;
      font-weight: 700;
    }
    .layout {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
    }
    .stack { display: grid; gap: 18px; }
    h2 {
      margin: 0 0 14px;
      font-size: 18px;
      font-weight: 800;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 0; }
    .toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      gap: 10px;
      flex-wrap: wrap;
    }
    button, .file-btn {
      border: 0;
      border-radius: 14px;
      padding: 10px 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary, .file-btn.secondary {
      background: #efe2d2;
      color: var(--ink);
    }
    button.ghost {
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    button:disabled {
      opacity: 0.58;
      cursor: not-allowed;
    }
    .file-btn input { display: none; }
    .muted { color: var(--muted); }
    .tiny { font-size: 12px; }
    .good { color: var(--ok); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .logbox {
      background: #201913;
      color: #f7f0e4;
      border-radius: 18px;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
      min-height: 160px;
      white-space: pre-wrap;
    }
    .subtle-box {
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.72);
    }
    .input-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 10px;
    }
    .input-row.three {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
    }
    textarea {
      resize: vertical;
      min-height: 96px;
    }
    .stats-inline {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .stats-inline .pill {
      margin: 0;
    }
    .table-wrap { overflow: auto; }
    .cell-code {
      word-break: break-all;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    @media (max-width: 1180px) {
      .hero, .layout { grid-template-columns: 1fr; }
      .quick-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .quick-grid, .input-row, .input-row.three { grid-template-columns: 1fr; }
      .wrap { padding: 18px 14px 28px; }
      .hero-title { font-size: 26px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="card">
        <h1 class="hero-title">Retool Gateway 管理台</h1>
        <p class="hero-sub">第一次导入账号密码和组织子域后，后续尽量都从这里完成账号维护、会话刷新、org 池观察和 API key 管理。当前默认建议走 CloakBrowser Headless；GeekEZ 仍可作为本地备用路径，Obscura 保留为实验登录后端。</p>
        <div class="quick-grid" id="metrics"></div>
      </div>
      <div class="card">
        <div class="status-pill" id="statusPill">加载中...</div>
        <div class="input-row" style="margin-top:14px;">
          <input id="adminToken" placeholder="输入具备 admin scope 的 Bearer token">
          <button id="saveTokenBtn">保存 Token</button>
        </div>
        <div class="actions">
          <label class="file-btn">
            导入 Bundle
            <input id="bundleInput" type="file" accept=".json">
          </label>
          <button class="secondary" id="refreshHealthBtn">刷新健康</button>
          <button class="secondary" id="refreshBtn">刷新视图</button>
        </div>
        <p class="muted tiny">管理端认证复用 Bearer key，但要求具备 <code>admin</code> scope。Token 仅保存在当前浏览器的 localStorage。账号刷新任务由服务端启动并持续写入任务日志。</p>
      </div>
    </section>

    <section class="layout">
      <div class="stack">
        <div class="card">
          <div class="toolbar">
            <h2>账号库存 / 会话刷新</h2>
            <span class="muted tiny">第一次录入账号密码后，后续在此编辑、删除、刷新登录态</span>
          </div>
          <div class="input-row">
            <select id="existingAccountSelect">
              <option value="">新建账号</option>
            </select>
            <button class="secondary" id="resetAccountFormBtn">切换到新建</button>
          </div>
          <div class="input-row">
            <input id="accountEmail" placeholder="Retool 邮箱">
            <input id="accountPassword" placeholder="Retool 密码，编辑时留空表示保留原密码">
          </div>
          <div class="input-row three">
            <input id="accountSubdomain" placeholder="组织子域名，如 example-subdomain">
            <select id="accountBrowserProvider">
              <option value="">默认浏览器提供方</option>
              <option value="geekez">GeekEZ</option>
              <option value="cloakbrowser">CloakBrowser Headless（推荐）</option>
            </select>
            <select id="accountEnabled">
              <option value="true">启用</option>
              <option value="false">停用</option>
            </select>
          </div>
          <div class="input-row">
            <input id="accountId" placeholder="账号 ID，可留空自动生成">
            <input id="accountNotes" placeholder="备注，可留空">
          </div>
          <div class="input-row">
            <input id="accountFingerprintSeed" placeholder="账号浏览器指纹 Seed" readonly>
            <button class="secondary" id="resetFingerprintBtn">重置当前账号指纹</button>
          </div>
          <div class="subtle-box tiny muted" id="accountSecretHint">密码不会在表格中明文回显；编辑已有账号时，密码留空表示保持不变。</div>
          <div class="actions">
            <button id="saveAccountBtn">保存账号</button>
            <button class="secondary" id="deleteAccountBtn">删除当前账号</button>
          </div>
          <div class="input-row" style="margin-top:14px;">
            <textarea id="accountImportBox" placeholder="批量导入账号。支持 CSV 或 JSON。标准 CSV 列顺序：email,password,expected_subdomain,enabled,notes"></textarea>
            <div class="subtle-box">
              <div class="tiny muted">批量导入说明</div>
              <div class="tiny muted" style="margin-top:8px;">
                标准带表头 CSV：<code>email,password,expected_subdomain,enabled,notes</code><br>
                也兼容扩展列：<code>browser_provider</code>、<code>fingerprint_seed</code><br>
                也支持 JSON 数组：<code>[{"email":"...","password":"...","expected_subdomain":"..."}]</code>
              </div>
              <div class="actions">
                <button class="secondary" id="mergeAccountsBtn">合并导入</button>
                <button class="secondary" id="replaceAccountsBtn">覆盖导入</button>
              </div>
            </div>
          </div>
          <div class="toolbar" style="margin-top:8px;">
            <span class="muted tiny">刷新时默认只刷状态缺失或失效账号；当前默认建议 CloakBrowser Headless，GeekEZ 可作为备用。实验 provider 已从页面隐藏，避免干扰本地回归。</span>
            <div class="actions" style="margin:0;">
              <button class="secondary" id="refreshSelectedAccountsBtn">刷新选中账号</button>
              <button class="secondary" id="refreshAllAccountsBtn">刷新全部启用账号</button>
              <button class="secondary" id="verifyAccountsBtn">仅校验现有会话</button>
            </div>
          </div>
          <div class="input-row three">
            <select id="refreshBrowserProvider">
              <option value="">默认浏览器提供方</option>
              <option value="geekez">GeekEZ</option>
              <option value="cloakbrowser" selected>CloakBrowser Headless（推荐）</option>
            </select>
            <input id="refreshConcurrency" type="number" min="1" placeholder="并发数，留空走默认">
            <input id="refreshCheckModels" placeholder="校验模型，逗号分隔，如 gpt-5.5,claude-sonnet-4-6">
          </div>
          <div class="input-row three">
            <label class="tiny muted"><input id="refreshOnlyToggle" type="checkbox" checked> 仅刷新缺失/失效账号</label>
            <label class="tiny muted"><input id="ignoreCooldownToggle" type="checkbox"> 忽略 cooldown</label>
            <label class="tiny muted"><input id="headlessToggle" type="checkbox" checked> headless</label>
          </div>
          <div id="accountJobBox" class="logbox" style="margin-top:12px;">等待刷新任务...</div>
          <div class="table-wrap" style="margin-top:12px;">
            <table>
              <thead>
                <tr>
                  <th><input id="selectAllAccounts" type="checkbox"></th>
                  <th>账号</th>
                  <th>状态</th>
                  <th>最近刷新</th>
                  <th>错误</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody id="accountRows"></tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="toolbar">
            <h2>Org Pool</h2>
            <span class="muted tiny">支持启停 org、重置 cooldown、观察过期与 agent 可用性</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Org</th>
                  <th>状态</th>
                  <th>模型</th>
                  <th>过期</th>
                  <th>最近结果</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody id="orgRows"></tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="toolbar">
            <h2>请求审计</h2>
            <span class="muted tiny">最近请求摘要</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>Key</th>
                  <th>模型</th>
                  <th>Org</th>
                  <th>结果</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody id="auditRows"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="stack">
        <div class="card">
          <div class="toolbar">
            <h2>API Keys</h2>
            <span class="muted tiny">支持初版内网 key 管理</span>
          </div>
          <div class="input-row">
            <input id="keyOwnerFilter" placeholder="按 owner 过滤">
            <select id="keyScopeFilter">
              <option value="">全部 scope</option>
              <option value="inference">仅 inference</option>
              <option value="admin">仅 admin</option>
              <option value="hybrid">inference + admin</option>
            </select>
          </div>
          <div class="input-row">
            <input id="batchOwner" placeholder="批量生成 owner，如 office-batch">
            <input id="batchCount" type="number" min="1" max="100" value="10" placeholder="批量数量">
          </div>
          <div class="actions" style="margin-top:0;">
            <button class="secondary" id="createBatchBtn">批量生成办公 Key</button>
          </div>
          <div class="input-row">
            <select id="existingKeySelect">
              <option value="">新建 Key</option>
            </select>
            <button class="secondary" id="resetKeyFormBtn">切换到新建</button>
          </div>
          <div class="input-row">
            <select id="apiKeyTemplate">
              <option value="inference">办公调用 Key（inference）</option>
              <option value="hybrid">管理员 Key（inference + admin）</option>
              <option value="admin">只管理 Key（admin）</option>
            </select>
            <input id="apiKeyOwner" placeholder="owner，如 office / finance / ops">
          </div>
          <div class="input-row">
            <input id="apiKeyConcurrency" type="number" min="1" placeholder="并发上限，可留空">
            <input id="apiKeyId" placeholder="自定义 id，可留空自动生成">
          </div>
          <div class="input-row">
            <input id="apiKeySecret" placeholder="自定义 secret，可留空自动生成随机 key">
            <div class="subtle-box tiny muted">
              通常只需要选择用途模板并填写 owner，`id` 和 `secret` 留空即可自动生成。
            </div>
          </div>
          <div class="input-row">
            <input id="apiKeyCurrentSecret" placeholder="当前完整 key，选中已有 key 或创建成功后显示" readonly>
            <button class="secondary" id="copyCurrentKeyBtn">复制完整 Key</button>
          </div>
          <div class="actions">
            <button id="saveKeyBtn">创建 / 更新 Key</button>
            <button class="secondary" id="rotateKeyBtn">重置 Secret</button>
            <button class="secondary" id="toggleKeyBtn">停用当前 Key</button>
            <button class="secondary" id="deleteKeyBtn">删除当前 Key</button>
          </div>
          <div id="keyResultBox" class="logbox" style="display:none; margin-top:12px;"></div>
          <div id="batchResultBox" class="logbox" style="display:none; margin-top:12px;"></div>
          <div class="table-wrap" style="margin-top:12px;">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Owner</th>
                  <th>Scope</th>
                  <th>状态</th>
                  <th>最近使用</th>
                  <th>预览</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody id="keyRows"></tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="toolbar">
            <h2>导入历史 / 系统设置</h2>
            <span class="muted tiny">帮助维护人员判断轮换节奏</span>
          </div>
          <div id="settingsBox" class="muted tiny"></div>
          <div class="table-wrap" style="margin-top:12px;">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>导入数</th>
                  <th>跳过数</th>
                  <th>来源</th>
                </tr>
              </thead>
              <tbody id="importRows"></tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <h2>操作日志</h2>
          <div id="logBox" class="logbox">等待操作...</div>
        </div>
      </div>
    </section>
  </div>
  <div id="toast" class="toast"></div>

  <script>
    const state = {
      bundleFile: null,
      apiKeys: [],
      selectedKeyId: "",
      accounts: [],
      selectedAccountId: "",
      refreshJob: null,
      refreshPollTimer: null,
    };
    const keyTemplates = {
      inference: { label: "办公调用 Key", scopes: ["inference"] },
      hybrid: { label: "管理员 Key", scopes: ["inference", "admin"] },
      admin: { label: "只管理 Key", scopes: ["admin"] }
    };
    const authStateLabels = {
      ready: "可用",
      cooldown: "冷却中",
      disabled: "已停用",
      captcha_blocked: "验证码拦截",
      browser_unsupported: "浏览器不兼容",
      login_required: "需要重新登录",
      workspace_bridge_failed: "工作区桥接失败",
      mfa_required: "需要二次验证",
      agent_missing: "Agent 不完整",
      unknown: "未知"
    };
    const jobStatusLabels = {
      idle: "空闲",
      queued: "排队中",
      running: "运行中",
      succeeded: "已成功",
      completed_with_errors: "部分成功",
      failed: "已失败",
      skipped: "已跳过"
    };
    const browserProviderLabels = {
      geekez: "GeekEZ",
      geekez_api: "GeekEZ API",
      geekez_executable: "GeekEZ 可执行",
      obscura: "Obscura",
      cloakbrowser: "CloakBrowser Headless",
      cloakbrowser_headless: "CloakBrowser Headless",
      verify_only_cloakbrowser_headless: "CloakBrowser Headless（仅校验）",
      verify_only_obscura: "Obscura（仅校验）",
      verify_only_geekez_executable: "GeekEZ 可执行（仅校验）",
      playwright: "Playwright 持久上下文",
      auto: "Auto"
    };

    function fmtTime(value) {
      if (!value) return "-";
      const numeric = Number(value);
      const date = new Date(numeric > 1e12 ? numeric : numeric * 1000);
      if (Number.isNaN(date.getTime())) return "-";
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    function fmtMs(value) {
      return value ? `${value} ms` : "-";
    }

    function summarizeText(value, maxLength = 160) {
      const text = String(value ?? "").replace(/\\s+/g, " ").trim();
      if (!text) return "";
      if (text.length <= maxLength) return text;
      return `${text.slice(0, maxLength - 3)}...`;
    }

    function labelAuthState(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return authStateLabels[normalized] || normalized || authStateLabels.unknown;
    }

    function labelJobStatus(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return jobStatusLabels[normalized] || normalized || "-";
    }

    function labelBrowserProvider(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return browserProviderLabels[normalized] || value || "-";
    }

    function summarizeAccountError(item) {
      const authState = String(item?.auth_state || "").trim().toLowerCase();
      const provider = String(item?.browser_provider_runtime || item?.browser_provider || "").trim().toLowerCase();
      if (["captcha_blocked", "browser_unsupported"].includes(authState) && provider.includes("obscura")) {
        return "Obscura 当前无法兼容 Retool 登录页，请改用 GeekEZ 刷新";
      }
      const raw = summarizeText(item?.last_error || "", 160);
      if (!raw) return "-";
      return raw;
    }

    function writeLog(message) {
      const box = document.getElementById("logBox");
      const stamp = new Date().toLocaleString("zh-CN", { hour12: false });
      box.textContent = `[${stamp}] ${message}\n` + box.textContent;
    }

    let toastTimer = null;
    function showToast(message) {
      const box = document.getElementById("toast");
      box.textContent = message;
      box.classList.add("show");
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => box.classList.remove("show"), 2200);
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function boolValueFromSelect(id) {
      return document.getElementById(id).value === "true";
    }

    function checkedAccountIds() {
      return [...document.querySelectorAll(".account-check:checked")]
        .map((node) => node.value)
        .filter(Boolean);
    }

    async function api(path, init = {}) {
      const adminToken = localStorage.getItem("retool_admin_token") || "";
      const response = await fetch(path, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          ...(adminToken ? { "Authorization": `Bearer ${adminToken}` } : {}),
          ...(init.headers || {})
        }
      });
      const text = await response.text();
      let payload = {};
      try { payload = text ? JSON.parse(text) : {}; } catch { payload = { raw: text }; }
      if (!response.ok) {
        throw new Error(payload.detail || payload.raw || response.statusText);
      }
      return payload;
    }

    function renderMetrics(data) {
      const items = [
        ["总 Org", data.total_orgs ?? 0],
        ["可用 Org", data.ready_orgs ?? 0],
        ["启用账号", data.enabled_account_count ?? 0],
        ["账号就绪", data.ready_account_count ?? 0],
        ["活跃请求", data.active_request_count ?? 0],
        ["审计失败", data.audit_failure_count ?? 0],
      ];
      document.getElementById("metrics").innerHTML = items.map(([label, value]) => `
        <div class="metric">
          <div class="label">${label}</div>
          <div class="value">${value}</div>
        </div>
      `).join("");
      const job = data.account_refresh_job || {};
      const jobText = job.status && job.status !== "idle" ? `，任务 ${labelJobStatus(job.status)}` : "";
      document.getElementById("statusPill").textContent =
        `可用 ${data.ready_orgs ?? 0} / ${data.total_orgs ?? 0}，账号 ${data.ready_account_count ?? 0} / ${data.enabled_account_count ?? 0}${jobText}`;
    }

    function renderOrgs(items) {
      document.getElementById("orgRows").innerHTML = items.map(item => `
        <tr>
          <td>
            <strong>${escapeHtml(item.domain_name)}</strong><br>
            <span class="muted tiny">${escapeHtml(item.source_email || item.source_account_id || "-")}</span>
          </td>
          <td>
            <div class="${item.enabled ? "good" : "warn"}">${item.enabled ? "enabled" : "disabled"}</div>
            <div class="${item.is_expired ? "bad" : item.is_expiring_soon ? "warn" : "muted"}">
              ${item.is_expired ? "expired" : item.is_expiring_soon ? "expiring soon" : escapeHtml(item.auth_state)}
            </div>
            <div class="tiny muted">cooldown: ${fmtTime(item.cooldown_until)}</div>
          </td>
          <td>${(item.verified_models || []).map(model => `<span class="tag">${escapeHtml(model)}</span>`).join("") || "-"}</td>
          <td>${fmtTime(item.expires_at)}</td>
          <td>
            <div class="good tiny">成功 ${fmtTime(item.last_success_at)}</div>
            <div class="bad tiny">失败 ${fmtTime(item.last_failure_at)}</div>
          </td>
          <td>
            <div class="actions">
              <button class="secondary" onclick="toggleOrg('${encodeURIComponent(item.id)}', ${!item.enabled})">${item.enabled ? "停用" : "启用"}</button>
              <button class="secondary" onclick="resetCooldown('${encodeURIComponent(item.id)}')">重置冷却</button>
            </div>
          </td>
        </tr>
      `).join("");
    }

    function renderAudit(items) {
      document.getElementById("auditRows").innerHTML = items.slice(0, 20).map(item => `
        <tr>
          <td>${fmtTime(item.happened_at)}</td>
          <td>${escapeHtml(item.api_key_id || "-")}</td>
          <td>${escapeHtml(item.model_id)}</td>
          <td>${escapeHtml(item.domain_name || "-")}</td>
          <td class="${item.success ? "good" : "bad"}">${item.success ? "success" : escapeHtml(item.error || "failed")}</td>
          <td>${fmtMs(item.duration_ms)}</td>
        </tr>
      `).join("");
    }

    function renderKeys(items) {
      state.apiKeys = items;
      syncKeySelect(items);
      const ownerFilter = (document.getElementById("keyOwnerFilter").value || "").trim().toLowerCase();
      const scopeFilter = (document.getElementById("keyScopeFilter").value || "").trim().toLowerCase();
      const filtered = items.filter(item => {
        const owner = String(item.owner || "").toLowerCase();
        const scopes = (item.scopes || []).map(v => String(v).toLowerCase());
        const ownerMatch = !ownerFilter || owner.includes(ownerFilter) || String(item.id || "").toLowerCase().includes(ownerFilter);
        const scopeMatch = !scopeFilter
          || (scopeFilter === "hybrid" && scopesEqual(scopes, ["inference", "admin"]))
          || (scopeFilter !== "hybrid" && scopes.includes(scopeFilter));
        return ownerMatch && scopeMatch;
      });
      document.getElementById("keyRows").innerHTML = filtered.map(item => `
        <tr>
          <td>${escapeHtml(item.id)}</td>
          <td>${escapeHtml(item.owner || "-")}</td>
          <td>${escapeHtml((item.scopes || []).join(", "))}</td>
          <td class="${item.enabled ? "good" : "warn"}">${item.enabled ? "enabled" : "disabled"}</td>
          <td>${fmtTime(item.last_used_at)}<br><span class="muted tiny">active ${item.active_requests} / total ${item.total_requests}</span></td>
          <td class="cell-code">${escapeHtml(item.key_preview)}</td>
          <td>
            <div class="actions">
              <button class="secondary" onclick="selectKey('${escapeHtml(item.id)}')">编辑</button>
              <button class="secondary" onclick="copyKeyById('${escapeHtml(item.id)}')">复制</button>
            </div>
          </td>
        </tr>
      `).join("");
    }

    function renderKeyResult(payload) {
      const box = document.getElementById("keyResultBox");
      if (!payload) {
        box.style.display = "none";
        box.textContent = "";
        return;
      }
      const fullKey = payload.key || "(本次没有返回完整 key，通常表示你更新的是已有 key 且没有重置 secret)";
      box.style.display = "block";
      box.innerHTML = [
        `<strong>${payload.updated ? "Key 已更新" : "Key 已创建"}</strong>`,
        `ID: ${escapeHtml(payload.id)}`,
        `Owner: ${escapeHtml(payload.owner || "-")}`,
        `Scopes: ${escapeHtml((payload.scopes || []).join(", ") || "-")}`,
        `完整 Key: ${escapeHtml(fullKey)}`,
      ].join("<br>");
    }

    function renderBatchResult(payload) {
      const box = document.getElementById("batchResultBox");
      if (!payload || !(payload.items || []).length) {
        box.style.display = "none";
        box.textContent = "";
        return;
      }
      box.style.display = "block";
      box.innerHTML = [
        `<strong>批量生成完成</strong>`,
        `数量: ${escapeHtml(payload.count)}`,
        ...payload.items.map(item => `ID: ${escapeHtml(item.id)}<br>完整 Key: ${escapeHtml(item.key)}`)
      ].join("<br><br>");
    }

    function renderImports(history) {
      const items = history.history || [];
      document.getElementById("importRows").innerHTML = items.slice(0, 20).map(item => `
        <tr>
          <td>${fmtTime(item.generated_at)}</td>
          <td>${escapeHtml(item.imported_org_count)}</td>
          <td>${escapeHtml(item.skipped_org_count)}</td>
          <td class="tiny">${escapeHtml(item.filename || item.bundle_path || "-")}</td>
        </tr>
      `).join("");
    }

    function renderSettings(settings) {
      const models = (settings.models || []).map(item => item.id).join(", ");
      document.getElementById("settingsBox").innerHTML = `
        <div>会话头: <strong>${escapeHtml(settings.conversation_header)}</strong></div>
        <div>冷却秒数: <strong>${escapeHtml(settings.health_cooldown_seconds)}</strong></div>
        <div>健康刷新秒数: <strong>${escapeHtml(settings.health_refresh_interval_seconds)}</strong></div>
        <div>预警天数: <strong>${escapeHtml(settings.admin_warning_days)}</strong></div>
        <div>受管池文件: <strong>${escapeHtml(settings.orgs_file)}</strong></div>
        <div>账号库存文件: <strong>${escapeHtml(settings.accounts_file || "-")}</strong></div>
        <div>账号状态文件: <strong>${escapeHtml(settings.account_state_file || "-")}</strong></div>
        <div>默认浏览器: <strong>${escapeHtml(labelBrowserProvider(settings.default_browser_provider || "-"))}</strong></div>
        <div>浏览器建议: <strong>CloakBrowser Headless</strong> 为当前默认推荐，<strong>GeekEZ</strong> 作为备用路径，<strong>Obscura</strong> 仅保留实验用途</div>
        <div>默认模型校验: <strong>${escapeHtml(models || "-")}</strong></div>
      `;
    }

    function scopesEqual(left, right) {
      const a = [...new Set((left || []).map(v => String(v).trim().toLowerCase()).filter(Boolean))].sort();
      const b = [...new Set((right || []).map(v => String(v).trim().toLowerCase()).filter(Boolean))].sort();
      return JSON.stringify(a) === JSON.stringify(b);
    }

    function inferTemplate(scopes) {
      for (const [name, template] of Object.entries(keyTemplates)) {
        if (scopesEqual(scopes, template.scopes)) return name;
      }
      return "hybrid";
    }

    function getKeyById(keyId) {
      return state.apiKeys.find(item => item.id === keyId) || null;
    }

    function setKeyFormMode(keyId) {
      state.selectedKeyId = keyId || "";
      const selected = getKeyById(state.selectedKeyId);
      document.getElementById("apiKeyId").readOnly = Boolean(selected);
      document.getElementById("rotateKeyBtn").disabled = !selected;
      document.getElementById("toggleKeyBtn").disabled = !selected;
      document.getElementById("deleteKeyBtn").disabled = !selected;
      document.getElementById("toggleKeyBtn").textContent = selected
        ? (selected.enabled ? "停用当前 Key" : "启用当前 Key")
        : "停用当前 Key";
    }

    function populateKeyForm(item, options = {}) {
      const keyId = item?.id || "";
      document.getElementById("existingKeySelect").value = keyId;
      document.getElementById("apiKeyId").value = keyId;
      document.getElementById("apiKeyOwner").value = item?.owner || "";
      document.getElementById("apiKeyConcurrency").value = item?.concurrency_limit ?? "";
      document.getElementById("apiKeyTemplate").value = inferTemplate(item?.scopes || keyTemplates.inference.scopes);
      document.getElementById("apiKeySecret").value = "";
      document.getElementById("apiKeyCurrentSecret").value = options.currentSecret ?? item?.key ?? "";
      setKeyFormMode(keyId);
      if (!item) renderKeyResult(null);
    }

    function resetKeyForm() {
      populateKeyForm(null, { currentSecret: "" });
    }

    function syncKeySelect(items) {
      const select = document.getElementById("existingKeySelect");
      const previous = state.selectedKeyId;
      select.innerHTML = [
        `<option value="">新建 Key</option>`,
        ...items.map(item => `<option value="${item.id}">${escapeHtml(item.id)} | ${escapeHtml(item.owner || "-")} | ${escapeHtml((item.scopes || []).join("+") || "-")}</option>`)
      ].join("");
      if (previous && items.some(item => item.id === previous)) {
        populateKeyForm(getKeyById(previous));
        return;
      }
      if (!items.length) resetKeyForm();
    }

    function getAccountById(accountId) {
      return state.accounts.find(item => item.id === accountId) || null;
    }

    function setAccountFormMode(accountId) {
      state.selectedAccountId = accountId || "";
      const selected = getAccountById(state.selectedAccountId);
      document.getElementById("accountId").readOnly = Boolean(selected);
      document.getElementById("deleteAccountBtn").disabled = !selected;
      document.getElementById("resetFingerprintBtn").disabled = !selected;
      document.getElementById("accountSecretHint").textContent = selected
        ? "编辑已有账号时，密码留空表示保持原密码不变；浏览器指纹 Seed 默认保持稳定，除非你手工点击重置。"
        : "新建账号时必须填写密码；账号保存后不会在表格中明文展示，首次保存会自动生成独立浏览器指纹 Seed。";
    }

    function populateAccountForm(item) {
      document.getElementById("existingAccountSelect").value = item?.id || "";
      document.getElementById("accountId").value = item?.id || "";
      document.getElementById("accountEmail").value = item?.email || "";
      document.getElementById("accountPassword").value = "";
      document.getElementById("accountSubdomain").value = item?.expected_subdomain || "";
      document.getElementById("accountBrowserProvider").value = item?.browser_provider || "";
      document.getElementById("accountFingerprintSeed").value = item?.fingerprint_seed || "";
      document.getElementById("accountEnabled").value = String(item?.enabled ?? true);
      document.getElementById("accountNotes").value = item?.notes || "";
      setAccountFormMode(item?.id || "");
    }

    function resetAccountForm() {
      populateAccountForm(null);
    }

    function syncHeadlessToggle() {
      const provider = (document.getElementById("refreshBrowserProvider").value || "").trim().toLowerCase();
      const headlessToggle = document.getElementById("headlessToggle");
      const mustBeHeadless = provider === "cloakbrowser";
      if (mustBeHeadless) {
        headlessToggle.checked = true;
      }
      headlessToggle.disabled = mustBeHeadless;
      headlessToggle.title = mustBeHeadless
        ? "CloakBrowser 当前固定要求 headless"
        : "";
    }

    function syncAccountSelect(items) {
      const select = document.getElementById("existingAccountSelect");
      const previous = state.selectedAccountId;
      select.innerHTML = [
        `<option value="">新建账号</option>`,
        ...items.map(item => `<option value="${item.id}">${escapeHtml(item.expected_subdomain)} | ${escapeHtml(item.email)}</option>`)
      ].join("");
      if (previous && items.some(item => item.id === previous)) {
        populateAccountForm(getAccountById(previous));
      } else if (!items.length) {
        resetAccountForm();
      }
    }

    function renderAccountRefreshJob(job) {
      state.refreshJob = job || null;
      const box = document.getElementById("accountJobBox");
      if (!job || !job.id) {
        box.textContent = "暂无刷新任务";
        return;
      }
      const summary = job.summary || {};
      const actualBrowser = summary.browser_provider_runtime || job.browser_provider || "-";
      const lines = [
        `任务: ${job.id}`,
        `状态: ${labelJobStatus(job.status || "-")}`,
        `模式: ${job.verify_only ? "verify-only" : (job.refresh_only ? "refresh-only" : "full-refresh")}`,
        `请求浏览器: ${labelBrowserProvider(job.browser_provider || "-")}`,
        `实际浏览器: ${labelBrowserProvider(actualBrowser)}`,
        `并发: ${job.max_concurrency || "-"}`,
        `开始: ${fmtTime(job.started_at)}`,
        `结束: ${fmtTime(job.finished_at)}`,
        `退出码: ${job.exit_code ?? "-"}`,
        `账号数: ${(job.account_ids || []).length}`,
        `成功导出: ${summary.success_count ?? "-"}`,
        `失败导出: ${summary.failure_count ?? "-"}`,
        `跳过: ${summary.skipped_count ?? "-"}`,
        `错误: ${summarizeText(job.error || "", 220) || "-"}`,
        "",
        ...(job.log_lines || []).slice(-24)
      ];
      box.textContent = lines.join("\\n");
    }

    function renderAccounts(items) {
      state.accounts = items || [];
      syncAccountSelect(state.accounts);
      const tbody = document.getElementById("accountRows");
      tbody.innerHTML = state.accounts.map(item => `
        <tr>
          <td><input class="account-check" type="checkbox" value="${escapeHtml(item.id)}"></td>
          <td>
            <strong>${escapeHtml(item.expected_subdomain)}</strong><br>
            <span class="muted tiny">${escapeHtml(item.email)}</span><br>
            <span class="muted tiny">${escapeHtml(item.password_masked || "")}</span><br>
            <span class="muted tiny">seed: ${escapeHtml(item.fingerprint_seed || "-")}</span>
          </td>
          <td>
            <div class="${item.enabled ? "good" : "warn"}">${item.enabled ? "enabled" : "disabled"}</div>
            <div class="${String(item.auth_state || "").includes("ready") ? "good" : (item.failure_count ? "bad" : "muted")}" title="${escapeHtml(item.auth_state || "unknown")}">${escapeHtml(labelAuthState(item.auth_state || "unknown"))}</div>
            <div class="tiny muted">provider: ${escapeHtml(labelBrowserProvider(item.browser_provider_runtime || item.browser_provider || ""))}</div>
            <div class="tiny muted">cooldown: ${fmtTime(item.cooldown_until)}</div>
          </td>
          <td>
            <div class="tiny">刷新 ${fmtTime(item.last_session_refresh_at)}</div>
            <div class="tiny muted">登录 ${fmtTime(item.last_login_success_at)}</div>
          </td>
          <td class="tiny ${item.last_error ? "bad" : "muted"}" title="${escapeHtml(item.last_error || "")}">${escapeHtml(summarizeAccountError(item))}</td>
          <td>
            <div class="actions">
              <button class="secondary" onclick="selectAccount('${escapeHtml(item.id)}')">编辑</button>
              <button class="secondary" onclick="refreshSingleAccount('${escapeHtml(item.id)}')">刷新</button>
            </div>
          </td>
        </tr>
      `).join("");
      const selectAll = document.getElementById("selectAllAccounts");
      selectAll.checked = false;
    }

    async function copyText(text, successMessage) {
      if (!text) {
        writeLog("复制失败：当前没有完整内容");
        return;
      }
      await navigator.clipboard.writeText(text);
      writeLog(successMessage);
      showToast(successMessage);
    }

    async function fetchAccountsAndJob() {
      const payload = await api("/admin/api/accounts");
      renderAccounts(payload.items || []);
      renderAccountRefreshJob(payload.refresh_job || null);
    }

    async function pollRefreshJobIfNeeded() {
      if (state.refreshPollTimer) {
        clearTimeout(state.refreshPollTimer);
        state.refreshPollTimer = null;
      }
      const job = state.refreshJob;
      if (!job || !["queued", "running"].includes(job.status)) return;
      state.refreshPollTimer = setTimeout(async () => {
        try {
          const latest = await api("/admin/api/accounts/refresh-status");
          renderAccountRefreshJob(latest);
          await fetchAccountsAndJob();
          if (["queued", "running"].includes(latest.status)) {
            await pollRefreshJobIfNeeded();
          } else {
            await refreshAll();
          }
        } catch (error) {
          writeLog(`刷新任务轮询失败: ${error.message}`);
        }
      }, 3000);
    }

    window.selectKey = function selectKey(keyId) {
      const item = getKeyById(keyId);
      if (!item) {
        writeLog(`未找到 key: ${keyId}`);
        return;
      }
      populateKeyForm(item);
      writeLog(`已载入 key: ${keyId}`);
    };

    window.copyKeyById = async function copyKeyById(keyId) {
      const item = getKeyById(keyId);
      if (!item) {
        writeLog(`复制失败：未找到 key ${keyId}`);
        return;
      }
      await copyText(item.key, `已复制完整 key: ${keyId}`);
    };

    window.selectAccount = function selectAccount(accountId) {
      const item = getAccountById(accountId);
      if (!item) {
        writeLog(`未找到账号: ${accountId}`);
        return;
      }
      populateAccountForm(item);
      writeLog(`已载入账号: ${accountId}`);
    };

    window.refreshSingleAccount = async function refreshSingleAccount(accountId) {
      document.querySelectorAll(".account-check").forEach((node) => {
        node.checked = node.value === accountId;
      });
      await startRefreshJob({ accountIds: [accountId], refreshAll: false, verifyOnly: false });
    };

    async function refreshAll() {
      const adminToken = localStorage.getItem("retool_admin_token") || "";
      if (!adminToken) {
        writeLog("请先输入 admin token，再加载管理数据");
        return;
      }
      const [overview, orgs, audit, keys, imports, settings, accountsPayload] = await Promise.all([
        api("/admin/api/overview"),
        api("/admin/api/orgs"),
        api("/admin/api/audit"),
        api("/admin/api/api-keys"),
        api("/admin/api/imports"),
        api("/admin/api/settings"),
        api("/admin/api/accounts"),
      ]);
      renderMetrics(overview);
      renderOrgs(orgs.items || []);
      renderAudit(audit.items || []);
      renderKeys(keys.items || []);
      renderImports(imports);
      renderSettings(settings);
      renderAccounts(accountsPayload.items || []);
      renderAccountRefreshJob(accountsPayload.refresh_job || null);
      await pollRefreshJobIfNeeded();
      writeLog("视图刷新完成");
    }

    async function toggleOrg(orgIdEncoded, enabled) {
      const orgId = decodeURIComponent(orgIdEncoded);
      await api(`/admin/api/orgs/${encodeURIComponent(orgId)}/enabled`, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      writeLog(`${enabled ? "启用" : "停用"} org: ${orgId}`);
      await refreshAll();
    }

    async function resetCooldown(orgIdEncoded) {
      const orgId = decodeURIComponent(orgIdEncoded);
      await api(`/admin/api/orgs/${encodeURIComponent(orgId)}/cooldown/reset`, {
        method: "POST",
        body: "{}",
      });
      writeLog(`重置 cooldown: ${orgId}`);
      await refreshAll();
    }

    async function saveAccount() {
      const current = getAccountById(state.selectedAccountId);
      const payload = await api("/admin/api/accounts", {
        method: "POST",
        body: JSON.stringify({
          id: document.getElementById("accountId").value.trim() || null,
          email: document.getElementById("accountEmail").value.trim() || null,
          password: document.getElementById("accountPassword").value.trim() || (current ? null : ""),
          expected_subdomain: document.getElementById("accountSubdomain").value.trim() || null,
          enabled: boolValueFromSelect("accountEnabled"),
          notes: document.getElementById("accountNotes").value.trim(),
          browser_provider: document.getElementById("accountBrowserProvider").value || null,
          fingerprint_seed: document.getElementById("accountFingerprintSeed").value.trim() || null
        }),
      });
      writeLog(`${payload.updated ? "更新" : "创建"} 账号: ${payload.id}`);
      showToast(`${payload.updated ? "已更新" : "已创建"} ${payload.id}`);
      document.getElementById("accountPassword").value = "";
      await refreshAll();
      selectAccount(payload.id);
    }

    async function deleteCurrentAccount() {
      const current = getAccountById(state.selectedAccountId);
      if (!current) {
        writeLog("请先选择一个已有账号");
        return;
      }
      const confirmed = window.confirm(`确认删除账号？\n\nID: ${current.id}\n邮箱: ${current.email}\n子域: ${current.expected_subdomain}\n\n删除后不会自动删除 orgs.json 中已存在的历史会话。`);
      if (!confirmed) {
        writeLog(`已取消删除账号: ${current.id}`);
        return;
      }
      await api("/admin/api/accounts/delete", {
        method: "POST",
        body: JSON.stringify({ id: current.id }),
      });
      writeLog(`已删除账号: ${current.id}`);
      showToast(`已删除 ${current.id}`);
      resetAccountForm();
      await refreshAll();
    }

    async function resetCurrentAccountFingerprint() {
      const current = getAccountById(state.selectedAccountId);
      if (!current) {
        writeLog("请先选择一个已有账号");
        return;
      }
      const confirmed = window.confirm(`确认重置账号浏览器指纹？\n\nID: ${current.id}\n子域: ${current.expected_subdomain}\n\n重置后下次刷新会切换到新的稳定浏览器身份。`);
      if (!confirmed) {
        writeLog(`已取消重置账号指纹: ${current.id}`);
        return;
      }
      const payload = await api("/admin/api/accounts/reset-fingerprint", {
        method: "POST",
        body: JSON.stringify({ account_ids: [current.id], refresh_all: false }),
      });
      writeLog(`已重置账号指纹: ${current.id}`);
      showToast(`已重置 ${payload.updated_count} 个账号指纹`);
      await refreshAll();
      selectAccount(current.id);
    }

    async function importAccounts(replace) {
      const content = document.getElementById("accountImportBox").value.trim();
      if (!content) {
        writeLog("批量导入失败：请输入账号内容");
        return;
      }
      const payload = await api("/admin/api/accounts/import", {
        method: "POST",
        body: JSON.stringify({ content, replace }),
      });
      writeLog(`${replace ? "覆盖" : "合并"}导入账号 ${payload.imported_count} 条，总数 ${payload.total_count}`);
      showToast(`已导入 ${payload.imported_count} 条账号`);
      document.getElementById("accountImportBox").value = "";
      await refreshAll();
    }

    function readRefreshOptions() {
      const modelsRaw = document.getElementById("refreshCheckModels").value.trim();
      syncHeadlessToggle();
      return {
        browserProvider: document.getElementById("refreshBrowserProvider").value || null,
        maxConcurrency: document.getElementById("refreshConcurrency").value.trim() || null,
        checkModels: modelsRaw ? modelsRaw.split(",").map(v => v.trim()).filter(Boolean) : null,
        refreshOnly: document.getElementById("refreshOnlyToggle").checked,
        ignoreCooldown: document.getElementById("ignoreCooldownToggle").checked,
        headless: document.getElementById("headlessToggle").checked,
      };
    }

    async function startRefreshJob({ accountIds, refreshAll, verifyOnly }) {
      const options = readRefreshOptions();
      const payload = await api("/admin/api/accounts/refresh", {
        method: "POST",
        body: JSON.stringify({
          account_ids: accountIds || [],
          refresh_all: Boolean(refreshAll),
          refresh_only: verifyOnly ? false : options.refreshOnly,
          verify_only: Boolean(verifyOnly),
          ignore_cooldown: options.ignoreCooldown,
          max_concurrency: options.maxConcurrency ? Number(options.maxConcurrency) : null,
          browser_provider: options.browserProvider,
          check_models: options.checkModels,
          headless: options.headless
        }),
      });
      renderAccountRefreshJob(payload);
      writeLog(`已启动账号刷新任务: ${payload.id}`);
      showToast(`任务已启动: ${payload.id}`);
      await pollRefreshJobIfNeeded();
    }

    async function saveKey() {
      const id = document.getElementById("apiKeyId").value.trim();
      const owner = document.getElementById("apiKeyOwner").value.trim();
      const key = document.getElementById("apiKeySecret").value.trim();
      const concurrencyRaw = document.getElementById("apiKeyConcurrency").value.trim();
      const template = keyTemplates[document.getElementById("apiKeyTemplate").value] || keyTemplates.inference;
      const concurrency = concurrencyRaw ? Number(concurrencyRaw) : null;
      const current = getKeyById(state.selectedKeyId);
      if (concurrencyRaw && (!Number.isFinite(concurrency) || concurrency < 1)) {
        writeLog("保存 key 失败：并发上限必须是大于 0 的数字");
        return;
      }
      const payload = await api("/admin/api/api-keys", {
        method: "POST",
        body: JSON.stringify({
          id: id || null,
          owner: owner || null,
          key: key || null,
          regenerate_key: false,
          concurrency_limit: concurrency,
          scopes: template.scopes,
          enabled: current ? current.enabled : true
        }),
      });
      renderKeyResult(payload);
      writeLog(`${payload.updated ? "更新" : "创建"} API key: ${payload.id}`);
      document.getElementById("apiKeyId").value = payload.id || "";
      document.getElementById("apiKeyCurrentSecret").value = payload.key || current?.key || "";
      document.getElementById("apiKeySecret").value = "";
      state.selectedKeyId = payload.id || "";
      showToast(`${payload.updated ? "已更新" : "已创建"} ${payload.id}`);
      await refreshAll();
    }

    async function toggleKeyEnabled() {
      const current = getKeyById(state.selectedKeyId);
      if (!current) {
        writeLog("请先从下拉列表或表格选择一个已有 key");
        return;
      }
      const payload = await api("/admin/api/api-keys", {
        method: "POST",
        body: JSON.stringify({
          id: current.id,
          owner: current.owner || null,
          key: null,
          regenerate_key: false,
          concurrency_limit: current.concurrency_limit,
          scopes: current.scopes,
          enabled: !current.enabled
        }),
      });
      renderKeyResult(payload);
      document.getElementById("apiKeyCurrentSecret").value = current.key || "";
      writeLog(`${current.enabled ? "停用" : "启用"} API key: ${current.id}`);
      showToast(`${current.enabled ? "已停用" : "已启用"} ${current.id}`);
      await refreshAll();
    }

    async function rotateKeySecret() {
      const current = getKeyById(state.selectedKeyId);
      if (!current) {
        writeLog("请先从下拉列表或表格选择一个已有 key");
        return;
      }
      const payload = await api("/admin/api/api-keys", {
        method: "POST",
        body: JSON.stringify({
          id: current.id,
          owner: current.owner || null,
          key: null,
          regenerate_key: true,
          concurrency_limit: current.concurrency_limit,
          scopes: current.scopes,
          enabled: current.enabled
        }),
      });
      renderKeyResult(payload);
      document.getElementById("apiKeyCurrentSecret").value = payload.key || "";
      writeLog(`已重置 secret: ${current.id}`);
      showToast(`已重置 ${current.id} 的 secret`);
      await refreshAll();
    }

    async function deleteCurrentKey() {
      const current = getKeyById(state.selectedKeyId);
      if (!current) {
        writeLog("请先从下拉列表或表格选择一个已有 key");
        return;
      }
      const confirmed = window.confirm(`确认删除 API key？\n\nID: ${current.id}\nOwner: ${current.owner || "-"}\n\n删除后不可恢复。`);
      if (!confirmed) {
        writeLog(`已取消删除 API key: ${current.id}`);
        return;
      }
      await api("/admin/api/api-keys/delete", {
        method: "POST",
        body: JSON.stringify({ id: current.id }),
      });
      writeLog(`已删除 API key: ${current.id}`);
      showToast(`已删除 ${current.id}`);
      resetKeyForm();
      await refreshAll();
    }

    async function createInferenceKeyBatch() {
      const owner = document.getElementById("batchOwner").value.trim();
      const count = Number(document.getElementById("batchCount").value.trim() || "0");
      if (!owner) {
        writeLog("批量生成失败：缺少 owner");
        return;
      }
      if (!Number.isFinite(count) || count < 1 || count > 100) {
        writeLog("批量生成失败：数量必须在 1 到 100 之间");
        return;
      }
      const payload = await api("/admin/api/api-keys/batch", {
        method: "POST",
        body: JSON.stringify({
          owner,
          count,
          scopes: ["inference"],
          enabled: true
        }),
      });
      renderBatchResult(payload);
      writeLog(`批量生成办公 key: ${owner} x ${payload.count}`);
      showToast(`已批量生成 ${payload.count} 个办公 key`);
      await refreshAll();
    }

    async function refreshHealth() {
      await api("/admin/api/health/refresh", { method: "POST", body: "{}" });
      writeLog("已触发健康刷新");
      await refreshAll();
    }

    async function importBundle(file) {
      const content = await file.text();
      await api("/admin/api/session-bundles/import", {
        method: "POST",
        body: JSON.stringify({
          filename: file.name,
          content,
          allow_expired: false
        }),
      });
      writeLog(`导入 bundle: ${file.name}`);
      await refreshAll();
    }

    document.getElementById("refreshBtn").addEventListener("click", () => refreshAll().catch((error) => writeLog(`刷新失败: ${error.message}`)));
    document.getElementById("refreshHealthBtn").addEventListener("click", () => refreshHealth().catch((error) => writeLog(`健康刷新失败: ${error.message}`)));
    document.getElementById("saveTokenBtn").addEventListener("click", () => {
      const token = document.getElementById("adminToken").value.trim();
      localStorage.setItem("retool_admin_token", token);
      writeLog("已保存 admin token");
      refreshAll().catch((error) => writeLog(`刷新失败: ${error.message}`));
    });
    document.getElementById("bundleInput").addEventListener("change", async (event) => {
      const [file] = event.target.files || [];
      if (!file) return;
      try {
        await importBundle(file);
      } catch (error) {
        writeLog(`导入失败: ${error.message}`);
      } finally {
        event.target.value = "";
      }
    });

    document.getElementById("saveKeyBtn").addEventListener("click", () => saveKey().catch((error) => writeLog(`保存 key 失败: ${error.message}`)));
    document.getElementById("createBatchBtn").addEventListener("click", () => createInferenceKeyBatch().catch((error) => writeLog(`批量生成失败: ${error.message}`)));
    document.getElementById("rotateKeyBtn").addEventListener("click", () => rotateKeySecret().catch((error) => writeLog(`重置 secret 失败: ${error.message}`)));
    document.getElementById("toggleKeyBtn").addEventListener("click", () => toggleKeyEnabled().catch((error) => writeLog(`切换 key 状态失败: ${error.message}`)));
    document.getElementById("copyCurrentKeyBtn").addEventListener("click", () => copyText(document.getElementById("apiKeyCurrentSecret").value, "已复制当前完整 key").catch((error) => writeLog(`复制失败: ${error.message}`)));
    document.getElementById("deleteKeyBtn").addEventListener("click", () => deleteCurrentKey().catch((error) => writeLog(`删除 key 失败: ${error.message}`)));
    document.getElementById("existingKeySelect").addEventListener("change", (event) => {
      const keyId = event.target.value;
      if (!keyId) {
        resetKeyForm();
        writeLog("已切换到新建 key 模式");
        return;
      }
      populateKeyForm(getKeyById(keyId));
      writeLog(`已选择 key: ${keyId}`);
    });
    document.getElementById("keyOwnerFilter").addEventListener("input", () => renderKeys(state.apiKeys));
    document.getElementById("keyScopeFilter").addEventListener("change", () => renderKeys(state.apiKeys));
    document.getElementById("resetKeyFormBtn").addEventListener("click", resetKeyForm);

    document.getElementById("saveAccountBtn").addEventListener("click", () => saveAccount().catch((error) => writeLog(`保存账号失败: ${error.message}`)));
    document.getElementById("deleteAccountBtn").addEventListener("click", () => deleteCurrentAccount().catch((error) => writeLog(`删除账号失败: ${error.message}`)));
    document.getElementById("resetFingerprintBtn").addEventListener("click", () => resetCurrentAccountFingerprint().catch((error) => writeLog(`重置账号指纹失败: ${error.message}`)));
    document.getElementById("mergeAccountsBtn").addEventListener("click", () => importAccounts(false).catch((error) => writeLog(`导入账号失败: ${error.message}`)));
    document.getElementById("replaceAccountsBtn").addEventListener("click", () => importAccounts(true).catch((error) => writeLog(`覆盖导入失败: ${error.message}`)));
    document.getElementById("existingAccountSelect").addEventListener("change", (event) => {
      const accountId = event.target.value;
      if (!accountId) {
        resetAccountForm();
        writeLog("已切换到新建账号模式");
        return;
      }
      populateAccountForm(getAccountById(accountId));
      writeLog(`已选择账号: ${accountId}`);
    });
    document.getElementById("resetAccountFormBtn").addEventListener("click", resetAccountForm);
    document.getElementById("refreshBrowserProvider").addEventListener("change", syncHeadlessToggle);
    document.getElementById("selectAllAccounts").addEventListener("change", (event) => {
      document.querySelectorAll(".account-check").forEach((node) => { node.checked = event.target.checked; });
    });
    document.getElementById("refreshSelectedAccountsBtn").addEventListener("click", () => {
      const accountIds = checkedAccountIds();
      if (!accountIds.length) {
        writeLog("请先勾选至少一个账号");
        return;
      }
      startRefreshJob({ accountIds, refreshAll: false, verifyOnly: false }).catch((error) => writeLog(`启动刷新失败: ${error.message}`));
    });
    document.getElementById("refreshAllAccountsBtn").addEventListener("click", () => {
      startRefreshJob({ accountIds: [], refreshAll: true, verifyOnly: false }).catch((error) => writeLog(`启动全量刷新失败: ${error.message}`));
    });
    document.getElementById("verifyAccountsBtn").addEventListener("click", () => {
      const accountIds = checkedAccountIds();
      const refreshAll = !accountIds.length;
      startRefreshJob({ accountIds, refreshAll, verifyOnly: true }).catch((error) => writeLog(`启动校验失败: ${error.message}`));
    });

    document.getElementById("adminToken").value = localStorage.getItem("retool_admin_token") || "";
    resetKeyForm();
    resetAccountForm();
    syncHeadlessToggle();
    refreshAll().catch((error) => writeLog(`初始化失败: ${error.message}`));
  </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    try:
        ensure_example_files()
    except OSError:
        pass

    print("Gateway endpoints:")
    print("  GET  /v1/models")
    print("  GET  /models")
    print("  POST /v1/chat/completions")
    print("  POST /v1/responses")
    print("  POST /v1/messages")
    print("  GET  /healthz")
    print("  GET  /admin")
    print(f"Conversation header: {gateway_config.conversation_header}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
