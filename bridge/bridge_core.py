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
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests

import state
import config
from ob_protocol import push_event, make_message_event
from privacy import chat_record

log = logging.getLogger("ob11-bridge")


# ============ 桥接核心 ============


def _text_value(value) -> str:
    return "" if value is None else str(value).strip()


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


def _event_fingerprint(data: dict) -> str:
    fingerprint_fields = {
        "content": _exact_text_value(data.get("content")),
        "message_type": _text_value(data.get("type") or data.get("msgType")),
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

    def _active(self):
        return (
            True
            if self.generation is None
            else state.is_generation_running(self.generation)
        )

    def should_ignore(self, data):
        content = data.get("content", "")
        msg_type = data.get("type", 0) or data.get("msgType", 0)
        if data.get("sourceName", "") in config.BOT_NICKNAMES:
            return True
        if config.BOT_WXID and data.get("talkerId", "") == config.BOT_WXID:
            return True
        if msg_type in (34,):  # 34=语音
            return True
        if content and ("[语音]" in content or "[表情]" in content):
            return True
        if not content or content.strip() == "":
            return True
        return False

    def add_to_buffer(self, data):
        """将消息加入缓冲区，等待合并后统一推送给 AstrBot。"""
        if not self._active():
            return
        content = data.get("content", "")
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
            return
        session_id_data = raw_session_id or source_name

        now = time.time()
        if content and content in self._sent_recently and now - self._sent_recently[content] < 120:
            log.info("⏭️ 自回复去重跳过: content_length=%d", len(content))
            return

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

        if content == "[图片]":
            # 图片消息：下载 → ollama 描述 → 注入缓冲区
            threading.Thread(
                target=self.process_image_message,
                args=(data,),
                daemon=True,
            ).start()
            return
        if content == "[视频]":
            threading.Thread(
                target=self.process_video_message,
                args=(data,),
                daemon=True,
            ).start()
            return

        if (
            is_group
            and state.group_reply_mode == "mention"
            and not any(f"@{n}" in content for n in config.BOT_NICKNAMES)
        ):
            return

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
                }
            entry = self.pending_buffers[buffer_key]
            if is_group and state.group_reply_mode == "batch" and sender_in_group:
                entry["messages"].append(f'成员"{sender_in_group}"在群"{base_name}"中对你说：{content}')
            else:
                entry["messages"].append(content)
            _insert_source_message_ref(entry, data)

            if not entry["processing"]:
                if entry["timer"]:
                    entry["timer"].cancel()
                entry["timer_version"] += 1
                version = entry["timer_version"]
                log.info(
                    "📩 消息已进入缓冲: content_length=%d group=%s wait_seconds=%s",
                    len(content), is_group, config.BUFFER_SECONDS,
                )
                timer = threading.Timer(config.BUFFER_SECONDS, lambda v=version, sid=buffer_key: self.process_sender(sid, v))
                timer.daemon = True
                timer.start()
                entry["timer"] = timer

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
                                       routing_name=contact)

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
        else:
            state._ob_id_to_contact[user_id] = contact

        sent = push_event(event)
        if sent > 0:
            log.info("✅ 已推送至 AstrBot 客户端: clients=%s", sent)
        else:
            log.warning("⚠️ 无 AstrBot 客户端在线")

        with self.buffer_lock:
            if sender_id in self.pending_buffers:
                entry = self.pending_buffers[sender_id]
                entry["processing"] = False
                if entry.get("messages"):
                    self._schedule_buffer_locked(sender_id, config.BUFFER_SECONDS)

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
        wait_seconds = config.BUFFER_SECONDS if delay is None else delay
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
                            self.processed_ids.add(raw_id)
                        if not self.should_ignore(data):
                            log.info(
                                "📩 收到 SSE 消息: content_length=%d",
                                len(data.get("content", "")),
                            )
                            self.add_to_buffer(data)
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

    def _enqueue_video_caption(self, data: dict, caption_text: str) -> None:
        """把一条视频语义作为文本消息送入既有缓冲链。"""

        session_id = _text_value(data.get("sessionId"))
        source_name = _text_value(data.get("sourceName")) or "未知"
        group_name = _text_value(data.get("groupName"))
        is_group = (
            data.get("sessionType", "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        talker_id = _text_value(data.get("talkerId")) or session_id
        media_text = f"[视频: {caption_text}]"
        with self.buffer_lock:
            if is_group and state.group_reply_mode == "batch" and group_name:
                base_name = re.sub(r"\s*\(\d+\)\s*$", "", group_name).strip()
                buffer_key = f"__batch__{base_name}"
                rendered = (
                    f'成员"{source_name}"在群"{group_name}"中对你说：{media_text}'
                )
            else:
                buffer_key = talker_id
                rendered = media_text

            if buffer_key in self.pending_buffers:
                entry = self.pending_buffers[buffer_key]
                entry["messages"].insert(0, rendered)
                _insert_source_message_ref(entry, data, 0)
                self._schedule_buffer_locked(buffer_key, 2)
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
            }
            timer_delay = 5 if is_group and state.group_reply_mode == "batch" else 2
            timer = threading.Timer(
                timer_delay,
                lambda v=1, sid=buffer_key: self.process_sender(sid, v),
            )
            timer.daemon = True
            timer.start()
            self.pending_buffers[buffer_key]["timer"] = timer
            self.pending_buffers[buffer_key]["timer_version"] = 1

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
                        self._schedule_buffer_locked(batch_key, 2)
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
                    }
                    log.info(f"📩 图片无文本跟随，创建批处理图片条目")
                    version = 1
                    timer = threading.Timer(5, lambda v=version, sid=batch_key: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[batch_key]["timer"] = timer
                    self.pending_buffers[batch_key]["timer_version"] = version
                elif talker_id in self.pending_buffers:
                    # 已有文本在排队，注入图片上下文
                    entry = self.pending_buffers[talker_id]
                    entry["messages"].insert(0, f"[图片: {caption_text}]")
                    _insert_source_message_ref(entry, data, 0)
                    entry["image_ready"] = True
                    self._schedule_buffer_locked(talker_id, 2)
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
                    }
                    version = 1
                    timer = threading.Timer(2, lambda v=version, sid=talker_id: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[talker_id]["timer"] = timer
                    self.pending_buffers[talker_id]["timer_version"] = version
        finally:
            # 确保 Event 被设置
            img_event.set()


def caption_image_via_ollama(image_path: str) -> str | None:
    """对图片进行文字描述，支持 ollama 和 OpenAI 兼容 API 两种后端。"""
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

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
                                "url": f"data:image/jpeg;base64,{img_b64}"
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
