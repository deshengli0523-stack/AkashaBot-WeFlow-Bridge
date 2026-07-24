from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Iterable
from typing import Any

from .context_builder import ContextBuilder, estimate_tokens
from .models import ContactRecord, QwenResult, QwenSessionRecord
from .qwen_client import QwenClient
from .store import MemoryRevisionChanged, MemoryStore


class StaleToolContinuation(RuntimeError):
    """A tool result from an older request must not dirty a newer session."""

    akasha_session_safe = True


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _function_output_ids(
    input_items: list[dict[str, Any]] | None,
) -> tuple[str, ...]:
    if input_items is None:
        return ()
    ids: list[str] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            return ()
        call_id = str(item.get("call_id") or "").strip()
        if not call_id or call_id in ids:
            return ()
        ids.append(call_id)
    return tuple(ids)


class QwenSessionManager:
    def __init__(
        self,
        *,
        store: MemoryStore,
        context_builder: ContextBuilder,
        client: QwenClient,
        model: str = "qwen3.7-max",
        soft_context_tokens: int = 700_000,
        cloud_ttl_days: float = 7,
    ) -> None:
        self.store = store
        self.context_builder = context_builder
        self.client = client
        self.model = model
        self.soft_context_tokens = max(10_000, int(soft_context_tokens))
        self.cloud_ttl_seconds = min(7.0, max(0.25, float(cloud_ttl_days))) * 86400
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, contact_id: int) -> asyncio.Lock:
        lock = self._locks.get(contact_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[contact_id] = lock
        return lock

    def _valid(
        self,
        session: QwenSessionRecord | None,
        *,
        persona_hash: str,
        tool_hash: str,
        now: float,
        memory_revision: int,
    ) -> bool:
        return bool(
            session
            and not session.dirty
            and session.model == self.model
            and session.persona_hash == persona_hash
            and session.tool_hash == tool_hash
            and session.memory_revision == memory_revision
            and session.expires_at > now + 60
            and session.estimated_tokens < self.soft_context_tokens
        )

    async def _create_session(
        self,
        contact: ContactRecord,
        *,
        system_prompt: str,
        persona_hash: str,
        tool_hash: str,
        current_prompt: str,
        exclude_source_uids: Iterable[str],
    ) -> QwenSessionRecord:
        system_tokens = estimate_tokens(system_prompt) + (8 if system_prompt else 0)
        for attempt in range(3):
            memory_revision = await self.store.contact_memory_revision(contact.id)
            bundle = await self.context_builder.build(
                contact.id,
                current_prompt=current_prompt,
                exclude_source_uids=exclude_source_uids,
                token_budget=max(
                    1000,
                    self.context_builder.seed_max_tokens - system_tokens,
                ),
            )
            seed_items: list[dict[str, str]] = []
            if system_prompt.strip():
                seed_items.append(
                    {
                        "role": "system",
                        "content": system_prompt.strip(),
                    }
                )
            seed_items.extend(bundle.items)
            created_at = time.time()
            conversation_id = await self.client.create_conversation(
                metadata={
                    "contact_hmac": contact.contact_hmac,
                    "epoch": str(int(created_at * 1000)),
                    "schema_version": "5",
                }
            )
            session: QwenSessionRecord | None = None
            try:
                session = await self.store.create_qwen_session(
                    contact.id,
                    conversation_id=conversation_id,
                    model=self.model,
                    persona_hash=persona_hash,
                    tool_hash=tool_hash,
                    created_at=created_at,
                    # Leave a small safety margin before the documented
                    # 7-day maximum.
                    expires_at=created_at + self.cloud_ttl_seconds - 300,
                    estimated_tokens=bundle.estimated_tokens + system_tokens,
                    expected_memory_revision=memory_revision,
                )
                for offset in range(0, len(seed_items), 20):
                    item_ids = await self.client.add_items(
                        conversation_id,
                        seed_items[offset : offset + 20],
                    )
                    await self.store.record_qwen_items(session.id, item_ids)
                return session
            except MemoryRevisionChanged:
                try:
                    await self.client.delete_conversation_fully(conversation_id)
                except Exception:
                    pass
                if attempt == 2:
                    raise
            except Exception:
                if session:
                    await self.store.mark_qwen_session_dirty(session.id)
                try:
                    await self.client.delete_conversation_fully(conversation_id)
                except Exception:
                    pass
                raise
        raise MemoryRevisionChanged(
            "contact history kept changing while creating Qwen session"
        )

    async def respond(
        self,
        contact: ContactRecord,
        *,
        prompt: str = "",
        system_prompt: str,
        tool_fingerprint: str = "",
        exclude_source_uids: Iterable[str] = (),
        request_key: str,
        input_items: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
    ) -> QwenResult:
        request_key = str(request_key or "").strip()
        if not request_key:
            raise ValueError("request_key is required")
        continuation_ids = _function_output_ids(input_items)
        if input_items is not None and not continuation_ids:
            raise StaleToolContinuation("invalid Qwen tool continuation")
        persona_hash = stable_hash(system_prompt)
        tool_hash = stable_hash(tool_fingerprint)
        async with self._lock_for(contact.id):
            if await self.store.is_contact_tombstoned(contact.id):
                raise RuntimeError("contact memory is tombstoned")
            now = time.time()
            session = await self.store.active_qwen_session(contact.id)
            memory_revision = await self.store.contact_memory_revision(contact.id)
            if session and session.pending_owner:
                if session.pending_owner != request_key:
                    if input_items is not None:
                        raise StaleToolContinuation(
                            "tool continuation belongs to an older request"
                        )
                    await self.store.mark_qwen_session_dirty(session.id)
                    session = None
                elif input_items is None:
                    await self.store.mark_qwen_session_dirty(session.id)
                    raise RuntimeError("pending Qwen tool call has no tool result")
                elif set(continuation_ids) != set(session.pending_call_ids):
                    await self.store.mark_qwen_session_dirty(session.id)
                    raise RuntimeError("Qwen tool result IDs do not match pending calls")
            elif input_items is not None:
                raise StaleToolContinuation("Qwen conversation has no pending tool call")
            if not self._valid(
                session,
                persona_hash=persona_hash,
                tool_hash=tool_hash,
                now=now,
                memory_revision=memory_revision,
            ):
                if input_items is not None:
                    if session:
                        await self.store.mark_qwen_session_dirty(session.id)
                    raise RuntimeError(
                        "cannot continue a tool call on an expired Qwen conversation"
                    )
                session = await self._create_session(
                    contact,
                    system_prompt=system_prompt,
                    persona_hash=persona_hash,
                    tool_hash=tool_hash,
                    current_prompt=prompt,
                    exclude_source_uids=exclude_source_uids,
                )
            try:
                await self.store.set_qwen_session_inflight(
                    session.id,
                    request_key=request_key,
                )
                result = await self.client.respond(
                    conversation_id=session.conversation_id,
                    model=self.model,
                    prompt=prompt,
                    input_items=input_items,
                    tools=tools,
                    tool_choice=tool_choice,
                    request_max_retries=request_max_retries,
                )
                pending_ids = tuple(call.call_id for call in result.tool_calls)
                if len(pending_ids) != len(set(pending_ids)):
                    raise RuntimeError("Qwen returned duplicate tool call IDs")
                estimated = max(
                    session.estimated_tokens,
                    result.input_tokens + result.output_tokens,
                )
                await self.store.update_qwen_session_usage(
                    session.id,
                    estimated_tokens=estimated,
                    response_id=result.response_id,
                    pending_owner=request_key if pending_ids else "",
                    pending_call_ids=pending_ids,
                )
                if result.text and not result.tool_calls:
                    await self.store.archive_generated(
                        contact.id,
                        response_id=result.response_id,
                        content=result.text,
                    )
                return result
            except Exception:
                await self.store.mark_qwen_session_dirty(session.id)
                raise

    async def archive_fallback_output(
        self,
        contact_id: int,
        content: str,
    ) -> None:
        if not content:
            return
        async with self._lock_for(contact_id):
            await self.store.archive_generated(
                contact_id,
                response_id=f"fallback-{uuid.uuid4().hex}",
                content=content,
                id_quality="fallback_response",
                advance_memory_revision=True,
            )
            # The fallback turn was produced outside Qwen Conversations. This
            # second invalidation occurs after the fallback completed, so it
            # also catches a newer session created during the fallback call.
            await self.store.mark_contact_sessions_dirty(contact_id)

    async def mark_dirty(self, contact_id: int) -> None:
        await self.store.mark_contact_sessions_dirty(contact_id)

    async def finish_request(self, contact_id: int, request_key: str) -> bool:
        async with self._lock_for(contact_id):
            session = await self.store.active_qwen_session(contact_id)
            if not session or session.pending_owner != request_key:
                return False
            return await self.store.abandon_qwen_pending(
                session.id,
                request_key=request_key,
            )

    async def rebuild(self, contact_id: int) -> int:
        async with self._lock_for(contact_id):
            conversation_ids = await self.store.list_contact_conversations(contact_id)
            for conversation_id in conversation_ids:
                try:
                    await self.client.delete_conversation_fully(conversation_id)
                except Exception:
                    pass
            await self.store.mark_contact_sessions_dirty(contact_id)
            return len(conversation_ids)

    async def forget(self, contact_id: int) -> tuple[bool, int]:
        async with self._lock_for(contact_id):
            await self.store.tombstone_contact(contact_id)
            conversation_ids = await self.store.list_contact_conversations(contact_id)
            failures = 0
            for conversation_id in conversation_ids:
                try:
                    await self.client.delete_conversation_fully(conversation_id)
                except Exception:
                    failures += 1
            if failures:
                return False, failures
            await self.store.forget_contact(contact_id)
            return True, 0
