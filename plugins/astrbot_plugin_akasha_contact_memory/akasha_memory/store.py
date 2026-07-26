from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from .models import ContactRecord, MemoryMessage, QwenSessionRecord

_T = TypeVar("_T")
_SCHEMA_VERSION = 7
SEND_FAILURE_REBUILD_MESSAGE_LIMIT = 20

_SCHEMA_V7 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    contact_hmac TEXT NOT NULL UNIQUE,
    account_enc TEXT NOT NULL,
    session_enc TEXT NOT NULL,
    routing_name TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    memory_revision INTEGER NOT NULL DEFAULT 0,
    next_seed_recent_limit INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    tombstoned_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source_uid TEXT NOT NULL,
    source_time REAL NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
    content TEXT NOT NULL,
    semantic_content TEXT,
    message_type TEXT NOT NULL DEFAULT 'text',
    content_hash TEXT NOT NULL,
    id_quality TEXT NOT NULL DEFAULT 'source',
    origin TEXT NOT NULL DEFAULT 'weflow',
    pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0, 1)),
    created_at REAL NOT NULL,
    UNIQUE(contact_id, source_uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_contact_time
    ON messages(contact_id, source_time, source_uid);
CREATE INDEX IF NOT EXISTS idx_messages_pending_hash
    ON messages(contact_id, direction, pending, content_hash, source_time);
CREATE TABLE IF NOT EXISTS sync_state (
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
    cursor_time REAL NOT NULL DEFAULT 0,
    cursor_uid TEXT NOT NULL DEFAULT '',
    backfill_offset INTEGER NOT NULL DEFAULT 0,
    backfill_end_time REAL NOT NULL DEFAULT 0,
    full_backfill_complete INTEGER NOT NULL DEFAULT 0
        CHECK(full_backfill_complete IN (0, 1)),
    last_sync_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS summaries (
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    through_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qwen_sessions (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    persona_hash TEXT NOT NULL,
    tool_hash TEXT NOT NULL,
    memory_revision INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    last_response_id TEXT,
    last_used_at REAL NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0, 1)),
    pending_owner TEXT NOT NULL DEFAULT '',
    pending_call_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_qwen_sessions_contact
    ON qwen_sessions(contact_id, last_used_at DESC);
CREATE TABLE IF NOT EXISTS qwen_items (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES qwen_sessions(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'item',
    created_at REAL NOT NULL,
    UNIQUE(session_id, item_id)
);
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V1_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE messages
    ADD COLUMN semantic_content TEXT;
ALTER TABLE contacts
    ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE contacts
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_state
    ADD COLUMN backfill_end_time REAL NOT NULL DEFAULT 0;
ALTER TABLE qwen_sessions
    ADD COLUMN pending_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE qwen_sessions
    ADD COLUMN pending_call_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE qwen_sessions
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
UPDATE qwen_sessions SET dirty = 1;
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V2_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE messages
    ADD COLUMN semantic_content TEXT;
ALTER TABLE contacts
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_state
    ADD COLUMN backfill_end_time REAL NOT NULL DEFAULT 0;
ALTER TABLE qwen_sessions
    ADD COLUMN pending_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE qwen_sessions
    ADD COLUMN pending_call_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE qwen_sessions
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
UPDATE qwen_sessions SET dirty = 1;
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V3_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE messages
    ADD COLUMN semantic_content TEXT;
ALTER TABLE contacts
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE qwen_sessions
    ADD COLUMN pending_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE qwen_sessions
    ADD COLUMN pending_call_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE qwen_sessions
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
UPDATE qwen_sessions SET dirty = 1;
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V4_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE messages
    ADD COLUMN semantic_content TEXT;
ALTER TABLE contacts
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE qwen_sessions
    ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0;
UPDATE qwen_sessions SET dirty = 1;
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V5_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE messages
    ADD COLUMN semantic_content TEXT;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 7;
COMMIT;
"""

_MIGRATE_V6_TO_V7 = """
BEGIN IMMEDIATE;
ALTER TABLE contacts
    ADD COLUMN next_seed_recent_limit INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 7;
COMMIT;
"""


class MemoryRevisionChanged(RuntimeError):
    """The contact history changed while a cloud session was being seeded."""


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _equivalence_text(content: str) -> str:
    return "".join(content.split())


def _contact_from_row(row: sqlite3.Row) -> ContactRecord:
    try:
        raw_aliases = json.loads(str(row["aliases_json"]))
    except (json.JSONDecodeError, TypeError):
        raw_aliases = []
    aliases = tuple(
        value.strip()
        for value in raw_aliases
        if isinstance(value, str) and value.strip()
    )
    return ContactRecord(
        id=int(row["id"]),
        contact_hmac=str(row["contact_hmac"]),
        routing_name=str(row["routing_name"]),
        aliases=aliases,
        tombstoned_at=(
            float(row["tombstoned_at"])
            if row["tombstoned_at"] is not None
            else None
        ),
    )


def _message_from_row(row: sqlite3.Row) -> MemoryMessage:
    return MemoryMessage(
        id=int(row["id"]),
        source_uid=str(row["source_uid"]),
        source_time=float(row["source_time"]),
        direction=str(row["direction"]),  # type: ignore[arg-type]
        content=str(row["content"]),
        semantic_content=(
            str(row["semantic_content"])
            if row["semantic_content"] is not None
            else None
        ),
        message_type=str(row["message_type"]),
        id_quality=str(row["id_quality"]),
        origin=str(row["origin"]),
        pending=bool(row["pending"]),
    )


def _session_from_row(row: sqlite3.Row) -> QwenSessionRecord:
    try:
        raw_pending_ids = json.loads(str(row["pending_call_ids_json"]))
    except (json.JSONDecodeError, TypeError):
        raw_pending_ids = []
    return QwenSessionRecord(
        id=int(row["id"]),
        contact_id=int(row["contact_id"]),
        conversation_id=str(row["conversation_id"]),
        model=str(row["model"]),
        persona_hash=str(row["persona_hash"]),
        tool_hash=str(row["tool_hash"]),
        memory_revision=int(row["memory_revision"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        estimated_tokens=int(row["estimated_tokens"]),
        last_response_id=(
            str(row["last_response_id"])
            if row["last_response_id"] is not None
            else None
        ),
        last_used_at=float(row["last_used_at"]),
        dirty=bool(row["dirty"]),
        pending_owner=str(row["pending_owner"]),
        pending_call_ids=tuple(
            str(value)
            for value in raw_pending_ids
            if isinstance(value, str) and value
        ),
    )


class MemoryStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        backup_dir: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "memory.db"
        self.backup_dir = (
            Path(backup_dir)
            if backup_dir is not None
            else self.data_dir / "backups"
        )
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    async def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        def run() -> _T:
            connection = self._connect()
            try:
                return operation(connection)
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        async with self._write_lock:
            def run() -> _T:
                connection = self._connect()
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            thread_task = asyncio.create_task(asyncio.to_thread(run))
            try:
                return await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                # sqlite3 work already running in a worker thread cannot be
                # cancelled. Keep the per-store write lock until it finishes
                # so privacy deletion cannot race an orphaned commit.
                await thread_task
                raise

    def _initialize_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        connection = self._connect()
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise sqlite3.DatabaseError("memory database quick_check failed")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > _SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"memory schema {current} is newer than supported {_SCHEMA_VERSION}"
                )
            if current == _SCHEMA_VERSION:
                wal_cursor = connection.execute("PRAGMA journal_mode = WAL")
                wal_cursor.fetchone()
                wal_cursor.close()
                return
            if existed:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup_path = self.backup_dir / f"memory-v{current}-{stamp}.db"
                backup = sqlite3.connect(backup_path)
                try:
                    connection.backup(backup)
                finally:
                    backup.close()
            if current == 0:
                connection.executescript(_SCHEMA_V7)
            elif current == 1:
                connection.executescript(_MIGRATE_V1_TO_V7)
            elif current == 2:
                connection.executescript(_MIGRATE_V2_TO_V7)
            elif current == 3:
                connection.executescript(_MIGRATE_V3_TO_V7)
            elif current == 4:
                connection.executescript(_MIGRATE_V4_TO_V7)
            elif current == 5:
                connection.executescript(_MIGRATE_V5_TO_V7)
            elif current == 6:
                connection.executescript(_MIGRATE_V6_TO_V7)
            else:
                raise sqlite3.DatabaseError(
                    f"no migration path from memory schema {current}"
                )
            wal_cursor = connection.execute("PRAGMA journal_mode = WAL")
            wal_cursor.fetchone()
            wal_cursor.close()
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def ensure_contact(
        self,
        *,
        contact_hmac: str,
        account_enc: str,
        session_enc: str,
        routing_name: str,
    ) -> ContactRecord:
        now = time.time()

        def operation(connection: sqlite3.Connection) -> ContactRecord:
            existing = connection.execute(
                "SELECT * FROM contacts WHERE contact_hmac = ?",
                (contact_hmac,),
            ).fetchone()
            normalized_name = routing_name.strip()
            if existing is None:
                aliases_json = json.dumps(
                    [normalized_name] if normalized_name else [],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                connection.execute(
                    """
                    INSERT INTO contacts(
                        contact_hmac, account_enc, session_enc, routing_name,
                        aliases_json, created_at, updated_at, tombstoned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        contact_hmac,
                        account_enc,
                        session_enc,
                        normalized_name,
                        aliases_json,
                        now,
                        now,
                    ),
                )
            elif existing["tombstoned_at"] is None:
                try:
                    aliases = [
                        value.strip()
                        for value in json.loads(str(existing["aliases_json"]))
                        if isinstance(value, str) and value.strip()
                    ]
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                old_name = str(existing["routing_name"]).strip()
                for candidate in (old_name, normalized_name):
                    if candidate and candidate not in aliases:
                        aliases.append(candidate)
                connection.execute(
                    """
                    UPDATE contacts SET
                        account_enc = ?,
                        session_enc = ?,
                        routing_name = ?,
                        aliases_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        account_enc,
                        session_enc,
                        normalized_name,
                        json.dumps(
                            aliases,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        int(existing["id"]),
                    ),
                )
            else:
                # A privacy tombstone is permanent until an explicit future
                # restore feature exists. Merely receiving another message
                # must never repopulate identifiers or history.
                connection.execute(
                    "UPDATE contacts SET updated_at = ? WHERE id = ?",
                    (now, int(existing["id"])),
                )
            row = connection.execute(
                "SELECT * FROM contacts WHERE contact_hmac = ?",
                (contact_hmac,),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("contact upsert returned no row")
            return _contact_from_row(row)

        return await self._write(operation)

    async def get_contact(self, contact_hmac: str) -> ContactRecord | None:
        def operation(connection: sqlite3.Connection) -> ContactRecord | None:
            row = connection.execute(
                "SELECT * FROM contacts WHERE contact_hmac = ?",
                (contact_hmac,),
            ).fetchone()
            return _contact_from_row(row) if row else None

        return await self._read(operation)

    async def is_contact_tombstoned(self, contact_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            return row is None or row["tombstoned_at"] is not None

        return await self._read(operation)

    async def contact_memory_revision(self, contact_id: int) -> int:
        revision, _ = await self.contact_seed_state(contact_id)
        return revision

    async def contact_seed_state(self, contact_id: int) -> tuple[int, int]:
        def operation(connection: sqlite3.Connection) -> tuple[int, int]:
            row = connection.execute(
                """
                SELECT tombstoned_at, memory_revision, next_seed_recent_limit
                FROM contacts
                WHERE id = ?
                """,
                (contact_id,),
            ).fetchone()
            if row is None or row["tombstoned_at"] is not None:
                raise RuntimeError("contact memory is tombstoned")
            return (
                int(row["memory_revision"]),
                max(0, int(row["next_seed_recent_limit"])),
            )

        return await self._read(operation)

    @staticmethod
    def _pending_origin(direction: str) -> str:
        return "generated" if direction == "out" else "bridge"

    async def upsert_messages(
        self,
        contact_id: int,
        messages: Iterable[MemoryMessage],
        *,
        advance_memory_revision: bool = False,
    ) -> int:
        records = tuple(messages)
        if not records:
            return 0

        def operation(connection: sqlite3.Connection) -> int:
            contact_state = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                return 0
            changed = 0
            external_changed = 0
            now = time.time()
            for message in records:
                if not message.source_uid or not message.content:
                    continue
                content_digest = _content_hash(message.content)
                existing = connection.execute(
                    """
                    SELECT
                        id, source_time, direction, content, semantic_content,
                        message_type, id_quality, origin, pending
                    FROM messages
                    WHERE contact_id = ? AND source_uid = ?
                    """,
                    (contact_id, message.source_uid),
                ).fetchone()
                if existing:
                    if (
                        str(existing["origin"]) == "weflow"
                        and message.origin != "weflow"
                    ):
                        semantic = (message.semantic_content or "").strip()
                        if (
                            semantic
                            and not existing["semantic_content"]
                            and MemoryMessage(
                                source_uid=message.source_uid,
                                source_time=message.source_time,
                                direction=message.direction,
                                content=str(existing["content"]),
                                semantic_content=semantic,
                            ).effective_content
                            == semantic
                        ):
                            connection.execute(
                                """
                                UPDATE messages SET semantic_content = ?
                                WHERE id = ?
                                """,
                                (semantic, int(existing["id"])),
                            )
                        continue
                    semantic_content = message.semantic_content
                    if message.origin == "weflow" and semantic_content is None:
                        candidate = (
                            str(existing["semantic_content"])
                            if existing["semantic_content"] is not None
                            else None
                        )
                        if candidate:
                            probe = MemoryMessage(
                                source_uid=message.source_uid,
                                source_time=message.source_time,
                                direction=message.direction,
                                content=message.content,
                                semantic_content=candidate,
                            )
                            if probe.effective_content == candidate:
                                semantic_content = candidate
                    authoritative_changed = (
                        message.origin == "weflow"
                        and str(existing["origin"]) == "weflow"
                        and not bool(existing["pending"])
                        and (
                            float(existing["source_time"]) != message.source_time
                            or str(existing["direction"]) != message.direction
                            or str(existing["content"]) != message.content
                            or str(existing["message_type"]) != message.message_type
                            or str(existing["id_quality"]) != message.id_quality
                        )
                    )
                    connection.execute(
                        """
                        UPDATE messages SET
                            source_time = ?, direction = ?, content = ?,
                            semantic_content = ?, message_type = ?,
                            content_hash = ?, id_quality = ?, origin = ?,
                            pending = ?
                        WHERE id = ?
                        """,
                        (
                            message.source_time,
                            message.direction,
                            message.content,
                            semantic_content,
                            message.message_type,
                            content_digest,
                            message.id_quality,
                            message.origin,
                            int(message.pending),
                            int(existing["id"]),
                        ),
                    )
                    if authoritative_changed:
                        changed += 1
                        external_changed += 1
                    continue

                pending = None
                if message.origin == "weflow":
                    pending = connection.execute(
                        """
                        SELECT id, id_quality, semantic_content FROM messages
                        WHERE contact_id = ?
                          AND direction = ?
                          AND pending = 1
                          AND origin = ?
                          AND content_hash = ?
                          AND ABS(source_time - ?) <= 300
                        ORDER BY ABS(source_time - ?), id
                        LIMIT 1
                        """,
                        (
                            contact_id,
                            message.direction,
                            self._pending_origin(message.direction),
                            content_digest,
                            message.source_time,
                            message.source_time,
                        ),
                    ).fetchone()
                if pending:
                    fallback_output = (
                        str(pending["id_quality"]) == "fallback_response"
                    )
                    try:
                        connection.execute(
                            """
                            UPDATE messages SET
                                source_uid = ?, source_time = ?, content = ?,
                                semantic_content = ?, message_type = ?,
                                content_hash = ?, id_quality = ?,
                                origin = 'weflow', pending = 0
                            WHERE id = ?
                            """,
                            (
                                message.source_uid,
                                message.source_time,
                                message.content,
                                (
                                    message.semantic_content
                                    if message.semantic_content is not None
                                    else pending["semantic_content"]
                                ),
                                message.message_type,
                                content_digest,
                                message.id_quality,
                                int(pending["id"]),
                            ),
                        )
                    except sqlite3.IntegrityError:
                        connection.execute(
                            "DELETE FROM messages WHERE id = ?",
                            (int(pending["id"]),),
                        )
                    if fallback_output:
                        # A fallback response is not part of any Qwen
                        # Conversation. Invalidate even a newer session that
                        # may have been created while UIA was sending it.
                        connection.execute(
                            """
                            UPDATE qwen_sessions SET dirty = 1
                            WHERE contact_id = ?
                            """,
                            (contact_id,),
                        )
                        connection.execute(
                            """
                            UPDATE contacts
                            SET memory_revision = memory_revision + 1
                            WHERE id = ?
                            """,
                            (contact_id,),
                        )
                    changed += 1
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        contact_id, source_uid, source_time, direction,
                        content, semantic_content, message_type, content_hash,
                        id_quality, origin, pending, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contact_id,
                        message.source_uid,
                        message.source_time,
                        message.direction,
                        message.content,
                        message.semantic_content,
                        message.message_type,
                        content_digest,
                        message.id_quality,
                        message.origin,
                        int(message.pending),
                        now,
                    ),
                )
                inserted = max(cursor.rowcount, 0)
                changed += inserted
                if inserted and message.origin == "weflow":
                    defer_split_outgoing = False
                    if message.direction == "out":
                        defer_split_outgoing = (
                            connection.execute(
                                """
                                SELECT 1
                                FROM messages
                                WHERE contact_id = ?
                                  AND direction = 'out'
                                  AND origin = 'generated'
                                  AND pending = 1
                                  AND ABS(source_time - ?) <= 900
                                LIMIT 1
                                """,
                                (contact_id, message.source_time),
                            ).fetchone()
                            is not None
                        )
                    if not defer_split_outgoing:
                        external_changed += 1
            if external_changed:
                connection.execute(
                    """
                    UPDATE contacts
                    SET memory_revision = memory_revision + 1
                    WHERE id = ?
                    """,
                    (contact_id,),
                )
                connection.execute(
                    "UPDATE qwen_sessions SET dirty = 1 WHERE contact_id = ?",
                    (contact_id,),
                )
            elif changed and advance_memory_revision:
                connection.execute(
                    """
                    UPDATE contacts
                    SET memory_revision = memory_revision + 1
                    WHERE id = ?
                    """,
                    (contact_id,),
                )
            return changed

        return await self._write(operation)

    async def archive_generated(
        self,
        contact_id: int,
        *,
        response_id: str,
        content: str,
        source_time: float | None = None,
        id_quality: str = "response_id",
        advance_memory_revision: bool = False,
    ) -> int:
        message = MemoryMessage(
            source_uid=f"generated:{response_id}",
            source_time=source_time or time.time(),
            direction="out",
            content=content,
            id_quality=id_quality,
            origin="generated",
            pending=True,
        )
        return await self.upsert_messages(
            contact_id,
            (message,),
            advance_memory_revision=advance_memory_revision,
        )

    async def recent_messages(
        self,
        contact_id: int,
        *,
        limit: int = 1000,
    ) -> list[MemoryMessage]:
        safe_limit = max(1, min(int(limit), 10000))

        def operation(connection: sqlite3.Connection) -> list[MemoryMessage]:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE contact_id = ? AND pending = 0
                    ORDER BY source_time DESC, source_uid DESC
                    LIMIT ?
                )
                ORDER BY source_time, source_uid
                """,
                (contact_id, safe_limit),
            ).fetchall()
            return [_message_from_row(row) for row in rows]

        return await self._read(operation)

    async def relevant_older_messages(
        self,
        contact_id: int,
        *,
        terms: Iterable[str],
        before_source_time: float,
        before_source_uid: str,
        limit: int = 120,
    ) -> list[MemoryMessage]:
        cleaned = tuple(
            term.strip()
            for term in terms
            if term.strip() and len(term.strip()) >= 2
        )[:8]
        if not cleaned or before_source_time < 0 or not before_source_uid:
            return []
        safe_limit = max(1, min(int(limit), 500))

        def operation(connection: sqlite3.Connection) -> list[MemoryMessage]:
            clauses = " OR ".join(
                "(content LIKE ? OR semantic_content LIKE ?)"
                for _ in cleaned
            )
            params: list[Any] = [
                contact_id,
                float(before_source_time),
                float(before_source_time),
                before_source_uid,
            ]
            for term in cleaned:
                pattern = f"%{term}%"
                params.extend((pattern, pattern))
            params.append(safe_limit)
            rows = connection.execute(
                f"""
                SELECT * FROM messages
                WHERE contact_id = ?
                  AND pending = 0
                  AND (
                      source_time < ?
                      OR (source_time = ? AND source_uid < ?)
                  )
                  AND ({clauses})
                ORDER BY source_time DESC, source_uid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            rows.reverse()
            return [_message_from_row(row) for row in rows]

        return await self._read(operation)

    async def reconcile_pending_outputs(
        self,
        contact_id: int,
        *,
        stale_after_seconds: float = 300,
    ) -> tuple[int, bool]:
        """Confirm split WeFlow sends and detect canceled/changed generations."""

        def operation(connection: sqlite3.Connection) -> tuple[int, bool]:
            contact_state = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                return 0, False
            pending_rows = connection.execute(
                """
                SELECT id, source_time, content, id_quality
                FROM messages
                WHERE contact_id = ?
                  AND direction = 'out'
                  AND origin = 'generated'
                  AND pending = 1
                ORDER BY source_time, id
                """,
                (contact_id,),
            ).fetchall()
            if not pending_rows:
                return 0, False
            confirmed = connection.execute(
                """
                SELECT id, source_time, content
                FROM messages
                WHERE contact_id = ?
                  AND direction = 'out'
                  AND origin = 'weflow'
                  AND pending = 0
                ORDER BY source_time, source_uid
                """,
                (contact_id,),
            ).fetchall()
            latest_incoming_row = connection.execute(
                """
                SELECT MAX(source_time)
                FROM messages
                WHERE contact_id = ?
                  AND direction = 'in'
                  AND origin = 'weflow'
                  AND pending = 0
                """,
                (contact_id,),
            ).fetchone()
            latest_incoming = (
                float(latest_incoming_row[0])
                if latest_incoming_row and latest_incoming_row[0] is not None
                else 0.0
            )
            resolved = 0
            dirty = False
            now = time.time()
            used_ids: set[int] = set()
            for pending_index, pending in enumerate(pending_rows):
                target = _equivalence_text(str(pending["content"]))
                if not target:
                    continue
                lower = float(pending["source_time"]) - 10
                if str(pending["id_quality"]) == "fallback_response":
                    # A slow fallback can finish before a newer Qwen request
                    # but reach WeFlow after that newer response. Match by its
                    # full normalized content across the normal send window
                    # instead of truncating at the next generated timestamp.
                    upper = float(pending["source_time"]) + max(
                        900.0,
                        float(stale_after_seconds),
                    )
                elif pending_index + 1 < len(pending_rows):
                    upper = float(pending_rows[pending_index + 1]["source_time"])
                else:
                    upper = float(pending["source_time"]) + max(
                        900.0,
                        float(stale_after_seconds),
                    )
                candidates = [
                    row
                    for row in confirmed
                    if int(row["id"]) not in used_ids
                    and lower <= float(row["source_time"]) < upper
                ]
                matched_ids: list[int] | None = None
                partial_prefix = False
                for start_index, row in enumerate(candidates):
                    assembled = ""
                    ids: list[int] = []
                    for candidate in candidates[start_index:]:
                        assembled += _equivalence_text(str(candidate["content"]))
                        ids.append(int(candidate["id"]))
                        if assembled == target:
                            matched_ids = ids
                            break
                        if target.startswith(assembled):
                            partial_prefix = True
                            continue
                        break
                    if matched_ids is not None:
                        break
                if matched_ids is not None:
                    connection.execute(
                        "DELETE FROM messages WHERE id = ?",
                        (int(pending["id"]),),
                    )
                    if str(pending["id_quality"]) == "fallback_response":
                        # Split UIA sends are reconciled here rather than in
                        # upsert_messages(). A newer clean cloud session still
                        # must be rebuilt so it receives the fallback turn.
                        connection.execute(
                            """
                            UPDATE qwen_sessions SET dirty = 1
                            WHERE contact_id = ?
                            """,
                            (contact_id,),
                        )
                        connection.execute(
                            """
                            UPDATE contacts
                            SET memory_revision = memory_revision + 1
                            WHERE id = ?
                            """,
                            (contact_id,),
                        )
                    used_ids.update(matched_ids)
                    resolved += 1
                    continue
                stale = (
                    now - float(pending["source_time"])
                    >= max(30.0, float(stale_after_seconds))
                )
                superseded_by_incoming = (
                    latest_incoming > float(pending["source_time"])
                )
                if (
                    (candidates and not partial_prefix)
                    or stale
                    or superseded_by_incoming
                ):
                    connection.execute(
                        "DELETE FROM messages WHERE id = ?",
                        (int(pending["id"]),),
                    )
                    dirty = True
            if dirty:
                connection.execute(
                    "UPDATE qwen_sessions SET dirty = 1 WHERE contact_id = ?",
                    (contact_id,),
                )
                connection.execute(
                    """
                    UPDATE contacts
                    SET memory_revision = memory_revision + 1
                    WHERE id = ?
                    """,
                    (contact_id,),
                )
            return resolved, dirty

        return await self._write(operation)

    async def get_summary(self, contact_id: int) -> tuple[str, int] | None:
        def operation(connection: sqlite3.Connection) -> tuple[str, int] | None:
            row = connection.execute(
                "SELECT content, through_message_id FROM summaries WHERE contact_id = ?",
                (contact_id,),
            ).fetchone()
            if not row:
                return None
            return str(row["content"]), int(row["through_message_id"])

        return await self._read(operation)

    async def save_summary(
        self,
        contact_id: int,
        *,
        content: str,
        through_message_id: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            contact_state = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                return
            connection.execute(
                """
                INSERT INTO summaries(
                    contact_id, content, through_message_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_id) DO UPDATE SET
                    content = excluded.content,
                    through_message_id = excluded.through_message_id,
                    updated_at = excluded.updated_at
                """,
                (contact_id, content, through_message_id, time.time()),
            )

        await self._write(operation)

    async def get_sync_state(self, contact_id: int) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM sync_state WHERE contact_id = ?",
                (contact_id,),
            ).fetchone()
            if not row:
                return {
                    "cursor_time": 0.0,
                    "cursor_uid": "",
                    "backfill_offset": 0,
                    "backfill_end_time": 0.0,
                    "full_backfill_complete": False,
                    "last_sync_at": 0.0,
                }
            return {
                "cursor_time": float(row["cursor_time"]),
                "cursor_uid": str(row["cursor_uid"]),
                "backfill_offset": int(row["backfill_offset"]),
                "backfill_end_time": float(row["backfill_end_time"]),
                "full_backfill_complete": bool(row["full_backfill_complete"]),
                "last_sync_at": float(row["last_sync_at"]),
            }

        return await self._read(operation)

    async def update_sync_state(
        self,
        contact_id: int,
        *,
        cursor_time: float,
        cursor_uid: str,
        backfill_offset: int | None = None,
        backfill_end_time: float | None = None,
        full_backfill_complete: bool | None = None,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            contact_state = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                return
            current = connection.execute(
                "SELECT * FROM sync_state WHERE contact_id = ?",
                (contact_id,),
            ).fetchone()
            offset = (
                int(backfill_offset)
                if backfill_offset is not None
                else int(current["backfill_offset"]) if current else 0
            )
            end_time = (
                float(backfill_end_time)
                if backfill_end_time is not None
                else float(current["backfill_end_time"]) if current else 0.0
            )
            complete = (
                int(full_backfill_complete)
                if full_backfill_complete is not None
                else int(current["full_backfill_complete"]) if current else 0
            )
            connection.execute(
                """
                INSERT INTO sync_state(
                    contact_id, cursor_time, cursor_uid, backfill_offset,
                    backfill_end_time, full_backfill_complete, last_sync_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contact_id) DO UPDATE SET
                    cursor_time = excluded.cursor_time,
                    cursor_uid = excluded.cursor_uid,
                    backfill_offset = excluded.backfill_offset,
                    backfill_end_time = excluded.backfill_end_time,
                    full_backfill_complete = excluded.full_backfill_complete,
                    last_sync_at = excluded.last_sync_at
                """,
                (
                    contact_id,
                    float(cursor_time),
                    cursor_uid,
                    max(0, offset),
                    max(0.0, end_time),
                    complete,
                    time.time(),
                ),
            )

        await self._write(operation)

    async def active_qwen_session(
        self,
        contact_id: int,
    ) -> QwenSessionRecord | None:
        def operation(connection: sqlite3.Connection) -> QwenSessionRecord | None:
            row = connection.execute(
                """
                SELECT * FROM qwen_sessions
                WHERE contact_id = ?
                ORDER BY last_used_at DESC, id DESC
                LIMIT 1
                """,
                (contact_id,),
            ).fetchone()
            return _session_from_row(row) if row else None

        return await self._read(operation)

    async def create_qwen_session(
        self,
        contact_id: int,
        *,
        conversation_id: str,
        model: str,
        persona_hash: str,
        tool_hash: str,
        created_at: float,
        expires_at: float,
        estimated_tokens: int,
        expected_memory_revision: int,
    ) -> QwenSessionRecord:
        def operation(connection: sqlite3.Connection) -> QwenSessionRecord:
            contact_state = connection.execute(
                """
                SELECT tombstoned_at, memory_revision
                FROM contacts
                WHERE id = ?
                """,
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                raise RuntimeError("contact memory is tombstoned")
            current_revision = int(contact_state["memory_revision"])
            if current_revision != int(expected_memory_revision):
                raise MemoryRevisionChanged(
                    "contact history changed while creating Qwen session"
                )
            connection.execute(
                "UPDATE qwen_sessions SET dirty = 1 WHERE contact_id = ?",
                (contact_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO qwen_sessions(
                    contact_id, conversation_id, model, persona_hash,
                    tool_hash, memory_revision, created_at, expires_at,
                    estimated_tokens, last_response_id, last_used_at, dirty
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1)
                """,
                (
                    contact_id,
                    conversation_id,
                    model,
                    persona_hash,
                    tool_hash,
                    current_revision,
                    created_at,
                    expires_at,
                    max(0, int(estimated_tokens)),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM qwen_sessions WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            if not row:
                raise sqlite3.DatabaseError("Qwen session insert returned no row")
            return _session_from_row(row)

        return await self._write(operation)

    async def activate_qwen_session(
        self,
        session_id: int,
        *,
        expected_memory_revision: int,
    ) -> QwenSessionRecord:
        def operation(connection: sqlite3.Connection) -> QwenSessionRecord:
            cursor = connection.execute(
                """
                UPDATE qwen_sessions
                SET dirty = 0, last_used_at = ?
                WHERE id = ?
                  AND dirty = 1
                  AND memory_revision = ?
                  AND EXISTS (
                      SELECT 1
                      FROM contacts
                      WHERE contacts.id = qwen_sessions.contact_id
                        AND contacts.tombstoned_at IS NULL
                        AND contacts.memory_revision = ?
                  )
                """,
                (
                    time.time(),
                    session_id,
                    int(expected_memory_revision),
                    int(expected_memory_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryRevisionChanged(
                    "contact history changed while seeding Qwen session"
                )
            connection.execute(
                """
                UPDATE contacts
                SET next_seed_recent_limit = 0
                WHERE id = (
                    SELECT contact_id
                    FROM qwen_sessions
                    WHERE id = ?
                )
                  AND memory_revision = ?
                """,
                (session_id, int(expected_memory_revision)),
            )
            row = connection.execute(
                "SELECT * FROM qwen_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise sqlite3.DatabaseError("activated Qwen session is missing")
            return _session_from_row(row)

        return await self._write(operation)

    async def update_qwen_session_usage(
        self,
        session_id: int,
        *,
        request_key: str,
        estimated_tokens: int,
        response_id: str,
        pending_owner: str = "",
        pending_call_ids: Iterable[str] = (),
    ) -> None:
        pending_ids = tuple(str(value) for value in pending_call_ids if str(value))

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE qwen_sessions SET
                    estimated_tokens = ?,
                    last_response_id = ?,
                    last_used_at = ?,
                    pending_owner = ?,
                    pending_call_ids_json = ?
                WHERE id = ? AND dirty = 0 AND pending_owner = ?
                """,
                (
                    max(0, int(estimated_tokens)),
                    response_id,
                    time.time(),
                    str(pending_owner),
                    json.dumps(pending_ids, ensure_ascii=False),
                    session_id,
                    str(request_key),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Qwen response no longer owns this session")

        await self._write(operation)

    async def set_qwen_session_inflight(
        self,
        session_id: int,
        *,
        request_key: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE qwen_sessions SET
                    pending_owner = ?,
                    pending_call_ids_json = '[]',
                    last_used_at = ?
                WHERE id = ? AND dirty = 0
                """,
                (request_key, time.time(), session_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Qwen session is no longer active")

        await self._write(operation)

    async def abandon_qwen_pending(
        self,
        session_id: int,
        *,
        request_key: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE qwen_sessions SET dirty = 1
                WHERE id = ? AND dirty = 0 AND pending_owner = ?
                """,
                (session_id, request_key),
            )
            return cursor.rowcount == 1

        return await self._write(operation)

    async def mark_qwen_session_dirty(self, session_id: int) -> None:
        await self._write(
            lambda connection: connection.execute(
                "UPDATE qwen_sessions SET dirty = 1 WHERE id = ?",
                (session_id,),
            )
        )

    async def mark_contact_sessions_dirty(self, contact_id: int) -> None:
        await self._write(
            lambda connection: connection.execute(
                "UPDATE qwen_sessions SET dirty = 1 WHERE contact_id = ?",
                (contact_id,),
            )
        )

    async def invalidate_unconfirmed_outputs(self, contact_id: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            contact_state = connection.execute(
                "SELECT tombstoned_at FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact_state is None or contact_state["tombstoned_at"] is not None:
                return 0
            cursor = connection.execute(
                """
                DELETE FROM messages
                WHERE contact_id = ?
                  AND direction = 'out'
                  AND origin = 'generated'
                  AND pending = 1
                """,
                (contact_id,),
            )
            connection.execute(
                "UPDATE qwen_sessions SET dirty = 1 WHERE contact_id = ?",
                (contact_id,),
            )
            connection.execute(
                """
                UPDATE contacts
                SET memory_revision = memory_revision + 1,
                    next_seed_recent_limit = ?
                WHERE id = ?
                """,
                (SEND_FAILURE_REBUILD_MESSAGE_LIMIT, contact_id),
            )
            return max(cursor.rowcount, 0)

        return await self._write(operation)

    async def fail_qwen_response(
        self,
        contact_id: int,
        session_id: int,
        *,
        response_id: str = "",
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if response_id:
                connection.execute(
                    """
                    DELETE FROM messages
                    WHERE contact_id = ?
                      AND source_uid = ?
                      AND origin = 'generated'
                      AND pending = 1
                    """,
                    (contact_id, f"generated:{response_id}"),
                )
            connection.execute(
                "UPDATE qwen_sessions SET dirty = 1 WHERE id = ?",
                (session_id,),
            )

        await self._write(operation)

    async def record_qwen_items(
        self,
        session_id: int,
        item_ids: Iterable[str],
    ) -> None:
        ids = tuple(item_id for item_id in item_ids if item_id)
        if not ids:
            return

        def operation(connection: sqlite3.Connection) -> None:
            now = time.time()
            connection.executemany(
                """
                INSERT OR IGNORE INTO qwen_items(
                    session_id, item_id, kind, created_at
                ) VALUES (?, ?, 'item', ?)
                """,
                ((session_id, item_id, now) for item_id in ids),
            )

        await self._write(operation)

    async def list_contact_conversations(self, contact_id: int) -> list[str]:
        return await self._read(
            lambda connection: [
                str(row["conversation_id"])
                for row in connection.execute(
                    """
                    SELECT conversation_id FROM qwen_sessions
                    WHERE contact_id = ?
                    ORDER BY created_at
                    """,
                    (contact_id,),
                ).fetchall()
            ]
        )

    async def forget_contact(self, contact_id: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM messages WHERE contact_id = ?", (contact_id,))
            connection.execute("DELETE FROM summaries WHERE contact_id = ?", (contact_id,))
            connection.execute("DELETE FROM sync_state WHERE contact_id = ?", (contact_id,))
            connection.execute(
                "DELETE FROM qwen_sessions WHERE contact_id = ?",
                (contact_id,),
            )
            connection.execute(
                """
                UPDATE contacts
                SET tombstoned_at = COALESCE(tombstoned_at, ?),
                    updated_at = ?,
                    routing_name = '',
                    aliases_json = '[]',
                    account_enc = '',
                    session_enc = ''
                WHERE id = ?
                """,
                (time.time(), time.time(), contact_id),
            )

        await self._write(operation)

    async def tombstone_contact(self, contact_id: int) -> None:
        """Block all future archive writes while privacy deletion is pending."""

        def operation(connection: sqlite3.Connection) -> None:
            now = time.time()
            connection.execute(
                """
                UPDATE contacts
                SET tombstoned_at = COALESCE(tombstoned_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, contact_id),
            )

        await self._write(operation)

    async def status(self, contact_id: int | None = None) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            result: dict[str, Any] = {
                "contacts": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM contacts WHERE tombstoned_at IS NULL"
                    ).fetchone()[0]
                ),
                "messages": int(
                    connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                ),
                "sessions": int(
                    connection.execute("SELECT COUNT(*) FROM qwen_sessions").fetchone()[0]
                ),
                "dirty_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM qwen_sessions WHERE dirty = 1"
                    ).fetchone()[0]
                ),
            }
            if contact_id is not None:
                result["contact_messages"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE contact_id = ?",
                        (contact_id,),
                    ).fetchone()[0]
                )
                sync = connection.execute(
                    "SELECT last_sync_at, full_backfill_complete FROM sync_state WHERE contact_id = ?",
                    (contact_id,),
                ).fetchone()
                result["last_sync_at"] = float(sync["last_sync_at"]) if sync else 0.0
                result["full_backfill_complete"] = (
                    bool(sync["full_backfill_complete"]) if sync else False
                )
            return result

        return await self._read(operation)
