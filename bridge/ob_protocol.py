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
import uuid

import requests

import state
import config
from privacy import chat_record
from favorite_sticker import STICKER_KEYS

log = logging.getLogger("ob11-bridge")
_CONTACTS_LIMIT = 10000
_CONTACTS_TIMEOUT_SECONDS = 5
_FAVORITE_STICKER_COMMIT_BUDGET_SECONDS = 25.0
_READ_ONLY_ACTIONS = {
    "get_login_info",
    "get_status",
    "get_version_info",
}
_SAFE_SEND_STAGES = {
    "request",
    "route",
    "preflight",
    "select_contact",
    "focus_input",
    "paste",
    "review",
    "submit",
    "image",
    "sticker",
    "complete",
}


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


def _record_safe_send_failure(code: str, stage: str) -> None:
    recorder = getattr(state, "record_send_result", None)
    if callable(recorder):
        recorder(False, code=code, stage=stage)


def _normalize_safe_failure(code: object, stage: object) -> tuple[str, str]:
    normalized_code = str(code or "")
    normalized_stage = str(stage or "")
    if not normalized_code.startswith(("E_UIA_", "E_OB_")):
        normalized_code = "E_UIA_SEND_FAILED"
    if normalized_stage not in _SAFE_SEND_STAGES:
        normalized_stage = "complete"
    return normalized_code, normalized_stage


def _last_send_failure() -> tuple[str, str] | None:
    getter = getattr(state, "get_last_send_result", None)
    if not callable(getter):
        return None
    try:
        result = getter()
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "failed":
        return None
    return _normalize_safe_failure(result.get("code"), result.get("stage"))


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


def _parse_favorite_sticker_request(
    submitted_params: object,
) -> tuple[str, int, str, str] | None:
    if not isinstance(submitted_params, dict):
        return None
    keys = set(submitted_params)
    if keys == {"user_id", "sticker_key", "request_id"}:
        scope = "private"
        target_value = submitted_params.get("user_id")
    elif keys == {"group_id", "sticker_key", "request_id"}:
        scope = "group"
        target_value = submitted_params.get("group_id")
    else:
        return None
    target_id = _normalize_target_id(target_value)
    sticker_key = submitted_params.get("sticker_key")
    request_id = submitted_params.get("request_id")
    if (
        target_id is None
        or not isinstance(sticker_key, str)
        or sticker_key not in STICKER_KEYS
        or not isinstance(request_id, str)
    ):
        return None
    try:
        parsed_request_id = uuid.UUID(request_id)
    except (ValueError, AttributeError):
        return None
    if str(parsed_request_id) != request_id.lower():
        return None
    return scope, target_id, sticker_key, request_id.lower()


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


async def _send_private_send_result(
    target_id: int,
    *,
    routing_name: str,
    account: str,
    session: str,
    success: bool,
    delivered_parts: tuple[str, ...] = (),
    error_code: str = "",
    error_stage: str = "",
) -> bool:
    """Tell the memory plugin whether the private reply reached UIA submit."""

    payload = {
        "time": int(time.time()),
        "self_id": int(getattr(state, "_self_id_int", 0) or 0),
        "post_type": "notice",
        "notice_type": "akasha_send_result",
        "sub_type": "sent" if success else "failed",
        "user_id": target_id,
        "akasha_schema": 1,
        "type": "private",
        "account": account,
        "session": session,
        "routing_name": routing_name,
        "source_messages": [],
        "success": bool(success),
    }
    if success:
        payload["delivered_parts"] = [
            str(part)
            for part in delivered_parts
            if isinstance(part, str) and part
        ]
    else:
        failure = (
            _normalize_safe_failure(error_code, error_stage)
            if error_code
            else _last_send_failure()
        )
        if failure is not None:
            payload["error_code"], payload["error_stage"] = failure
    return await _send_api_response(payload)


async def _send_private_send_failure(
    target_id: int,
    *,
    routing_name: str,
    account: str,
    session: str,
    error_code: str,
    error_stage: str,
) -> bool:
    return await _send_private_send_result(
        target_id,
        routing_name=routing_name,
        account=account,
        session=session,
        success=False,
        error_code=error_code,
        error_stage=error_stage,
    )


async def _handle_ob_api(data: dict, generation=None):
    """处理 AstrBot 发来的 API 请求。"""
    action = data.get("action", "")
    has_echo = "echo" in data
    echo = data.get("echo")
    favorite_commit_deadline = (
        time.monotonic() + _FAVORITE_STICKER_COMMIT_BUDGET_SECONDS
        if action == "send_wechat_favorite_sticker"
        else None
    )
    if (
        generation is not None
        and not state.is_generation_running(generation)
    ):
        return
    supported_actions = {
        "send_msg",
        "send_private_msg",
        "send_group_msg",
        "send_wechat_favorite_sticker",
    } | _READ_ONLY_ACTIONS
    if action not in supported_actions:
        _record_safe_send_failure("E_OB_UNSUPPORTED_ACTION", "request")
        response = {
            "status": "failed",
            "retcode": 1404,
            "data": {
                "confirmed": False,
                "error_code": "E_OB_UNSUPPORTED_ACTION",
                "error_stage": "request",
                "committed": False,
            },
            **({"echo": echo} if has_echo else {}),
        }
        await _send_api_response(response)
        log.warning("[OB11] 收到不支持的 API 操作")
        return

    if action in _READ_ONLY_ACTIONS:
        if action == "get_login_info":
            nicknames = getattr(config, "BOT_NICKNAMES", ())
            nickname = (
                str(nicknames[0]).strip()
                if isinstance(nicknames, (list, tuple))
                and nicknames
                and str(nicknames[0]).strip()
                else "AkashaBot"
            )
            response_data = {
                "user_id": int(getattr(state, "_self_id_int", 0) or 0),
                "nickname": nickname,
            }
        elif action == "get_status":
            ready = bool(
                getattr(state, "running", False)
                and getattr(state, "sender_instance", None) is not None
            )
            response_data = {"online": ready, "good": ready}
        else:
            response_data = {
                "app_name": "AkashaBot-WeFlow-Bridge",
                "app_version": "native",
                "protocol_version": "v11",
            }
        response = {
            "status": "ok",
            "retcode": 0,
            "data": response_data,
            **({"echo": echo} if has_echo else {}),
        }
        await _send_api_response(response)
        return

    sender_instance = (
        state.sender_instance
        if generation is None
        else state.get_sender_for_generation(generation)
    )
    if sender_instance is None:
        _record_safe_send_failure("E_UIA_SENDER_STOPPED", "request")
        response = {
            "status": "failed",
            "retcode": 1503,
            "data": {
                "confirmed": False,
                "error_code": "E_UIA_SENDER_STOPPED",
                "error_stage": "request",
                "committed": False,
            },
            **({"echo": echo} if has_echo else {}),
        }
        await _send_api_response(response)
        return
    submitted_params = data.get("params", {})
    params_valid = isinstance(submitted_params, dict)
    params = submitted_params if params_valid else {}
    log.info("[OB11] 收到 API 请求: has_echo=%s", has_echo)

    resp_data = {"status": "ok", "retcode": 0, "data": {}}
    if has_echo:
        resp_data["echo"] = echo

    if action == "send_wechat_favorite_sticker":
        parsed = _parse_favorite_sticker_request(submitted_params)
        if parsed is None:
            _record_safe_send_failure("E_OB_INVALID_REQUEST", "request")
            response = {
                "status": "failed",
                "retcode": 1400,
                "data": {
                    "confirmed": False,
                    "error_code": "E_OB_INVALID_REQUEST",
                    "error_stage": "request",
                    "committed": False,
                },
                **({"echo": echo} if has_echo else {}),
            }
            await _send_api_response(response)
            return

        scope, target_id, sticker_key, request_id = parsed
        contact = ""
        session = ""
        if scope == "private":
            private_identity = await asyncio.to_thread(
                _resolve_private_contact,
                target_id,
            )
            if private_identity is not None:
                contact, _, session = private_identity
        else:
            binding = state.get_group_route_binding(target_id)
            if isinstance(binding, dict):
                contact = str(binding.get("routing_name") or "").strip()
                session = str(binding.get("session") or "").strip()
        if not contact or not session:
            route_error = (
                "E_OB_PRIVATE_ROUTE"
                if scope == "private"
                else "E_OB_GROUP_ROUTE"
            )
            _record_safe_send_failure(route_error, "route")
            response = {
                "status": "failed",
                "retcode": 1404,
                "data": {
                    "confirmed": False,
                    "error_code": route_error,
                    "error_stage": "route",
                    "committed": False,
                    "request_id": request_id,
                },
                **({"echo": echo} if has_echo else {}),
            }
            await _send_api_response(response)
            _write_outbound_log(scope, "未知", "[收藏表情]", False)
            return

        try:
            result = await asyncio.to_thread(
                sender_instance.send_favorite_sticker,
                contact,
                session,
                sticker_key,
                request_id,
                favorite_commit_deadline,
            )
        except Exception:
            _record_safe_send_failure("E_OB_SEND_EXCEPTION", "sticker")
            response = {
                "status": "failed",
                "retcode": 1500,
                "data": {
                    "confirmed": False,
                    "error_code": "E_OB_SEND_EXCEPTION",
                    "error_stage": "sticker",
                    "committed": False,
                    "request_id": request_id,
                },
                **({"echo": echo} if has_echo else {}),
            }
            await _send_api_response(response)
            _write_outbound_log(scope, contact, "[收藏表情]", False)
            return

        confirmed = result.confirmed is True
        error_code = str(result.error_code or "")
        error_stage = str(result.error_stage or "complete")
        response = {
            "status": "ok" if confirmed else "failed",
            "retcode": 0 if confirmed else (1409 if result.in_progress else 1500),
            "data": {
                "confirmed": confirmed,
                "error_code": None if confirmed else error_code,
                "error_stage": error_stage,
                "committed": result.committed is True,
                "cached": result.cached is True,
                "request_id": request_id,
                "sticker_key": sticker_key,
            },
            **({"echo": echo} if has_echo else {}),
        }
        await _send_api_response(response)
        _write_outbound_log(scope, contact, "[收藏表情]", confirmed)
        return

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
            _record_safe_send_failure("E_OB_INVALID_REQUEST", "request")
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
                        error_code="E_OB_INVALID_REQUEST",
                        error_stage="request",
                    )
            failed_response = {
                "status": "failed",
                "retcode": 1400,
                "data": {
                    "error_code": "E_OB_INVALID_REQUEST",
                    "error_stage": "request",
                },
            }
            if has_echo:
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
                _record_safe_send_failure("E_OB_PRIVATE_ROUTE", "route")
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
                        error_code="E_OB_PRIVATE_ROUTE",
                        error_stage="route",
                    )
                failed_response = {
                    "status": "failed",
                    "retcode": 1404,
                    "data": {
                        "error_code": "E_OB_PRIVATE_ROUTE",
                        "error_stage": "route",
                    },
                }
                if has_echo:
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
        delivered_parts: list[str] = []
        protocol_failure: tuple[str, str] | None = None

        def remember_protocol_failure(code: str, stage: str) -> None:
            nonlocal protocol_failure
            if protocol_failure is None:
                protocol_failure = (code, stage)

        def remember_sender_failure() -> None:
            remember_protocol_failure(
                *(
                    _last_send_failure()
                    or ("E_UIA_SEND_FAILED", "submit")
                )
            )

        for seg in message:
            if not isinstance(seg, dict):
                _write_outbound_log(scope, contact, "[无效消息]", False)
                all_sent = False
                remember_protocol_failure("E_OB_INVALID_SEGMENT", "request")
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})
            if not isinstance(seg_type, str):
                _write_outbound_log(scope, contact, "[无效消息]", False)
                all_sent = False
                remember_protocol_failure("E_OB_INVALID_SEGMENT", "request")
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
                remember_protocol_failure("E_OB_INVALID_SEGMENT", "request")
                continue

            if seg_type == "text":
                text = seg_data.get("text", "")
                if not isinstance(text, str):
                    _write_outbound_log(scope, contact, "[无效文本]", False)
                    all_sent = False
                    remember_protocol_failure("E_OB_INVALID_SEGMENT", "request")
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
                        remember_protocol_failure("E_OB_SEND_EXCEPTION", "submit")
                        continue
                    _write_outbound_log(scope, contact, text, sent)
                    if sent is not True:
                        all_sent = False
                        remember_sender_failure()
                    else:
                        delivered_parts.append(text)

            elif seg_type == "image":
                file_val = seg_data.get("file", "")
                if not isinstance(file_val, str):
                    _write_outbound_log(scope, contact, "[图片]", False)
                    all_sent = False
                    remember_protocol_failure("E_OB_INVALID_SEGMENT", "image")
                    continue
                if not file_val:
                    _write_outbound_log(scope, contact, "[图片]", False)
                    all_sent = False
                    remember_protocol_failure("E_OB_IMAGE_NOT_FOUND", "image")
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
                        remember_protocol_failure("E_OB_IMAGE_DECODE", "image")
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
                    if protocol_failure is None:
                        remember_protocol_failure(
                            "E_OB_IMAGE_NOT_FOUND",
                            "image",
                        )
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
                        remember_protocol_failure("E_OB_SEND_EXCEPTION", "submit")
                        continue
                    _write_outbound_log(scope, contact, "[图片]", sent)
                    if sent is not True:
                        all_sent = False
                        remember_sender_failure()
                    else:
                        delivered_parts.append("[图片]")
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
                    remember_protocol_failure("E_OB_SEND_EXCEPTION", "submit")
                    continue
                _write_outbound_log(scope, contact, "[表情]", sent)
                if sent is not True:
                    all_sent = False
                    remember_sender_failure()
                else:
                    delivered_parts.append("[表情]")

            # 其他类型（record, video 等）忽略

        send_succeeded = all_sent and sendable_segments > 0
        final_failure: tuple[str, str] | None = None
        if not send_succeeded:
            if protocol_failure is not None:
                final_failure = protocol_failure
            elif sendable_segments == 0:
                final_failure = (
                    "E_OB_NO_SENDABLE_SEGMENTS",
                    "request",
                )
            else:
                final_failure = ("E_UIA_SEND_FAILED", "submit")
            _record_safe_send_failure(*final_failure)
        if not send_succeeded and private_identity is not None:
            routing_name, account, session = private_identity
            assert final_failure is not None
            await _send_private_send_failure(
                target_id,
                routing_name=routing_name,
                account=account,
                session=session,
                error_code=final_failure[0],
                error_stage=final_failure[1],
            )
        elif send_succeeded and private_identity is not None:
            routing_name, account, session = private_identity
            await _send_private_send_result(
                target_id,
                routing_name=routing_name,
                account=account,
                session=session,
                success=True,
                delivered_parts=tuple(delivered_parts),
            )
        response = resp_data
        if not send_succeeded:
            assert final_failure is not None
            response = {
                "status": "failed",
                "retcode": 1500,
                "data": {
                    "error_code": final_failure[0],
                    "error_stage": final_failure[1],
                },
                **({"echo": echo} if has_echo else {}),
            }
        await _send_api_response(response)


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
