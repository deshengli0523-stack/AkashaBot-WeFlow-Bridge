"""
入口与生命周期管理模块。

负责桥接的启动、停止、主循环重连逻辑，以及命令行入口。
"""

import json
import logging
import os
import sys
import threading
import time

import requests

import state
import config
from uia_fixed_sender import UiaFixedSender
from ob_client import _run_ob_client
from bridge_core import WeFlowBridge
from web_panel import WebHandler, PAGE
from http.server import HTTPServer

log = logging.getLogger("ob11-bridge")


# ============ 启动 / 停止 ============


def _start_bridge():
    with state.run_lock:
        if state.running:
            return
        sender = UiaFixedSender(
            config.UIA_FIXED_CALIBRATION,
            pre_paste_preview_delay=config.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY,
            pre_send_delay=config.UIA_FIXED_PRE_SEND_DELAY,
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
            state.bridge_instance = None


def start_web():
    server = HTTPServer(("127.0.0.1", config.WEB_PORT), WebHandler)
    log.info(f"Web: http://127.0.0.1:{config.WEB_PORT}")
    server.serve_forever()


# ============ 入口 ============

if __name__ == "__main__":
    # 从 config 初始化 state 中需要计算的值
    state._self_id_int = state._wxid_to_int(config.BOT_WXID or "wechat_bot")
    state.group_reply_mode = config.GROUP_REPLY_MODE

    STATE_DIR = os.path.abspath(os.environ.get("AKASHABOT_STATE_DIR", os.path.dirname(os.path.abspath(__file__))))
    os.makedirs(STATE_DIR, exist_ok=True)
    PID_FILE = os.path.join(STATE_DIR, "bridge.pid")

    def pid_exists(pid):
        try:
            import ctypes
            from ctypes import wintypes
            h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if pid_exists(old_pid):
                log.error("⚠️ bridge.pid 已存在")
                sys.exit(1)
            else:
                os.remove(PID_FILE)
        except (ValueError, OSError):
            os.remove(PID_FILE)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        log.info("=" * 50)
        log.info(" WeFlow 微信桥接 (OneBot v11)")
        log.info("=" * 50)
        log.info("Bridge 版本: 2026-06-03 OB11")
        _start_bridge()
        start_web()
    finally:
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
