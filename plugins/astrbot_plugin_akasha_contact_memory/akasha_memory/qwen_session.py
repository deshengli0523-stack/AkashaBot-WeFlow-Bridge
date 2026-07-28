from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Iterable
from typing import Any

from .context_builder import ContextBuilder, estimate_tokens
from .models import ContactRecord, MemoryMessage, QwenResult, QwenSessionRecord
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


def _normalized_content(value: str) -> str:
    return "".join(str(value).split())


def _as_conversation_item(message: MemoryMessage) -> dict[str, str]:
    return {
        "role": "user" if message.direction == "in" else "assistant",
        "content": message.effective_content,
    }


def _bounded_conversation_item(
    message: MemoryMessage,
    token_budget: int,
) -> tuple[dict[str, str], int] | None:
    """Represent one oversized turn without consuming the full context."""

    budget = max(0, int(token_budget))
    prefix = "【单条消息过长，以下为节选；完整内容仍保存在本地】\n"
    separator = "\n…\n"
    minimum_cost = estimate_tokens(prefix + separator) + 8
    if budget < max(128, minimum_cost):
        return None
    content = message.effective_content
    low = 0
    high = len(content)
    best = prefix + separator
    while low <= high:
        length = (low + high) // 2
        head_length = (length * 2) // 3
        tail_length = length - head_length
        tail = content[-tail_length:] if tail_length else ""
        candidate = (
            prefix
            + content[:head_length]
            + separator
            + tail
        )
        cost = estimate_tokens(candidate) + 8
        if cost <= budget:
            best = candidate
            low = length + 1
        else:
            high = length - 1
    cost = estimate_tokens(best) + 8
    return (
        {
            "role": "user" if message.direction == "in" else "assistant",
            "content": best,
        },
        cost,
    )


class QwenSessionManager:
    def __init__(
        self,
        *,
        store: MemoryStore,
        context_builder: ContextBuilder,
        client: QwenClient,
        model: str = "qwen3.7-max",
        soft_context_tokens: int = 120_000,
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
        now: float,
    ) -> bool:
        return bool(
            session
            and not session.dirty
            and session.model == self.model
            and session.persona_hash == persona_hash
            and session.expires_at > now + 60
        )

    async def _append_external_delta(
        self,
        session: QwenSessionRecord,
        *,
        contact_id: int,
        target_memory_revision: int,
        current_prompt: str,
        exclude_source_uids: Iterable[str],
        represented_prompt_messages: Iterable[MemoryMessage] = (),
    ) -> QwenSessionRecord:
        excluded = {str(value) for value in exclude_source_uids if str(value)}
        prompt_snapshots = tuple(represented_prompt_messages)
        legacy_excluded = excluded if not prompt_snapshots else set()
        normalized_prompt = _normalized_content(current_prompt)

        delta_budget = max(
            0,
            min(
                12_000,
                self.soft_context_tokens
                - session.estimated_tokens
                - 2_000,
            ),
        )
        added_tokens = 0

        try:
            # These source messages are represented by the prompt passed to
            # respond(), so they must never be appended a second time.
            await self.store.record_qwen_prompt_messages(
                session.id,
                prompt_snapshots,
            )
            await self.store.record_qwen_external_sources(
                session.id,
                contact_id,
                legacy_excluded,
            )
            prompt_fallback_consumed = False
            while True:
                messages = await self.store.session_delta_messages(
                    contact_id,
                    session_id=session.id,
                    after_created_at=session.created_at,
                    recent_source_floor=max(0.0, session.created_at - 300.0),
                    limit=200,
                )
                if not messages:
                    break

                if normalized_prompt and not prompt_fallback_consumed:
                    for index in range(len(messages) - 1, -1, -1):
                        candidate = messages[index]
                        if (
                            candidate.direction == "in"
                            and _normalized_content(
                                candidate.effective_content
                            )
                            == normalized_prompt
                        ):
                            await self.store.record_qwen_prompt_messages(
                                session.id,
                                (candidate,),
                            )
                            del messages[index]
                            prompt_fallback_consumed = True
                            break

                selected: list[
                    tuple[MemoryMessage, dict[str, str], int]
                ] = []
                blocked = False
                for message in messages:
                    if message.source_uid in legacy_excluded:
                        await self.store.record_qwen_external_sources(
                            session.id,
                            contact_id,
                            (message.source_uid,),
                        )
                        continue
                    cost = estimate_tokens(message.effective_content) + 8
                    if cost > delta_budget - added_tokens:
                        bounded = (
                            _bounded_conversation_item(
                                message,
                                delta_budget - added_tokens,
                            )
                            if added_tokens == 0
                            else None
                        )
                        if bounded is None:
                            blocked = True
                            break
                        item, cost = bounded
                    else:
                        item = _as_conversation_item(message)
                    selected.append((message, item, cost))
                    added_tokens += cost

                for offset in range(0, len(selected), 20):
                    chunk = selected[offset : offset + 20]
                    item_ids = await self.client.add_items(
                        session.conversation_id,
                        [item for _message, item, _cost in chunk],
                    )
                    await self.store.record_qwen_items(session.id, item_ids)
                    await self.store.record_qwen_external_messages(
                        session.id,
                        (message for message, _item, _cost in chunk),
                    )
                if blocked:
                    break

            if added_tokens:
                session = await self.store.record_qwen_delta_tokens(
                    session.id,
                    expected_memory_revision=session.memory_revision,
                    added_tokens=added_tokens,
                )

            remaining = await self.store.session_delta_messages(
                contact_id,
                session_id=session.id,
                after_created_at=session.created_at,
                recent_source_floor=max(0.0, session.created_at - 300.0),
                limit=1,
            )
            if remaining:
                return session
            try:
                return await self.store.advance_qwen_session_revision(
                    session.id,
                    expected_memory_revision=session.memory_revision,
                    target_memory_revision=target_memory_revision,
                    added_tokens=0,
                )
            except MemoryRevisionChanged:
                current = await self.store.active_qwen_session(contact_id)
                if current is not None and current.id == session.id:
                    return current
                raise
        except asyncio.CancelledError:
            dirty_task = asyncio.create_task(
                self.store.mark_qwen_session_dirty(session.id)
            )
            try:
                await asyncio.shield(dirty_task)
            except asyncio.CancelledError:
                await dirty_task
            raise
        except Exception:
            await self.store.mark_qwen_session_dirty(session.id)
            raise

    async def _create_session(
        self,
        contact: ContactRecord,
        *,
        system_prompt: str,
        persona_hash: str,
        tool_hash: str,
        current_prompt: str,
        exclude_source_uids: Iterable[str],
        represented_prompt_messages: Iterable[MemoryMessage],
    ) -> QwenSessionRecord:
        system_tokens = estimate_tokens(system_prompt) + (8 if system_prompt else 0)
        for attempt in range(3):
            memory_revision, recent_message_limit = (
                await self.store.contact_seed_state(contact.id)
            )
            build_options = {
                "current_prompt": current_prompt,
                "exclude_source_uids": exclude_source_uids,
                "represented_prompt_messages": represented_prompt_messages,
                "token_budget": max(
                    1000,
                    self.context_builder.seed_max_tokens - system_tokens,
                ),
            }
            if recent_message_limit:
                build_options["recent_message_limit"] = recent_message_limit
            bundle = await self.context_builder.build(
                contact.id,
                **build_options,
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
                    "schema_version": "10",
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
                await self.store.record_qwen_external_messages(
                    session.id,
                    getattr(bundle, "represented_messages", ()),
                )
                session = await self.store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=memory_revision,
                )
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
        represented_prompt_messages: Iterable[MemoryMessage] = (),
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
        prompt_snapshots = tuple(represented_prompt_messages)
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
                now=now,
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
                    represented_prompt_messages=prompt_snapshots,
                )
            elif (
                session is not None
                and session.memory_revision != memory_revision
            ):
                if input_items is not None:
                    await self.store.mark_qwen_session_dirty(session.id)
                    raise RuntimeError(
                        "contact history changed during a Qwen tool continuation"
                    )
                session = await self._append_external_delta(
                    session,
                    contact_id=contact.id,
                    target_memory_revision=memory_revision,
                    current_prompt=prompt,
                    exclude_source_uids=exclude_source_uids,
                    represented_prompt_messages=prompt_snapshots,
                )
            await self.store.record_qwen_prompt_messages(
                session.id,
                prompt_snapshots,
            )
            await self.store.record_qwen_external_sources(
                session.id,
                contact.id,
                exclude_source_uids if not prompt_snapshots else (),
            )
            response_id = ""
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
                response_id = result.response_id
                pending_ids = tuple(call.call_id for call in result.tool_calls)
                if len(pending_ids) != len(set(pending_ids)):
                    raise RuntimeError("Qwen returned duplicate tool call IDs")
                estimated = max(
                    session.estimated_tokens,
                    result.input_tokens + result.output_tokens,
                )
                if result.text and not result.tool_calls:
                    generated = await self.store.archive_generated_snapshot(
                        contact.id,
                        response_id=result.response_id,
                        content=result.text,
                    )
                    if generated is not None:
                        await self.store.record_qwen_external_messages(
                            session.id,
                            (generated,),
                        )
                await self.store.update_qwen_session_usage(
                    session.id,
                    request_key=request_key,
                    estimated_tokens=estimated,
                    response_id=result.response_id,
                    pending_owner=request_key if pending_ids else "",
                    pending_call_ids=pending_ids,
                )
                return result
            except asyncio.CancelledError:
                # A cancelled caller must never leave a clean cloud
                # conversation containing an answer that was not sent.
                dirty_task = asyncio.create_task(
                    self.store.fail_qwen_response(
                        contact.id,
                        session.id,
                        response_id=response_id,
                    )
                )
                try:
                    await asyncio.shield(dirty_task)
                except asyncio.CancelledError:
                    await dirty_task
                raise
            except Exception:
                await self.store.fail_qwen_response(
                    contact.id,
                    session.id,
                    response_id=response_id,
                )
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
            )

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
