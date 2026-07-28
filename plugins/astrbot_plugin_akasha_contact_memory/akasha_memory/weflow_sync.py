from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import aiohttp

from .models import ContactBinding, MemoryMessage, SyncResult
from .store import MemoryStore


class WeFlowConfigurationError(RuntimeError):
    pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    # WeFlow sources may expose either seconds or milliseconds.
    if result > 10_000_000_000:
        result /= 1000.0
    return max(0.0, result)


def _backfill_watermark(records: list[dict[str, Any]]) -> float:
    return max(
        (
            _timestamp(
                record.get("createTime")
                or record.get("timestamp")
                or record.get("time")
            )
            for record in records
        ),
        default=0.0,
    )


def _message_content(record: dict[str, Any]) -> str:
    message_type = _as_text(
        record.get("localType") or record.get("type") or record.get("msgType")
    ).casefold()
    media_type = _as_text(
        record.get("mediaType") or record.get("messageType")
    ).casefold()

    if message_type in {"3", "image"} or media_type in {
        "image",
        "photo",
    }:
        return "[图片]"
    if message_type in {"43", "video"} or media_type == "video":
        return "[视频]"
    if message_type in {"34", "voice", "audio"} or media_type in {
        "voice",
        "audio",
    }:
        return "[语音]"
    if message_type in {"47", "sticker", "emoji"} or media_type in {
        "sticker",
        "emoji",
    }:
        # Custom stickers are exported as GIF/WebP image media. Normalizing
        # them as images lets the bridge's short visual description survive
        # the later authoritative WeFlow sync for the same source message.
        return "[图片]"
    if message_type in {"49", "app"} or media_type in {
        "app",
        "file",
        "document",
    }:
        filename = ""
        for key in ("fileName", "filename", "name"):
            candidate = _as_text(record.get(key))
            if candidate:
                filename = candidate
                break
        filename = re.sub(r"[\x00-\x1f\x7f]+", " ", filename)
        filename = re.split(r"[\\/]", filename)[-1].strip()
        if filename and media_type in {"file", "document"}:
            return f"[文件: {filename[:80]}]"
        return "[微信应用消息]"

    for key in ("parsedContent", "content", "rawContent"):
        value = record.get(key)
        if isinstance(value, dict):
            for nested_key in ("text", "content", "title", "description"):
                nested = _as_text(value.get(nested_key))
                if nested:
                    normalized = nested.lstrip().casefold()
                    if normalized.startswith(
                        ("<?xml", "<msg", "<appmsg", "<xml")
                    ):
                        return "[微信应用消息]"
                    return nested
        text = _as_text(value)
        if text:
            normalized = text.lstrip().casefold()
            if (
                normalized.startswith("<?xml")
                or normalized.startswith("<msg")
                or normalized.startswith("<appmsg")
                or normalized.startswith("<xml")
            ):
                return "[微信应用消息]"
            return text
    return f"[{message_type or '非文本消息'}]"


def _stable_source_uid(record: dict[str, Any]) -> tuple[str, str] | None:
    for prefix, key in (
        ("raw", "serverId"),
        ("raw", "rawid"),
        ("raw", "messageId"),
        ("local", "localId"),
    ):
        value = _as_text(record.get(key))
        if value:
            return f"{prefix}:{value}", key
    return None


def _canonical_fallback(
    record: dict[str, Any],
    *,
    content: str,
    source_time: float,
) -> str:
    canonical = {
        "content": content,
        "direction": bool(record.get("isSend")),
        "sender": _as_text(record.get("senderUsername")),
        "time": source_time,
        "type": _as_text(
            record.get("localType") or record.get("type") or record.get("msgType")
        ),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_weflow_messages(
    records: list[dict[str, Any]],
    *,
    ordinal_base: int = 0,
) -> list[MemoryMessage]:
    prepared: list[tuple[float, str, int, dict[str, Any], str, str | None]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        source_time = _timestamp(
            record.get("createTime")
            or record.get("timestamp")
            or record.get("time")
        )
        content = _message_content(record)
        stable = _stable_source_uid(record)
        fallback = _canonical_fallback(
            record,
            content=content,
            source_time=source_time,
        )
        prepared.append(
            (
                source_time,
                stable[0] if stable else fallback,
                index,
                record,
                content,
                stable[1] if stable else None,
            )
        )
    prepared.sort(key=lambda item: (item[0], item[1], item[2]))

    fallback_counts: Counter[str] = Counter()
    output: list[MemoryMessage] = []
    for source_time, identity, record_index, record, content, stable_key in prepared:
        if stable_key:
            source_uid = identity
            id_quality = stable_key
        else:
            fallback_counts[identity] += 1
            # WeFlow cannot provide exactly-once identity for this record.
            # Include its absolute page ordinal so indistinguishable records
            # are retained with at-least-once semantics instead of being lost.
            absolute_ordinal = max(0, int(ordinal_base)) + record_index + 1
            source_uid = (
                f"hash:{identity}:{absolute_ordinal}:"
                f"{fallback_counts[identity]}"
            )
            id_quality = "degraded_hash"
        output.append(
            MemoryMessage(
                source_uid=source_uid,
                source_time=source_time,
                direction="out" if bool(record.get("isSend")) else "in",
                content=content,
                message_type=_as_text(
                    record.get("localType")
                    or record.get("type")
                    or record.get("msgType")
                )
                or "text",
                id_quality=id_quality,
                origin="weflow",
                pending=False,
            )
        )
    return output


class WeFlowSync:
    def __init__(
        self,
        store: MemoryStore,
        *,
        bridge_config_path: str = "",
        allow_non_loopback: bool = False,
        cold_limit: int = 2000,
        fallback_limit: int = 500,
        sync_budget_seconds: float = 3.0,
    ) -> None:
        self.store = store
        self.bridge_config_path = bridge_config_path.strip()
        self.allow_non_loopback = bool(allow_non_loopback)
        self.cold_limit = max(1, min(int(cold_limit), 10000))
        self.fallback_limit = max(1, min(int(fallback_limit), self.cold_limit))
        self.sync_budget_seconds = max(0.5, float(sync_budget_seconds))
        self._session: aiohttp.ClientSession | None = None
        self._backfills: dict[int, asyncio.Task[None]] = {}
        self._contact_locks: dict[int, asyncio.Lock] = {}
        self._closed = False

    def _lock_for(self, contact_id: int) -> asyncio.Lock:
        lock = self._contact_locks.get(contact_id)
        if lock is None:
            lock = asyncio.Lock()
            self._contact_locks[contact_id] = lock
        return lock

    def _config_path(self) -> Path:
        import os

        configured = self.bridge_config_path or os.environ.get(
            "AKASHABOT_BRIDGE_CONFIG_PATH",
            "",
        ).strip()
        if not configured:
            raise WeFlowConfigurationError("bridge config path is not configured")
        return Path(configured)

    def _load_endpoint(self, expected_account: str = "") -> tuple[str, str]:
        path = self._config_path()
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WeFlowConfigurationError("bridge config cannot be read") from exc
        base_url = _as_text(config.get("weflow_base_url")).rstrip("/")
        token = _as_text(config.get("access_token"))
        configured_account = _as_text(config.get("bot_wxid"))
        if not configured_account:
            raise WeFlowConfigurationError(
                "bridge bot_wxid is required for contact isolation"
            )
        if expected_account and configured_account != expected_account:
            raise WeFlowConfigurationError(
                "bridge account does not match the current private event"
            )
        if not base_url:
            raise WeFlowConfigurationError("WeFlow base URL is missing")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise WeFlowConfigurationError("WeFlow base URL is invalid")
        loopback_hosts = {"localhost", "127.0.0.1", "::1"}
        if not self.allow_non_loopback and parsed.hostname.lower() not in loopback_hosts:
            raise WeFlowConfigurationError("non-loopback WeFlow URL is blocked")
        return base_url, token

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _fetch_page(
        self,
        *,
        talker: str,
        limit: int,
        offset: int = 0,
        start: int | None = None,
        end: int | None = None,
        expected_account: str = "",
    ) -> tuple[list[dict[str, Any]], bool]:
        base_url, token = self._load_endpoint(expected_account)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params: dict[str, Any] = {
            "talker": talker,
            "limit": max(1, min(int(limit), 10000)),
            "offset": max(0, int(offset)),
        }
        if start is not None and start > 0:
            params["start"] = int(start)
        if end is not None and end > 0:
            params["end"] = int(end)
        client = await self._client()
        async with client.get(
            f"{base_url}/api/v1/messages",
            params=params,
            headers=headers,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"WeFlow messages request failed ({response.status})")
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError("WeFlow messages response was unsuccessful")
        if _as_text(payload.get("talker")) != talker:
            raise RuntimeError("WeFlow messages response talker mismatch")
        records = payload.get("messages", [])
        if not isinstance(records, list):
            raise RuntimeError("WeFlow messages response has invalid records")
        return [item for item in records if isinstance(item, dict)], bool(
            payload.get("hasMore")
        )

    async def _store_page(
        self,
        contact_id: int,
        records: list[dict[str, Any]],
        *,
        ordinal_base: int = 0,
        backfill_offset: int | None = None,
        backfill_end_time: float | None = None,
        full_backfill_complete: bool | None = None,
    ) -> tuple[int, int]:
        messages = parse_weflow_messages(records, ordinal_base=ordinal_base)
        imported = await self.store.upsert_messages(contact_id, messages)
        await self.store.reconcile_pending_outputs(contact_id)
        state = await self.store.get_sync_state(contact_id)
        cursor_time = float(state["cursor_time"])
        cursor_uid = str(state["cursor_uid"])
        if messages:
            newest = max(messages, key=lambda message: (message.source_time, message.source_uid))
            if (newest.source_time, newest.source_uid) > (cursor_time, cursor_uid):
                cursor_time = newest.source_time
                cursor_uid = newest.source_uid
        await self.store.update_sync_state(
            contact_id,
            cursor_time=cursor_time,
            cursor_uid=cursor_uid,
            backfill_offset=backfill_offset,
            backfill_end_time=backfill_end_time,
            full_backfill_complete=full_backfill_complete,
        )
        return imported, len(messages)

    def _schedule_backfill(
        self,
        contact_id: int,
        *,
        talker: str,
        account: str,
        initial_offset: int,
        end_time: float,
    ) -> bool:
        existing = self._backfills.get(contact_id)
        if existing and not existing.done():
            return False
        task = asyncio.create_task(
            self._run_backfill(
                contact_id,
                talker=talker,
                account=account,
                initial_offset=initial_offset,
                end_time=end_time,
            ),
            name=f"akasha-memory-backfill-{contact_id}",
        )
        self._backfills[contact_id] = task
        task.add_done_callback(
            lambda completed: (
                self._backfills.pop(contact_id, None)
                if self._backfills.get(contact_id) is completed
                else None
            )
        )
        return True

    async def _run_backfill(
        self,
        contact_id: int,
        *,
        talker: str,
        account: str,
        initial_offset: int,
        end_time: float,
    ) -> None:
        offset = max(0, int(initial_offset))
        try:
            while not self._closed:
                async with self._lock_for(contact_id):
                    if await self.store.is_contact_tombstoned(contact_id):
                        break
                    records, has_more = await self._fetch_page(
                        talker=talker,
                        limit=1000,
                        offset=offset,
                        end=int(end_time) if end_time > 0 else None,
                        expected_account=account,
                    )
                    next_offset = offset + len(records)
                    await self._store_page(
                        contact_id,
                        records,
                        ordinal_base=offset,
                        backfill_offset=next_offset,
                        backfill_end_time=end_time,
                        full_backfill_complete=not has_more,
                    )
                if not has_more or not records:
                    break
                offset = next_offset
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The next private message resumes from the stored offset.
            return

    async def _cancel_backfill(self, contact_id: int) -> None:
        task = self._backfills.pop(contact_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def exclusive_contact(self, contact_id: int) -> AsyncIterator[None]:
        """Quiesce sync/backfill and block new sync while deleting memory."""

        await self._cancel_backfill(contact_id)
        async with self._lock_for(contact_id):
            # Catch a task scheduled by a sync that won the lock race between
            # the first cancellation and this exclusive section.
            await self._cancel_backfill(contact_id)
            yield

    async def sync_contact(
        self,
        contact_id: int,
        binding: ContactBinding,
    ) -> SyncResult:
        if self._closed:
            return SyncResult(available=False, error_kind="closed")
        async with self._lock_for(contact_id):
            if await self.store.is_contact_tombstoned(contact_id):
                return SyncResult(error_kind="tombstoned")
            state = await self.store.get_sync_state(contact_id)
            cold = float(state["last_sync_at"]) <= 0
            try:
                if cold:
                    timed_out = False
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + self.sync_budget_seconds
                    try:
                        records, has_more = await asyncio.wait_for(
                            self._fetch_page(
                                talker=binding.session,
                                limit=self.cold_limit,
                                expected_account=binding.account,
                            ),
                            timeout=max(0.25, self.sync_budget_seconds * 0.7),
                        )
                    except TimeoutError:
                        timed_out = True
                        remaining = max(0.05, deadline - loop.time())
                        records, has_more = await asyncio.wait_for(
                            self._fetch_page(
                                talker=binding.session,
                                limit=self.fallback_limit,
                                expected_account=binding.account,
                            ),
                            timeout=remaining,
                        )
                    imported, fetched = await self._store_page(
                        contact_id,
                        records,
                        ordinal_base=0,
                        backfill_offset=len(records),
                        backfill_end_time=_backfill_watermark(records),
                        full_backfill_complete=not has_more,
                    )
                    scheduled = False
                    if has_more:
                        scheduled = self._schedule_backfill(
                            contact_id,
                            talker=binding.session,
                            account=binding.account,
                            initial_offset=len(records),
                            end_time=_backfill_watermark(records),
                        )
                    return SyncResult(
                        imported=imported,
                        fetched=fetched,
                        timed_out=timed_out,
                        full_backfill_scheduled=scheduled,
                    )

                start = max(0, int(float(state["cursor_time"]) - 120))
                records, has_more = await asyncio.wait_for(
                    self._fetch_page(
                        talker=binding.session,
                        limit=1000,
                        start=start,
                        expected_account=binding.account,
                    ),
                    timeout=self.sync_budget_seconds,
                )
                imported, fetched = await self._store_page(contact_id, records)
                scheduled = False
                if not bool(state["full_backfill_complete"]):
                    scheduled = self._schedule_backfill(
                        contact_id,
                        talker=binding.session,
                        account=binding.account,
                        initial_offset=int(state["backfill_offset"]),
                        end_time=float(state["backfill_end_time"]),
                    )
                return SyncResult(
                    imported=imported,
                    fetched=fetched,
                    full_backfill_scheduled=scheduled or has_more,
                )
            except TimeoutError:
                scheduled = self._schedule_backfill(
                    contact_id,
                    talker=binding.session,
                    account=binding.account,
                    initial_offset=int(state["backfill_offset"]),
                    end_time=float(state["backfill_end_time"]),
                )
                return SyncResult(
                    timed_out=True,
                    available=True,
                    full_backfill_scheduled=scheduled,
                    error_kind="timeout",
                )
            except WeFlowConfigurationError:
                return SyncResult(
                    available=False,
                    error_kind="configuration",
                )
            except (aiohttp.ClientError, OSError):
                return SyncResult(
                    available=False,
                    error_kind="connection",
                )
            except Exception:
                return SyncResult(
                    available=False,
                    error_kind="response",
                )

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._backfills.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._backfills.clear()
        if self._session and not self._session.closed:
            await self._session.close()
