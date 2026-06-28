import asyncio
from typing import Any, Dict, List

import httpx

from models import ModelAliasConfig, OrgConfig


RETOOL_TRUNCATION_MARKER = "\n...[truncated for Retool compatibility]...\n"
RETOOL_MAX_TOTAL_MESSAGE_CHARS = 5500
RETOOL_MAX_SYSTEM_MESSAGE_CHARS = 1200
RETOOL_MAX_MESSAGE_CHARS = 2200
RETOOL_MAX_LAST_USER_MESSAGE_CHARS = 3000


class RetoolApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    def is_prompt_too_long(self) -> bool:
        message = str(self).lower()
        return self.status_code in {400, 403} and (
            "message is too long" in message or "configure your own api key to continue" in message
        )


def build_headers(org: OrgConfig) -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
        ),
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Content-Type": "application/json",
        "x-xsrf-token": org.x_xsrf_token,
        "Cookie": f"accessToken={org.access_token}",
    }


def _truncate_middle(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(RETOOL_TRUNCATION_MARKER) + 16:
        return text[:limit]
    remaining = limit - len(RETOOL_TRUNCATION_MARKER)
    head = remaining // 2
    tail = remaining - head
    return f"{text[:head]}{RETOOL_TRUNCATION_MARKER}{text[-tail:]}"


def _role_label(message: Any) -> str:
    role = getattr(message, "role", "")
    if role == "system":
        return "System"
    if role == "user":
        return "Human"
    return "Assistant"


def _message_char_limit(role_label: str, is_last_message: bool) -> int:
    if role_label == "System":
        return RETOOL_MAX_SYSTEM_MESSAGE_CHARS
    if role_label == "Human" and is_last_message:
        return RETOOL_MAX_LAST_USER_MESSAGE_CHARS
    return RETOOL_MAX_MESSAGE_CHARS


def format_messages_for_retool(messages: List[Any]) -> str:
    formatted_parts: list[str] = []
    total_messages = len(messages)

    for index, msg in enumerate(messages):
        role_label = _role_label(msg)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        clipped_content = _truncate_middle(
            content,
            _message_char_limit(role_label, index == total_messages - 1),
        )
        formatted_parts.append(f"{role_label}: {clipped_content}")

    if messages and getattr(messages[-1], "role", "") == "assistant":
        formatted_parts.append("Human: ")

    formatted = "\n\n".join(formatted_parts).lstrip()
    if len(formatted) <= RETOOL_MAX_TOTAL_MESSAGE_CHARS:
        return formatted

    preserved_tail_budget = RETOOL_MAX_TOTAL_MESSAGE_CHARS - len(RETOOL_TRUNCATION_MARKER)
    if preserved_tail_budget <= 0:
        return formatted[-RETOOL_MAX_TOTAL_MESSAGE_CHARS:]
    return f"{RETOOL_TRUNCATION_MARKER}{formatted[-preserved_tail_budget:]}"


class RetoolClient:
    def __init__(self, timeout_seconds: int, poll_interval_seconds: float, poll_max_attempts: int, timezone: str):
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_max_attempts = poll_max_attempts
        self.timezone = timezone

    async def _request(self, method: str, org: OrgConfig, path: str, **kwargs):
        url = f"https://{org.domain_name}{path}"
        headers = build_headers(org)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise RetoolApiError(
                f"Retool API {method} {path} failed for {org.domain_name}: {response.status_code} {response.text[:500]}",
                status_code=response.status_code,
            )
        return response.json()

    async def list_agents(self, org: OrgConfig) -> List[Dict[str, Any]]:
        data = await self._request("GET", org, "/api/agents")
        return data.get("agents", [])

    async def resolve_agent(self, org: OrgConfig, model_alias: ModelAliasConfig) -> Dict[str, Any]:
        agents = await self.list_agents(org)
        for agent in agents:
            if model_alias.agent_name and agent.get("name") != model_alias.agent_name:
                continue
            if model_alias.model_name and agent.get("data", {}).get("model") != model_alias.model_name:
                continue
            return agent
        raise RetoolApiError(
            f"No matching agent found for model alias '{model_alias.id}' in org {org.domain_name}"
        )

    async def create_thread(self, org: OrgConfig, agent_id: str, conversation_id: str) -> str:
        data = await self._request(
            "POST",
            org,
            f"/api/agents/{agent_id}/threads",
            json={"name": conversation_id, "timezone": self.timezone},
        )
        return data["id"]

    async def send_message(self, org: OrgConfig, agent_id: str, thread_id: str, message: str) -> str:
        data = await self._request(
            "POST",
            org,
            f"/api/agents/{agent_id}/threads/{thread_id}/messages",
            json={"type": "text", "text": message, "timezone": self.timezone},
        )
        run_id = data.get("content", {}).get("runId")
        if not run_id:
            raise RetoolApiError(f"Missing runId from message response for {org.domain_name}")
        return run_id

    async def poll_message(self, org: OrgConfig, agent_id: str, run_id: str) -> str:
        last_log_uuid = "00000000-0000-7000-8000-000000000000"
        for _ in range(self.poll_max_attempts):
            data = await self._request(
                "GET",
                org,
                f"/api/agents/{agent_id}/logs/{run_id}?startAfterUUID={last_log_uuid}&limit=100",
            )
            pagination = data.get("pagination") or {}
            if pagination.get("lastLogUUID"):
                last_log_uuid = pagination["lastLogUUID"]

            status = data.get("status")
            trace = data.get("trace") or []
            if status == "COMPLETED":
                for span in reversed(trace):
                    content = (
                        span.get("data", {})
                        .get("data", {})
                        .get("content")
                    )
                    if content:
                        return content
                raise RetoolApiError(
                    f"Completed run missing final content for {org.domain_name}"
                )
            if status == "FAILED":
                raise RetoolApiError(f"Retool run failed for {org.domain_name}")

            await asyncio.sleep(self.poll_interval_seconds)

        raise RetoolApiError(f"Timed out waiting for Retool run in {org.domain_name}")
