import json
import re
import time
import uuid
from typing import Any, Optional

from fastapi import HTTPException

from audit_store import AuditStore
from conversation_store import ConversationStore
from models import (
    ApiKeyConfig,
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicTextBlock,
    AnthropicUsage,
    AuditEntry,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ModelAliasConfig,
    ModelInfo,
    ModelList,
    ResponsesInputMessage,
    ResponsesOutputMessage,
    ResponsesOutputText,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsage,
    StreamChoice,
    StreamResponse,
)
from org_pool import OrgPool, OrgPoolError
from retool_client import RetoolApiError, RetoolClient, format_messages_for_retool


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TRAILING_BRACKET_SUFFIX_RE = re.compile(r"(?:\[[^\[\]]+\])+$")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def debug_log(enabled: bool, message: str):
    if enabled:
        print(f"[DEBUG] {message}")


class GatewayService:
    def __init__(
        self,
        model_aliases: list[ModelAliasConfig],
        org_pool: OrgPool,
        conversation_store: ConversationStore,
        retool_client: RetoolClient,
        audit_store: AuditStore,
        debug_mode: bool = False,
    ):
        self.model_aliases = {alias.id: alias for alias in model_aliases}
        self.model_catalog = self._build_model_catalog(model_aliases)
        self.normalized_model_alias_ids = self._build_normalized_model_alias_ids(model_aliases)
        self.anthropic_alias_ids = [
            alias.id for alias in model_aliases if str(alias.owned_by or "").strip().lower() == "anthropic"
        ]
        self.org_pool = org_pool
        self.conversation_store = conversation_store
        self.retool_client = retool_client
        self.audit_store = audit_store
        self.debug_mode = debug_mode

    async def startup(self) -> None:
        self.org_pool.reload_from_file()
        await self.org_pool.refresh_agents(list(self.model_aliases.values()))

    def get_models_list_response(self) -> ModelList:
        return ModelList(
            data=[
                ModelInfo(
                    id=model_id,
                    name=alias.display_name or alias.id,
                    created=int(time.time()),
                    owned_by=alias.owned_by,
                )
                for model_id, alias in self.model_catalog.items()
            ]
        )

    @staticmethod
    def _normalize_model_id(raw_model: str) -> str:
        if not raw_model:
            return ""
        normalized = ANSI_ESCAPE_RE.sub("", str(raw_model)).strip().lower()
        normalized = TRAILING_BRACKET_SUFFIX_RE.sub("", normalized).strip()
        return NON_ALNUM_RE.sub("-", normalized).strip("-")

    @staticmethod
    def _looks_like_anthropic_model(normalized_model: str) -> bool:
        return any(token in normalized_model for token in ("claude", "sonnet", "opus", "haiku"))

    def _build_model_catalog(self, model_aliases: list[ModelAliasConfig]) -> dict[str, ModelAliasConfig]:
        model_catalog: dict[str, ModelAliasConfig] = {}
        for alias in model_aliases:
            model_catalog[alias.id] = alias
            if str(alias.owned_by or "").strip().lower() == "anthropic":
                model_catalog.setdefault(f"{alias.id}[1m]", alias)
        return model_catalog

    def _build_normalized_model_alias_ids(self, model_aliases: list[ModelAliasConfig]) -> dict[str, str]:
        normalized_ids: dict[str, str] = {}
        for alias in model_aliases:
            candidates = [alias.id]
            if alias.model_name:
                candidates.append(alias.model_name)
            if alias.display_name:
                candidates.append(alias.display_name)
            if str(alias.owned_by or "").strip().lower() == "anthropic":
                candidates.append(f"{alias.id}[1m]")
            for candidate in candidates:
                normalized_candidate = self._normalize_model_id(candidate)
                if normalized_candidate:
                    normalized_ids.setdefault(normalized_candidate, alias.id)
        return normalized_ids

    def _resolve_model_alias(self, requested_model: str) -> tuple[ModelAliasConfig, str]:
        normalized_requested_model = self._normalize_model_id(requested_model)
        resolved_model_id = self.normalized_model_alias_ids.get(normalized_requested_model)
        if resolved_model_id:
            return self.model_aliases[resolved_model_id], resolved_model_id

        if self._looks_like_anthropic_model(normalized_requested_model) and len(self.anthropic_alias_ids) == 1:
            fallback_model_id = self.anthropic_alias_ids[0]
            return self.model_aliases[fallback_model_id], fallback_model_id

        raise HTTPException(status_code=404, detail=f"Model '{requested_model}' not found.")

    def resolve_conversation_id(self, request: ChatCompletionRequest, header_value: Optional[str]) -> str:
        conversation_id = header_value or request.conversation_id
        if not conversation_id:
            import hashlib
            first_msg_content = ""
            if request.messages:
                first_msg_content = str(request.messages[0].content or "")
            h = hashlib.md5(first_msg_content.encode("utf-8", errors="ignore")).hexdigest()
            conversation_id = f"hash-{h}"
        return conversation_id

    @staticmethod
    def _same_message(left: ChatMessage, right: ChatMessage) -> bool:
        return left.role == right.role and left.content == right.content

    def _resolve_messages_for_thread(
        self,
        request: ChatCompletionRequest,
        mapping,
    ) -> tuple[list[ChatMessage], bool]:
        if not mapping:
            return request.messages, False

        expected_prefix = list(mapping.last_request_messages)
        if mapping.last_assistant_message:
            expected_prefix.append(mapping.last_assistant_message)

        if len(request.messages) < len(expected_prefix):
            return request.messages, False

        for index, existing in enumerate(expected_prefix):
            if not self._same_message(existing, request.messages[index]):
                return request.messages, False

        delta_messages = request.messages[len(expected_prefix):]
        if not delta_messages:
            return request.messages, False
        return delta_messages, True

    @staticmethod
    def _flatten_anthropic_content(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            if text_parts:
                return "\n".join(text_parts)
        return str(content)

    def convert_anthropic_messages_request(
        self,
        request: AnthropicMessagesRequest,
        conversation_id: str,
    ) -> ChatCompletionRequest:
        messages: list[ChatMessage] = []

        if isinstance(request.system, str) and request.system.strip():
            messages.append(ChatMessage(role="system", content=request.system))
        elif isinstance(request.system, list):
            system_text = self._flatten_anthropic_content(request.system)
            if system_text.strip():
                messages.append(ChatMessage(role="system", content=system_text))

        for message in request.messages:
            messages.append(
                ChatMessage(
                    role=message.role,
                    content=self._flatten_anthropic_content(message.content),
                )
            )

        return ChatCompletionRequest(
            model=request.model,
            messages=messages,
            stream=False,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            top_p=request.top_p,
            conversation_id=conversation_id,
        )

    def convert_responses_request(
        self,
        request: ResponsesRequest,
        conversation_id: str,
    ) -> ChatCompletionRequest:
        messages: list[ChatMessage] = []

        if request.instructions and request.instructions.strip():
            messages.append(ChatMessage(role="system", content=request.instructions))

        if isinstance(request.input, str):
            messages.append(ChatMessage(role="user", content=request.input))
        else:
            for item in request.input:
                if isinstance(item, ResponsesInputMessage):
                    role = item.role
                    content = self._flatten_anthropic_content(item.content)
                elif isinstance(item, dict):
                    role = str(item.get("role") or "user")
                    content = self._flatten_anthropic_content(item.get("content", ""))
                else:
                    continue
                messages.append(ChatMessage(role=role, content=content))

        return ChatCompletionRequest(
            model=request.model,
            messages=messages,
            stream=False,
            temperature=request.temperature,
            max_tokens=request.max_output_tokens,
            top_p=request.top_p,
            user=request.user,
            conversation_id=conversation_id,
        )

    @staticmethod
    def build_anthropic_messages_response(
        model_id: str,
        content: str,
    ) -> AnthropicMessagesResponse:
        return AnthropicMessagesResponse(
            model=model_id,
            content=[AnthropicTextBlock(text=content)],
            usage=AnthropicUsage(),
        )

    @staticmethod
    def build_responses_response(
        model_id: str,
        content: str,
        request: ResponsesRequest,
    ) -> ResponsesResponse:
        return ResponsesResponse(
            instructions=request.instructions,
            max_output_tokens=request.max_output_tokens,
            model=model_id,
            output=[
                ResponsesOutputMessage(
                    content=[ResponsesOutputText(text=content)],
                )
            ],
            temperature=request.temperature,
            top_p=request.top_p,
            usage=ResponsesUsage(),
            user=request.user,
        )

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key: ApiKeyConfig,
        conversation_id: str,
    ) -> ChatCompletionResponse | str:
        requested_model = request.model
        model_alias, resolved_model_id = self._resolve_model_alias(requested_model)
        request.model = resolved_model_id
        if not request.messages:
            raise HTTPException(status_code=400, detail="No messages provided.")

        mapping = self.conversation_store.get(conversation_id)
        preferred_org_id = mapping.org_id if mapping and mapping.model_id == request.model else None
        excluded_org_ids: set[str] = set()
        last_error: Optional[Exception] = None
        request_started_at = time.time()
        attempt_count = 0

        if requested_model != resolved_model_id:
            debug_log(
                self.debug_mode,
                f"Conversation {conversation_id} remapped model {requested_model!r} -> {resolved_model_id!r}",
            )
        debug_log(self.debug_mode, f"Conversation {conversation_id} using model {request.model}")

        while len(excluded_org_ids) < len(self.org_pool.orgs):
            try:
                org = self.org_pool.choose_org(model_alias, preferred_org_id=preferred_org_id, excluded_org_ids=excluded_org_ids)
            except OrgPoolError as exc:
                self._append_audit(
                    api_key=api_key,
                    conversation_id=conversation_id,
                    model_id=request.model,
                    success=False,
                    duration_ms=int((time.time() - request_started_at) * 1000),
                    attempt_count=max(attempt_count, 1),
                    error=str(exc),
                )
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            try:
                attempt_count += 1
                agent = await self.retool_client.resolve_agent(org, model_alias)
                agent_id = agent["id"]
                messages_to_send, can_reuse_thread = self._resolve_messages_for_thread(request, mapping)
                thread_id = (
                    mapping.thread_id
                    if mapping
                    and can_reuse_thread
                    and mapping.org_id == org.resolved_id
                    and mapping.agent_id == agent_id
                    else ""
                )
                if not thread_id:
                    thread_id = await self.retool_client.create_thread(org, agent_id, conversation_id)
                    messages_to_send = request.messages

                formatted_message = format_messages_for_retool(messages_to_send)
                run_id = await self.retool_client.send_message(org, agent_id, thread_id, formatted_message)
                content = await self.retool_client.poll_message(org, agent_id, run_id)
                assistant_message = ChatMessage(role="assistant", content=content)
                self.conversation_store.upsert(
                    conversation_id=conversation_id,
                    model_id=request.model,
                    org_id=org.resolved_id,
                    domain_name=org.domain_name,
                    agent_id=agent_id,
                    thread_id=thread_id,
                    api_key_id=api_key.id,
                    last_request_messages=request.messages,
                    last_assistant_message=assistant_message,
                )
                self.org_pool.mark_success(org.resolved_id)
                self._append_audit(
                    api_key=api_key,
                    conversation_id=conversation_id,
                    model_id=request.model,
                    org_id=org.resolved_id,
                    domain_name=org.domain_name,
                    success=True,
                    duration_ms=int((time.time() - request_started_at) * 1000),
                    attempt_count=attempt_count,
                )
                debug_log(self.debug_mode, f"Conversation {conversation_id} served by {org.domain_name}")
                if request.stream:
                    return content
                return ChatCompletionResponse(
                    model=request.model,
                    choices=[
                        ChatCompletionChoice(
                            message=assistant_message
                        )
                    ],
                )
            except RetoolApiError as exc:
                last_error = exc
                if exc.is_prompt_too_long():
                    self._append_audit(
                        api_key=api_key,
                        conversation_id=conversation_id,
                        model_id=request.model,
                        org_id=org.resolved_id,
                        domain_name=org.domain_name,
                        success=False,
                        duration_ms=int((time.time() - request_started_at) * 1000),
                        attempt_count=attempt_count,
                        error=str(exc),
                    )
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Retool rejected the prompt as too long for the pooled built-in API key. "
                            "Reduce prompt size and retry."
                        ),
                    ) from exc

                auth_failed = exc.status_code in {401, 403}
                self.org_pool.mark_failure(org.resolved_id, str(exc), auth_failed=auth_failed)
                excluded_org_ids.add(org.resolved_id)
                preferred_org_id = None
                if mapping and mapping.org_id == org.resolved_id:
                    self.conversation_store.delete(conversation_id)
                    mapping = None

        self._append_audit(
            api_key=api_key,
            conversation_id=conversation_id,
            model_id=request.model,
            success=False,
            duration_ms=int((time.time() - request_started_at) * 1000),
            attempt_count=max(attempt_count, 1),
            error=str(last_error or "All Retool org attempts failed."),
        )
        raise HTTPException(
            status_code=503,
            detail=str(last_error or "All Retool org attempts failed."),
        )

    def _append_audit(
        self,
        *,
        api_key: ApiKeyConfig,
        conversation_id: str,
        model_id: str,
        success: bool,
        duration_ms: int,
        attempt_count: int,
        org_id: Optional[str] = None,
        domain_name: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.audit_store.append(
            AuditEntry(
                api_key_id=api_key.id,
                api_key_owner=api_key.owner,
                conversation_id=conversation_id,
                model_id=model_id,
                org_id=org_id,
                domain_name=domain_name,
                success=success,
                duration_ms=duration_ms,
                attempt_count=attempt_count,
                error=error,
            )
        )


async def stream_text_response(full_message: str, model_id: str):
    stream_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())
    yield (
        f"data: {StreamResponse(id=stream_id, created=created_time, model=model_id, choices=[StreamChoice(delta={'role': 'assistant'})]).model_dump_json()}\n\n"
    )

    chunk_size = 5
    for i in range(0, len(full_message), chunk_size):
        chunk = full_message[i:i + chunk_size]
        payload = StreamResponse(
            id=stream_id,
            created=created_time,
            model=model_id,
            choices=[StreamChoice(delta={"content": chunk})],
        )
        yield f"data: {payload.model_dump_json()}\n\n"

    final_payload = StreamResponse(
        id=stream_id,
        created=created_time,
        model=model_id,
        choices=[StreamChoice(delta={}, finish_reason="stop")],
    )
    yield f"data: {final_payload.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


def _responses_sse_event(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_responses_api_response(
    *,
    model_id: str,
    content: str,
    request: ResponsesRequest,
):
    response_payload = GatewayService.build_responses_response(model_id=model_id, content=content, request=request)
    response_id = response_payload.id
    output_message = response_payload.output[0]
    item_id = output_message.id
    event_id = lambda: f"event_{uuid.uuid4().hex}"

    yield _responses_sse_event(
        "response.created",
        {
            "type": "response.created",
            "event_id": event_id(),
            "response": response_payload.model_dump(mode="json"),
        },
    )

    yield _responses_sse_event(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "event_id": event_id(),
            "response_id": response_id,
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
    )

    yield _responses_sse_event(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "event_id": event_id(),
            "response_id": response_id,
            "output_index": 0,
            "item_id": item_id,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "",
                "annotations": [],
            },
        },
    )

    chunk_size = 64
    for i in range(0, len(content), chunk_size):
        chunk = content[i:i + chunk_size]
        yield _responses_sse_event(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "event_id": event_id(),
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            },
        )

    yield _responses_sse_event(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "event_id": event_id(),
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": content,
        },
    )

    yield _responses_sse_event(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "event_id": event_id(),
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": output_message.content[0].model_dump(mode="json"),
        },
    )

    yield _responses_sse_event(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "event_id": event_id(),
            "response_id": response_id,
            "output_index": 0,
            "item": output_message.model_dump(mode="json"),
        },
    )

    yield _responses_sse_event(
        "response.completed",
        {
            "type": "response.completed",
            "event_id": event_id(),
            "response": response_payload.model_dump(mode="json"),
        },
    )


async def error_stream_generator(error_detail: str, status_code: int):
    yield (
        f"data: {json.dumps({'error': {'message': error_detail, 'type': 'retool_api_error', 'code': status_code}})}\n\n"
    )
    yield "data: [DONE]\n\n"
