from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["in", "out"]


@dataclass(frozen=True, slots=True)
class ContactBinding:
    """Stable identity extracted from one Akasha bridge event."""

    account: str
    session: str
    routing_name: str
    unified_origin: str
    source_messages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ContactRecord:
    id: int
    contact_hmac: str
    routing_name: str
    aliases: tuple[str, ...] = ()
    tombstoned_at: float | None = None


@dataclass(frozen=True, slots=True)
class MemoryMessage:
    source_uid: str
    source_time: float
    direction: Direction
    content: str
    semantic_content: str | None = None
    message_type: str = "text"
    id_quality: str = "source"
    origin: str = "weflow"
    pending: bool = False
    id: int | None = None

    @property
    def effective_content(self) -> str:
        """Return enriched media text only while raw content is that media."""

        semantic = (self.semantic_content or "").strip()
        raw = self.content.strip()
        for kind in ("图片", "视频"):
            if raw == f"[{kind}]" and semantic.startswith(f"[{kind}:"):
                return semantic
        return self.content


@dataclass(frozen=True, slots=True)
class QwenSessionRecord:
    id: int
    contact_id: int
    conversation_id: str
    model: str
    persona_hash: str
    tool_hash: str
    memory_revision: int
    created_at: float
    expires_at: float
    estimated_tokens: int
    last_response_id: str | None
    last_used_at: float
    dirty: bool
    pending_owner: str = ""
    pending_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncResult:
    imported: int = 0
    fetched: int = 0
    timed_out: bool = False
    full_backfill_scheduled: bool = False
    available: bool = True
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ContextBundle:
    items: tuple[dict[str, Any], ...]
    estimated_tokens: int
    recent_count: int
    retrieved_count: int
    represented_messages: tuple[MemoryMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class QwenToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QwenResult:
    response_id: str
    text: str
    tool_calls: tuple[QwenToolCall, ...] = ()
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)
