"""
OneBot WebSocket 客户端模块。

维护到 AstrBot aiocqhttp 服务端的 WebSocket 长连接，
推送事件并从 AstrBot 接收 API 请求。
"""

import asyncio
import json
import logging
import threading

import websockets
from websockets.asyncio.server import ServerConnection, serve

import state
import config
from ob_protocol import _handle_ob_api

log = logging.getLogger("ob11-bridge")


def _run_ob_client(generation):
    """后台线程：维护到 AstrBot 的 WebSocket 连接。"""
    _loop = asyncio.new_event_loop()
    state._ob_ws_loop = _loop
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ob_client_main(generation))
    finally:
        try:
            _loop.close()
        except Exception:
            pass
        if state._ob_ws_loop is _loop:
            state._ob_ws_loop = None


async def _ob_client_main(generation):
    """WebSocket 客户端主协程：连接 AstrBot，发送事件，接收 API 响应。"""
    active_ws = None
    while state.is_generation_running(generation):
        active_ws = None
        try:
            log.info("[OB11] 正在连接 AstrBot OneBot 端点")
            async with websockets.connect(
                config.ASTRBOT_OB_URL,
                additional_headers={
                    "X-Self-ID": str(state._self_id_int),
                    "X-Client-Role": "Universal",
                    "User-Agent": "OneBot/11",
                }
            ) as ws:
                if not state.is_generation_running(generation):
                    break
                active_ws = ws
                state._ob_ws = ws
                state._ob_ws_ready.set()
                log.info(f"[OB11] ✅ 已连接到 AstrBot")

                # 心跳保活：每 15 秒发 ping
                async def _keepalive():
                    while state.is_generation_running(generation):
                        await asyncio.sleep(15)
                        try:
                            await ws.ping()
                        except Exception:
                            break
                ka_task = asyncio.create_task(_keepalive())
                try:
                    # 持续接收 API 请求（异步处理，不阻塞）
                    async for raw in ws:
                        if not state.is_generation_running(generation):
                            break
                        try:
                            data = json.loads(raw)
                            # 用 create_task 异步处理，不阻塞消息循环
                            asyncio.create_task(
                                _handle_ob_api(data, generation=generation)
                            )
                        except json.JSONDecodeError:
                            log.warning(f"[OB11] 收到无效 JSON")
                        except Exception:
                            log.error("[OB11] 处理 API 请求失败")
                finally:
                    ka_task.cancel()

        except websockets.exceptions.ConnectionClosed:
            log.warning(f"[OB11] 连接断开，5 秒后重连")
        except (ConnectionRefusedError, OSError):
            log.warning("[OB11] 无法连接 AstrBot，5 秒后重试")
        except Exception:
            log.error("[OB11] 连接异常，5 秒后重试")

        if active_ws is not None and state._ob_ws is active_ws:
            state._ob_ws = None
            state._ob_ws_ready.clear()
        if not state.is_generation_running(generation):
            break
        await asyncio.sleep(5)

    if active_ws is not None and state._ob_ws is active_ws:
        state._ob_ws = None
        state._ob_ws_ready.clear()
