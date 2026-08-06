"""
桥接核心模块：WeFlowBridge 类。

职责：
1. 连接 WeFlow SSE 推送，接收微信消息
2. 消息缓冲合并（BUFFER_SECONDS）
3. 构造 OneBot 事件，推送给 AstrBot
4. 多层消息去重（rawid、内容、自回复）
"""

import hashlib
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests

_BRIDGE_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BRIDGE_MODULE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_MODULE_DIR)

import state
import config
from ob_protocol import push_event, make_message_event
from reply_store import ReplyStoreError, default_store
from privacy import chat_record
from money_service import MoneyActionService, WeFlowMoneySource

log = logging.getLogger("ob11-bridge")

_STICKER_CONTENT_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_STICKER_MAX_BYTES = 6 * 1024 * 1024
_CONTACT_TYPE_CACHE_CAPACITY = 4096
_CONTACT_TYPE_LOOKUP_LIMIT = 100
_CONTACT_TYPE_TIMEOUT_SECONDS = 3


# ============ 桥接核心 ============


def _text_value(value) -> str:
    return "" if value is None else str(value).strip()


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _exact_text_value(value) -> str:
    return "" if value is None else str(value)


def _source_timestamp(value):
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _message_type_values(data: dict) -> set[str]:
    return {
        value.casefold()
        for key in ("localType", "type", "msgType", "messageType")
        if (value := _text_value(data.get(key)))
    }


def _is_sticker_message(data: dict) -> bool:
    if _message_type_values(data) & {"47", "emoji", "sticker"}:
        return True
    media_type = _text_value(data.get("mediaType")).casefold()
    if media_type in {"emoji", "sticker"}:
        return True
    for key in ("content", "parsedContent"):
        if _text_value(data.get(key)) == "[表情]":
            return True
    for key in ("content", "rawContent", "parsedContent"):
        candidate = _text_value(data.get(key)).casefold()
        if candidate.startswith("<") and "<emoji" in candidate:
            return True
    return False


def _event_fingerprint(data: dict) -> str:
    fingerprint_fields = {
        "content": _exact_text_value(data.get("content")),
        "message_type": _text_value(
            data.get("localType") or data.get("type") or data.get("msgType")
        ),
        "rawid": _text_value(data.get("rawid")),
        "sender": _text_value(data.get("senderName") or data.get("sender")),
        "session": _text_value(data.get("sessionId")),
        "session_type": _text_value(data.get("sessionType")),
        "source": _text_value(data.get("sourceName") or data.get("talkerName")),
        "talker": _text_value(data.get("talkerId")),
        "timestamp": _source_timestamp(data.get("timestamp")),
    }
    canonical = json.dumps(
        fingerprint_fields,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_message_ref(data: dict, buffer_ordinal: int) -> dict:
    raw_id = _text_value(data.get("rawid"))
    return {
        "rawid": raw_id,
        "timestamp": _source_timestamp(data.get("timestamp")),
        "event_fingerprint": _event_fingerprint(data),
        "buffer_ordinal": int(buffer_ordinal),
        "id_quality": "rawid" if raw_id else "event_fingerprint",
    }


def _insert_source_message_ref(
    entry: dict,
    data: dict,
    index: int | None = None,
) -> None:
    refs = entry.setdefault("source_messages", [])
    insertion_index = len(refs) if index is None else max(0, min(index, len(refs)))
    refs.insert(insertion_index, _source_message_ref(data, insertion_index + 1))
    for ordinal, source_ref in enumerate(refs, start=1):
        source_ref["buffer_ordinal"] = ordinal


def _account_identity() -> str:
    return _text_value(getattr(config, "BOT_WXID", "")) or "wechat_bot"


def _merged_runtime_available() -> bool:
    """Require the complete merged-reply state surface, not a partial test stub."""

    return all(
        callable(getattr(state, name, None))
        for name in (
            "merged_reply_ready",
            "ordinary_ingress_open",
            "merged_reply_capability_identity",
            "create_reply_admission",
            "is_reply_current",
        )
    )


def _is_red_packet_receipt_text(content: object) -> bool:
    text = _text_value(content)
    normalized = re.sub(r"\s+", "", text)
    return (
        normalized.startswith("你领取了")
        and normalized.endswith("的红包")
        and len(normalized) <= 256
    )


def _money_receipt_display(content: object) -> str:
    """Hide unresolved WeFlow wxid identifiers from reader-facing history."""
    text = _text_value(content)
    if _is_red_packet_receipt_text(text):
        return "红包领取成功"
    return text


class WeFlowBridge:
    """WeFlow ↔ AstrBot 桥接器（OneBot v11 版）。"""

    def __init__(self, sender, generation=None):
        self.sender = sender
        self.generation = generation
        self.processed_ids = set()
        self.start_timestamp = int(time.time())
        self.pending_buffers = {}
        self.buffer_lock = threading.Lock()
        self.chat_histories = defaultdict(list)
        self.contact_map = {}
        self._sse_session = None
        self._recent_seen = {}
        self._sent_recently = {}
        self._sse_event_keys = {}
        self._pending_image = {}  # talkerId → {"caption": None|str, "event": threading.Event()}
        self._contact_type_cache = {}
        self._spool_stop = threading.Event()
        self._spool_thread = None
        if _merged_runtime_available():
            self._spool_thread = threading.Thread(
                target=self._drain_private_spool_loop,
                daemon=True,
                name=f"akasha-ingress-spool-{int(generation or 0)}",
            )
            self._spool_thread.start()
        self.money_actions = None
        if sender is not None and bool(
            getattr(config, "MONEY_RECEIVE_ENABLED", False)
        ):
            self.money_actions = MoneyActionService(
                sender=sender,
                generation=int(generation or 0),
                source=WeFlowMoneySource(
                    base_url=config.WE_FLOW_BASE_URL,
                    access_token=config.ACCESS_TOKEN,
                    request_get=requests.get,
                ),
                notifier=self._notify_money_action,
                timeout_seconds=float(
                    getattr(config, "MONEY_RECEIVE_TIMEOUT_SECONDS", 180.0)
                ),
                receipt_poll_seconds=float(
                    getattr(config, "MONEY_RECEIPT_POLL_SECONDS", 1.0)
                ),
                account_id=str(getattr(config, "BOT_WXID", "") or ""),
            )
        self._raw_recovery_thread = threading.Thread(
            target=self._recover_pending_raw_envelopes,
            daemon=True,
            name=f"akasha-raw-recovery-{int(generation or 0)}",
        )
        self._raw_recovery_thread.start()

    def _notify_money_action(self, payload: dict[str, object]) -> bool:
        return push_event(
            {
                "time": int(time.time()),
                "self_id": int(getattr(state, "_self_id_int", 0) or 0),
                "post_type": "notice",
                "notice_type": "akasha_money_action",
                "sub_type": "start",
                "user_id": int(getattr(state, "_self_id_int", 0) or 1),
                "akasha_schema": 1,
                **payload,
            }
        )

    def _recover_pending_raw_envelopes(self) -> None:
        try:
            envelopes = default_store().pending_raw_envelopes()
        except Exception:
            log.exception("普通入站 raw WAL 恢复失败")
            return
        for envelope in envelopes:
            if not self._active():
                return
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                continue
            if self.generation is not None:
                rebased = default_store().rebase_raw_envelope(
                    str(envelope.get("raw_id_hash") or ""),
                    int(self.generation),
                )
                if rebased is None:
                    continue
                envelope = {**envelope, **rebased}
            recovered = dict(payload)
            recovered["_akasha_recovery"] = {
                "target_id": envelope.get("target_id"),
                "generation": envelope.get("generation"),
                "reply_epoch": envelope.get("epoch"),
                "accepted_at": envelope.get("accepted_at"),
            }
            self.add_to_buffer(recovered)

    def stop_money_actions(self) -> None:
        self._spool_stop.set()
        if self.money_actions is not None:
            self.money_actions.stop()

    def _active(self):
        return (
            True
            if self.generation is None
            else state.is_generation_running(self.generation)
        )

    def _contact_type(self, data: dict) -> str:
        for key in ("contactType", "sourceType", "talkerType"):
            direct_type = _text_value(data.get(key)).casefold()
            if direct_type:
                return direct_type

        session_id = _text_value(data.get("sessionId"))
        session_type = _text_value(data.get("sessionType")).casefold()
        if (
            not session_id
            or session_type == "group"
            or "@chatroom" in session_id
        ):
            return ""

        cached = self._contact_type_cache.get(session_id)
        if cached is not None:
            return cached

        base_url = _text_value(getattr(config, "WE_FLOW_BASE_URL", "")).rstrip("/")
        access_token = _text_value(getattr(config, "ACCESS_TOKEN", ""))
        if not base_url or not access_token:
            return ""

        try:
            response = requests.get(
                f"{base_url}/api/v1/contacts",
                params={
                    "keyword": session_id,
                    "limit": _CONTACT_TYPE_LOOKUP_LIMIT,
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=_CONTACT_TYPE_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return ""
            payload = response.json()
        except Exception:
            return ""

        if not isinstance(payload, dict) or payload.get("success") is not True:
            return ""
        contacts = payload.get("contacts")
        count = payload.get("count")
        if (
            not isinstance(contacts, list)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(contacts)
            or count >= _CONTACT_TYPE_LOOKUP_LIMIT
            or any(not isinstance(contact, dict) for contact in contacts)
        ):
            return ""

        exact_matches = [
            contact
            for contact in contacts
            if _text_value(contact.get("username")) == session_id
        ]
        if len(exact_matches) != 1:
            return ""
        contact_type = _text_value(exact_matches[0].get("type")).casefold()
        if not contact_type:
            return ""

        if len(self._contact_type_cache) >= _CONTACT_TYPE_CACHE_CAPACITY:
            oldest = next(iter(self._contact_type_cache))
            self._contact_type_cache.pop(oldest, None)
        self._contact_type_cache[session_id] = contact_type
        return contact_type

    def _is_group_inbound(self, data: dict) -> bool:
        session_type = _text_value(data.get("sessionType")).casefold()
        direct_types = {
            _text_value(data.get(key)).casefold()
            for key in ("contactType", "sourceType", "talkerType")
        }
        group_identifiers = (
            data.get("sessionId"),
            data.get("talkerId"),
            data.get("sourceName"),
        )
        return (
            session_type in {"group", "chatroom"}
            or bool(_text_value(data.get("groupName")))
            or bool(direct_types & {"group", "chatroom"})
            or any(
                "@chatroom" in _text_value(value).casefold()
                for value in group_identifiers
            )
        )

    def _remember_filtered_group_route(self, data: dict) -> None:
        source_name = _text_value(
            data.get("sourceName") or data.get("talkerName")
        )
        session_id = _text_value(
            data.get("sessionId") or data.get("talkerId")
        )
        group_raw = _text_value(data.get("groupName")) or source_name
        routing_name = re.sub(r"\s*\(\d+\)\s*$", "", group_raw).strip()
        if not routing_name:
            return

        account = _account_identity()
        try:
            group_id = state._wxid_to_int(
                routing_name,
                account=account,
                identity_type="group",
            )
            state._ob_id_to_contact[group_id] = routing_name
            if session_id:
                state.remember_group_route(
                    group_id,
                    account=account,
                    session=session_id,
                    routing_name=routing_name,
                )
        except Exception:
            log.warning("群聊 route 元数据保存失败，仍过滤本条入站消息")

    def _inbound_filter_type(self, data: dict) -> str:
        if self._is_group_inbound(data):
            return "group"

        session_type = _text_value(data.get("sessionType")).casefold()
        if session_type in {"channel", "official"}:
            return "official"

        contact_type = self._contact_type(data)
        if contact_type in {"group", "chatroom"}:
            return "group"
        if contact_type == "official":
            return "official"
        return ""

    def should_ignore(self, data):
        content = _text_value(data.get("content"))
        msg_types = _message_type_values(data)
        is_send = data.get("isSend")
        if (
            is_send is True
            or (not isinstance(is_send, bool) and is_send == 1)
            or _text_value(is_send).casefold() in {"1", "true", "yes"}
        ):
            return True
        if data.get("sourceName", "") in config.BOT_NICKNAMES:
            return True
        if config.BOT_WXID and data.get("talkerId", "") == config.BOT_WXID:
            return True
        if "34" in msg_types:  # 34=语音
            return True
        if _is_sticker_message(data):
            return False
        if content and "[语音]" in content:
            return True
        if not content:
            return True
        return False

    def log_money_event(self, data: dict, *, body: object | None = None) -> None:
        """Record a money marker or receipt in the local panel without buffering it."""
        source_name = _text_value(
            data.get("sourceName", "") or data.get("talkerName", "")
        ) or "未知"
        raw_session_id = _text_value(data.get("sessionId"))
        group_name_raw = _text_value(data.get("groupName"))
        session_probe = raw_session_id or source_name
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name_raw)
            or "@chatroom" in session_probe
        )
        display_body = _money_receipt_display(
            data.get("content", "") if body is None else body
        )
        if is_group:
            group_raw = group_name_raw or source_name
            contact = re.sub(r"\s*\(\d+\)\s*$", "", group_raw).strip()
            sender = _text_value(
                data.get("senderName", "")
                or data.get("sender", "")
                or data.get("sourceName", "")
            )
            log.info(
                "CHAT %s",
                chat_record(
                    event="inbound",
                    scope="group",
                    contact=contact,
                    sender=sender or source_name,
                    status="received",
                    body=display_body,
                ),
            )
            return
        log.info(
            "CHAT %s",
            chat_record(
                event="inbound",
                scope="private",
                contact=source_name,
                status="received",
                body=display_body,
            ),
        )

    def add_to_buffer(self, data):
        """将消息加入缓冲区，等待合并后统一推送给 AstrBot。"""
        if not self._active():
            return False
        is_sticker = _is_sticker_message(data)
        raw_content = data.get("content", "")
        content = "[表情]" if is_sticker else _money_receipt_display(raw_content)
        source_name = _text_value(
            data.get("sourceName", "") or data.get("talkerName", "")
        ) or "未知"
        raw_session_id = _text_value(data.get("sessionId"))
        group_name_raw = _text_value(data.get("groupName"))
        session_probe = raw_session_id or source_name
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name_raw)
            or "@chatroom" in session_probe
        )
        if not is_group and not raw_session_id:
            log.warning("跳过缺少稳定 sessionId 的私聊消息")
            return False
        session_id_data = raw_session_id or source_name

        now = time.time()
        if (
            not is_sticker
            and content
            and content in self._sent_recently
            and now - self._sent_recently[content] < 120
        ):
            log.info("⏭️ 自回复去重跳过: content_length=%d", len(content))
            return True

        sender_in_group = _text_value(
            data.get("senderName", "")
            or data.get("sender", "")
            or data.get("sourceName", "")
        )

        if is_group:
            group_raw = group_name_raw or source_name
            base_name = re.sub(r'\s*\(\d+\)\s*$', '', group_raw).strip()
            contact = base_name
            log.info(
                "CHAT %s",
                chat_record(
                    event="inbound",
                    scope="group",
                    contact=contact,
                    sender=sender_in_group or source_name,
                    status="received",
                    body=content,
                ),
            )
        else:
            contact = source_name
            log.info(
                "CHAT %s",
                chat_record(
                    event="inbound",
                    scope="private",
                    contact=contact,
                    status="received",
                    body=content,
                ),
            )

        if _is_red_packet_receipt_text(raw_content):
            return True

        reply_target_id = None
        reply_epoch = None
        reply_generation = int(
            self.generation
            or getattr(state, "lifecycle_generation", 0)
            or 0
        )
        if not is_group and reply_generation > 0:
            advance_epoch = getattr(state, "advance_reply_epoch", None)
            if callable(advance_epoch):
                private_account = _text_value(
                    getattr(config, "BOT_WXID", "")
                )
                if not private_account:
                    log.warning("跳过缺少 BOT_WXID 的私聊消息")
                    return False
                try:
                    reply_target_id = state.remember_private_route(
                        session_id_data,
                        account=private_account,
                        routing_name=contact,
                    )
                    recovery = data.get("_akasha_recovery")
                    if isinstance(recovery, dict):
                        recovered_target = _positive_int(
                            recovery.get("target_id")
                        )
                        recovered_generation = _positive_int(
                            recovery.get("generation")
                        )
                        recovered_epoch = _positive_int(
                            recovery.get("reply_epoch")
                        )
                        if (
                            recovered_target != reply_target_id
                            or recovered_generation != reply_generation
                            or recovered_epoch is None
                        ):
                            raise ValueError("raw WAL identity mismatch")
                        reply_epoch = recovered_epoch
                    else:
                        reply_epoch = advance_epoch(
                            reply_target_id,
                            reply_generation,
                            _text_value(data.get("rawid")),
                            data,
                        )
                        if reply_epoch is None:
                            log.info("⏭️ 持久 rawid 去重跳过")
                            return True
                    state._ob_id_to_contact[reply_target_id] = contact
                except Exception:
                    log.warning("私聊 reply epoch 建立失败，本轮不推送")
                    return False

        if reply_epoch is not None:
            data = dict(data)
            data["_akasha_reply_target_id"] = reply_target_id
            data["_akasha_reply_epoch"] = reply_epoch
            data["_akasha_bridge_generation"] = reply_generation

        if is_sticker:
            threading.Thread(
                target=self.process_sticker_message,
                args=(data,),
                daemon=True,
            ).start()
            return True
        if content == "[图片]":
            # 图片消息：下载 → ollama 描述 → 注入缓冲区
            threading.Thread(
                target=self.process_image_message,
                args=(data,),
                daemon=True,
            ).start()
            return True
        if content == "[视频]":
            threading.Thread(
                target=self.process_video_message,
                args=(data,),
                daemon=True,
            ).start()
            return True

        if (
            is_group
            and state.group_reply_mode == "mention"
            and not any(f"@{n}" in content for n in config.BOT_NICKNAMES)
        ):
            return True

        if is_group and state.group_reply_mode == "batch":
            buffer_key = f"__batch__{base_name}"
        elif is_group and sender_in_group:
            buffer_key = f"{session_id_data}_{sender_in_group}"
        else:
            buffer_key = session_id_data

        with self.buffer_lock:
            if buffer_key not in self.pending_buffers:
                self.pending_buffers[buffer_key] = {
                    "messages": [],
                    "timer": None,
                    "timer_version": 0,
                    "processing": False,
                    "contact": contact,
                    "is_group": is_group,
                    "source_name": source_name,
                    "group_name": base_name if is_group else "",
                    "sender_in_group": sender_in_group if is_group else "",
                    "session_id_data": session_id_data,
                    "source_messages": [],
                    "batch_started_monotonic": None,
                    "last_accepted_monotonic": None,
                    "reply_target_id": None,
                    "reply_epoch": None,
                    "bridge_generation": None,
                }
            entry = self.pending_buffers[buffer_key]
            batch_was_empty = not entry["messages"]
            if is_group and state.group_reply_mode == "batch" and sender_in_group:
                entry["messages"].append(f'成员"{sender_in_group}"在群"{base_name}"中对你说：{content}')
            else:
                entry["messages"].append(content)
            _insert_source_message_ref(entry, data)

            accepted_at = time.monotonic()
            if batch_was_empty or entry.get("batch_started_monotonic") is None:
                entry["batch_started_monotonic"] = accepted_at
            entry["last_accepted_monotonic"] = accepted_at
            if reply_epoch is not None:
                entry["reply_target_id"] = reply_target_id
                entry["reply_epoch"] = reply_epoch
                entry["bridge_generation"] = reply_generation

            if not entry["processing"]:
                log.info(
                    "📩 消息已进入缓冲: content_length=%d group=%s quiet=%s max=%s",
                    len(content),
                    is_group,
                    getattr(
                        config,
                        "BUFFER_QUIET_SECONDS",
                        config.BUFFER_SECONDS,
                    ),
                    getattr(
                        config,
                        "BUFFER_MAX_SECONDS",
                        config.BUFFER_SECONDS,
                    ),
                )
                self._schedule_buffer_locked(buffer_key)

        if not is_group:
            try:
                default_store().mark_raw_envelope_buffered(
                    _text_value(data.get("rawid"))
                )
            except Exception:
                log.exception("普通入站 raw WAL 状态更新失败")
        return True

    def _drain_private_spool_loop(self) -> None:
        while not self._spool_stop.is_set() and self._active():
            try:
                self._drain_private_spool()
            except Exception:
                log.exception("私聊 pending spool 排空失败")
            self._spool_stop.wait(0.5)

    def _drain_private_spool(self) -> None:
        if (
            not self._active()
            or not state.merged_reply_ready()
            or not state.ordinary_ingress_open()
        ):
            return
        store = default_store()
        capability = state.merged_reply_capability_identity()
        if capability is None:
            return
        for batch in store.pending_batches(32):
            target_id = _positive_int(batch.get("target_id"))
            generation = _positive_int(batch.get("generation"))
            reply_epoch = _positive_int(batch.get("epoch"))
            batch_id = str(batch.get("batch_id") or "")
            if (
                target_id is None
                or generation is None
                or reply_epoch is None
                or not batch_id
            ):
                store.mark_batch_sent(batch_id)
                continue
            if not state.is_reply_current(target_id, generation, reply_epoch):
                store.mark_batch_sent(batch_id)
                log.info(
                    "丢弃 spool 中已淘汰批次: target=%s generation=%s epoch=%s",
                    target_id,
                    generation,
                    reply_epoch,
                )
                continue

            admission_token = str(batch.get("admission_token") or "")
            plugin_instance_id = ""
            if admission_token:
                admission = store.admission_status(admission_token)
                if (
                    admission is not None
                    and admission.get("state") == "active"
                    and admission.get("plugin_instance_id")
                    == capability.get("plugin_instance_id")
                ):
                    plugin_instance_id = str(admission["plugin_instance_id"])
                else:
                    store.clear_batch_admission(batch_id, admission_token)
                    admission_token = ""
            if not admission_token:
                try:
                    admission_token, plugin_instance_id = (
                        state.create_reply_admission(
                            target_id,
                            generation,
                            reply_epoch,
                        )
                    )
                except ReplyStoreError as error:
                    if error.code in {
                        "E_ADMISSION_CAPACITY",
                        "E_OB_STATE_CAPACITY",
                        "E_MERGED_REPLY_NOT_READY",
                    }:
                        return
                    if error.code == "E_REPLY_SUPERSEDED":
                        store.mark_batch_sent(batch_id)
                        continue
                    raise
                store.bind_batch_admission(batch_id, admission_token)

            event = dict(batch["payload"])
            event["plugin_instance_id"] = plugin_instance_id
            event["admission_token"] = admission_token
            event["akasha_batch_id"] = batch_id
            if not push_event(event):
                return
            store.mark_batch_sent(batch_id, admission_token)
            log.info(
                "✅ 已从持久 spool 推送私聊批次: target=%s epoch=%s",
                target_id,
                reply_epoch,
            )

    def process_sender(self, sender_id, version=None):
        """缓冲到期：通过 OneBot 事件推送给 AstrBot。"""
        if not self._active():
            return
        with self.buffer_lock:
            if sender_id not in self.pending_buffers:
                return
            entry = self.pending_buffers[sender_id]
            if version is not None and entry.get("timer_version", 0) != version:
                return
            if entry.get("processing"):
                return
            if not entry["messages"]:
                return
            msgs = entry["messages"].copy()
            source_messages = [
                dict(source_message)
                for source_message in entry.get("source_messages", [])
            ]
            entry["messages"] = []
            entry["source_messages"] = []
            entry["processing"] = True
            reply_target_id = entry.get("reply_target_id")
            reply_epoch = entry.get("reply_epoch")
            reply_generation = entry.get("bridge_generation")
            entry["batch_started_monotonic"] = None
            entry["last_accepted_monotonic"] = None
            entry["reply_target_id"] = None
            entry["reply_epoch"] = None
            entry["bridge_generation"] = None
            if entry["timer"]:
                entry["timer"].cancel()
                entry["timer"] = None

        contact = entry.get("contact", sender_id)
        is_group = entry.get("is_group", False)
        account = _account_identity()
        session_id_data = _text_value(entry.get("session_id_data"))
        combined = "\n".join(msgs)
        log.info(
            "推送缓冲消息: messages=%d characters=%d group=%s",
            len(msgs), len(combined), is_group,
        )

        # 构建 OneBot 事件（user_id 要用发言人身份，不能用群 sessionId）
        if is_group:
            sender_wxid = session_id_data + "_" + (entry.get("sender_in_group", "") or entry.get("source_name", ""))
            user_id = state._wxid_to_int(
                sender_wxid,
                account=account,
                identity_type="group_sender",
            )
        else:
            if not session_id_data:
                log.warning("跳过缺少稳定 sessionId 的私聊缓冲")
                with self.buffer_lock:
                    entry["processing"] = False
                return
            private_account = _text_value(getattr(config, "BOT_WXID", ""))
            if not private_account:
                log.warning("跳过缺少 BOT_WXID 的私聊缓冲")
                with self.buffer_lock:
                    current = self.pending_buffers.get(sender_id)
                    if current is not None:
                        current["messages"] = msgs + current.get("messages", [])
                        current["source_messages"] = (
                            source_messages + current.get("source_messages", [])
                        )
                        current["processing"] = False
                return
            account = private_account
            try:
                user_id = state.remember_private_route(
                    session_id_data,
                    account=account,
                    routing_name=contact,
                )
            except Exception:
                log.warning("私聊 route 持久化失败，本轮不推送")
                with self.buffer_lock:
                    current = self.pending_buffers.get(sender_id)
                    if current is not None:
                        current["messages"] = msgs + current.get("messages", [])
                        current["source_messages"] = (
                            source_messages + current.get("source_messages", [])
                        )
                        current["processing"] = False
                return
            if (
                reply_target_id is not None
                and int(reply_target_id) != int(user_id)
            ):
                log.warning("私聊 reply epoch 目标与持久路由不一致，本轮不推送")
                with self.buffer_lock:
                    current = self.pending_buffers.get(sender_id)
                    if current is not None:
                        current["processing"] = False
                return

        if is_group:
            group_id = state._wxid_to_int(
                entry.get("group_name", contact),
                account=account,
                identity_type="group",
            )
            sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "未知")

            if state.group_reply_mode == "batch":
                # 批处理模式：消息已预格式化好，直接使用
                formatted = combined
            else:
                # 去掉消息中的 @机器人 纯文本，换为 OneBot at 元素
                clean_text = combined
                for nick in config.BOT_NICKNAMES:
                    at_pattern = f"@{nick}"
                    if at_pattern in clean_text:
                        clean_text = clean_text.replace(at_pattern, "").strip()

                formatted = clean_text
                if sender_name:
                    formatted = f'{sender_name}在群{entry.get("group_name", contact)}中说：{clean_text}'

            # 消息段：先 at 机器人（让 aiocqhttp 识别为 @），再发文本
            msg_segments = [
                {"type": "at", "data": {"qq": str(state._self_id_int)}},
                {"type": "text", "data": {"text": f" {formatted}"}},
            ]
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=entry.get("group_name", contact),
                                       nickname=sender_name,
                                       account=account,
                                       session=session_id_data,
                                       source_messages=source_messages,
                                       routing_name=contact)
        else:
            sender_name = entry.get("source_name", contact)
            event = make_message_event("private", user_id,
                                       [{"type": "text", "data": {"text": combined}}],
                                       nickname=sender_name,
                                       account=account,
                                       session=session_id_data,
                                       source_messages=source_messages,
                                       routing_name=contact,
                                       bridge_generation=reply_generation,
                                       reply_epoch=reply_epoch)

        if not self._active():
            return

        # 记录 user_id → contact 映射，供 API 回复时查找
        if is_group:
            group_id = state._wxid_to_int(
                entry.get("group_name", contact),
                account=account,
                identity_type="group",
            )
            state._ob_id_to_contact[group_id] = contact
            try:
                state.remember_group_route(
                    group_id,
                    account=account,
                    session=session_id_data,
                    routing_name=contact,
                )
            except Exception:
                log.warning("群聊 route 绑定失败，本轮不允许原生收藏表情回复")
        else:
            state._ob_id_to_contact[user_id] = contact

        if is_group or not _merged_runtime_available():
            sent = push_event(event)
            if sent:
                log.info("✅ 已推送至 AstrBot 客户端")
            else:
                log.warning("⚠️ 无 AstrBot 客户端在线")
        else:
            try:
                default_store().spool_batch(
                    event,
                    target_id=int(user_id),
                    generation=int(reply_generation),
                    epoch=int(reply_epoch),
                    raw_ids=(
                        source_message.get("rawid", "")
                        for source_message in source_messages
                    ),
                )
            except (ReplyStoreError, TypeError, ValueError):
                log.warning(
                    "私聊批次无法写入持久 spool: code=E_INGRESS_SPOOL",
                )
                with self.buffer_lock:
                    current = self.pending_buffers.get(sender_id)
                    if current is not None:
                        current["messages"] = msgs + current.get("messages", [])
                        current["source_messages"] = (
                            source_messages + current.get("source_messages", [])
                        )
                        current["reply_target_id"] = reply_target_id
                        current["reply_epoch"] = reply_epoch
                        current["bridge_generation"] = reply_generation
                        current["processing"] = False
                        self._schedule_buffer_locked(sender_id, delay=1.0)
                return
            # Readiness/admission may still be closed.  The durable spool owns
            # the batch now and the background drain will retry without loss.
            self._drain_private_spool()

        with self.buffer_lock:
            if sender_id in self.pending_buffers:
                entry = self.pending_buffers[sender_id]
                entry["processing"] = False
                if entry.get("messages"):
                    self._schedule_buffer_locked(sender_id)

    @staticmethod
    def _buffer_deadline_locked(entry: dict) -> float | None:
        started = entry.get("batch_started_monotonic")
        latest = entry.get("last_accepted_monotonic")
        if not isinstance(started, (int, float)) or not isinstance(
            latest,
            (int, float),
        ):
            return None
        quiet_seconds = float(
            getattr(
                config,
                "BUFFER_QUIET_SECONDS",
                getattr(config, "BUFFER_SECONDS", 5.0),
            )
        )
        max_seconds = float(
            getattr(
                config,
                "BUFFER_MAX_SECONDS",
                getattr(config, "BUFFER_SECONDS", 5.0),
            )
        )
        return min(
            float(started) + max(0.0, max_seconds),
            float(latest) + max(0.0, quiet_seconds),
        )

    def _schedule_buffer_locked(self, buffer_key: str, delay: float | None = None):
        """Start or restart a buffer timer. Caller must hold buffer_lock."""
        if not self._active():
            return
        entry = self.pending_buffers.get(buffer_key)
        if not entry or entry.get("processing"):
            return
        if entry.get("timer"):
            entry["timer"].cancel()
        entry["timer_version"] = entry.get("timer_version", 0) + 1
        version = entry["timer_version"]
        if delay is None:
            deadline = self._buffer_deadline_locked(entry)
            wait_seconds = (
                getattr(config, "BUFFER_SECONDS", 5.0)
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
        else:
            wait_seconds = max(0.0, float(delay))
        timer = threading.Timer(
            wait_seconds,
            lambda v=version, sid=buffer_key: self.process_sender(sid, v),
        )
        timer.daemon = True
        timer.start()
        entry["timer"] = timer

    def listen_sse(self):
        """连接 WeFlow SSE 推送。"""
        if not self._active():
            return
        sse_url = f"{config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token={config.ACCESS_TOKEN}"
        log.info("连接 WeFlow 推送服务: /api/v1/push/messages")
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

        try:
            self._sse_session = requests.get(sse_url, headers=headers, stream=True, timeout=None)
            if self._sse_session.status_code != 200:
                log.error(f"连接失败: HTTP {self._sse_session.status_code}")
                return
            log.info("✅ 已连接到 WeFlow 推送")

            for line in self._sse_session.iter_lines(decode_unicode=True):
                if not self._active():
                    break
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        msg_time = data.get("timestamp", 0)
                        if msg_time < self.start_timestamp:
                            continue
                        raw_id = _text_value(data.get("rawid"))
                        if raw_id:
                            if raw_id in self.processed_ids:
                                continue
                        filtered_type = self._inbound_filter_type(data)
                        if filtered_type:
                            if raw_id:
                                self.processed_ids.add(raw_id)
                            if filtered_type == "group":
                                self._remember_filtered_group_route(data)
                            log.info(
                                "⏭️ 已过滤 %s 类型入站消息",
                                filtered_type,
                            )
                            continue
                        if (
                            self.money_actions is not None
                            and self.money_actions.handle_sse(data)
                        ):
                            if raw_id:
                                self.processed_ids.add(raw_id)
                            self.log_money_event(data)
                            log.info("检测到红包/转账候选；普通 FIFO 已进入等待")
                            continue
                        if not self.should_ignore(data):
                            log.info(
                                "📩 收到 SSE 消息: content_length=%d",
                                len(data.get("content", "")),
                            )
                            accepted = self.add_to_buffer(data)
                            if accepted and raw_id:
                                self.processed_ids.add(raw_id)
                        elif raw_id:
                            self.processed_ids.add(raw_id)
                    except json.JSONDecodeError:
                        pass

        except requests.exceptions.ConnectionError:
            log.error("无法连接 WeFlow")
        except Exception:
            log.error("SSE 推送连接异常")
        finally:
            self._sse_session = None

    def _fetch_wechat_image(self, talker: str) -> str | None:
        """从 WeFlow REST API 获取最新图片并保存到本地"""
        try:
            url = f"{config.WE_FLOW_BASE_URL}/api/v1/messages"
            params = {
                "access_token": config.ACCESS_TOKEN,
                "talker": talker,
                "media": "true",
                "limit": 3,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                log.error(f"WeFlow 消息API: HTTP {resp.status_code}")
                return None

            data = resp.json()
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if not isinstance(messages, list):
                messages = []

            for msg in messages:
                if msg.get("mediaType") == "image" and msg.get("mediaUrl"):
                    media_url = msg["mediaUrl"]
                    sep = "&" if "?" in media_url else "?"
                    dl_url = f"{media_url}{sep}access_token={config.ACCESS_TOKEN}"

                    img_resp = requests.get(dl_url, timeout=30)
                    if img_resp.status_code != 200:
                        continue

                    # 根据 Content-Type 确定扩展名
                    ct = img_resp.headers.get("Content-Type", "")
                    ext = ".jpg"
                    if "png" in ct: ext = ".png"
                    elif "gif" in ct: ext = ".gif"
                    elif "webp" in ct: ext = ".webp"

                    filename = f"wechat_{int(time.time())}{ext}"
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images")
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(img_resp.content)

                    log.info("✅ 微信图片已保存: bytes=%d", len(img_resp.content))
                    return save_path

            log.warning("消息列表无图片 mediaUrl")
            return None
        except Exception:
            log.error("获取微信图片异常")
            return None

    def _fetch_wechat_sticker(self, data: dict) -> str | None:
        """按 SSE 原始消息 ID 从 WeFlow 精确导出并受限下载表情媒体。"""

        session_id = _text_value(data.get("sessionId"))
        raw_id = _text_value(data.get("rawid"))
        if not session_id or not raw_id:
            return None
        timestamp = _source_timestamp(data.get("timestamp"))
        numeric_time = (
            float(timestamp)
            if isinstance(timestamp, (int, float))
            else 0.0
        )
        delta = 2000 if numeric_time > 10_000_000_000 else 2
        params = {
            "access_token": config.ACCESS_TOKEN,
            "talker": session_id,
            "media": "true",
            "image": "false",
            "voice": "false",
            "video": "false",
            "emoji": "true",
            "limit": 20,
        }
        if numeric_time:
            params["start"] = numeric_time - delta
            params["end"] = numeric_time + delta

        part_path = ""
        final_path = ""
        download = None
        try:
            response = requests.get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
                timeout=10,
            )
            if response.status_code != 200:
                log.warning("WeFlow 表情消息查询失败: status=%s", response.status_code)
                return None
            payload = response.json()
            messages = (
                payload
                if isinstance(payload, list)
                else payload.get("messages", payload.get("data", []))
            )
            if not isinstance(messages, list):
                return None
            sticker_rows = [
                item
                for item in messages
                if isinstance(item, dict)
                and item.get("mediaUrl")
                and _text_value(item.get("isSend")).casefold()
                not in {"1", "true", "yes", "on"}
                and (
                    _text_value(item.get("localType")) == "47"
                    or _text_value(item.get("mediaType")).casefold()
                    in {"emoji", "sticker"}
                )
            ]
            selected = next(
                (
                    item
                    for item in sticker_rows
                    if _text_value(item.get("serverId")) == raw_id
                ),
                None,
            )
            if selected is None:
                log.warning("WeFlow 未返回与原始消息匹配的表情媒体")
                return None

            media_url = _text_value(selected.get("mediaUrl"))
            absolute_url = urljoin(
                config.WE_FLOW_BASE_URL.rstrip("/") + "/",
                media_url,
            )
            base_parts = urlsplit(config.WE_FLOW_BASE_URL)
            media_parts = urlsplit(absolute_url)
            if (
                media_parts.scheme not in ("http", "https")
                or media_parts.hostname != base_parts.hostname
                or media_parts.port != base_parts.port
                or not media_parts.path.startswith("/api/v1/media/")
            ):
                log.warning("拒绝非 WeFlow 本地来源的表情 URL")
                return None

            download = requests.get(
                absolute_url,
                headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
                stream=True,
                timeout=(5, 30),
            )
            if download.status_code != 200:
                log.warning("WeFlow 表情下载失败: status=%s", download.status_code)
                return None
            content_type = (
                _text_value(download.headers.get("Content-Type"))
                .split(";", 1)[0]
                .lower()
            )
            extension = _STICKER_CONTENT_TYPES.get(content_type)
            if extension is None:
                log.warning("拒绝非图片格式的表情响应")
                return None
            raw_length = _text_value(download.headers.get("Content-Length"))
            if raw_length:
                if int(raw_length) > _STICKER_MAX_BYTES:
                    log.warning("表情超过描述大小上限")
                    return None

            save_root = config.ASTRBOT_ATTACHMENTS or tempfile.gettempdir()
            save_dir = os.path.join(save_root, "wechat_stickers")
            os.makedirs(save_dir, exist_ok=True)
            identity = "\0".join((session_id, raw_id, str(timestamp)))
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            final_path = os.path.join(save_dir, f"wechat_{digest}{extension}")
            part_path = final_path + ".part"
            total = 0
            with open(part_path, "wb") as output:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _STICKER_MAX_BYTES:
                        raise ValueError("sticker too large")
                    output.write(chunk)
            if total <= 0:
                return None
            os.replace(part_path, final_path)
            part_path = ""
            log.info("✅ 微信表情已临时下载: bytes=%d", total)
            return final_path
        except (TypeError, ValueError):
            log.warning("表情超过描述大小上限或响应无效")
            return None
        except requests.Timeout:
            log.warning("获取微信表情超时")
            return None
        except Exception:
            log.warning("获取微信表情异常")
            return None
        finally:
            if download is not None:
                close = getattr(download, "close", None)
                if callable(close):
                    close()
            if part_path:
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    def _fetch_wechat_video(self, data: dict) -> str | None:
        """按 SSE 原始消息 ID 从 WeFlow 精确导出并受限下载视频。"""

        session_id = _text_value(data.get("sessionId"))
        if not session_id:
            return None
        raw_id = _text_value(data.get("rawid"))
        timestamp = _source_timestamp(data.get("timestamp"))
        numeric_time = (
            float(timestamp)
            if isinstance(timestamp, (int, float))
            else 0.0
        )
        delta = 2000 if numeric_time > 10_000_000_000 else 2
        params = {
            "access_token": config.ACCESS_TOKEN,
            "talker": session_id,
            "media": "true",
            "image": "false",
            "voice": "false",
            "video": "true",
            "emoji": "false",
            "limit": 20,
        }
        if numeric_time:
            params["start"] = numeric_time - delta
            params["end"] = numeric_time + delta

        part_path = ""
        final_path = ""
        download = None
        try:
            response = requests.get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
                timeout=10,
            )
            if response.status_code != 200:
                log.warning("WeFlow 视频消息查询失败: status=%s", response.status_code)
                return None
            payload = response.json()
            messages = (
                payload
                if isinstance(payload, list)
                else payload.get("messages", payload.get("data", []))
            )
            if not isinstance(messages, list):
                return None
            video_rows = [
                item
                for item in messages
                if isinstance(item, dict)
                and item.get("mediaType") == "video"
                and item.get("mediaUrl")
            ]
            selected = None
            if raw_id:
                selected = next(
                    (
                        item
                        for item in video_rows
                        if _text_value(item.get("serverId")) == raw_id
                    ),
                    None,
                )
            else:
                timed = []
                for item in video_rows:
                    item_time = _source_timestamp(
                        item.get("createTime") or item.get("timestamp")
                    )
                    if not isinstance(item_time, (int, float)):
                        continue
                    if numeric_time and abs(float(item_time) - numeric_time) > delta:
                        continue
                    if bool(item.get("isSend")):
                        continue
                    if _text_value(item.get("localType")) not in ("", "43"):
                        continue
                    timed.append(item)
                if len(timed) == 1:
                    selected = timed[0]
            if selected is None:
                log.warning("WeFlow 未返回可唯一匹配的视频媒体")
                return None

            media_url = _text_value(selected.get("mediaUrl"))
            absolute_url = urljoin(
                config.WE_FLOW_BASE_URL.rstrip("/") + "/",
                media_url,
            )
            base_parts = urlsplit(config.WE_FLOW_BASE_URL)
            media_parts = urlsplit(absolute_url)
            if (
                media_parts.scheme not in ("http", "https")
                or media_parts.hostname != base_parts.hostname
                or media_parts.port != base_parts.port
            ):
                log.warning("拒绝非 WeFlow 本地来源的视频 URL")
                return None

            download = requests.get(
                absolute_url,
                headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
                stream=True,
                timeout=(5, 60),
            )
            if download.status_code != 200:
                log.warning("WeFlow 视频下载失败: status=%s", download.status_code)
                return None
            content_type = (
                _text_value(download.headers.get("Content-Type"))
                .split(";", 1)[0]
                .lower()
            )
            if content_type not in ("video/mp4", "application/octet-stream"):
                log.warning("拒绝非 MP4 视频响应")
                return None
            maximum = int(config.VIDEO_CAPTION_MAX_MIB) * 1024 * 1024
            raw_length = _text_value(download.headers.get("Content-Length"))
            if raw_length:
                try:
                    if int(raw_length) > maximum:
                        log.warning("视频超过描述大小上限")
                        return None
                except ValueError:
                    return None

            save_root = config.ASTRBOT_ATTACHMENTS or tempfile.gettempdir()
            save_dir = os.path.join(save_root, "wechat_videos")
            os.makedirs(save_dir, exist_ok=True)
            identity = "\0".join((session_id, raw_id, str(timestamp)))
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            final_path = os.path.join(save_dir, f"wechat_{digest}.mp4")
            part_path = final_path + ".part"
            total = 0
            with open(part_path, "wb") as output:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError("video too large")
                    output.write(chunk)
            if total <= 0:
                return None
            os.replace(part_path, final_path)
            part_path = ""
            log.info("✅ 微信视频已临时下载: bytes=%d", total)
            return final_path
        except ValueError:
            log.warning("视频超过描述大小上限或响应无效")
            return None
        except requests.Timeout:
            log.warning("获取微信视频超时")
            return None
        except Exception:
            log.warning("获取微信视频异常")
            return None
        finally:
            if download is not None:
                close = getattr(download, "close", None)
                if callable(close):
                    close()
            if part_path:
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    @staticmethod
    def _reply_metadata_from_data(
        data: dict,
    ) -> tuple[int, int, int] | None:
        try:
            target_id = int(data.get("_akasha_reply_target_id"))
            generation = int(data.get("_akasha_bridge_generation"))
            reply_epoch = int(data.get("_akasha_reply_epoch"))
        except (TypeError, ValueError):
            return None
        if target_id <= 0 or generation <= 0 or reply_epoch <= 0:
            return None
        return target_id, generation, reply_epoch

    @classmethod
    def _touch_media_buffer_entry(cls, entry: dict, data: dict) -> None:
        accepted_at = time.monotonic()
        if entry.get("batch_started_monotonic") is None:
            entry["batch_started_monotonic"] = accepted_at
        entry["last_accepted_monotonic"] = accepted_at
        metadata = cls._reply_metadata_from_data(data)
        if metadata is not None:
            target_id, generation, reply_epoch = metadata
            entry["reply_target_id"] = target_id
            entry["bridge_generation"] = generation
            entry["reply_epoch"] = reply_epoch

    def _enqueue_media_caption(
        self,
        data: dict,
        media_label: str,
        caption_text: str,
    ) -> None:
        """把一条安全的媒体短转述作为文本消息送入既有缓冲链。"""

        session_id = _text_value(data.get("sessionId"))
        source_name = _text_value(data.get("sourceName")) or "未知"
        group_name = _text_value(data.get("groupName"))
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        talker_id = _text_value(data.get("talkerId")) or session_id
        media_text = f"[{media_label}: {caption_text}]"
        reply_metadata = self._reply_metadata_from_data(data)
        if (
            reply_metadata is not None
            and not state.is_reply_current(*reply_metadata)
        ):
            log.info("丢弃已被新消息淘汰的媒体描述")
            return
        with self.buffer_lock:
            if is_group and state.group_reply_mode == "batch" and group_name:
                base_name = re.sub(r"\s*\(\d+\)\s*$", "", group_name).strip()
                buffer_key = f"__batch__{base_name}"
                rendered = (
                    f'成员"{source_name}"在群"{group_name}"中对你说：{media_text}'
                )
            else:
                media_identity = (
                    _text_value(data.get("rawid"))
                    or _event_fingerprint(data)
                )
                buffer_key = f"{talker_id}::__media__:{media_identity}"
                rendered = media_text

            if buffer_key in self.pending_buffers:
                entry = self.pending_buffers[buffer_key]
                entry["messages"].insert(0, rendered)
                _insert_source_message_ref(entry, data, 0)
                self._touch_media_buffer_entry(entry, data)
                self._schedule_buffer_locked(buffer_key)
                if not is_group:
                    default_store().mark_raw_envelope_buffered(
                        _text_value(data.get("rawid"))
                    )
                return

            self.pending_buffers[buffer_key] = {
                "messages": [rendered],
                "timer": None,
                "timer_version": 0,
                "processing": False,
                "contact": group_name if is_group and group_name else source_name,
                "is_group": is_group,
                "source_name": source_name,
                "session_id_data": session_id,
                "group_name": group_name if is_group else "",
                "sender_in_group": source_name if is_group else "",
                "source_messages": [_source_message_ref(data, 1)],
                "batch_started_monotonic": None,
                "last_accepted_monotonic": None,
                "reply_target_id": None,
                "reply_epoch": None,
                "bridge_generation": None,
            }
            self._touch_media_buffer_entry(
                self.pending_buffers[buffer_key],
                data,
            )
            self._schedule_buffer_locked(buffer_key)
            if not is_group:
                default_store().mark_raw_envelope_buffered(
                    _text_value(data.get("rawid"))
                )

    def _enqueue_video_caption(self, data: dict, caption_text: str) -> None:
        self._enqueue_media_caption(data, "视频", caption_text)

    def process_sticker_message(self, data: dict) -> None:
        """导出自定义表情，以图片短转述注入既有文本链。"""

        if not self._active():
            return
        session_id = _text_value(data.get("sessionId"))
        group_name = _text_value(data.get("groupName"))
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        if not is_group and not session_id:
            log.warning("跳过缺少稳定 sessionId 的私聊表情消息")
            return

        sticker_path = self._fetch_wechat_sticker(data)
        try:
            caption = (
                caption_image_via_ollama(sticker_path)
                if sticker_path
                else None
            )
            normalized = " ".join(caption.split()) if caption else ""
            if len(normalized) > 180:
                normalized = normalized[:179].rstrip() + "…"
            caption_text = (
                f"微信表情包：{normalized}"
                if normalized
                else "微信表情包（内容无法描述）"
            )
            if not self._active():
                return
            # 自定义表情是 GIF/WebP 等图片媒体，沿用图片语义记忆链；
            # 原生 Unicode emoji 仍然是普通文本，不会进入此路径。
            self._enqueue_media_caption(data, "图片", caption_text)
        finally:
            if sticker_path:
                try:
                    os.remove(sticker_path)
                except OSError:
                    pass

    def process_video_message(self, data: dict) -> None:
        """从 WeFlow 取视频，经视觉模型转述后注入既有文本链。"""

        if not self._active():
            return
        session_id = _text_value(data.get("sessionId"))
        group_name = _text_value(data.get("groupName"))
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        if not is_group and not session_id:
            log.warning("跳过缺少稳定 sessionId 的私聊视频消息")
            return

        video_path = self._fetch_wechat_video(data)
        try:
            caption = caption_video_via_openai(video_path) if video_path else None
            caption_text = (
                " ".join(caption.split())
                if caption
                else "（视频内容无法描述）"
            )
            if not self._active():
                return
            self._enqueue_video_caption(data, caption_text)
        finally:
            if video_path:
                try:
                    os.remove(video_path)
                except OSError:
                    pass

    def process_image_message(self, data):
        """处理图片消息：从 WeFlow 取图 → ollama 描述 → 注入缓冲区"""
        if not self._active():
            return
        session_id = _text_value(data.get("sessionId"))
        source_name = _text_value(data.get("sourceName")) or "未知"
        group_name = _text_value(data.get("groupName"))
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        if not is_group and not session_id:
            log.warning("跳过缺少稳定 sessionId 的私聊图片消息")
            return

        log.info("🖼️ 收到图片消息: group=%s", bool(group_name))

        talker_id = _text_value(data.get("talkerId")) or session_id

        # 注册待处理的图片（ollama 完成前标记为 pending）
        img_event = threading.Event()
        self._pending_image[talker_id] = {"caption": None, "event": img_event}

        try:
            # 取图 + ollama 描述
            image_path = self._fetch_wechat_image(session_id)
            caption = None
            if image_path:
                caption = caption_image_via_ollama(image_path)

            caption_text = " ".join(caption.split()) if caption else None
            if caption_text:
                log.info("📝 图片描述完成: characters=%d", len(caption_text))
            else:
                log.info("⚠️ 图片描述失败")
                caption_text = "（图片内容无法描述）"

            if not self._active():
                return

            reply_metadata = self._reply_metadata_from_data(data)
            if (
                reply_metadata is not None
                and not state.is_reply_current(*reply_metadata)
            ):
                log.info("丢弃已被新消息淘汰的图片描述")
                return

            # Media is an independent inbound batch.  Its asynchronous
            # caption completion never appends into an open text batch.
            self._enqueue_media_caption(data, "图片", caption_text)
            return

            # 注入图片描述到缓冲区
            with self.buffer_lock:
                self._pending_image[talker_id] = {"caption": caption_text, "event": img_event}

                # 批处理模式用群共享 key
                if is_group and state.group_reply_mode == "batch" and group_name:
                    g_base = re.sub(r'\s*\(\d+\)\s*$', '', group_name).strip()
                    batch_key = f"__batch__{g_base}"
                    if batch_key in self.pending_buffers:
                        entry = self.pending_buffers[batch_key]
                        entry["messages"].insert(0, f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]')
                        _insert_source_message_ref(entry, data, 0)
                        entry["image_ready"] = True
                        self._touch_media_buffer_entry(entry, data)
                        self._schedule_buffer_locked(batch_key)
                        log.info(f"📝 图片已注入批处理队列")
                        return
                    # 没有文字排队，用 batch key 创建独立条目
                    self.pending_buffers[batch_key] = {
                        "messages": [f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]'],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name,
                        "is_group": True,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name,
                        "sender_in_group": source_name,
                        "source_messages": [_source_message_ref(data, 1)],
                        "batch_started_monotonic": None,
                        "last_accepted_monotonic": None,
                        "reply_target_id": None,
                        "reply_epoch": None,
                        "bridge_generation": None,
                    }
                    log.info(f"📩 图片无文本跟随，创建批处理图片条目")
                    self._touch_media_buffer_entry(
                        self.pending_buffers[batch_key],
                        data,
                    )
                    self._schedule_buffer_locked(batch_key)
                elif talker_id in self.pending_buffers:
                    # 已有文本在排队，注入图片上下文
                    entry = self.pending_buffers[talker_id]
                    entry["messages"].insert(0, f"[图片: {caption_text}]")
                    _insert_source_message_ref(entry, data, 0)
                    entry["image_ready"] = True
                    self._touch_media_buffer_entry(entry, data)
                    self._schedule_buffer_locked(talker_id)
                    log.info(f"📝 图片已注入待处理文本队列")
                else:
                    # 没有文本排队，创建单条图片消息处理
                    log.info(f"📩 图片无文本跟随，直接处理")
                    self.pending_buffers[talker_id] = {
                        "messages": [f"[图片: {caption_text}]"],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name if is_group and group_name else source_name,
                        "is_group": is_group,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name if is_group else "",
                        "sender_in_group": "",
                        "source_messages": [_source_message_ref(data, 1)],
                        "batch_started_monotonic": None,
                        "last_accepted_monotonic": None,
                        "reply_target_id": None,
                        "reply_epoch": None,
                        "bridge_generation": None,
                    }
                    self._touch_media_buffer_entry(
                        self.pending_buffers[talker_id],
                        data,
                    )
                    self._schedule_buffer_locked(talker_id)
        finally:
            # 确保 Event 被设置
            img_event.set()


def caption_image_via_ollama(image_path: str) -> str | None:
    """对图片进行文字描述，支持 ollama 和 OpenAI 兼容 API 两种后端。"""
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        image_mime = {
            ".gif": "image/gif",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(os.path.splitext(image_path)[1].lower(), "image/jpeg")

        if config.IMAGE_CAPTION_PROVIDER == "openai":
            # OpenAI 兼容 API（mimo）
            resp = requests.post(
                f"{config.IMAGE_CAPTION_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.IMAGE_CAPTION_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": config.IMAGE_CAPTION_PROMPT},
                            {"type": "image_url", "image_url": {
                                "url": f"data:{image_mime};base64,{img_b64}"
                            }},
                        ],
                    }],
                    "max_tokens": 300,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                caption = resp.json()["choices"][0]["message"]["content"].strip()
                if caption:
                    log.info("🖼️ 图片描述完成: characters=%d", len(caption))
                    return caption
            else:
                log.warning("mimo 返回 HTTP %s", resp.status_code)
        else:
            # ollama 原生 API
            resp = requests.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "prompt": config.IMAGE_CAPTION_PROMPT,
                    "images": [img_b64],
                    "stream": False,
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            if resp.status_code == 200:
                caption = resp.json().get("response", "").strip()
                if caption:
                    log.info("🖼️ 图片描述完成: characters=%d", len(caption))
                    return caption
            else:
                log.warning("ollama 返回 HTTP %s", resp.status_code)

    except requests.Timeout:
        log.warning(f"图片描述超时 (30s)")
    except Exception:
        log.warning("图片描述失败")
    return None


def caption_video_via_openai(video_path: str) -> str | None:
    """用 OpenAI 兼容多模态接口将本地 MP4 转述为短文本。"""

    if (
        not video_path
        or config.IMAGE_CAPTION_PROVIDER != "openai"
        or not config.IMAGE_CAPTION_API_KEY
    ):
        log.warning("视频描述需要已配置的 OpenAI 兼容视觉服务")
        return None
    try:
        import base64

        with open(video_path, "rb") as video_file:
            video_b64 = base64.b64encode(video_file.read()).decode("ascii")
        response = requests.post(
            f"{config.IMAGE_CAPTION_API_BASE.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.IMAGE_CAPTION_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.IMAGE_CAPTION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": config.VIDEO_CAPTION_PROMPT},
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": f"data:video/mp4;base64,{video_b64}"
                            },
                            "fps": 2,
                        },
                    ],
                }],
                "max_tokens": 500,
            },
            timeout=(10, 180),
        )
        if response.status_code != 200:
            log.warning("视频描述服务返回 HTTP %s", response.status_code)
            return None
        caption = response.json()["choices"][0]["message"]["content"].strip()
        if caption:
            log.info("🎬 视频描述完成: characters=%d", len(caption))
            return caption
    except requests.Timeout:
        log.warning("视频描述超时")
    except Exception:
        log.warning("视频描述失败")
    return None
