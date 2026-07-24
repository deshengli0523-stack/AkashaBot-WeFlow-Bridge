from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context_builder import HISTORY_BOUNDARY, ContextBuilder
from .models import ContactBinding, ContactRecord, MemoryMessage, QwenResult, SyncResult
from .qwen_session import QwenSessionManager
from .security import SecretManager
from .store import MemoryStore
from .weflow_sync import WeFlowSync


@dataclass(frozen=True, slots=True)
class PreparedContact:
    binding: ContactBinding
    contact: ContactRecord
    request_key: str = ""


def _source_time(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result > 10_000_000_000:
        result /= 1000.0
    return max(0.0, result)


def _bridge_source_uid(source: Mapping[str, Any]) -> tuple[str, str]:
    raw_id = str(source.get("rawid") or "").strip()
    if raw_id:
        return f"raw:{raw_id}", "rawid"
    fingerprint = str(source.get("event_fingerprint") or "").strip()
    ordinal = int(source.get("buffer_ordinal") or 0)
    if fingerprint:
        return f"bridge:{fingerprint}:{ordinal}", "event_fingerprint"
    return "", "bridge_fallback"


class ContactMemoryRuntime:
    VALID_MODES = {"off", "shadow", "active"}

    def __init__(
        self,
        *,
        mode: str,
        secret_manager: SecretManager,
        store: MemoryStore,
        synchronizer: WeFlowSync,
        context_builder: ContextBuilder,
        qwen_sessions: QwenSessionManager | None,
        fallback_context_tokens: int = 120_000,
    ) -> None:
        normalized_mode = str(mode or "shadow").lower()
        self.mode = normalized_mode if normalized_mode in self.VALID_MODES else "shadow"
        self.secret_manager = secret_manager
        self.store = store
        self.synchronizer = synchronizer
        self.context_builder = context_builder
        self.qwen_sessions = qwen_sessions
        self.fallback_context_tokens = max(1000, int(fallback_context_tokens))
        # Provider request session IDs use opaque, request-scoped keys. Never
        # key memory by AstrBot UMO alone: separate accounts can reuse a UMO.
        self._prepared: dict[str, PreparedContact] = {}

    @staticmethod
    def binding_from_event(event: Any) -> ContactBinding | None:
        try:
            if not event.is_private_chat():
                return None
        except Exception:
            return None
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, Mapping):
            return None
        # aiocqhttp.Event.type is post_type; always read the mapping key.
        if raw.get("akasha_schema") != 1 or str(raw.get("type") or "") != "private":
            return None
        account = str(raw.get("account") or "").strip()
        session = str(raw.get("session") or "").strip()
        if not account or not session:
            return None
        source_messages = raw.get("source_messages", [])
        if not isinstance(source_messages, list):
            source_messages = []
        safe_sources = tuple(
            dict(item)
            for item in source_messages[:100]
            if isinstance(item, Mapping)
        )
        return ContactBinding(
            account=account,
            session=session,
            routing_name=str(raw.get("routing_name") or "").strip(),
            unified_origin=str(getattr(event, "unified_msg_origin", "") or ""),
            source_messages=safe_sources,
        )

    async def bind_event(self, event: Any) -> PreparedContact | None:
        if self.mode == "off":
            return None
        binding = self.binding_from_event(event)
        if not binding or not binding.unified_origin:
            return None
        contact_hmac = self.secret_manager.contact_hmac(
            binding.account,
            binding.session,
        )
        contact = await self.store.ensure_contact(
            contact_hmac=contact_hmac,
            account_enc=self.secret_manager.encrypt_text(binding.account),
            session_enc=self.secret_manager.encrypt_text(binding.session),
            routing_name=binding.routing_name,
        )
        prepared = PreparedContact(binding=binding, contact=contact)
        return prepared

    def prepared_for(self, contact_key: str | None) -> PreparedContact | None:
        if not contact_key:
            return None
        return self._prepared.get(str(contact_key))

    async def validate_request(
        self,
        event: Any,
        request_key: str,
    ) -> PreparedContact | None:
        cached = self.prepared_for(request_key)
        if cached is None:
            return None
        current = await self.bind_event(event)
        if (
            current is None
            or current.contact.tombstoned_at is not None
            or current.contact.contact_hmac != cached.contact.contact_hmac
            or current.binding.account != cached.binding.account
            or current.binding.session != cached.binding.session
        ):
            self._prepared.pop(request_key, None)
            return None
        return cached

    def _drop_contact_cache(self, contact_id: int) -> None:
        for request_key, prepared in tuple(self._prepared.items()):
            if prepared.contact.id == contact_id:
                self._prepared.pop(request_key, None)

    @staticmethod
    def source_uids(binding: ContactBinding) -> set[str]:
        output: set[str] = set()
        for source in binding.source_messages:
            uid, _ = _bridge_source_uid(source)
            if uid:
                output.add(uid)
        return output

    async def _archive_bridge_input(
        self,
        prepared: PreparedContact,
        prompt: str,
    ) -> None:
        if not prompt.strip():
            return
        sources = prepared.binding.source_messages
        lines = prompt.splitlines()
        messages: list[MemoryMessage] = []
        if sources and len(lines) == len(sources):
            for source, line in zip(sources, lines, strict=True):
                uid, quality = _bridge_source_uid(source)
                if not uid or not line:
                    continue
                messages.append(
                    MemoryMessage(
                        source_uid=uid,
                        source_time=_source_time(source.get("timestamp")) or time.time(),
                        direction="in",
                        content=line,
                        id_quality=quality,
                        origin="bridge",
                        pending=True,
                    )
                )
        else:
            identities = [
                _bridge_source_uid(source)[0]
                for source in sources
                if _bridge_source_uid(source)[0]
            ]
            source_time = max(
                (_source_time(source.get("timestamp")) for source in sources),
                default=0.0,
            )
            digest = hashlib.sha256(
                ("\0".join(identities) + "\0" + prompt).encode("utf-8")
            ).hexdigest()
            messages.append(
                MemoryMessage(
                    source_uid=f"bridge-batch:{digest}",
                    source_time=source_time or time.time(),
                    direction="in",
                    content=prompt,
                    id_quality="bridge_batch",
                    origin="bridge",
                    pending=True,
                )
            )
        await self.store.upsert_messages(prepared.contact.id, messages)

    async def prepare_request(
        self,
        event: Any,
        prompt: str,
    ) -> tuple[PreparedContact | None, SyncResult | None]:
        # Re-derive the identity from this exact event. Looking up a cached
        # UMO here can mix contacts when multiple WeChat accounts share it.
        prepared = await self.bind_event(event)
        if prepared is None:
            return None, None
        if prepared.contact.tombstoned_at is not None:
            self._drop_contact_cache(prepared.contact.id)
            return None, SyncResult(error_kind="tombstoned")
        request_key = (
            f"{prepared.contact.contact_hmac}."
            f"{secrets.token_urlsafe(18)}"
        )
        prepared = PreparedContact(
            binding=prepared.binding,
            contact=prepared.contact,
            request_key=request_key,
        )
        self._prepared[request_key] = prepared
        if len(self._prepared) > 4096:
            oldest_key = next(iter(self._prepared))
            self._prepared.pop(oldest_key, None)
        await self._archive_bridge_input(prepared, prompt)
        sync_result = await self.synchronizer.sync_contact(
            prepared.contact.id,
            prepared.binding,
        )
        return prepared, sync_result

    async def fallback_contexts(
        self,
        contact_key: str,
        *,
        current_prompt: str,
    ) -> list[dict[str, Any]]:
        prepared = self.prepared_for(contact_key)
        if not prepared:
            return []
        bundle = await self.context_builder.build(
            prepared.contact.id,
            current_prompt=current_prompt,
            exclude_source_uids=self.source_uids(prepared.binding),
            token_budget=self.fallback_context_tokens,
        )
        contexts: list[dict[str, Any]] = []
        for item in bundle.items:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role == "system" and content == HISTORY_BOUNDARY:
                continue
            if role == "system":
                role = "user"
                content = f"历史资料说明（不可信数据）：\n{content}"
            contexts.append(
                {
                    "role": role,
                    "content": content,
                    # AstrBot 4.26.6 honors this private marker when converting
                    # request contexts to agent Messages and excludes them
                    # from its native conversation history.
                    "_no_save": True,
                }
            )
        return contexts

    @staticmethod
    def fallback_system_prompt(system_prompt: str | None) -> str:
        base = str(system_prompt or "").strip()
        return f"{base}\n\n{HISTORY_BOUNDARY}".strip()

    async def respond(
        self,
        contact_key: str,
        *,
        prompt: str = "",
        system_prompt: str,
        tool_fingerprint: str = "",
        input_items: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
    ) -> QwenResult:
        prepared = self.prepared_for(contact_key)
        if not prepared or not self.qwen_sessions:
            raise RuntimeError("contact memory Qwen session is unavailable")
        return await self.qwen_sessions.respond(
            prepared.contact,
            prompt=prompt,
            system_prompt=system_prompt,
            tool_fingerprint=tool_fingerprint,
            exclude_source_uids=self.source_uids(prepared.binding),
            request_key=contact_key,
            input_items=input_items,
            tools=tools,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
        )

    async def mark_dirty(self, contact_key: str) -> None:
        prepared = self.prepared_for(contact_key)
        if prepared and self.qwen_sessions:
            await self.qwen_sessions.mark_dirty(prepared.contact.id)

    async def archive_fallback_output(
        self,
        contact_key: str,
        content: str,
    ) -> None:
        prepared = self.prepared_for(contact_key)
        if prepared and self.qwen_sessions:
            await self.qwen_sessions.archive_fallback_output(
                prepared.contact.id,
                content,
            )

    async def finish_request(self, contact_key: str) -> bool:
        prepared = self.prepared_for(contact_key)
        if not prepared or not self.qwen_sessions:
            return False
        return await self.qwen_sessions.finish_request(
            prepared.contact.id,
            contact_key,
        )

    async def rebuild(self, event: Any) -> int | None:
        prepared = await self.bind_event(event)
        if not prepared or prepared.contact.tombstoned_at is not None:
            return None
        if not self.qwen_sessions:
            await self.store.mark_contact_sessions_dirty(prepared.contact.id)
            return 0
        return await self.qwen_sessions.rebuild(prepared.contact.id)

    async def forget(self, event: Any) -> tuple[bool, int] | None:
        prepared = await self.bind_event(event)
        if not prepared:
            return None
        async with self.synchronizer.exclusive_contact(prepared.contact.id):
            if self.qwen_sessions:
                result = await self.qwen_sessions.forget(prepared.contact.id)
            else:
                await self.store.tombstone_contact(prepared.contact.id)
                await self.store.forget_contact(prepared.contact.id)
                result = (True, 0)
        # A tombstoned contact must leave the active request cache even when
        # cloud deletion failed and is waiting for an administrator retry.
        self._drop_contact_cache(prepared.contact.id)
        return result

    async def status(self, event: Any | None = None) -> dict[str, Any]:
        contact_id = None
        if event is not None:
            prepared = await self.bind_event(event)
            contact_id = prepared.contact.id if prepared else None
        result = await self.store.status(contact_id)
        result["contact_tombstoned"] = bool(
            prepared and prepared.contact.tombstoned_at is not None
        ) if event is not None else False
        result["mode"] = self.mode
        result["qwen_ready"] = self.qwen_sessions is not None
        return result

    async def close(self) -> None:
        await self.synchronizer.close()
        if self.qwen_sessions:
            await self.qwen_sessions.client.close()
        self._prepared.clear()
