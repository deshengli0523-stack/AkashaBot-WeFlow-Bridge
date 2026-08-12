"""
微信 ↔ AstrBot 桥接（OneBot v11 版）
=====================================
消息接收：WeFlow SSE 推送
AI 服务：AstrBot 通过 aiocqhttp (OneBot v11) 接入
消息发送：bridge 接收 AstrBot 的 API 调用 → WeFlow API / UIA

架构：
  WeFlow ──SSE──→ bridge.py ──WS 客户端──→ AstrBot (aiocqhttp 服务端)
                   ↑ 连接 ws://127.0.0.1:19777  ↑ 监听端口，等待客户端连入
                   发送 OneBot 事件             返回 API 响应
"""

# 共享状态：所有模块通过 import state 访问这些变量
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import time
from typing import Optional

_BRIDGE_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BRIDGE_MODULE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_MODULE_DIR)
from reply_store import ReplyStoreError, default_store

# ============ 状态控制 ============

running = False
stopping = False
paused = threading.Event()
paused.clear()
run_lock = threading.Lock()
bridge_thread = None
lifecycle_generation = 0


def allocate_lifecycle_generation() -> int:
    """Allocate a restart-safe generation before accepting any work."""

    global lifecycle_generation
    lifecycle_generation = default_store().allocate_generation()
    return lifecycle_generation


def is_generation_running(generation: int) -> bool:
    return bool(running and lifecycle_generation == generation)


def get_sender_for_generation(generation: int):
    with run_lock:
        if not is_generation_running(generation):
            return None
        return sender_instance


def deactivate_generation(generation: int) -> bool:
    """Atomically stop only the generation that observed the failure."""
    global running, stopping, lifecycle_generation
    with run_lock:
        if not is_generation_running(generation):
            return False
        running = False
        stopping = False
        clear_merged_reply_capability()
        stop_merged_reply_workers(generation)
        sender = sender_instance
        if sender is not None:
            sender.stop_pending()
        return True

# ============ Outbound send result ============

_SEND_RESULT_MESSAGES = {
    "OK": "发送操作已提交给微信",
    "E_UIA_CALIBRATION_REQUIRED": "尚未完成固定坐标标定",
    "E_UIA_CALIBRATION_INVALID": "固定坐标标定数据无效",
    "E_UIA_CALIBRATION_WINDOW": "微信窗口未处于标定要求的前台最大化状态",
    "E_UIA_CALIBRATION_BUSY": "标定期间检测到其他输入操作",
    "E_UIA_RECALIBRATION_REQUIRED": "微信窗口环境已变化，需要重新标定",
    "E_UIA_CONTACT_SELECTION_FAILED": "未能确认唯一的联系人搜索结果",
    "E_UIA_INPUT_FOCUS_FAILED": "未能定位或清空微信消息输入框",
    "E_UIA_SENDER_STOPPED": "桥接已停止，未执行发送",
    "E_UIA_SEND_CANCELLED": "此条发送已取消",
    "E_UIA_REPLY_SUPERSEDED": "同一联系人发来新消息，旧回复已淘汰",
    "E_UIA_REPLY_SUPERSEDED_DRAFT_QUARANTINED": (
        "旧回复已淘汰，但输入框内容所有权无法确认；已暂停该联系人的自动回复"
    ),
    "E_UIA_DRAFT_QUARANTINED": (
        "联系人输入框存在无法确认所有权的草稿，自动回复已暂停"
    ),
    "E_UIA_COMMIT_UNKNOWN": "发送按钮操作结果未知，禁止自动重试",
    "E_UIA_PASTE_FAILED": "剪贴板内容未能粘贴到微信",
    "E_UIA_IMAGE_MISSING": "待发送图片不存在或不可读取",
    "E_UIA_IMAGE_CLIPBOARD_FAILED": "图片未能写入微信粘贴所需的剪贴板",
    "E_UIA_SUBMIT_FAILED": "未能完成微信发送按钮操作",
    "E_UIA_STICKER_CALIBRATION_REQUIRED": "尚未完成收藏表情标定",
    "E_UIA_STICKER_CALIBRATION_INVALID": "收藏表情标定数据无效",
    "E_UIA_STICKER_PANEL_FAILED": "未能确认微信收藏表情面板",
    "E_UIA_STICKER_TEMPLATE_MISSING": "收藏表情本机模板缺失",
    "E_UIA_STICKER_MATCH_LOW_CONFIDENCE": "收藏表情识别置信度不足",
    "E_UIA_STICKER_MATCH_AMBIGUOUS": "收藏表情识别出现并列候选",
    "E_UIA_STICKER_CONFIRMATION_UNAVAILABLE": "发送前无法建立 WeFlow 回执基线",
    "E_UIA_STICKER_CONFIRMATION_UNKNOWN": "收藏表情已点击但未获得 WeFlow 回执",
    "E_UIA_STICKER_COMMIT_UNKNOWN": "收藏表情提交结果未知，禁止自动重试",
    "E_UIA_STICKER_REQUEST_IN_PROGRESS": "同一收藏表情请求仍在处理中",
    "E_UIA_STICKER_REQUEST_CAPACITY": "收藏表情请求队列已满",
    "E_UIA_STICKER_QUEUE_EXPIRED": "收藏表情请求在提交前已过期",
    "E_UIA_SEND_FAILED": "微信界面发送操作未完成",
    "E_OB_INVALID_REQUEST": "发送请求格式无效",
    "E_OB_PRIVATE_ROUTE": "无法确认私聊联系人",
    "E_OB_GROUP_ROUTE": "无法确认群聊会话",
    "E_OB_INVALID_SEGMENT": "消息中包含无效内容",
    "E_OB_IMAGE_DECODE": "图片数据无法读取",
    "E_OB_IMAGE_NOT_FOUND": "待发送图片未找到",
    "E_OB_SEND_EXCEPTION": "发送组件执行异常",
    "E_OB_NO_SENDABLE_SEGMENTS": "消息中没有可发送的内容",
    "E_OB_UNSUPPORTED_ACTION": "不支持的 OneBot 操作",
    "E_OB_IDEMPOTENCY_CONFLICT": "相同请求标识携带了不同正文或目标",
    "E_OB_STATE_CAPACITY": "合并回复持久状态已达到安全容量",
    "E_EPOCH_CAPACITY": "合并回复目标回合表已达到安全容量",
    "E_INGRESS_SPOOL_CAPACITY": "普通入站持久缓冲已达到安全容量",
    "E_MERGED_REPLY_NOT_READY": "合并回复插件能力租约尚未就绪",
    "E_MERGED_REPLY_LEASE_INVALID": "合并回复插件能力租约无效或已过期",
    "E_MERGED_REPLY_ADMISSION_REQUIRED": "普通私聊回复缺少合并回复 admission",
    "E_ADMISSION_INVALID": "合并回复 admission 无效或已结束",
    "E_ADMISSION_CAPACITY": "合并回复 admission 已达到安全容量",
    "E_OUTBOX_CAPACITY": "合并回复结果通知已达到安全容量",
    "E_UIA_DRAFT_OWNERSHIP_LOST": "无法证明输入框草稿仍由本次回复所有",
    "E_UIA_DRAFT_RECOVERY_REQUIRED": "重启后发现未确认草稿，需要人工检查",
    "E_UIA_STOPPED": "桥接停止前未完成发送",
}
_SEND_RESULT_STAGES = {
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
send_result_lock = threading.Lock()
last_send_result: Optional[dict[str, object]] = None
send_result_sequence = 0


def record_send_result(
    success: bool,
    *,
    code: str,
    stage: str,
) -> dict[str, object]:
    """Publish one privacy-safe result for the local control panel."""

    global last_send_result, send_result_sequence
    normalized_success = success is True
    normalized_code = str(code)
    if normalized_success:
        normalized_code = "OK"
    elif normalized_code not in _SEND_RESULT_MESSAGES:
        normalized_code = "E_UIA_SEND_FAILED"
    normalized_stage = (
        str(stage) if str(stage) in _SEND_RESULT_STAGES else "complete"
    )
    with send_result_lock:
        send_result_sequence += 1
        last_send_result = {
            "sequence": send_result_sequence,
            "status": "sent" if normalized_success else "failed",
            "code": normalized_code,
            "stage": normalized_stage,
            "message": _SEND_RESULT_MESSAGES[normalized_code],
            "time": time.time(),
        }
        return dict(last_send_result)


def get_last_send_result() -> Optional[dict[str, object]]:
    with send_result_lock:
        return None if last_send_result is None else dict(last_send_result)


def clear_last_send_result() -> None:
    global last_send_result
    with send_result_lock:
        last_send_result = None

# ============ Outbound text review ============

send_preview_lock = threading.Lock()
current_send_cancel_event: Optional[threading.Event] = None
current_send_preview: Optional[dict[str, object]] = None
send_preview_sequence = 0
_reply_epochs: dict[int, tuple[int, int]] = {}
_reply_draft_quarantine: dict[int, dict[str, object]] = {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def advance_reply_epoch(
    target_id: object,
    generation: object,
    raw_id: object = "",
    raw_payload: object = None,
) -> int | None:
    """Advance one private contact and cancel only its uncommitted old reply."""

    normalized_target = _positive_int(target_id)
    normalized_generation = _positive_int(generation)
    if normalized_target is None or normalized_generation is None:
        raise ValueError("invalid reply epoch identity")

    normalized_raw_id = str(raw_id or "").strip()
    next_epoch = (
        default_store().accept_raw_and_advance_epoch(
            normalized_target,
            normalized_generation,
            normalized_raw_id,
            dict(raw_payload) if isinstance(raw_payload, dict) else None,
        )
        if normalized_raw_id
        else default_store().advance_epoch(
            normalized_target,
            normalized_generation,
        )
    )
    if next_epoch is None:
        return None
    with send_preview_lock:
        previous = _reply_epochs.get(normalized_target)
        if previous is not None and previous[0] > normalized_generation:
            raise ReplyStoreError("E_STALE_BRIDGE_GENERATION")
        _reply_epochs[normalized_target] = (normalized_generation, next_epoch)

        preview = current_send_preview
        cancel_event = current_send_cancel_event
        if (
            preview is not None
            and cancel_event is not None
            and preview.get("target_id") == normalized_target
            and preview.get("generation") == normalized_generation
            and _positive_int(preview.get("reply_epoch")) is not None
            and int(preview["reply_epoch"]) < next_epoch
            and preview.get("stage") != "submitting"
            and not cancel_event.is_set()
        ):
            if not str(preview.get("cancel_reason") or ""):
                preview["cancel_reason"] = "superseded"
                cancel_event.set()
        return next_epoch


def current_reply_epoch(target_id: object, generation: object) -> int | None:
    normalized_target = _positive_int(target_id)
    normalized_generation = _positive_int(generation)
    if normalized_target is None or normalized_generation is None:
        return None
    current = default_store().current_epoch(
        normalized_target,
        normalized_generation,
    )
    if current is not None:
        with send_preview_lock:
            _reply_epochs[normalized_target] = (
                normalized_generation,
                current,
            )
    return current


def is_reply_current(
    target_id: object,
    generation: object,
    reply_epoch: object,
) -> bool:
    normalized_epoch = _positive_int(reply_epoch)
    return bool(
        normalized_epoch is not None
        and current_reply_epoch(target_id, generation) == normalized_epoch
    )


def mark_reply_draft_quarantine(
    target_id: object,
    *,
    request_id: str = "",
) -> None:
    normalized_target = _positive_int(target_id)
    if normalized_target is None:
        return
    value = {
        "target_id": normalized_target,
        "request_id": str(request_id),
        "reason": "E_UIA_DRAFT_OWNERSHIP_LOST",
        "time": time.time(),
    }
    default_store().set_quarantine(
        normalized_target,
        str(request_id),
        str(value["reason"]),
    )
    with send_preview_lock:
        _reply_draft_quarantine[normalized_target] = value


def get_reply_draft_quarantine(target_id: object) -> Optional[dict[str, object]]:
    normalized_target = _positive_int(target_id)
    if normalized_target is None:
        return None
    value = default_store().get_quarantine(normalized_target)
    if value is None:
        with send_preview_lock:
            _reply_draft_quarantine.pop(normalized_target, None)
        return None
    result = {
        "target_id": normalized_target,
        "request_id": str(value.get("request_id") or ""),
        "reason": str(value.get("reason") or ""),
        "time": float(value.get("created_at") or 0.0),
    }
    with send_preview_lock:
        _reply_draft_quarantine[normalized_target] = result
    return dict(result)


def clear_reply_draft_quarantine(
    target_id: object,
    request_id: object = "",
) -> bool:
    normalized_target = _positive_int(target_id)
    if normalized_target is None:
        return False
    normalized_request = str(request_id or "").strip()
    if not normalized_request:
        return False
    removed = default_store().resolve_quarantine(
        normalized_target,
        normalized_request,
    )
    if removed:
        with send_preview_lock:
            _reply_draft_quarantine.pop(normalized_target, None)
    return removed


def begin_send_preview(
    contact: str,
    content: str,
    *,
    message_type: str = "text",
    target_id: object = None,
    generation: object = None,
    reply_epoch: object = None,
    request_id: str = "",
) -> threading.Event:
    """Publish one text item before any WeChat input is touched."""
    global current_send_cancel_event, current_send_preview, send_preview_sequence
    clear_last_send_result()
    cancel_event = threading.Event()
    normalized_target = _positive_int(target_id)
    normalized_generation = _positive_int(generation)
    normalized_epoch = _positive_int(reply_epoch)
    with send_preview_lock:
        send_preview_sequence += 1
        current_send_cancel_event = cancel_event
        current_send_preview = {
            "preview_id": send_preview_sequence,
            "contact": str(contact),
            "content": str(content),
            "message_type": str(message_type),
            "stage": "before_paste",
            "remaining_seconds": None,
            "target_id": normalized_target,
            "generation": normalized_generation,
            "reply_epoch": normalized_epoch,
            "request_id": str(request_id),
            "cancel_reason": "",
        }
        if (
            normalized_target is not None
            and normalized_generation is not None
            and normalized_epoch is not None
            and default_store().current_epoch(
                normalized_target,
                normalized_generation,
            )
            != normalized_epoch
        ):
            current_send_preview["cancel_reason"] = "superseded"
            cancel_event.set()
    return cancel_event


def update_send_preview(
    cancel_event: threading.Event,
    *,
    stage: Optional[str] = None,
    remaining_seconds: Optional[float] = None,
) -> None:
    with send_preview_lock:
        if (
            current_send_cancel_event is not cancel_event
            or current_send_preview is None
        ):
            return
        if stage is not None:
            current_send_preview["stage"] = str(stage)
        current_send_preview["remaining_seconds"] = (
            None
            if remaining_seconds is None
            else max(0.0, round(float(remaining_seconds), 1))
        )


def get_send_preview() -> Optional[dict[str, object]]:
    with send_preview_lock:
        if current_send_preview is None:
            return None
        preview = dict(current_send_preview)
        if paused.is_set() and preview.get("stage") != "submitting":
            preview["paused_stage"] = preview.get("stage")
            preview["stage"] = "paused"
        return preview


def try_commit_send(cancel_event: threading.Event) -> bool:
    """Atomically close cancellation before the OS-level submit action."""
    with send_preview_lock:
        preview = current_send_preview
        if (
            current_send_cancel_event is not cancel_event
            or preview is None
            or cancel_event.is_set()
            or not running
            or stopping
            or paused.is_set()
        ):
            return False
        target_id = _positive_int(preview.get("target_id"))
        generation = _positive_int(preview.get("generation"))
        reply_epoch = _positive_int(preview.get("reply_epoch"))
        if (
            target_id is not None
            and generation is not None
            and reply_epoch is not None
            and default_store().current_epoch(target_id, generation)
            != reply_epoch
        ):
            preview["cancel_reason"] = "superseded"
            cancel_event.set()
            return False
        preview["stage"] = "submitting"
        preview["remaining_seconds"] = 0.0
        return True


def get_send_cancel_reason(cancel_event: threading.Event) -> str:
    with send_preview_lock:
        if (
            current_send_cancel_event is not cancel_event
            or current_send_preview is None
        ):
            return ""
        return str(current_send_preview.get("cancel_reason") or "")


def cancel_current_preview(expected_preview_id: int) -> bool:
    """Cancel exactly the preview the operator saw, never a later item."""
    with send_preview_lock:
        cancel_event = current_send_cancel_event
        preview = current_send_preview
        if (
            cancel_event is None
            or preview is None
            or preview.get("preview_id") != expected_preview_id
            or preview.get("stage") == "submitting"
            or cancel_event.is_set()
        ):
            return False
        if not str(preview.get("cancel_reason") or ""):
            preview["cancel_reason"] = "manual_cancel"
            cancel_event.set()
        else:
            return False
        return True


def end_send_preview(cancel_event: threading.Event) -> None:
    global current_send_cancel_event, current_send_preview
    with send_preview_lock:
        if current_send_cancel_event is cancel_event:
            current_send_cancel_event = None
            current_send_preview = None


# ============ Merged reply capability and durable workers ============

_MERGED_PROTOCOL_VERSION = 1
_MERGED_ACTION_VERSION = 1
_MERGED_RESULT_SCHEMA = 2
_MERGED_MEMORY_SCHEMA = 1
_MERGED_LEASE_SECONDS = 15.0
_merged_capability_lock = threading.RLock()
_merged_capability: Optional[dict[str, object]] = None
_merged_worker_lock = threading.Lock()
_merged_worker_generation = 0
_merged_worker_stop: Optional[threading.Event] = None
_merged_worker_wake = threading.Event()
_merged_outbox_wake = threading.Event()
_merged_worker_thread = None
_merged_outbox_thread = None


def _valid_uuid(value: object) -> str | None:
    try:
        import uuid

        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None
    normalized = str(parsed)
    return normalized if normalized == str(value).lower() else None


def clear_merged_reply_capability(connection: object = None) -> None:
    """Fail readiness closed when the reverse WS identity changes."""

    global _merged_capability
    with _merged_capability_lock:
        if connection is not None and _merged_capability is not None:
            if _merged_capability.get("connection_id") != id(connection):
                return
        _merged_capability = None


def merged_reply_capability_status() -> dict[str, object]:
    """Return a credential-free lease snapshot for local diagnostics."""

    global _merged_capability
    now = time.monotonic()
    with _merged_capability_lock:
        capability = _merged_capability
        valid = _merged_capability_runtime_valid(capability, now)
        if capability is not None and not valid:
            _merged_capability = None
            capability = None
        expires_in = (
            max(0.0, float(capability.get("expires_monotonic") or 0.0) - now)
            if capability is not None
            else 0.0
        )
        recovery_ready = bool(
            valid
            and capability is not None
            and capability.get("recovery_ready") is True
        )
        return {
            "present": capability is not None,
            "valid": bool(valid),
            "ready": recovery_ready,
            "generation": (
                int(capability.get("generation") or 0)
                if capability is not None
                else 0
            ),
            "expires_in_seconds": round(expires_in, 3),
            "phase": (
                "ready"
                if recovery_ready
                else "recovering"
                if valid
                else "disconnected"
            ),
        }


def request_merged_reply_rehandshake() -> dict[str, object]:
    """Invalidate only the live lease so the plugin supervisor registers anew.

    Durable admissions and jobs are deliberately left untouched.  Their
    idempotency boundary remains authoritative while the plugin obtains a new
    connection-bound capability.
    """

    global _merged_capability
    connected = bool(_ob_ws is not None and _ob_ws_ready.is_set())
    if not running or stopping or not connected:
        raise ReplyStoreError("E_RECOVERY_MERGED_OFFLINE")
    with _merged_capability_lock:
        _merged_capability = None
    _merged_worker_wake.set()
    _merged_outbox_wake.set()
    return {
        "requested": True,
        "generation": int(lifecycle_generation),
    }


def _merged_capability_runtime_valid(
    capability: Optional[dict[str, object]],
    now: float,
) -> bool:
    """Validate the stored lease independently of caller-supplied credentials."""

    return bool(
        capability is not None
        and _ob_ws is not None
        and capability.get("connection_id") == id(_ob_ws)
        and capability.get("generation") == lifecycle_generation
        and float(capability.get("expires_monotonic") or 0.0) > now
        and is_generation_running(lifecycle_generation)
        and not stopping
    )


def register_merged_reply_capability(
    *,
    plugin_instance_id: str,
    generation: int,
    protocol_version: int,
    action_version: int,
    result_schema: int,
    memory_schema: int,
    streaming_response: bool,
    lifecycle_tracker_ready: bool,
    connection: object,
) -> dict[str, object]:
    """Issue a connection-bound 15 second capability lease."""

    global _merged_capability
    normalized_instance = _valid_uuid(plugin_instance_id)
    if (
        normalized_instance is None
        or not is_generation_running(generation)
        or stopping
        or connection is None
        or connection is not _ob_ws
        or protocol_version != _MERGED_PROTOCOL_VERSION
        or action_version != _MERGED_ACTION_VERSION
        or result_schema != _MERGED_RESULT_SCHEMA
        or memory_schema != _MERGED_MEMORY_SCHEMA
        or streaming_response is not False
        or lifecycle_tracker_ready is not True
    ):
        raise ReplyStoreError("E_MERGED_REPLY_NOT_READY")
    lease_id = secrets.token_urlsafe(32)
    expires_monotonic = time.monotonic() + _MERGED_LEASE_SECONDS
    default_store().release_foreign_admissions(
        int(generation),
        normalized_instance,
    )
    with _merged_capability_lock:
        _merged_capability = {
            "plugin_instance_id": normalized_instance,
            "lease_id": lease_id,
            "generation": int(generation),
            "connection_id": id(connection),
            "expires_monotonic": expires_monotonic,
            "protocol_version": protocol_version,
            "action_version": action_version,
            "result_schema": result_schema,
            "memory_schema": memory_schema,
            "streaming_response": False,
            "lifecycle_tracker_ready": True,
            "recovery_ready": False,
        }
    _merged_outbox_wake.set()
    return {
        "lease_id": lease_id,
        "bridge_generation": int(generation),
        "expires_in_seconds": _MERGED_LEASE_SECONDS,
        "protocol_version": protocol_version,
        "action_version": action_version,
        "result_schema": result_schema,
    }


def renew_merged_reply_capability(
    *,
    plugin_instance_id: str,
    lease_id: str,
    generation: int,
    connection: object,
) -> dict[str, object]:
    global _merged_capability
    now = time.monotonic()
    with _merged_capability_lock:
        capability = _merged_capability
        runtime_valid = _merged_capability_runtime_valid(capability, now)
        credentials_match = bool(
            runtime_valid
            and capability is not None
            and capability.get("plugin_instance_id") == plugin_instance_id
            and capability.get("lease_id") == lease_id
            and capability.get("generation") == generation
            and capability.get("connection_id") == id(connection)
            and connection is _ob_ws
        )
        if capability is not None and not runtime_valid:
            _merged_capability = None
        if not credentials_match:
            raise ReplyStoreError("E_MERGED_REPLY_LEASE_INVALID")
        assert capability is not None
        capability["expires_monotonic"] = now + _MERGED_LEASE_SECONDS
    _merged_outbox_wake.set()
    return {
        "lease_id": lease_id,
        "bridge_generation": generation,
        "expires_in_seconds": _MERGED_LEASE_SECONDS,
    }


def activate_merged_reply_capability(
    *,
    plugin_instance_id: str,
    lease_id: str,
    generation: int,
    connection: object,
) -> dict[str, object]:
    """Open ordinary ingress only after memory recovery has completed."""

    global _merged_capability
    if not validate_merged_reply_lease(
        plugin_instance_id=plugin_instance_id,
        lease_id=lease_id,
        generation=generation,
        require_ready=False,
    ):
        raise ReplyStoreError("E_MERGED_REPLY_LEASE_INVALID")
    with _merged_capability_lock:
        capability = _merged_capability
        if (
            capability is None
            or capability.get("connection_id") != id(connection)
            or connection is not _ob_ws
            or stopping
        ):
            raise ReplyStoreError("E_MERGED_REPLY_LEASE_INVALID")
        capability["recovery_ready"] = True
    _merged_outbox_wake.set()
    return {
        "lease_id": lease_id,
        "bridge_generation": generation,
        "ready": True,
    }


def merged_reply_ready() -> bool:
    global _merged_capability
    now = time.monotonic()
    with _merged_capability_lock:
        capability = _merged_capability
        valid = _merged_capability_runtime_valid(capability, now)
        if capability is not None and not valid:
            _merged_capability = None
        return bool(valid and capability.get("recovery_ready") is True)


def merged_reply_capability_identity() -> Optional[dict[str, object]]:
    if not merged_reply_ready():
        return None
    with _merged_capability_lock:
        assert _merged_capability is not None
        return {
            "plugin_instance_id": str(
                _merged_capability.get("plugin_instance_id") or ""
            ),
            "generation": int(_merged_capability.get("generation") or 0),
        }


def validate_merged_reply_lease(
    *,
    plugin_instance_id: str,
    lease_id: str,
    generation: int,
    require_ready: bool = True,
) -> bool:
    global _merged_capability
    now = time.monotonic()
    with _merged_capability_lock:
        capability = _merged_capability
        runtime_valid = _merged_capability_runtime_valid(capability, now)
        credentials_match = bool(
            runtime_valid
            and capability is not None
            and capability.get("plugin_instance_id") == plugin_instance_id
            and capability.get("lease_id") == lease_id
            and capability.get("generation") == generation
        )
        if capability is not None and not runtime_valid:
            _merged_capability = None
        return bool(
            credentials_match
            and (
                not require_ready
                or capability.get("recovery_ready") is True
            )
        )


def ordinary_ingress_open() -> bool:
    try:
        return bool(default_store().health()["ordinary_ingress_open"])
    except Exception:
        return False


def create_reply_admission(
    target_id: int,
    generation: int,
    reply_epoch: int,
) -> tuple[str, str]:
    identity = merged_reply_capability_identity()
    if identity is None:
        raise ReplyStoreError("E_MERGED_REPLY_NOT_READY")
    if not ordinary_ingress_open():
        raise ReplyStoreError("E_OB_STATE_CAPACITY")
    plugin_instance_id = str(identity["plugin_instance_id"])
    token = default_store().create_admission(
        target_id=target_id,
        generation=generation,
        epoch=reply_epoch,
        plugin_instance_id=plugin_instance_id,
    )
    return token, plugin_instance_id


def finish_reply_admission(
    token: str,
    *,
    plugin_instance_id: str,
    generation: int,
    outcome: str,
    reason: str,
    request_id: str = "",
) -> dict[str, object]:
    return default_store().finish_admission(
        token,
        plugin_instance_id=plugin_instance_id,
        generation=generation,
        outcome=outcome,
        reason=reason,
        request_id=request_id,
    )


def merged_reply_admission_active(target_id: int) -> bool:
    try:
        return default_store().has_active_admission(target_id)
    except Exception:
        return True


def accept_merged_reply_job(*, lease_id: str, **kwargs) -> dict[str, object]:
    generation = int(kwargs.get("generation") or 0)
    plugin_instance_id = str(kwargs.get("plugin_instance_id") or "")
    # Keep the final lifecycle/lease check and durable admission in the same
    # critical section as Stop's transition to ``stopping``.  Otherwise the
    # private-route lookup can finish after Stop has already observed an empty
    # queue, leaving a newly accepted old-generation job without a worker.
    with run_lock:
        if not validate_merged_reply_lease(
            plugin_instance_id=plugin_instance_id,
            lease_id=lease_id,
            generation=generation,
        ):
            raise ReplyStoreError("E_MERGED_REPLY_LEASE_INVALID")
        result = default_store().accept_job(**kwargs)
    if result.get("accepted") is True and result.get("outcome") == "accepted":
        _merged_worker_wake.set()
    return result


def persist_merged_job_stage(request_id: str, stage: str) -> bool:
    if not request_id:
        return False
    return default_store().set_job_stage(request_id, stage)


def _merged_worker_loop(generation: int, stop_event: threading.Event) -> None:
    import logging

    worker_log = logging.getLogger("ob11-bridge")
    while not stop_event.is_set() and is_generation_running(generation):
        try:
            job = default_store().claim_next_job(generation)
        except Exception:
            worker_log.exception("[MERGED] 无法取得持久发送作业")
            stop_event.wait(1.0)
            continue
        if job is None:
            _merged_worker_wake.wait(0.5)
            _merged_worker_wake.clear()
            continue

        request_id = str(job["request_id"])
        target_id = int(job["target_id"])
        reply_generation = int(job["generation"])
        reply_epoch = int(job["epoch"])
        outcome = "failed"
        error_code = "E_UIA_SEND_FAILED"
        error_stage = "complete"
        committed = False
        draft_quarantined = False
        try:
            sender = get_sender_for_generation(generation)
            if sender is None:
                error_code, error_stage = "E_UIA_STOPPED", "preflight"
            elif not is_reply_current(target_id, reply_generation, reply_epoch):
                outcome = "superseded"
                error_code, error_stage = (
                    "E_UIA_REPLY_SUPERSEDED",
                    "preflight",
                )
            else:
                sent = sender.send_text(
                    str(job["routing_name"]),
                    str(job["text"]),
                    target_id=target_id,
                    generation=reply_generation,
                    reply_epoch=reply_epoch,
                    request_id=request_id,
                )
                if sent is True:
                    outcome = "sent"
                    error_code, error_stage, committed = "", "complete", True
                else:
                    failure = get_last_send_result() or {}
                    error_code = str(failure.get("code") or "E_UIA_SEND_FAILED")
                    error_stage = str(failure.get("stage") or "complete")
                    if error_code.startswith("E_UIA_REPLY_SUPERSEDED"):
                        outcome = "superseded"
                    elif error_code == "E_UIA_SEND_CANCELLED":
                        outcome = "manual_cancel"
                    elif error_code == "E_UIA_COMMIT_UNKNOWN":
                        outcome, committed = "commit_unknown", True
                    else:
                        outcome = "failed"
                draft_quarantined = bool(
                    get_reply_draft_quarantine(target_id)
                )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            worker_log.warning(
                "[MERGED] UIA 作业异常: request=%s type=%s",
                request_id,
                type(error).__name__,
            )
            error_code, error_stage = "E_OB_SEND_EXCEPTION", "complete"
        try:
            default_store().finish_job(
                request_id,
                outcome=outcome,
                error_code=error_code,
                error_stage=error_stage,
                committed=committed,
                draft_quarantined=draft_quarantined,
            )
            _merged_outbox_wake.set()
        except Exception:
            worker_log.exception(
                "[MERGED] 无法原子落盘发送终态: request=%s",
                request_id,
            )


def _merged_outbox_loop(generation: int, stop_event: threading.Event) -> None:
    import logging

    publisher_log = logging.getLogger("ob11-bridge")
    while not stop_event.is_set() and is_generation_running(generation):
        if not merged_reply_ready():
            _merged_outbox_wake.wait(1.0)
            _merged_outbox_wake.clear()
            continue
        try:
            notices = default_store().pending_notices(16)
        except Exception:
            publisher_log.exception("[MERGED] 结果 outbox 不可读")
            stop_event.wait(1.0)
            continue
        if not notices:
            _merged_outbox_wake.wait(1.0)
            _merged_outbox_wake.clear()
            continue
        for notice in notices:
            if stop_event.is_set() or not merged_reply_ready():
                break
            try:
                from ob_protocol import push_event

                payload = dict(notice["payload"])
                payload["self_id"] = int(_self_id_int or 0)
                default_store().mark_notice_attempt(str(notice["notice_id"]))
                push_event(payload)
            except Exception:
                publisher_log.warning("[MERGED] 结果通知发布失败，将由 outbox 重放")
                break
        stop_event.wait(1.0)


def start_merged_reply_workers(generation: int) -> None:
    global _merged_worker_generation, _merged_worker_stop
    global _merged_worker_thread, _merged_outbox_thread
    with _merged_worker_lock:
        if (
            _merged_worker_stop is not None
            and not _merged_worker_stop.is_set()
            and _merged_worker_generation == generation
        ):
            return
        if _merged_worker_stop is not None:
            _merged_worker_stop.set()
        stop_event = threading.Event()
        _merged_worker_generation = generation
        _merged_worker_stop = stop_event
        _merged_worker_thread = threading.Thread(
            target=_merged_worker_loop,
            args=(generation, stop_event),
            daemon=True,
            name=f"akasha-merged-worker-{generation}",
        )
        _merged_outbox_thread = threading.Thread(
            target=_merged_outbox_loop,
            args=(generation, stop_event),
            daemon=True,
            name=f"akasha-merged-outbox-{generation}",
        )
        _merged_worker_thread.start()
        _merged_outbox_thread.start()


def stop_merged_reply_workers(generation: int) -> None:
    with _merged_worker_lock:
        if (
            _merged_worker_stop is not None
            and _merged_worker_generation == generation
        ):
            _merged_worker_stop.set()
            _merged_worker_wake.set()
            _merged_outbox_wake.set()


def drain_merged_reply_workers(
    generation: int,
    timeout_seconds: float = 20.0,
) -> bool:
    """Let accepted jobs reach a durable terminal state before shutdown."""

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    _merged_worker_wake.set()
    while time.monotonic() < deadline:
        try:
            if default_store().open_job_count(generation) == 0:
                break
        except Exception:
            return False
        time.sleep(0.05)
    else:
        return False

    stop_merged_reply_workers(generation)
    with _merged_worker_lock:
        worker = _merged_worker_thread
        outbox = _merged_outbox_thread
    current = threading.current_thread()
    for thread in (worker, outbox):
        if thread is not None and thread is not current:
            thread.join(timeout=5.0)
    return not bool(worker is not None and worker.is_alive())


def merged_reply_health() -> dict[str, object]:
    try:
        health = default_store().health()
    except Exception:
        health = {
            "ordinary_ingress_open": False,
            "jobs": 0,
            "active_admissions": 0,
            "outbox_pending": 0,
            "spool_pending": 0,
            "draft_quarantines": 0,
        }
    capability = merged_reply_capability_status()
    health["merged_reply_ready"] = bool(capability["ready"])
    health["merged_reply_capability"] = capability
    return health

# ============ OneBot WebSocket 客户端管理 ============

_ob_ws = None          # WebSocket 连接实例
_ob_ws_loop = None     # 事件循环
_ob_ws_ready = threading.Event()
_self_id_int = 0       # 启动时从 config 初始化

_ONEBOT_ID_MAX = (1 << 53) - 1
_IDENTITY_DB_FILENAME = "bridge_identity.sqlite3"
_IDENTITY_SCHEMA_VERSION = 2
_identity_db_lock = threading.RLock()
# Plain source identities are intentionally process-local. The persistent
# route table keeps only HMACs, while this binding lets a failed outbound
# preflight notify AstrBot about the exact private contact that triggered it.
_private_route_bindings: dict[int, tuple[str, str, str]] = {}
_group_route_bindings: dict[int, tuple[str, str, str, float]] = {}
_group_route_lock = threading.Lock()
_GROUP_ROUTE_CAPACITY = 2048
_GROUP_ROUTE_TTL_SECONDS = 24 * 60 * 60


def _identity_db_path() -> str:
    state_dir = os.environ.get("AKASHABOT_STATE_DIR", "").strip()
    if not state_dir:
        state_dir = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.abspath(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, _IDENTITY_DB_FILENAME)


def _canonical_identity(
    account: str,
    identity_type: str,
    source_id: str,
) -> bytes:
    canonical = bytearray(b"akasha-bridge-identity-v2")
    for value in (account, identity_type, source_id):
        encoded = value.encode("utf-8")
        canonical.extend(len(encoded).to_bytes(4, "big"))
        canonical.extend(encoded)
    return bytes(canonical)


def _identity_hmac(
    identity_salt: bytes,
    account: str,
    identity_type: str,
    source_id: str,
) -> bytes:
    return hmac.new(
        identity_salt,
        _canonical_identity(account, identity_type, source_id),
        hashlib.sha256,
    ).digest()


def _identity_candidate(identity_hmac: bytes, probe: int) -> int:
    if probe == 0:
        digest = identity_hmac
    else:
        digest = hmac.new(
            identity_hmac,
            b"akasha-bridge-collision-v1" + probe.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
    return (int.from_bytes(digest[:8], "big") % _ONEBOT_ID_MAX) + 1


def _identity_table_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(identity_map)").fetchall()
    }


def _load_or_create_identity_salt(connection: sqlite3.Connection) -> bytes:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_meta (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL
        )
        """
    )
    row = connection.execute(
        "SELECT value FROM identity_meta WHERE key = 'identity_salt'"
    ).fetchone()
    if row is None:
        identity_salt = secrets.token_bytes(32)
        connection.execute(
            "INSERT INTO identity_meta (key, value) VALUES ('identity_salt', ?)",
            (identity_salt,),
        )
        return identity_salt
    identity_salt = bytes(row[0])
    if len(identity_salt) != 32:
        raise RuntimeError("bridge identity salt is invalid")
    return identity_salt


def _create_private_identity_table(
    connection: sqlite3.Connection,
    table_name: str,
) -> None:
    if table_name not in {"identity_map", "identity_map_v2"}:
        raise ValueError("invalid identity table name")
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
            identity_hmac BLOB PRIMARY KEY
                CHECK (length(identity_hmac) = 32),
            ob_id INTEGER NOT NULL UNIQUE
                CHECK (ob_id > 0 AND ob_id <= 9007199254740991),
            created_at INTEGER NOT NULL
        )
        """
    )


def _initialize_identity_db(connection: sqlite3.Connection) -> bytes:
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA secure_delete = ON")
    identity_salt = _load_or_create_identity_salt(connection)
    columns = _identity_table_columns(connection)
    if not columns:
        _create_private_identity_table(connection, "identity_map")
    elif {"account", "identity_type", "source_id", "ob_id"}.issubset(columns):
        _create_private_identity_table(connection, "identity_map_v2")
        legacy_rows = connection.execute(
            """
            SELECT account, identity_type, source_id, ob_id, created_at
            FROM identity_map
            """
        ).fetchall()
        for account, identity_type, source_id, ob_id, created_at in legacy_rows:
            identity_key = _identity_hmac(
                identity_salt,
                str(account),
                str(identity_type),
                str(source_id),
            )
            connection.execute(
                """
                INSERT INTO identity_map_v2 (
                    identity_hmac,
                    ob_id,
                    created_at
                ) VALUES (?, ?, ?)
                """,
                (identity_key, int(ob_id), int(created_at)),
            )
        connection.execute("DROP TABLE identity_map")
        connection.execute("ALTER TABLE identity_map_v2 RENAME TO identity_map")
    elif "identity_hmac" not in columns:
        raise RuntimeError("unsupported bridge identity schema")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS private_routes (
            ob_id INTEGER PRIMARY KEY
                CHECK (ob_id > 0 AND ob_id <= 9007199254740991),
            identity_hmac BLOB NOT NULL UNIQUE
                CHECK (length(identity_hmac) = 32),
            routing_name TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(f"PRAGMA user_version = {_IDENTITY_SCHEMA_VERSION}")
    return identity_salt


def _identity_values(
    source_id: object,
    account: object,
    identity_type: object,
) -> tuple[str, str, str]:
    normalized_source = str(source_id).strip()
    if not normalized_source:
        raise ValueError("source identity must not be empty")
    normalized_account = str(account).strip() or "default"
    normalized_type = str(identity_type).strip() or "wechat"
    return normalized_source, normalized_account, normalized_type


def _get_or_create_identity(
    connection: sqlite3.Connection,
    identity_salt: bytes,
    *,
    source_id: str,
    account: str,
    identity_type: str,
) -> tuple[int, bytes]:
    identity_key = _identity_hmac(
        identity_salt,
        account,
        identity_type,
        source_id,
    )
    existing = connection.execute(
        """
        SELECT ob_id
        FROM identity_map
        WHERE identity_hmac = ?
        """,
        (identity_key,),
    ).fetchone()
    if existing is not None:
        return int(existing[0]), identity_key

    probe = 0
    while True:
        candidate = _identity_candidate(identity_key, probe)
        owner = connection.execute(
            """
            SELECT identity_hmac
            FROM identity_map
            WHERE ob_id = ?
            """,
            (candidate,),
        ).fetchone()
        if owner is None:
            connection.execute(
                """
                INSERT INTO identity_map (
                    identity_hmac,
                    ob_id,
                    created_at
                ) VALUES (?, ?, ?)
                """,
                (
                    identity_key,
                    candidate,
                    int(time.time()),
                ),
            )
            return candidate, identity_key
        if hmac.compare_digest(bytes(owner[0]), identity_key):
            return candidate, identity_key
        probe += 1


def _wxid_to_int(
    wxid: str,
    *,
    account: str = "",
    identity_type: str = "wechat",
) -> int:
    """Persistently map one source identity to a JSON-safe OneBot integer."""
    source_id, account_id, identity_kind = _identity_values(
        wxid,
        account,
        identity_type,
    )

    with _identity_db_lock:
        connection = sqlite3.connect(_identity_db_path(), timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            identity_salt = _initialize_identity_db(connection)
            ob_id, _ = _get_or_create_identity(
                connection,
                identity_salt,
                source_id=source_id,
                account=account_id,
                identity_type=identity_kind,
            )
            connection.commit()
            return ob_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def remember_private_route(
    session: str,
    *,
    account: str,
    routing_name: str,
) -> int:
    """Atomically persist a private identity and its current UI routing name."""
    source_id, account_id, _ = _identity_values(session, account, "private")
    route_name = str(routing_name).strip()
    if not route_name:
        raise ValueError("private routing name must not be empty")

    with _identity_db_lock:
        connection = sqlite3.connect(_identity_db_path(), timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            identity_salt = _initialize_identity_db(connection)
            ob_id, identity_key = _get_or_create_identity(
                connection,
                identity_salt,
                source_id=source_id,
                account=account_id,
                identity_type="private",
            )
            connection.execute(
                """
                INSERT INTO private_routes (
                    ob_id,
                    identity_hmac,
                    routing_name,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(ob_id) DO UPDATE SET
                    identity_hmac = excluded.identity_hmac,
                    routing_name = excluded.routing_name,
                    updated_at = excluded.updated_at
                """,
                (ob_id, identity_key, route_name, int(time.time())),
            )
            connection.commit()
            _private_route_bindings[ob_id] = (
                account_id,
                source_id,
                route_name,
            )
            return ob_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def get_private_route_binding(ob_id: object) -> Optional[dict[str, object]]:
    if isinstance(ob_id, bool):
        return None
    try:
        normalized_id = int(ob_id)
    except (TypeError, ValueError):
        return None
    if normalized_id <= 0 or normalized_id > _ONEBOT_ID_MAX:
        return None
    with _identity_db_lock:
        binding = _private_route_bindings.get(normalized_id)
        if binding is None:
            return None
        account, session, routing_name = binding
        return {
            "ob_id": normalized_id,
            "account": account,
            "session": session,
            "routing_name": routing_name,
        }


def get_private_route(ob_id: object) -> Optional[dict[str, object]]:
    if isinstance(ob_id, bool):
        return None
    try:
        normalized_id = int(ob_id)
    except (TypeError, ValueError):
        return None
    if normalized_id <= 0 or normalized_id > _ONEBOT_ID_MAX:
        return None

    with _identity_db_lock:
        connection = sqlite3.connect(_identity_db_path(), timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _initialize_identity_db(connection)
            row = connection.execute(
                """
                SELECT identity_hmac, routing_name
                FROM private_routes
                WHERE ob_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    if row is None:
        return None
    return {
        "ob_id": normalized_id,
        "identity_hmac": bytes(row[0]),
        "routing_name": str(row[1]),
    }


def private_route_matches(
    route: object,
    *,
    account: str,
    session: str,
) -> bool:
    if not isinstance(route, dict):
        return False
    expected = route.get("identity_hmac")
    if not isinstance(expected, bytes) or len(expected) != 32:
        return False
    try:
        source_id, account_id, _ = _identity_values(session, account, "private")
    except ValueError:
        return False

    with _identity_db_lock:
        connection = sqlite3.connect(_identity_db_path(), timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            identity_salt = _initialize_identity_db(connection)
            candidate = _identity_hmac(
                identity_salt,
                account_id,
                "private",
                source_id,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    return hmac.compare_digest(expected, candidate)


def remember_group_route(
    ob_id: object,
    *,
    account: str,
    session: str,
    routing_name: str,
) -> None:
    """Keep one bounded process-local group route for safe outbound confirmation."""
    if isinstance(ob_id, bool):
        raise ValueError("invalid group id")
    normalized_id = int(ob_id)
    account_id = str(account).strip()
    session_id = str(session).strip()
    route_name = str(routing_name).strip()
    if (
        normalized_id <= 0
        or normalized_id > _ONEBOT_ID_MAX
        or not account_id
        or not session_id
        or not route_name
    ):
        raise ValueError("invalid group route")
    now = time.monotonic()
    with _group_route_lock:
        expired = [
            key
            for key, (_, _, _, created) in _group_route_bindings.items()
            if now - created > _GROUP_ROUTE_TTL_SECONDS
        ]
        for key in expired:
            _group_route_bindings.pop(key, None)
        _group_route_bindings.pop(normalized_id, None)
        _group_route_bindings[normalized_id] = (
            account_id,
            session_id,
            route_name,
            now,
        )
        while len(_group_route_bindings) > _GROUP_ROUTE_CAPACITY:
            oldest = next(iter(_group_route_bindings))
            _group_route_bindings.pop(oldest, None)


def get_group_route_binding(ob_id: object) -> Optional[dict[str, object]]:
    if isinstance(ob_id, bool):
        return None
    try:
        normalized_id = int(ob_id)
    except (TypeError, ValueError):
        return None
    now = time.monotonic()
    with _group_route_lock:
        binding = _group_route_bindings.get(normalized_id)
        if binding is None:
            return None
        account, session, routing_name, created = binding
        if now - created > _GROUP_ROUTE_TTL_SECONDS:
            _group_route_bindings.pop(normalized_id, None)
            return None
        return {
            "ob_id": normalized_id,
            "account": account,
            "session": session,
            "routing_name": routing_name,
        }


# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # OneBot user_id/group_id → 微信联系名

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "mention"
