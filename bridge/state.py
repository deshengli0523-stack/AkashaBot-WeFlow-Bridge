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
import threading
from typing import Optional

# ============ 状态控制 ============

running = False
paused = threading.Event()
paused.clear()
run_lock = threading.Lock()
bridge_thread = None
lifecycle_generation = 0


def is_generation_running(generation: int) -> bool:
    return bool(running and lifecycle_generation == generation)


def get_sender_for_generation(generation: int):
    with run_lock:
        if not is_generation_running(generation):
            return None
        return sender_instance


def deactivate_generation(generation: int) -> bool:
    """Atomically stop only the generation that observed the failure."""
    global running, lifecycle_generation
    with run_lock:
        if not is_generation_running(generation):
            return False
        running = False
        lifecycle_generation += 1
        sender = sender_instance
        if sender is not None:
            sender.stop_pending()
        return True

# ============ Outbound text review ============

send_preview_lock = threading.Lock()
current_send_cancel_event: Optional[threading.Event] = None
current_send_preview: Optional[dict[str, object]] = None
send_preview_sequence = 0


def begin_send_preview(content: str) -> threading.Event:
    """Publish one text item before any WeChat input is touched."""
    global current_send_cancel_event, current_send_preview, send_preview_sequence
    cancel_event = threading.Event()
    with send_preview_lock:
        send_preview_sequence += 1
        current_send_cancel_event = cancel_event
        current_send_preview = {
            "preview_id": send_preview_sequence,
            "content": str(content),
            "message_type": "text",
            "stage": "before_paste",
            "remaining_seconds": None,
        }
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
        if (
            current_send_cancel_event is not cancel_event
            or current_send_preview is None
            or cancel_event.is_set()
            or not running
            or paused.is_set()
        ):
            return False
        current_send_preview["stage"] = "submitting"
        current_send_preview["remaining_seconds"] = 0.0
        return True


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
        cancel_event.set()
        return True


def end_send_preview(cancel_event: threading.Event) -> None:
    global current_send_cancel_event, current_send_preview
    with send_preview_lock:
        if current_send_cancel_event is cancel_event:
            current_send_cancel_event = None
            current_send_preview = None

# ============ OneBot WebSocket 客户端管理 ============

_ob_ws = None          # WebSocket 连接实例
_ob_ws_loop = None     # 事件循环
_ob_ws_ready = threading.Event()
_self_id_int = 0       # 启动时从 config 初始化


def _wxid_to_int(wxid: str) -> int:
    """将微信 wxid 映射为稳定的整数 ID。"""
    return abs(hash(wxid)) % (2**31)


# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # OneBot user_id/group_id → 微信联系名
ob_client_started = False

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "mention"
