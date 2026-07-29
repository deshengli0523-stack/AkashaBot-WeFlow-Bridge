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
import threading
import time
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


def begin_send_preview(
    contact: str,
    content: str,
    *,
    message_type: str = "text",
) -> threading.Event:
    """Publish one text item before any WeChat input is touched."""
    global current_send_cancel_event, current_send_preview, send_preview_sequence
    clear_last_send_result()
    cancel_event = threading.Event()
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
