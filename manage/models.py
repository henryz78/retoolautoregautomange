import time
import uuid
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[ChatMessage]
    stream: bool = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    user: Optional[str] = None
    conversation_id: Optional[str] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str
    name: Optional[str] = None


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


class ChatCompletionChoice(BaseModel):
    message: ChatMessage
    index: int = 0
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int] = Field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )


class StreamChoice(BaseModel):
    delta: Dict[str, Any] = Field(default_factory=dict)
    index: int = 0
    finish_reason: Optional[str] = None


class StreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[StreamChoice]


class AnthropicRequestMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[AnthropicRequestMessage]
    max_tokens: Optional[int] = None
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    stop_sequences: Optional[List[str]] = None


class AnthropicTextBlock(BaseModel):
    type: str = "text"
    text: str


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicMessagesResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    type: str = "message"
    role: str = "assistant"
    model: str
    content: List[AnthropicTextBlock]
    stop_reason: str = "end_turn"
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


class ResponsesInputMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: Union[str, List[ResponsesInputMessage], List[Dict[str, Any]]]
    instructions: Optional[str] = None
    stream: bool = False
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    user: Optional[str] = None
    conversation_id: Optional[str] = None


class ResponsesOutputText(BaseModel):
    type: str = "output_text"
    text: str
    annotations: List[Dict[str, Any]] = Field(default_factory=list)


class ResponsesOutputMessage(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    type: str = "message"
    role: str = "assistant"
    status: str = "completed"
    content: List[ResponsesOutputText]


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ResponsesResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex}")
    object: str = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: str = "completed"
    error: Optional[Dict[str, Any]] = None
    incomplete_details: Optional[Dict[str, Any]] = None
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    model: str
    output: List[ResponsesOutputMessage]
    parallel_tool_calls: bool = True
    temperature: Optional[float] = None
    tool_choice: str = "auto"
    tools: List[Dict[str, Any]] = Field(default_factory=list)
    top_p: Optional[float] = None
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)
    user: Optional[str] = None


class ModelAliasConfig(BaseModel):
    id: str
    owned_by: str = "openai"
    agent_name: Optional[str] = None
    model_name: Optional[str] = None
    display_name: Optional[str] = None

    @model_validator(mode="after")
    def validate_selector(self):
        if not self.agent_name and not self.model_name:
            raise ValueError("Each model alias must define agent_name or model_name")
        return self


class OrgConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[str] = None
    domain_name: str
    x_xsrf_token: str = Field(alias="x_xsrf_token")
    access_token: str = Field(alias="accessToken")
    enabled: bool = True
    source_account_id: Optional[str] = None
    source_email: Optional[str] = None
    refreshed_at: Optional[int] = None
    expires_at: Optional[int] = None
    verified_models: List[str] = Field(default_factory=list)
    auth_state: str = "ready"
    last_error: str = ""
    bundle_version: Optional[str] = None
    bundle_generated_at: Optional[int] = None
    bundle_expires_at: Optional[int] = None
    bundle_generated_by: Optional[Dict[str, Any]] = None

    @property
    def resolved_id(self) -> str:
        return self.id or self.domain_name

    def is_expired(self, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        current_time = int(now if now is not None else time.time())
        return self.expires_at <= current_time

    def is_auth_ready(self) -> bool:
        return str(self.auth_state or "").strip().lower() in {"", "ready"}


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation_header: str = "X-Conversation-ID"
    timezone: str = "Asia/Shanghai"
    orgs_file: Optional[str] = None
    allow_empty_org_pool: bool = False
    request_timeout_seconds: int = 120
    poll_interval_seconds: float = 1.0
    poll_max_attempts: int = 300
    mapping_ttl_seconds: int = 7 * 24 * 60 * 60
    health_cooldown_seconds: int = 300
    admin_warning_days: int = 2
    audit_history_limit: int = 500
    health_refresh_interval_seconds: int = 300
    model_aliases: List[ModelAliasConfig] = Field(default_factory=list, alias="models")
    orgs: List[OrgConfig] = Field(default_factory=list)


class ManagedAccountConfig(BaseModel):
    id: str
    email: str
    password: str
    expected_subdomain: str
    enabled: bool = True
    notes: str = ""
    browser_provider: Optional[str] = None
    fingerprint_seed: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ApiKeyConfig(BaseModel):
    id: str
    key: str
    enabled: bool = True
    owner: Optional[str] = None
    concurrency_limit: Optional[int] = None
    scopes: Optional[List[str]] = None

    def resolved_scopes(self) -> List[str]:
        if self.scopes:
            return [scope.strip().lower() for scope in self.scopes if scope and scope.strip()]
        return ["inference", "admin"]

    def has_scope(self, scope: str) -> bool:
        return scope.strip().lower() in self.resolved_scopes()


class ApiKeyRuntimeState(BaseModel):
    key_id: str
    active_requests: int = 0
    total_requests: int = 0
    success_requests: int = 0
    failed_requests: int = 0
    last_used_at: Optional[float] = None


class ConversationMapping(BaseModel):
    conversation_id: str
    model_id: str
    org_id: str
    domain_name: str
    agent_id: str
    thread_id: str
    api_key_id: Optional[str] = None
    last_request_messages: List[ChatMessage] = Field(default_factory=list)
    last_assistant_message: Optional[ChatMessage] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class HealthRecord(BaseModel):
    org_id: str
    domain_name: str
    last_used_at: float = 0
    last_success_at: float = 0
    last_failure_at: float = 0
    failure_count: int = 0
    cooldown_until: float = 0
    auth_failed: bool = False
    last_error: Optional[str] = None
    discovered_agents: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    agent_cache_refreshed_at: float = 0


class HealthState(BaseModel):
    orgs: Dict[str, HealthRecord] = Field(default_factory=dict)


class ConversationState(BaseModel):
    conversations: Dict[str, ConversationMapping] = Field(default_factory=dict)


class AuditEntry(BaseModel):
    request_id: str = Field(default_factory=lambda: f"req-{uuid.uuid4().hex}")
    happened_at: float = Field(default_factory=time.time)
    api_key_id: Optional[str] = None
    api_key_owner: Optional[str] = None
    conversation_id: Optional[str] = None
    model_id: str
    org_id: Optional[str] = None
    domain_name: Optional[str] = None
    success: bool
    duration_ms: int = 0
    attempt_count: int = 1
    error: Optional[str] = None


class AuditState(BaseModel):
    entries: List[AuditEntry] = Field(default_factory=list)


class ToggleEnabledRequest(BaseModel):
    enabled: bool


class ApiKeyUpsertRequest(BaseModel):
    id: Optional[str] = None
    key: Optional[str] = None
    regenerate_key: bool = False
    enabled: bool = True
    owner: Optional[str] = None
    concurrency_limit: Optional[int] = None
    scopes: Optional[List[str]] = None


class ApiKeyDeleteRequest(BaseModel):
    id: str


class ApiKeyBatchCreateRequest(BaseModel):
    owner: str
    count: int = 1
    concurrency_limit: Optional[int] = None
    scopes: Optional[List[str]] = None
    enabled: bool = True


class BundleImportRequest(BaseModel):
    filename: str
    content: str
    allow_expired: bool = False


class ManagedAccountUpsertRequest(BaseModel):
    id: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    expected_subdomain: Optional[str] = None
    enabled: bool = True
    notes: str = ""
    browser_provider: Optional[str] = None
    fingerprint_seed: Optional[str] = None
    regenerate_fingerprint: bool = False


class ManagedAccountDeleteRequest(BaseModel):
    id: str


class ManagedAccountImportRequest(BaseModel):
    content: str
    replace: bool = False


class ManagedAccountFingerprintResetRequest(BaseModel):
    account_ids: List[str] = Field(default_factory=list)
    refresh_all: bool = False


class AccountRefreshRequest(BaseModel):
    account_ids: List[str] = Field(default_factory=list)
    refresh_all: bool = False
    refresh_only: bool = True
    verify_only: bool = False
    ignore_cooldown: bool = False
    max_concurrency: Optional[int] = None
    browser_provider: Optional[str] = None
    check_models: Optional[List[str]] = None
    headless: bool = False
