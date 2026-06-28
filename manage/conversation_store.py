import time
from typing import Optional

from models import ChatMessage, ConversationMapping, ConversationState
from state_store import JsonStateStore


class ConversationStore:
    def __init__(self, store: JsonStateStore, ttl_seconds: int):
        self._store = store
        self._ttl_seconds = ttl_seconds

    def get(self, conversation_id: str) -> Optional[ConversationMapping]:
        state = self._store.load()
        mapping = state.conversations.get(conversation_id)
        if not mapping:
            return None
        if time.time() - mapping.updated_at > self._ttl_seconds:
            del state.conversations[conversation_id]
            self._store.save(state)
            return None
        return mapping

    def upsert(
        self,
        conversation_id: str,
        model_id: str,
        org_id: str,
        domain_name: str,
        agent_id: str,
        thread_id: str,
        api_key_id: Optional[str],
        last_request_messages: list[ChatMessage],
        last_assistant_message: Optional[ChatMessage],
    ) -> ConversationMapping:
        state = self._store.load()
        existing = state.conversations.get(conversation_id)
        now = time.time()
        mapping = ConversationMapping(
            conversation_id=conversation_id,
            model_id=model_id,
            org_id=org_id,
            domain_name=domain_name,
            agent_id=agent_id,
            thread_id=thread_id,
            api_key_id=api_key_id,
            last_request_messages=last_request_messages,
            last_assistant_message=last_assistant_message,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        state.conversations[conversation_id] = mapping
        self._store.save(state)
        return mapping

    def delete(self, conversation_id: str) -> None:
        state = self._store.load()
        if conversation_id in state.conversations:
            del state.conversations[conversation_id]
            self._store.save(state)

    def all(self) -> ConversationState:
        return self._store.load()

