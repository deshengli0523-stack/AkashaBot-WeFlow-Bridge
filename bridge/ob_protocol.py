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
from privacy import chat_record

log = logging.getLogger("ob11-bridge")
_CONTACTS_LIMIT = 10000
_CONTACTS_TIMEOUT_SECONDS = 5


def _write_outbound_log(scope: str, contact: object, body: object, sent: object) -> None:
    entry = chat_record(
        event="outbound",
        scope=scope,
        contact=contact,
        status="sent" if sent is True else "failed",
        body=body,
    )
    if sent is True:
        log.info("CHAT %s", entry)
    else:
        log.error("CHAT %s", entry)


def _normalize_target_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdecimal():
            parsed = int(normalized)
            return parsed if parsed > 0 else None
    return None


def _private_failure_identity(target_id: int) -> tuple[str, str, str] | None:
    getter = getattr(state, "get_private_route_binding", None)
    if not callable(getter):
        return None
    try:
        binding = getter(target_id)
    except Exception:
        return None
    if not isinstance(binding, dict):
        return None
    routing_name = str(binding.get("routing_name") or "").strip()
    account = str(binding.get("account") or "").strip()
    session = str(binding.get("session") or "").strip()
    if not routing_name or not account or not session:
        return None
    return routing_name, account, session


def _contact_route_names(contact: dict) -> set[str]:
    output = set()
    for key in ("displayName", "remark", "nickname", "alias"):
        value = contact.get(key)
        if isinstance(value, str) and value.strip():
            output.add(value.strip().casefold())
    return output


def _resolve_private_contact(target_id: int) -> tuple[str, str, str] | None:
    try:
        route = state.get_private_route(target_id)
    except Exception:
        log.warning("[OB11] 私聊路由状态不可用")
        return None
    if not isinstance(route, dict):
        log.warning("[OB11] 私聊路由不存在")
        return None
    routing_name = route.get("routing_name")
    if not isinstance(routing_name, str) or not routing_name.strip():
        log.warning("[OB11] 私聊路由名称无效")
        return None
    routing_name = routing_name.strip()
    base_url = str(getattr(config, "WE_FLOW_BASE_URL", "") or "").strip().rstrip("/")
    token = str(getattr(config, "ACCESS_TOKEN", "") or "").strip()
    account = str(getattr(config, "BOT_WXID", "") or "").strip()
    if not account:
        log.warning("[OB11] 私聊账号标识未配置")
        return None
    if not base_url or not token:
        log.warning("[OB11] 私聊联系人校验未配置")
        return None

    try:
        response = requests.get(
            f"{base_url}/api/v1/contacts",
            params={"keyword": routing_name, "limit": _CONTACTS_LIMIT},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=_CONTACTS_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            log.warning(
                "[OB11] 私聊联系人校验失败: status=%s",
                response.status_code,
            )
            return None
        payload = response.json()
    except Exception:
        log.warning("[OB11] 私聊联系人 API 不可用")
        return None

    if not isinstance(payload, dict) or payload.get("success") is not True:
        log.warning("[OB11] 私聊联系人响应无效")
        return None
    contacts = payload.get("contacts")
    count = payload.get("count")
    if (
        not isinstance(contacts, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(contacts)
        or count >= _CONTACTS_LIMIT
        or any(not isinstance(contact, dict) for contact in contacts)
    ):
        log.warning("[OB11] 私聊联系人列表不可验证")
        return None

    normalized_route = routing_name.casefold()
    exact_matches = [
        contact
        for contact in contacts
        if normalized_route in _contact_route_names(contact)
    ]
    if len(exact_matches) != 1:
        log.warning(
            "[OB11] 私聊联系人名称不唯一: matches=%s",
            len(exact_matches),
        )
        return None
    selected = exact_matches[0]
    if str(selected.get("type") or "").strip().casefold() != "friend":
        log.warning("[OB11] 私聊联系人类型不匹配")
        return None
    session = selected.get("username")
    if not isinstance(session, str) or not session.strip():
        log.warning("[OB11] 私聊联系人稳定标识缺失")
        return None
    try:
        target_matches = state.private_route_matches(
            route,
            account=account,
            session=session.strip(),
        )
    except Exception:
        log.warning("[OB11] 私聊联系人稳定标识不可验证")
        return None
    if target_matches is not True:
        log.warning("[OB11] 私聊联系人目标不匹配")
        return None
    return routing_name, account, session.strip()


async def _send_api_response(response_data: dict) -> bool:
    for retry in range(10):
        try:
            if state._ob_ws:
                await state._ob_ws.send(json.dumps(response_data, ensure_ascii=False))
                log.info("[OB11] API 响应已发送")
                return True
            if retry < 9:
                await asyncio.sleep(0.5)
        except Exception:
            log.warning("[OB11] API 响应发送失败: attempt=%s/10", retry + 1)
            if retry < 9:
                await asyncio.sleep(0.5)
    log.warning("[OB11] WS 未连接，API 响应未发送；继续尝试本地处理")
    return False


async def _send_private_send_failure(
    target_id: int,
    *,
    routing_name: str,
    account: str,
    session: str,
) -> bool:
    """Tell the memory plugin that a generated private reply was not sent."""

    return await _send_api_response(
        {
            "time": int(time.time()),
            "self_id": int(getattr(state, "_self_id_int", 0) or 0),
            "post_type": "notice",
            "notice_type": "akasha_send_result",
            "sub_type": "failed",
            "user_id": target_id,
            "akasha_schema": 1,
            "type": "private",
            "account": account,
            "session": session,
            "routing_name": routing_name,
            "source_messages": [],
            "success": False,
        }
    )


async def _handle_ob_api(data: dict, generation=None):
    """处理 AstrBot 发来的 API 请求。"""
    if (
        generation is not None
        and not state.is_generation_running(generation)
    ):
        return
    sender_instance = (
        state.sender_instance
        if generation is None
        else state.get_sender_for_generation(generation)
    )
    if sender_instance is None:
        return
    action = data.get("action", "")
    submitted_params = data.get("params", {})
    params_valid = isinstance(submitted_params, dict)
    params = submitted_params if params_valid else {}
    echo = data.get("echo", "")
    log.info("[OB11] 收到 API 请求: has_echo=%s", bool(echo))

    resp_data = {"status": "ok", "retcode": 0, "data": {}}
    if echo:
        resp_data["echo"] = echo

    if action in ("send_msg", "send_private_msg", "send_group_msg"):
        is_group = action == "send_group_msg" or (
            action == "send_msg"
            and (
                str(params.get("message_type", "")).lower() == "group"
                or params.get("group_id") not in (None, "", 0, "0")
            )
        )
        submitted_target = params.get("group_id" if is_group else "user_id", 0)
        target_id = _normalize_target_id(submitted_target)
        message = params.get("message", [])
        scope = "group" if is_group else "private"
        if not params_valid or target_id is None or not isinstance(message, list):
            if not is_group and target_id is not None:
                failure_identity = await asyncio.to_thread(
                    _private_failure_identity,
                    target_id,
                )
                if failure_identity is not None:
                    routing_name, account, session = failure_identity
                    await _send_private_send_failure(
                        target_id,
                        routing_name=routing_name,
                        account=account,
                        session=session,
                    )
            failed_response = {
                "status": "failed",
                "retcode": 1400,
                "data": {},
            }
            if echo:
                failed_response["echo"] = echo
            await _send_api_response(failed_response)
            contact = "未知"
            _write_outbound_log(scope, contact, "[无效消息]", False)
            return

        private_identity: tuple[str, str, str] | None = None
        if is_group:
            contact = state._ob_id_to_contact.get(target_id, str(target_id))
        else:
            private_identity = await asyncio.to_thread(
                _resolve_private_contact,
                target_id,
            )
            if private_identity is None:
                failure_identity = await asyncio.to_thread(
                    _private_failure_identity,
                    target_id,
                )
                if failure_identity is not None:
                    routing_name, account, session = failure_identity
                    await _send_private_send_failure(
                        target_id,
                        routing_name=routing_name,
                        account=account,
                        session=session,
                    )
                failed_response = {
                    "status": "failed",
                    "retcode": 1404,
                    "data": {},
                }
                if echo:
                    failed_response["echo"] = echo
                await _send_api_response(failed_response)
                _write_outbound_log(
                    "private",
                    "未知",
                    "[路由校验失败]",
                    False,
                )
                return
            contact, _, _ = private_identity

        # 逐段处理：文字和图片分别发送
        all_sent = True
        sendable_segments = 0
        for seg in message:
            if not isinstance(seg, dict):
                _write_outbound_log(scope, contact, "[无效消息]", False)
                all_sent = False
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})
            if not isinstance(seg_type, str):
                _write_outbound_log(scope, contact, "[无效消息]", False)
                all_sent = False
                continue
            if not isinstance(seg_data, dict):
                invalid_body = {
                    "text": "[无效文本]",
                    "image": "[图片]",
                    "face": "[表情]",
                }.get(seg_type)
                if invalid_body:
                    _write_outbound_log(scope, contact, invalid_body, False)
                all_sent = False
                continue

            if seg_type == "text":
                text = seg_data.get("text", "")
                if not isinstance(text, str):
                    _write_outbound_log(scope, contact, "[无效文本]", False)
                    all_sent = False
                    continue
                if text:
                    sendable_segments += 1
                    try:
                        sent = await asyncio.to_thread(
                            sender_instance.send_text,
                            contact,
                            text,
                        )
                    except Exception:
                        _write_outbound_log(scope, contact, text, False)
                        all_sent = False
                        continue
                    _write_outbound_log(scope, contact, text, sent)
                    if sent is not True:
                        all_sent = False

            elif seg_type == "image":
                file_val = seg_data.get("file", "")
                if not isinstance(file_val, str):
                    _write_outbound_log(scope, contact, "[图片]", False)
                    all_sent = False
                    continue
                if not file_val:
                    _write_outbound_log(scope, contact, "[图片]", False)
                    all_sent = False
                    continue
                sendable_segments += 1

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

                if not img_path:
                    _write_outbound_log(scope, contact, "[图片]", False)
                    all_sent = False
                    continue

                try:
                    # 使用线程池执行同步的 UIA 发送，避免阻塞事件循环
                    try:
                        sent = await asyncio.to_thread(
                            sender_instance.send_image,
                            contact,
                            img_path,
                        )
                    except Exception:
                        _write_outbound_log(scope, contact, "[图片]", False)
                        all_sent = False
                        continue
                    _write_outbound_log(scope, contact, "[图片]", sent)
                    if sent is not True:
                        all_sent = False
                finally:
                    # 临时文件用完删除
                    if temporary_image:
                        try:
                            os.unlink(img_path)
                        except Exception:
                            pass

            elif seg_type == "face":
                sendable_segments += 1
                try:
                    sent = await asyncio.to_thread(
                        sender_instance.send_text,
                        contact,
                        "[表情]",
                    )
                except Exception:
                    _write_outbound_log(scope, contact, "[表情]", False)
                    all_sent = False
                    continue
                _write_outbound_log(scope, contact, "[表情]", sent)
                if sent is not True:
                    all_sent = False

            # 其他类型（record, video 等）忽略

        send_succeeded = all_sent and sendable_segments > 0
        if not send_succeeded and private_identity is not None:
            routing_name, account, session = private_identity
            await _send_private_send_failure(
                target_id,
                routing_name=routing_name,
                account=account,
                session=session,
            )
        response = (
            resp_data
            if send_succeeded
            else {
                "status": "failed",
                "retcode": 1500,
                "data": {},
                **({"echo": echo} if echo else {}),
            }
        )
        await _send_api_response(response)

    else:
        await _send_api_response(resp_data)
        log.debug("[OB11] 收到未处理的 API 操作")


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
                       nickname: str = "", account: str = "",
                       session: str = "", source_messages: list | None = None,
                       routing_name: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": state._self_id_int,
        "post_type": "message",
        "akasha_schema": 1,
        "account": str(account),
        "session": str(session),
        "type": str(message_type),
        "source_messages": [
            dict(source_message)
            for source_message in (source_messages or [])
            if isinstance(source_message, dict)
        ],
        "routing_name": str(routing_name),
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
