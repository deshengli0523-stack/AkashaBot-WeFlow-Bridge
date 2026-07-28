"""
入口与生命周期管理模块。

负责桥接的启动、停止、主循环重连逻辑，以及命令行入口。
"""

import json
import logging
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer

_STARTUP_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9._-]{8,}"),
    re.compile(
        r"""(?ix)
        (?:api[\s_-]?key|access[\s_-]?token|password|secret|token)
        \s*[:=]\s*
        ["']?[^"',;\s}&]+
        """
    ),
)
_STARTUP_PATH_PATTERN = re.compile(
    r"""(?ix)
    (?:
        [a-z]:[\\/]
        |
        \\\\
    )
    [^\r\n]+
    """
)


def _sanitize_startup_detail(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for pattern in _STARTUP_SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _STARTUP_PATH_PATTERN.sub("[REDACTED]", text)
    return text[:1000]


def _write_startup_failure(stage: str, error: BaseException) -> None:
    """Write one bounded, redacted diagnostic even when normal logging failed."""

    try:
        log_dir = os.environ.get(
            "AKASHABOT_LOG_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
        )
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "bridge-startup.log")
        detail = _sanitize_startup_detail(error)
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(
                "E_BRIDGE_STARTUP "
                f"stage={stage} type={type(error).__name__} detail={detail}\n"
            )
    except Exception:
        pass


try:
    import requests

    import state
    import config
    from uia_fixed_sender import UiaFixedSender
    from ob_client import _run_ob_client
    from bridge_core import WeFlowBridge
    from web_panel import WebHandler, PAGE
except Exception as import_error:
    _write_startup_failure("import", import_error)
    raise

log = logging.getLogger("ob11-bridge")


# ============ 启动 / 停止 ============


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30.0)
        return request, address


def _process_start_token(pid: int) -> int | None:
    """Return the Windows creation FILETIME used to detect PID reuse."""

    if pid <= 0 or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", wintypes.DWORD),
                ("high", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return (int(created.high) << 32) | int(created.low)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _legacy_bridge_endpoint_is_live() -> bool:
    """Verify an old PID-only record through the loopback status endpoint."""

    try:
        response = requests.get(
            f"http://127.0.0.1:{int(config.WEB_PORT)}/status",
            timeout=0.5,
        )
        payload = response.json() if response.status_code == 200 else None
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("running"), bool)
            and "ob_connected" in payload
            and "weflow_connected" in payload
        )
    except Exception:
        return False


def _pid_record_status(raw_record: str) -> str:
    """Classify an existing record without treating query failure as stale."""

    parts = raw_record.strip().split(":", 1)
    try:
        pid = int(parts[0])
    except (TypeError, ValueError):
        return "unverifiable"
    if pid <= 0:
        return "unverifiable"
    if len(parts) == 2:
        try:
            expected_start = int(parts[1])
        except ValueError:
            return "unverifiable"
        if expected_start <= 0:
            return "unverifiable"
        actual_start = _process_start_token(pid)
        if actual_start is None:
            return "unverifiable"
        return "live" if actual_start == expected_start else "stale"
    return "live" if _legacy_bridge_endpoint_is_live() else "stale"


def _pid_record_is_live(raw_record: str) -> bool:
    return _pid_record_status(raw_record) == "live"


def _claim_pid_file(path: str, record: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(record)
            stream.flush()
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _release_pid_file_if_owned(path: str, record: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            current = stream.read().strip()
        if current == record:
            os.remove(path)
    except FileNotFoundError:
        pass


def _start_bridge():
    with state.run_lock:
        if state.running:
            return
        sender = UiaFixedSender(
            config.UIA_FIXED_CALIBRATION,
            pre_paste_preview_delay=config.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY,
            pre_send_delay=config.UIA_FIXED_PRE_SEND_DELAY,
            settle_jitter_max_seconds=config.UIA_FIXED_SETTLE_JITTER_MAX_SECONDS,
        )
        state.lifecycle_generation += 1
        generation = state.lifecycle_generation
        state.running = True
        state.sender_instance = sender
    state.paused.clear()

    t = threading.Thread(
        target=_run_ob_client,
        args=(generation,),
        daemon=True,
        name=f"ob11-client-{generation}",
    )
    t.start()

    state.bridge_thread = threading.Thread(
        target=_bridge_loop,
        args=(generation,),
        daemon=True,
        name="bridge",
    )
    state.bridge_thread.start()
    log.info("[Web] 已启动")


def _stop_bridge():
    with state.run_lock:
        state.running = False
        state.lifecycle_generation += 1
        sender = state.sender_instance
        if sender is not None:
            sender.stop_pending()

    # 切断 SSE 长连接，让 _bridge_loop 的 listen_sse() 从阻塞中退出
    with state.bridge_lock:
        if state.bridge_instance and hasattr(
            state.bridge_instance,
            "stop_money_actions",
        ):
            state.bridge_instance.stop_money_actions()
        if state.bridge_instance and state.bridge_instance._sse_session:
            try:
                state.bridge_instance._sse_session.close()
                log.info("[Web] SSE 连接已断开")
            except Exception:
                log.warning("[Web] 断开 SSE 连接失败")

    # 关闭 WebSocket 连接，让 _ob_client_main 从 async for 中退出
    _ws = state._ob_ws
    _loop = state._ob_ws_loop
    if _ws:
        try:
            if _loop and _loop.is_running():
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _ws.close(), _loop
                )
                log.info("[Web] WebSocket 连接已关闭")
        except Exception:
            log.warning("[Web] 关闭 WebSocket 连接失败")

    state._ob_ws_ready.clear()

    state._ob_ws_loop = None

    log.info("[Web] 已停止")


def _bridge_loop(generation: int):
    if not state.is_generation_running(generation):
        return
    if not config.ACCESS_TOKEN:
        log.error("❌ 未配置 access_token")
        state.deactivate_generation(generation)
        return

    log.info("Bridge | endpoints=WeFlow,OB11 | sender_mode=uia_fixed")

    with state.run_lock:
        if not state.is_generation_running(generation):
            return
        bridge = WeFlowBridge(
            state.sender_instance,
            generation=generation,
        )
        with state.bridge_lock:
            state.bridge_instance = bridge

    readiness_attempt = 0
    while state.is_generation_running(generation):
        readiness_attempt += 1
        try:
            response = requests.get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/sessions",
                params={
                    "limit": 1,
                    "access_token": config.ACCESS_TOKEN,
                },
                timeout=5,
            )
            if not state.is_generation_running(generation):
                return
            if response.status_code == 200:
                log.info("✅ WeFlow API 正常")
                break
            if response.status_code == 401:
                log.error("❌ Access Token 无效")
                state.deactivate_generation(generation)
                return
            if readiness_attempt == 1 or readiness_attempt % 15 == 0:
                log.warning(
                    "WeFlow 尚未就绪，2 秒后重试: status=%s",
                    response.status_code,
                )
        except requests.exceptions.RequestException:
            if not state.is_generation_running(generation):
                return
            if readiness_attempt == 1 or readiness_attempt % 15 == 0:
                log.warning("WeFlow 尚未就绪，2 秒后重试")

        for _ in range(20):
            if not state.is_generation_running(generation):
                return
            time.sleep(0.1)

    if not state.is_generation_running(generation):
        return

    while state.is_generation_running(generation):
        try:
            bridge.listen_sse()
        except Exception:
            log.error("SSE 连接异常")
        if not state.is_generation_running(generation):
            break
        log.warning("⚠️ SSE 断开，10s 后重连")
        for _ in range(10):
            if not state.is_generation_running(generation):
                break
            time.sleep(1)

    with state.bridge_lock:
        if state.bridge_instance is bridge:
            if hasattr(bridge, "stop_money_actions"):
                bridge.stop_money_actions()
            state.bridge_instance = None


def start_web():
    server = None
    for attempt in range(1, 11):
        try:
            server = LocalHTTPServer(
                ("127.0.0.1", int(config.WEB_PORT)),
                WebHandler,
            )
            break
        except OSError as error:
            if attempt >= 10:
                log.error(
                    "Bridge Web 面板启动失败: port=%s code=E_BRIDGE_WEB_BIND",
                    config.WEB_PORT,
                )
                raise
            log.warning(
                "Bridge Web 端口暂不可用，1 秒后重试: port=%s attempt=%s",
                config.WEB_PORT,
                attempt,
            )
            time.sleep(1)
    if server is None:
        raise RuntimeError("E_BRIDGE_WEB_BIND")
    log.info("Web: http://127.0.0.1:%s", config.WEB_PORT)
    server.serve_forever()


# ============ 入口 ============


def _run_main() -> None:
    # 从 config 初始化 state 中需要计算的值
    state._self_id_int = state._wxid_to_int(config.BOT_WXID or "wechat_bot")
    state.group_reply_mode = config.GROUP_REPLY_MODE

    STATE_DIR = os.path.abspath(os.environ.get("AKASHABOT_STATE_DIR", os.path.dirname(os.path.abspath(__file__))))
    os.makedirs(STATE_DIR, exist_ok=True)
    PID_FILE = os.path.join(STATE_DIR, "bridge.pid")

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid_record = f.read().strip()
            pid_status = _pid_record_status(pid_record)
            if pid_status == "live":
                log.error("⚠️ bridge.pid 已存在")
                sys.exit(1)
            if pid_status != "stale":
                raise RuntimeError("E_BRIDGE_PID_STATE")
            os.remove(PID_FILE)
            log.warning("已清理失效的 bridge.pid")
        except OSError as error:
            raise RuntimeError("E_BRIDGE_PID_STATE") from error

    current_pid = os.getpid()
    current_start = _process_start_token(current_pid)
    current_record = (
        f"{current_pid}:{current_start}"
        if current_start is not None
        else str(current_pid)
    )
    try:
        _claim_pid_file(PID_FILE, current_record)
    except FileExistsError:
        log.error("⚠️ bridge.pid 已由另一个进程占用")
        sys.exit(1)

    try:
        log.info("=" * 50)
        log.info(" WeFlow 微信桥接 (OneBot v11)")
        log.info("=" * 50)
        log.info("Bridge 版本: 2026-06-03 OB11")
        _start_bridge()
        start_web()
    finally:
        _release_pid_file_if_owned(PID_FILE, current_record)


if __name__ == "__main__":
    try:
        _run_main()
    except SystemExit:
        raise
    except Exception as startup_error:
        _write_startup_failure("runtime", startup_error)
        log.error(
            "E_BRIDGE_STARTUP: Bridge startup failed; type=%s",
            type(startup_error).__name__,
        )
        raise
