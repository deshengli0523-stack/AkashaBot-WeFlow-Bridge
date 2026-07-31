"""Durable authority for merged private replies.

The reverse OneBot connection is transport only.  Epochs, admissions, UIA
jobs, quarantines and result notices live here so a reconnect or Bridge
restart cannot turn an unknown request into a second send.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
EPOCH_CAPACITY = 2048
JOB_CAPACITY = 2048
JOB_HIGH_WATER = 2016
ADMISSION_CAPACITY = 32
OUTBOX_CAPACITY = 4096
OUTBOX_HIGH_WATER = 4000
OUTBOX_BYTES_CAPACITY = 64 * 1024 * 1024
OUTBOX_BYTES_HIGH_WATER = 60 * 1024 * 1024
RAW_ENVELOPE_CAPACITY = 2048
RAW_ENVELOPE_HIGH_WATER = 2016
RAW_ENVELOPE_BYTES_CAPACITY = 10 * 1024 * 1024
RAW_ENVELOPE_BYTES_HIGH_WATER = 9 * 1024 * 1024
RESULT_RESERVATION_BYTES = 20 * 1024
TERMINAL_TTL_SECONDS = 24 * 60 * 60

TERMINAL_OUTCOMES = {
    "sent",
    "superseded",
    "manual_cancel",
    "failed",
    "commit_unknown",
}
ACTIVE_JOB_STATES = {
    "queued",
    "preview_before_paste",
    "ui_selected",
    "pasted_owned",
    "committing",
    "committed",
}


class ReplyStoreError(RuntimeError):
    """A stable, privacy-safe store error surfaced to protocol code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ReplyStore:
    """Small SQLite WAL with transaction-sized public operations."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.path.abspath(os.fspath(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                value = operation(connection)
                connection.execute("COMMIT")
                return value
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def _read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            connection = self._connect()
            try:
                return operation(connection)
            finally:
                connection.close()

    def _initialize(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS epochs (
                    target_id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admissions (
                    token TEXT PRIMARY KEY,
                    target_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    plugin_instance_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS admissions_target_state
                    ON admissions(target_id, state);
                CREATE TABLE IF NOT EXISTS jobs (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    plugin_instance_id TEXT NOT NULL,
                    admission_token TEXT NOT NULL UNIQUE,
                    routing_name TEXT NOT NULL,
                    account TEXT NOT NULL,
                    session TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_stage TEXT NOT NULL DEFAULT '',
                    draft_quarantined INTEGER NOT NULL DEFAULT 0,
                    committed INTEGER NOT NULL DEFAULT 0,
                    result_revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    commit_attempted_at REAL
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created
                    ON jobs(state, created_at);
                CREATE INDEX IF NOT EXISTS jobs_target_active
                    ON jobs(target_id, state);
                CREATE TABLE IF NOT EXISTS outbox (
                    notice_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    acked INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_attempt_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(request_id, revision)
                );
                CREATE INDEX IF NOT EXISTS outbox_pending
                    ON outbox(acked, updated_at);
                CREATE TABLE IF NOT EXISTS quarantines (
                    target_id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingress_batches (
                    batch_id TEXT PRIMARY KEY,
                    target_id INTEGER,
                    generation INTEGER,
                    epoch INTEGER,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    admission_token TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ingress_batches_state_created
                    ON ingress_batches(state, created_at);
                CREATE TABLE IF NOT EXISTS raw_dedupe (
                    raw_id_hash TEXT PRIMARY KEY,
                    accepted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_envelopes (
                    raw_id_hash TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    accepted_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS raw_envelopes_pending
                    ON raw_envelopes(state,accepted_at);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

        with self._lock:
            connection = self._connect()
            try:
                operation(connection)
            finally:
                connection.close()

    # ------------------------------------------------------------------
    # Process generation and target epoch

    def allocate_generation(self) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            generation = current + 1
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('generation', ?)",
                (str(generation),),
            )
            self._recover_prior_generation(connection, generation)
            return generation

        return int(self._write(operation))

    def _recover_prior_generation(
        self,
        connection: sqlite3.Connection,
        generation: int,
    ) -> None:
        now = time.time()
        rows = connection.execute(
            """
            SELECT * FROM jobs
            WHERE generation < ? AND state IN (
                'queued','preview_before_paste','ui_selected',
                'pasted_owned','committing','committed'
            )
            """,
            (generation,),
        ).fetchall()
        for row in rows:
            state = str(row["state"])
            if state == "committed":
                outcome, code, stage, committed = "sent", "", "complete", 1
            elif state == "committing":
                outcome, code, stage, committed = (
                    "commit_unknown",
                    "E_UIA_COMMIT_UNKNOWN",
                    "submit",
                    1,
                )
            elif state == "pasted_owned":
                outcome, code, stage, committed = (
                    "failed",
                    "E_UIA_DRAFT_RECOVERY_REQUIRED",
                    "paste",
                    0,
                )
                connection.execute(
                    """
                    INSERT INTO quarantines(target_id, request_id, reason, created_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(target_id) DO UPDATE SET
                        request_id=excluded.request_id,
                        reason=excluded.reason,
                        created_at=excluded.created_at
                    """,
                    (
                        int(row["target_id"]),
                        str(row["request_id"]),
                        "E_UIA_DRAFT_RECOVERY_REQUIRED",
                        now,
                    ),
                )
            else:
                outcome, code, stage, committed = (
                    "failed",
                    "E_UIA_STOPPED",
                    str(row["stage"] or "complete"),
                    0,
                )
            self._terminalize_row(
                connection,
                row,
                outcome=outcome,
                error_code=code,
                error_stage=stage,
                committed=bool(committed),
                draft_quarantined=state == "pasted_owned",
                now=now,
            )
        connection.execute(
            """
            UPDATE admissions
            SET state='released', outcome='failed', reason='E_BRIDGE_RESTARTED',
                updated_at=?
            WHERE generation < ? AND state='active'
            """,
            (now, generation),
        )

    def advance_epoch(self, target_id: int, generation: int) -> int:
        target_id = _positive_int(target_id) or 0
        generation = _positive_int(generation) or 0
        if not target_id or not generation:
            raise ValueError("invalid reply epoch identity")

        def operation(connection: sqlite3.Connection) -> int:
            return self._advance_epoch_in_transaction(
                connection,
                target_id,
                generation,
                time.time(),
            )

        return int(self._write(operation))

    def accept_raw_and_advance_epoch(
        self,
        target_id: int,
        generation: int,
        raw_id: str,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """Atomically dedupe one ordinary rawid and advance its target epoch."""

        if not raw_id:
            return self.advance_epoch(target_id, generation)
        raw_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()

        def operation(connection: sqlite3.Connection) -> int | None:
            if connection.execute(
                "SELECT 1 FROM raw_dedupe WHERE raw_id_hash=?",
                (raw_hash,),
            ).fetchone():
                return None
            cutoff = time.time() - TERMINAL_TTL_SECONDS
            connection.execute(
                "DELETE FROM raw_dedupe WHERE accepted_at<?", (cutoff,)
            )
            dedupe_count = int(
                connection.execute("SELECT COUNT(*) FROM raw_dedupe").fetchone()[0]
            )
            if dedupe_count >= 65536:
                raise ReplyStoreError("E_INGRESS_SPOOL_CAPACITY")
            if payload is not None:
                pending = connection.execute(
                    """
                    SELECT COUNT(*),
                           COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0)
                    FROM raw_envelopes
                    WHERE state IN ('accepted','buffered')
                    """
                ).fetchone()
                payload_bytes = len(_canonical_json(payload).encode("utf-8"))
                if (
                    int(pending[0]) >= RAW_ENVELOPE_CAPACITY
                    or int(pending[1]) + payload_bytes
                    > RAW_ENVELOPE_BYTES_CAPACITY
                ):
                    raise ReplyStoreError("E_INGRESS_SPOOL_CAPACITY")
            now = time.time()
            epoch = self._advance_epoch_in_transaction(
                connection,
                target_id,
                generation,
                now,
            )
            connection.execute(
                "INSERT INTO raw_dedupe(raw_id_hash,accepted_at) VALUES(?,?)",
                (raw_hash, now),
            )
            if payload is not None:
                connection.execute(
                    """
                    INSERT INTO raw_envelopes(
                        raw_id_hash,payload_json,target_id,generation,epoch,
                        state,accepted_at,updated_at
                    ) VALUES(?,?,?,?,?,'accepted',?,?)
                    """,
                    (
                        raw_hash,
                        _canonical_json(payload),
                        target_id,
                        generation,
                        epoch,
                        now,
                        now,
                    ),
                )
            return epoch

        return self._write(operation)

    def pending_raw_envelopes(self, limit: int = 2048) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT * FROM raw_envelopes
                WHERE state IN ('accepted','buffered')
                ORDER BY accepted_at,raw_id_hash LIMIT ?
                """,
                (max(1, min(int(limit), 2048)),),
            ).fetchall()
            return [
                {**dict(row), "payload": json.loads(str(row["payload_json"]))}
                for row in rows
            ]

        return self._read(operation)

    def rebase_raw_envelope(
        self,
        raw_id_hash: str,
        generation: int,
    ) -> dict[str, Any] | None:
        """Move a crash-recovered raw envelope into the new lifecycle."""

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT * FROM raw_envelopes
                WHERE raw_id_hash=? AND state IN ('accepted','buffered')
                """,
                (raw_id_hash,),
            ).fetchone()
            if row is None:
                return None
            if int(row["generation"]) == generation:
                return dict(row)
            now = time.time()
            epoch = self._advance_epoch_in_transaction(
                connection,
                int(row["target_id"]),
                generation,
                now,
            )
            connection.execute(
                """
                UPDATE raw_envelopes SET generation=?,epoch=?,updated_at=?
                WHERE raw_id_hash=?
                """,
                (generation, epoch, now, raw_id_hash),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM raw_envelopes WHERE raw_id_hash=?",
                    (raw_id_hash,),
                ).fetchone()
            )

        return self._write(operation)

    def mark_raw_envelope_buffered(self, raw_id: str) -> bool:
        if not raw_id:
            return False
        raw_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE raw_envelopes SET state='buffered',updated_at=?
                WHERE raw_id_hash=? AND state IN ('accepted','buffered')
                """,
                (time.time(), raw_hash),
            )
            return cursor.rowcount == 1

        return bool(self._write(operation))

    def close_raw_envelopes(self, raw_ids: Iterable[str]) -> None:
        hashes = [
            hashlib.sha256(str(raw_id).encode("utf-8")).hexdigest()
            for raw_id in raw_ids
            if str(raw_id)
        ]
        if not hashes:
            return

        def operation(connection: sqlite3.Connection) -> None:
            now = time.time()
            connection.executemany(
                """
                UPDATE raw_envelopes SET state='closed',updated_at=?
                WHERE raw_id_hash=?
                """,
                ((now, raw_hash) for raw_hash in hashes),
            )

        self._write(operation)

    def _advance_epoch_in_transaction(
        self,
        connection: sqlite3.Connection,
        target_id: int,
        generation: int,
        now: float,
    ) -> int:
        row = connection.execute(
            "SELECT generation, epoch FROM epochs WHERE target_id=?",
            (target_id,),
        ).fetchone()
        if row is None:
            epoch_count = int(
                connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
            )
            if epoch_count >= EPOCH_CAPACITY:
                cutoff = now - TERMINAL_TTL_SECONDS
                victim = connection.execute(
                    """
                    SELECT epochs.target_id FROM epochs
                    WHERE epochs.updated_at<?
                      AND NOT EXISTS (
                        SELECT 1 FROM admissions
                        WHERE admissions.target_id=epochs.target_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.target_id=epochs.target_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM quarantines
                        WHERE quarantines.target_id=epochs.target_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM ingress_batches
                        WHERE ingress_batches.target_id=epochs.target_id
                          AND ingress_batches.state='pending'
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM raw_envelopes
                        WHERE raw_envelopes.target_id=epochs.target_id
                          AND raw_envelopes.state IN ('accepted','buffered')
                      )
                    ORDER BY epochs.updated_at,epochs.target_id LIMIT 1
                    """,
                    (cutoff,),
                ).fetchone()
                if victim is None:
                    raise ReplyStoreError("E_EPOCH_CAPACITY")
                connection.execute(
                    "DELETE FROM epochs WHERE target_id=?",
                    (int(victim["target_id"]),),
                )
        if row is not None and int(row["generation"]) > generation:
            raise ReplyStoreError("E_STALE_BRIDGE_GENERATION")
        epoch = (
            int(row["epoch"]) + 1
            if row is not None and int(row["generation"]) == generation
            else 1
        )
        connection.execute(
            """
            INSERT INTO epochs(target_id,generation,epoch,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(target_id) DO UPDATE SET
                generation=excluded.generation,
                epoch=excluded.epoch,
                updated_at=excluded.updated_at
            """,
            (target_id, generation, epoch, now),
        )
        connection.execute(
            """
            UPDATE admissions
            SET state='superseded_before_action',
                outcome='superseded', reason='E_REPLY_SUPERSEDED', updated_at=?
            WHERE target_id=? AND generation=? AND epoch<? AND state='active'
            """,
            (now, target_id, generation, epoch),
        )
        queued = connection.execute(
            """
            SELECT * FROM jobs
            WHERE target_id=? AND generation=? AND epoch<? AND state='queued'
            """,
            (target_id, generation, epoch),
        ).fetchall()
        for job in queued:
            self._terminalize_row(
                connection,
                job,
                outcome="superseded",
                error_code="E_UIA_REPLY_SUPERSEDED",
                error_stage="preflight",
                committed=False,
                draft_quarantined=False,
                now=now,
            )
        return epoch

    def current_epoch(self, target_id: int, generation: int) -> int | None:
        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "SELECT generation,epoch FROM epochs WHERE target_id=?",
                (target_id,),
            ).fetchone()
            if row is None or int(row["generation"]) != generation:
                return None
            return int(row["epoch"])

        return self._read(operation)

    # ------------------------------------------------------------------
    # Admissions

    def create_admission(
        self,
        *,
        target_id: int,
        generation: int,
        epoch: int,
        plugin_instance_id: str,
    ) -> str:
        token = secrets.token_urlsafe(32)

        def operation(connection: sqlite3.Connection) -> str:
            now = time.time()
            current = connection.execute(
                "SELECT generation,epoch FROM epochs WHERE target_id=?",
                (target_id,),
            ).fetchone()
            if (
                current is None
                or int(current["generation"]) != generation
                or int(current["epoch"]) != epoch
            ):
                raise ReplyStoreError("E_REPLY_SUPERSEDED")
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE state='active'"
                ).fetchone()[0]
            )
            if active_count >= ADMISSION_CAPACITY:
                raise ReplyStoreError("E_ADMISSION_CAPACITY")
            older = connection.execute(
                """
                SELECT token FROM admissions
                WHERE target_id=? AND state='active'
                """,
                (target_id,),
            ).fetchall()
            for row in older:
                connection.execute(
                    """
                    UPDATE admissions
                    SET state='superseded_before_action', outcome='superseded',
                        reason='E_REPLY_SUPERSEDED', updated_at=?
                    WHERE token=?
                    """,
                    (now, str(row["token"])),
                )
            if not self._capacity_open(connection, include_reserved=1):
                raise ReplyStoreError("E_OB_STATE_CAPACITY")
            if not self._outbox_capacity_open(
                connection,
                include_admissions=1,
            ):
                raise ReplyStoreError("E_OUTBOX_CAPACITY")
            connection.execute(
                """
                INSERT INTO admissions(
                    token,target_id,generation,epoch,plugin_instance_id,state,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,'active',?,?)
                """,
                (
                    token,
                    target_id,
                    generation,
                    epoch,
                    plugin_instance_id,
                    now,
                    now,
                ),
            )
            return token

        return str(self._write(operation))

    def admission_status(self, token: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM admissions WHERE token=?", (token,)
            ).fetchone()
            return None if row is None else dict(row)

        return self._read(operation)

    def finish_admission(
        self,
        token: str,
        *,
        plugin_instance_id: str,
        generation: int,
        outcome: str,
        reason: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        if outcome not in {"released", "failed", "superseded"}:
            raise ReplyStoreError("E_OB_INVALID_REQUEST")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM admissions WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                raise ReplyStoreError("E_ADMISSION_INVALID")
            if (
                str(row["plugin_instance_id"]) != plugin_instance_id
                or int(row["generation"]) != generation
            ):
                raise ReplyStoreError("E_ADMISSION_INVALID")
            if str(row["state"]) == "consumed":
                return dict(row)
            if str(row["state"]) == "superseded_before_action":
                return dict(row)
            now = time.time()
            state = "released" if outcome == "released" else "terminal"
            connection.execute(
                """
                UPDATE admissions SET state=?,request_id=?,outcome=?,reason=?,updated_at=?
                WHERE token=?
                """,
                (state, request_id, outcome, reason, now, token),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM admissions WHERE token=?", (token,)
                ).fetchone()
            )

        return self._write(operation)

    def has_active_admission(self, target_id: int) -> bool:
        return bool(
            self._read(
                lambda connection: connection.execute(
                    """
                    SELECT 1 FROM admissions
                    WHERE target_id=? AND state='active' LIMIT 1
                    """,
                    (target_id,),
                ).fetchone()
            )
        )

    def release_foreign_admissions(
        self,
        generation: int,
        plugin_instance_id: str,
    ) -> None:
        now = time.time()
        self._write(
            lambda connection: connection.execute(
                """
                UPDATE admissions
                SET state='released',outcome='failed',
                    reason='E_PLUGIN_INSTANCE_CHANGED',updated_at=?
                WHERE generation=? AND state='active' AND plugin_instance_id<>?
                """,
                (now, generation, plugin_instance_id),
            )
        )

    # ------------------------------------------------------------------
    # Idempotent request -> durable job

    def lookup_request(
        self,
        request_id: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Consult idempotency before any live lease/epoch decision."""

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            if str(row["fingerprint"]) != fingerprint:
                raise ReplyStoreError("E_OB_IDEMPOTENCY_CONFLICT")
            return self._job_response(row, cached=True)

        return self._read(operation)

    def accept_job(
        self,
        *,
        request_id: str,
        fingerprint: str,
        target_id: int,
        generation: int,
        epoch: int,
        text: str,
        plugin_instance_id: str,
        admission_token: str,
        routing_name: str,
        account: str,
        session: str,
    ) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["fingerprint"]) != fingerprint:
                    raise ReplyStoreError("E_OB_IDEMPOTENCY_CONFLICT")
                return self._job_response(existing, cached=True)

            admission = connection.execute(
                "SELECT * FROM admissions WHERE token=?", (admission_token,)
            ).fetchone()
            if admission is None:
                raise ReplyStoreError("E_ADMISSION_INVALID")
            if (
                str(admission["plugin_instance_id"]) != plugin_instance_id
                or int(admission["target_id"]) != target_id
                or int(admission["generation"]) != generation
                or int(admission["epoch"]) != epoch
            ):
                raise ReplyStoreError("E_ADMISSION_INVALID")
            admission_state = str(admission["state"])
            if admission_state == "superseded_before_action":
                return {
                    "accepted": False,
                    "pre_action_terminal": True,
                    "outcome": "superseded",
                    "reason": str(admission["reason"] or "E_REPLY_SUPERSEDED"),
                    "request_id": request_id,
                    "cached": True,
                }
            if admission_state != "active":
                raise ReplyStoreError("E_ADMISSION_INVALID")
            current = connection.execute(
                "SELECT generation,epoch FROM epochs WHERE target_id=?",
                (target_id,),
            ).fetchone()
            if (
                current is None
                or int(current["generation"]) != generation
                or int(current["epoch"]) != epoch
            ):
                now = time.time()
                connection.execute(
                    """
                    UPDATE admissions SET state='terminal',request_id=?,
                        outcome='superseded',reason='E_REPLY_SUPERSEDED',updated_at=?
                    WHERE token=?
                    """,
                    (request_id, now, admission_token),
                )
                return {
                    "accepted": False,
                    "pre_action_terminal": True,
                    "outcome": "superseded",
                    "reason": "E_REPLY_SUPERSEDED",
                    "request_id": request_id,
                    "cached": False,
                }
            if connection.execute(
                "SELECT 1 FROM quarantines WHERE target_id=?", (target_id,)
            ).fetchone():
                raise ReplyStoreError("E_UIA_DRAFT_QUARANTINED")
            if not self._capacity_open(connection):
                raise ReplyStoreError("E_OB_STATE_CAPACITY")
            now = time.time()
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO jobs(
                    request_id,fingerprint,target_id,generation,epoch,text,
                    text_sha256,plugin_instance_id,admission_token,routing_name,
                    account,session,state,stage,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued','preflight',?,?)
                """,
                (
                    request_id,
                    fingerprint,
                    target_id,
                    generation,
                    epoch,
                    text,
                    text_sha,
                    plugin_instance_id,
                    admission_token,
                    routing_name,
                    account,
                    session,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE admissions SET state='consumed',request_id=?,updated_at=?
                WHERE token=?
                """,
                (request_id, now, admission_token),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            return self._job_response(row, cached=False)

        return self._write(operation)

    @staticmethod
    def _job_response(row: sqlite3.Row, *, cached: bool) -> dict[str, Any]:
        outcome = str(row["outcome"])
        if outcome:
            response: dict[str, Any] = {
                "accepted": True,
                "request_id": str(row["request_id"]),
                "outcome": outcome,
                "committed": bool(row["committed"]),
                "cached": cached,
            }
            if row["error_code"]:
                response["error_code"] = str(row["error_code"])
                response["error_stage"] = str(row["error_stage"])
            return response
        return {
            "accepted": True,
            "request_id": str(row["request_id"]),
            "outcome": "accepted",
            "committed": False,
            "cached": cached,
        }

    def claim_next_job(self, generation: int) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE generation=? AND state='queued'
                ORDER BY created_at, request_id LIMIT 1
                """,
                (generation,),
            ).fetchone()
            if row is None:
                return None
            current = connection.execute(
                "SELECT generation,epoch FROM epochs WHERE target_id=?",
                (int(row["target_id"]),),
            ).fetchone()
            now = time.time()
            if (
                current is None
                or int(current["generation"]) != int(row["generation"])
                or int(current["epoch"]) != int(row["epoch"])
            ):
                self._terminalize_row(
                    connection,
                    row,
                    outcome="superseded",
                    error_code="E_UIA_REPLY_SUPERSEDED",
                    error_stage="preflight",
                    committed=False,
                    draft_quarantined=False,
                    now=now,
                )
                return None
            connection.execute(
                """
                UPDATE jobs SET state='preview_before_paste',stage='before_paste',
                    updated_at=? WHERE request_id=? AND state='queued'
                """,
                (now, str(row["request_id"])),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?",
                (str(row["request_id"]),),
            ).fetchone()
            return dict(claimed)

        return self._write(operation)

    def set_job_stage(self, request_id: str, stage: str) -> bool:
        allowed = {
            "preview_before_paste",
            "ui_selected",
            "pasted_owned",
            "committing",
            "committed",
        }
        if stage not in allowed:
            return False

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT state FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or str(row["state"]) not in ACTIVE_JOB_STATES:
                return False
            now = time.time()
            connection.execute(
                """
                UPDATE jobs SET state=?,stage=?,updated_at=?,
                    commit_attempted_at=CASE WHEN ?='committing' THEN ?
                        ELSE commit_attempted_at END,
                    committed=CASE WHEN ?='committed' THEN 1 ELSE committed END
                WHERE request_id=?
                """,
                (stage, stage, now, stage, now, stage, request_id),
            )
            return True

        return bool(self._write(operation))

    def finish_job(
        self,
        request_id: str,
        *,
        outcome: str,
        error_code: str = "",
        error_stage: str = "",
        committed: bool = False,
        draft_quarantined: bool = False,
    ) -> dict[str, Any]:
        if outcome not in TERMINAL_OUTCOMES:
            raise ReplyStoreError("E_OB_INVALID_REQUEST")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReplyStoreError("E_OB_UNKNOWN_REQUEST")
            if str(row["outcome"]):
                return self._job_response(row, cached=True)
            now = time.time()
            self._terminalize_row(
                connection,
                row,
                outcome=outcome,
                error_code=error_code,
                error_stage=error_stage,
                committed=committed,
                draft_quarantined=draft_quarantined,
                now=now,
            )
            final = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            return self._job_response(final, cached=False)

        return self._write(operation)

    def resolve_commit_unknown(
        self,
        request_id: str,
        revision: int,
        resolution: str,
    ) -> bool:
        if resolution not in {"sent", "not_sent"}:
            return False

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if (
                row is None
                or str(row["outcome"]) != "commit_unknown"
                or int(row["result_revision"]) != revision
            ):
                return False
            self._terminalize_row(
                connection,
                row,
                outcome="sent" if resolution == "sent" else "failed",
                error_code="" if resolution == "sent" else "E_UIA_SUBMIT_FAILED",
                error_stage="complete" if resolution == "sent" else "submit",
                committed=resolution == "sent",
                draft_quarantined=bool(row["draft_quarantined"]),
                now=time.time(),
            )
            return True

        return bool(self._write(operation))

    def _terminalize_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        outcome: str,
        error_code: str,
        error_stage: str,
        committed: bool,
        draft_quarantined: bool,
        now: float,
    ) -> None:
        request_id = str(row["request_id"])
        revision = int(row["result_revision"] or 0) + 1
        connection.execute(
            """
            UPDATE jobs SET state='done',stage=?,outcome=?,error_code=?,
                error_stage=?,draft_quarantined=?,committed=?,
                result_revision=?,updated_at=? WHERE request_id=?
            """,
            (
                error_stage or "complete",
                outcome,
                error_code,
                error_stage,
                1 if draft_quarantined else 0,
                1 if committed else 0,
                revision,
                now,
                request_id,
            ),
        )
        payload = self._result_payload(
            row,
            outcome=outcome,
            revision=revision,
            error_code=error_code,
            error_stage=error_stage,
            committed=committed,
            draft_quarantined=draft_quarantined,
        )
        digest_source = {
            "schema": 2,
            "request_id": request_id,
            "revision": revision,
            "target_id": int(row["target_id"]),
            "generation": int(row["generation"]),
            "epoch": int(row["epoch"]),
            "outcome": outcome,
            "stage": error_stage or "complete",
            "text_sha256": str(row["text_sha256"]),
        }
        digest = hashlib.sha256(
            _canonical_json(digest_source).encode("utf-8")
        ).hexdigest()
        notice_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"akasha-send-result:{request_id}:{revision}",
            )
        )
        payload["notice_id"] = notice_id
        payload["result_revision"] = revision
        payload["result_digest"] = digest
        payload_json = _canonical_json(payload)
        if not self._outbox_capacity_open(
            connection,
            extra_bytes=len(payload_json.encode("utf-8")),
            consume_job_request_id=request_id,
        ):
            raise ReplyStoreError("E_OUTBOX_CAPACITY")
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox(
                notice_id,request_id,revision,digest,payload_json,payload_bytes,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                notice_id,
                request_id,
                revision,
                digest,
                payload_json,
                len(payload_json.encode("utf-8")),
                now,
                now,
            ),
        )

    @staticmethod
    def _result_payload(
        row: sqlite3.Row,
        *,
        outcome: str,
        revision: int,
        error_code: str,
        error_stage: str,
        committed: bool,
        draft_quarantined: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "time": int(time.time()),
            "post_type": "notice",
            "notice_type": "akasha_send_result",
            "sub_type": outcome,
            "user_id": int(row["target_id"]),
            "akasha_schema": 2,
            "akasha_send_result_schema": 2,
            "type": "private",
            "account": str(row["account"]),
            "session": str(row["session"]),
            "routing_name": str(row["routing_name"]),
            "source_messages": [],
            "outcome": outcome,
            "request_id": str(row["request_id"]),
            "bridge_generation": int(row["generation"]),
            "reply_epoch": int(row["epoch"]),
            "committed": bool(committed),
            "draft_quarantined": bool(draft_quarantined),
        }
        text = str(row["text"])
        if outcome == "sent":
            payload["success"] = True
            payload["delivered_parts"] = [text]
        elif outcome == "failed":
            payload["success"] = False
            payload["discarded_parts"] = [text]
        elif outcome in {"superseded", "manual_cancel"}:
            payload["discarded_parts"] = [text]
        if error_code:
            payload["error_code"] = error_code
            payload["error_stage"] = error_stage or "complete"
        return payload

    # ------------------------------------------------------------------
    # Durable result outbox

    def pending_notices(self, limit: int = 32) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT notice_id,digest,payload_json FROM outbox
                WHERE acked=0 ORDER BY created_at,notice_id LIMIT ?
                """,
                (max(1, min(int(limit), 128)),),
            ).fetchall()
            output = []
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                output.append(
                    {
                        "notice_id": str(row["notice_id"]),
                        "digest": str(row["digest"]),
                        "payload": payload,
                    }
                )
            return output

        return self._read(operation)

    def mark_notice_attempt(self, notice_id: str) -> None:
        self._write(
            lambda connection: connection.execute(
                """
                UPDATE outbox SET attempts=attempts+1,last_attempt_at=?,updated_at=?
                WHERE notice_id=? AND acked=0
                """,
                (time.time(), time.time(), notice_id),
            )
        )

    def ack_notice(self, notice_id: str, digest: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT digest,acked FROM outbox WHERE notice_id=?",
                (notice_id,),
            ).fetchone()
            if row is None or str(row["digest"]) != digest:
                return False
            if int(row["acked"]) == 0:
                connection.execute(
                    "UPDATE outbox SET acked=1,updated_at=? WHERE notice_id=?",
                    (time.time(), notice_id),
                )
            return True

        return bool(self._write(operation))

    # ------------------------------------------------------------------
    # Draft quarantine

    def set_quarantine(self, target_id: int, request_id: str, reason: str) -> None:
        self._write(
            lambda connection: connection.execute(
                """
                INSERT INTO quarantines(target_id,request_id,reason,created_at)
                VALUES(?,?,?,?)
                ON CONFLICT(target_id) DO UPDATE SET
                    request_id=excluded.request_id,
                    reason=excluded.reason,
                    created_at=excluded.created_at
                """,
                (target_id, request_id, reason, time.time()),
            )
        )

    def get_quarantine(self, target_id: int) -> dict[str, Any] | None:
        return self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM quarantines WHERE target_id=?", (target_id,)
                    ).fetchone()
                )
                is None
                else dict(row)
            )
        )

    def resolve_quarantine(self, target_id: int, request_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM quarantines WHERE target_id=? AND request_id=?",
                (target_id, request_id),
            )
            return cursor.rowcount == 1

        return bool(self._write(operation))

    def list_quarantines(self) -> list[dict[str, Any]]:
        return self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT target_id,request_id,reason,created_at
                    FROM quarantines ORDER BY created_at,target_id
                    """
                ).fetchall()
            ]
        )

    def list_commit_unknown(self) -> list[dict[str, Any]]:
        return self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT request_id,target_id,result_revision,error_code,updated_at
                    FROM jobs
                    WHERE outcome='commit_unknown'
                    ORDER BY updated_at,request_id
                    """
                ).fetchall()
            ]
        )

    # ------------------------------------------------------------------
    # Closed ingress batch spool.  The in-memory quiet/max window remains in
    # bridge_core, but a closed private batch is durable before WS delivery.

    def spool_batch(
        self,
        payload: dict[str, Any],
        *,
        target_id: int | None,
        generation: int | None,
        epoch: int | None,
        raw_ids: Iterable[str] = (),
    ) -> str:
        batch_id = str(uuid.uuid4())
        payload_json = _canonical_json(payload)
        raw_hashes = [
            hashlib.sha256(str(raw_id).encode("utf-8")).hexdigest()
            for raw_id in raw_ids
            if str(raw_id)
        ]

        def operation(connection: sqlite3.Connection) -> str:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ingress_batches WHERE state='pending'"
                ).fetchone()[0]
            )
            total_bytes = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0)
                    FROM ingress_batches WHERE state='pending'
                    """
                ).fetchone()[0]
            )
            if count >= 512 or total_bytes + len(payload_json.encode("utf-8")) > 10 * 1024 * 1024:
                raise ReplyStoreError("E_INGRESS_SPOOL_CAPACITY")
            now = time.time()
            connection.execute(
                """
                INSERT INTO ingress_batches(
                    batch_id,target_id,generation,epoch,payload_json,state,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,'pending',?,?)
                """,
                (batch_id, target_id, generation, epoch, payload_json, now, now),
            )
            if raw_hashes:
                connection.executemany(
                    """
                    UPDATE raw_envelopes SET state='closed',updated_at=?
                    WHERE raw_id_hash=? AND state IN ('accepted','buffered')
                    """,
                    ((now, raw_hash) for raw_hash in raw_hashes),
                )
            return batch_id

        return str(self._write(operation))

    def pending_batches(self, limit: int = 32) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT * FROM ingress_batches WHERE state='pending'
                ORDER BY created_at,batch_id LIMIT ?
                """,
                (max(1, min(int(limit), 128)),),
            ).fetchall()
            return [
                {**dict(row), "payload": json.loads(str(row["payload_json"]))}
                for row in rows
            ]

        return self._read(operation)

    def mark_batch_sent(self, batch_id: str, admission_token: str = "") -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE ingress_batches SET state='sent',admission_token=?,updated_at=?
                WHERE batch_id=? AND state='pending'
                """,
                (admission_token, time.time(), batch_id),
            )
            return cursor.rowcount == 1

        return bool(self._write(operation))

    def bind_batch_admission(self, batch_id: str, admission_token: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE ingress_batches SET admission_token=?,updated_at=?
                WHERE batch_id=? AND state='pending'
                """,
                (admission_token, time.time(), batch_id),
            )
            return cursor.rowcount == 1

        return bool(self._write(operation))

    def clear_batch_admission(self, batch_id: str, admission_token: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE ingress_batches SET admission_token='',updated_at=?
                WHERE batch_id=? AND state='pending' AND admission_token=?
                """,
                (time.time(), batch_id, admission_token),
            )
            return cursor.rowcount == 1

        return bool(self._write(operation))

    # ------------------------------------------------------------------
    # Capacity, cleanup and health

    def _capacity_open(
        self,
        connection: sqlite3.Connection,
        *,
        include_reserved: int = 0,
    ) -> bool:
        jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        active = int(
            connection.execute(
                "SELECT COUNT(*) FROM admissions WHERE state='active'"
            ).fetchone()[0]
        )
        return jobs + active + include_reserved < JOB_CAPACITY

    def _outbox_capacity_open(
        self,
        connection: sqlite3.Connection,
        *,
        extra_bytes: int = 0,
        include_admissions: int = 0,
        consume_job_request_id: str = "",
    ) -> bool:
        row = connection.execute(
            """
            SELECT COUNT(*),COALESCE(SUM(payload_bytes),0)
            FROM outbox WHERE acked=0
            """
        ).fetchone()
        admissions = int(
            connection.execute(
                "SELECT COUNT(*) FROM admissions WHERE state='active'"
            ).fetchone()[0]
        )
        if consume_job_request_id:
            jobs = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(
                    LENGTH(CAST(text AS BLOB)) + 4096
                ),0)
                FROM jobs WHERE outcome='' AND request_id<>?
                """,
                (consume_job_request_id,),
            ).fetchone()
        else:
            jobs = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(
                    LENGTH(CAST(text AS BLOB)) + 4096
                ),0)
                FROM jobs WHERE outcome=''
                """
            ).fetchone()
        reserved_count = admissions + int(jobs[0]) + include_admissions
        reserved_bytes = (
            (admissions + include_admissions) * RESULT_RESERVATION_BYTES
            + int(jobs[1])
        )
        return (
            int(row[0]) + reserved_count <= OUTBOX_CAPACITY
            and int(row[1]) + reserved_bytes + extra_bytes
            <= OUTBOX_BYTES_CAPACITY
        )

    def cleanup(self) -> None:
        cutoff = time.time() - TERMINAL_TTL_SECONDS

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM raw_dedupe WHERE accepted_at<?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM raw_envelopes WHERE state='closed' AND updated_at<?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM ingress_batches WHERE state='sent' AND updated_at<?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM outbox WHERE acked=1 AND updated_at<?", (cutoff,)
            )
            connection.execute(
                """
                DELETE FROM jobs
                WHERE state='done' AND updated_at<?
                  AND outcome!='commit_unknown'
                  AND request_id NOT IN (
                    SELECT request_id FROM outbox WHERE acked=0
                  )
                  AND target_id NOT IN (SELECT target_id FROM quarantines)
                """,
                (cutoff,),
            )
            connection.execute(
                """
                DELETE FROM admissions
                WHERE state!='active' AND updated_at<?
                  AND request_id NOT IN (SELECT request_id FROM jobs)
                """,
                (cutoff,),
            )
            connection.execute(
                """
                DELETE FROM epochs
                WHERE updated_at<?
                  AND target_id NOT IN (SELECT target_id FROM admissions)
                  AND target_id NOT IN (SELECT target_id FROM jobs)
                  AND target_id NOT IN (SELECT target_id FROM quarantines)
                  AND target_id NOT IN (
                    SELECT target_id FROM ingress_batches WHERE state='pending'
                  )
                  AND target_id NOT IN (
                    SELECT target_id FROM raw_envelopes
                    WHERE state IN ('accepted','buffered')
                  )
                """,
                (cutoff,),
            )

        self._write(operation)

    def health(self) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            admissions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM admissions WHERE state='active'"
                ).fetchone()[0]
            )
            outbox = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(payload_bytes),0)
                FROM outbox WHERE acked=0
                """
            ).fetchone()
            spool = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0)
                FROM ingress_batches WHERE state='pending'
                """
            ).fetchone()
            raw = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0)
                FROM raw_envelopes WHERE state IN ('accepted','buffered')
                """
            ).fetchone()
            outbox_count, outbox_bytes = int(outbox[0]), int(outbox[1])
            raw_count, raw_bytes = int(raw[0]), int(raw[1])
            open_jobs = connection.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(
                    LENGTH(CAST(text AS BLOB)) + 4096
                ),0) FROM jobs WHERE outcome=''
                """
            ).fetchone()
            reserved_count = admissions + int(open_jobs[0])
            reserved_bytes = (
                admissions * RESULT_RESERVATION_BYTES + int(open_jobs[1])
            )
            ordinary_open = (
                jobs + admissions < JOB_HIGH_WATER
                and outbox_count + reserved_count < OUTBOX_HIGH_WATER
                and outbox_bytes + reserved_bytes < OUTBOX_BYTES_HIGH_WATER
                and raw_count < RAW_ENVELOPE_HIGH_WATER
                and raw_bytes < RAW_ENVELOPE_BYTES_HIGH_WATER
                and int(spool[0]) < 512
                and int(spool[1]) < 10 * 1024 * 1024
            )
            return {
                "ordinary_ingress_open": ordinary_open,
                "jobs": jobs,
                "active_admissions": admissions,
                "outbox_pending": outbox_count,
                "outbox_bytes": outbox_bytes,
                "outbox_reserved": reserved_count,
                "outbox_reserved_bytes": reserved_bytes,
                "spool_pending": int(spool[0]),
                "spool_bytes": int(spool[1]),
                "raw_pending": raw_count,
                "raw_bytes": raw_bytes,
                "draft_quarantines": int(
                    connection.execute("SELECT COUNT(*) FROM quarantines").fetchone()[0]
                ),
                "commit_unknown": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE outcome='commit_unknown'"
                    ).fetchone()[0]
                ),
                "epoch_targets": int(
                    connection.execute("SELECT COUNT(*) FROM epochs").fetchone()[0]
                ),
            }

        return self._read(operation)

    def open_job_count(self, generation: int) -> int:
        return int(
            self._read(
                lambda connection: connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE generation=? AND outcome=''",
                    (int(generation),),
                ).fetchone()[0]
            )
        )


_default_store: ReplyStore | None = None
_default_lock = threading.Lock()


def default_store() -> ReplyStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            state_dir = os.environ.get("AKASHABOT_STATE_DIR", "").strip()
            if not state_dir:
                state_dir = os.path.dirname(os.path.abspath(__file__))
            _default_store = ReplyStore(
                os.path.join(os.path.abspath(state_dir), "merged_reply.sqlite3")
            )
        return _default_store


def reset_default_store_for_tests() -> None:
    global _default_store
    with _default_lock:
        _default_store = None
