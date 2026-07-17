"""
OneBot v11 协议处理模块。

包括：
- make_message_event() — 构造 OneBot 消息事件 JSON
- push_event() — 通过 WebSocket 推送事件给 AstrBot
- _handle_ob_api() — 处理 AstrBot 发来的 API 请求（send_msg 等）
- _extract_text() — 从 OneBot message 段提取纯文本
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import logging

import requests

import state
import config

log = logging.getLogger("ob11-bridge")


async def _handle_ob_api(data: dict):
    """处理 AstrBot 发来的 API 请求。"""
    action = data.get("action", "")
    params = data.get("params", {})
    echo = data.get("echo", "")
    log.info("[OB11] 收到 API 请求: has_echo=%s", bool(echo))

    # 先回响应（必须在处理消息前回，否则 AstrBot 超时）
    resp_sent = False
    resp_data = {"status": "ok", "retcode": 0, "data": {}}
    if echo:
        resp_data["echo"] = echo
    # 如果 WS 暂时断连，等一会重试
    for retry in range(10):
        try:
            if state._ob_ws:
                await state._ob_ws.send(json.dumps(resp_data, ensure_ascii=False))
                resp_sent = True
                log.info("[OB11] API 响应已发送")
                break
            if retry < 9:
                await asyncio.sleep(0.5)
        except Exception:
            log.warning("[OB11] API 响应发送失败: attempt=%s/10", retry + 1)
            if retry < 9:
                await asyncio.sleep(0.5)
    if not resp_sent:
        log.warning("[OB11] WS 未连接，API 响应未发送；继续尝试本地处理")

    if action in ("send_msg", "send_private_msg", "send_group_msg"):
        is_group = action == "send_group_msg"
        target_id = params.get("group_id" if is_group else "user_id", 0)
        message = params.get("message", [])
        contact = state._ob_id_to_contact.get(target_id, str(target_id))

        # 逐段处理：文字和图片分别发送
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})

            if seg_type == "text":
                text = seg_data.get("text", "")
                if text:
                    sent = await asyncio.to_thread(
                        state.sender_instance.send_text,
                        contact,
                        text,
                    )
                    if sent is True:
                        log.info("[OB11] 文字已发送: characters=%s", len(text))
                    else:
                        log.error("[OB11] 消息发送失败")

            elif seg_type == "image":
                file_val = seg_data.get("file", "")
                if not file_val:
                    continue

                img_path = None
                temporary_image = False

                # AstrBot 通过 aiocqhttp 发图片时用 base64:// 格式
                if file_val.startswith("base64://"):
                    try:
                        # 解码 + 写文件在线程池执行，避免大图卡死事件循环
                        b64_data = file_val[9:]
                        img_path = await asyncio.to_thread(_decode_base64_image, b64_data)
                        if img_path:
                            temporary_image = True
                            log.info("[OB11] 图片已解码")
                    except Exception:
                        log.warning("[OB11] base64 图片解码失败")
                else:
                    # 文件名模式：在附件目录找
                    if config.ASTRBOT_ATTACHMENTS:
                        candidates = [
                            os.path.join(config.ASTRBOT_ATTACHMENTS, file_val),
                            os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images", file_val),
                        ]
                        for p in candidates:
                            if os.path.exists(p):
                                img_path = p
                                break
                        if not img_path:
                            log.warning("[OB11] 图片文件未找到")

                if img_path:
                    try:
                        # 使用线程池执行同步的 UIA 发送，避免阻塞事件循环
                        sent = await asyncio.to_thread(
                            state.sender_instance.send_image,
                            contact,
                            img_path,
                        )
                        if sent is True:
                            log.info("[OB11] 图片已发送")
                        else:
                            log.error("[OB11] 消息发送失败")
                    finally:
                        # 临时文件用完删除
                        if temporary_image:
                            try:
                                os.unlink(img_path)
                            except Exception:
                                pass

            elif seg_type == "face":
                sent = await asyncio.to_thread(
                    state.sender_instance.send_text,
                    contact,
                    "[表情]",
                )
                if sent is True:
                    log.info("[OB11] 表情已发送")
                else:
                    log.error("[OB11] 消息发送失败")

            # 其他类型（record, video 等）忽略

    else:
        log.debug("[OB11] 收到未处理的 API 操作")

    # 注意：API 响应已在函数开头统一发送，此处不再重复


def _extract_text(message: list) -> str:
    """从 OneBot message 段中提取可发送的文本。"""
    text_parts = []
    for seg in message:
        if isinstance(seg, dict):
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "image":
                text_parts.append("[图片]")
            elif t == "face":
                text_parts.append("[表情]")
            elif t == "record":
                text_parts.append("[语音]")
            elif t == "video":
                text_parts.append("[视频]")
            elif t == "reply":
                if d.get("text"):
                    text_parts.append(f'"{d["text"]}"')
            elif t == "at":
                text_parts.append(f"@{d.get('qq', d.get('name', ''))}")
            else:
                # 其他未知类型也尝试提取文本
                text_parts.append(d.get("text", ""))
    return "".join(text_parts).strip()


# ============ OneBot 协议处理 ============


def make_message_event(message_type: str, user_id: int, message: list,
                       group_id: int = 0, group_name: str = "",
                       nickname: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": state._self_id_int,
        "post_type": "message",
    }
    if message_type == "group":
        event["message_type"] = "group"
        event["group_id"] = group_id
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
        event["group_name"] = group_name or str(group_id)
    else:
        event["message_type"] = "private"
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
    return event


def push_event(event: dict) -> bool:
    """通过 WebSocket 客户端连接向 AstrBot 推送事件。"""
    if not state._ob_ws or not state._ob_ws_loop:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            state._ob_ws.send(json.dumps(event, ensure_ascii=False)),
            state._ob_ws_loop,
        )
        future.result(timeout=5)
        return True
    except Exception:
        log.warning("[OB11] 推送事件失败")
        return False


def _close_failed_tempfile(tmp) -> None:
    """Best-effort close without replacing the operation's original error."""
    try:
        tmp.close()
        return
    except BaseException:
        pass

    try:
        raw_file = tmp.file
    except BaseException:
        return
    try:
        raw_file.close()
    except BaseException:
        pass


def _discard_owned_tempfile(tmp, owned_path: str) -> None:
    """Close and remove only the temporary path created by this module."""
    _close_failed_tempfile(tmp)
    for attempt in range(2):
        try:
            os.unlink(owned_path)
            return
        except FileNotFoundError:
            return
        except BaseException:
            if attempt == 0:
                _close_failed_tempfile(tmp)


def _decode_base64_image(b64_data: str) -> str | None:
    """在线程池中执行：解码 base64 图片并保存为临时文件。"""
    img_data = base64.b64decode(b64_data)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    owned_path = os.fspath(tmp.name)
    try:
        tmp.write(img_data)
        tmp.flush()
        tmp.close()
    except BaseException:
        _discard_owned_tempfile(tmp, owned_path)
        raise
    return owned_path
