from __future__ import annotations

import asyncio
import math
import time
import uuid
from pathlib import Path
from typing import Any

from aiocqhttp.exceptions import ActionFailed
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool

from .catalog import (
    STATE_DIR_ENV,
    StickerEntry,
    build_tool_description,
    load_catalog,
    resolve_catalog_path,
)

PLUGIN_NAME = "astrbot_plugin_akasha_favorite_stickers"
TOOL_NAME = "send_wechat_favorite_sticker"
ACTION_NAME = "send_wechat_favorite_sticker"
_EVENT_USED_KEY = "_akasha_favorite_sticker_used"


class _FavoriteStickerTool(FunctionTool):
    """Keep AstrBot's dynamic tool ownership attached to this plugin module."""


def _config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    return value if isinstance(value, bool) else default


def _config_float(
    config: dict,
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, minimum), maximum)


def _positive_onebot_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.enabled = _config_bool(self.config, "enabled", True)
        self.allow_private = _config_bool(
            self.config,
            "allow_private",
            True,
        )
        self.allow_group = _config_bool(self.config, "allow_group", True)
        self.cooldown_seconds = _config_float(
            self.config,
            "cooldown_seconds",
            60.0,
            minimum=0.0,
            maximum=3600.0,
        )
        self.action_timeout_seconds = _config_float(
            self.config,
            "action_timeout_seconds",
            45.0,
            minimum=45.0,
            maximum=180.0,
        )

        bundled_catalog = Path(__file__).with_name("catalog.json")
        self.catalog_path = resolve_catalog_path(bundled_catalog)
        self.catalog = load_catalog(self.catalog_path)
        self._catalog_by_id = {
            entry.sticker_id: entry for entry in self.catalog
        }
        self._cooldown_lock = asyncio.Lock()
        self._last_attempt_by_target: dict[str, float] = {}

        if self.enabled and (self.allow_private or self.allow_group):
            tool = _FavoriteStickerTool(
                name=TOOL_NAME,
                description=build_tool_description(self.catalog),
                parameters={
                    "type": "object",
                    "properties": {
                        "sticker_id": {
                            "type": "string",
                            "enum": [
                                entry.sticker_id for entry in self.catalog
                            ],
                            "description": (
                                "从工具说明的语义目录中选择一个固定 ID"
                            ),
                        }
                    },
                    "required": ["sticker_id"],
                    "additionalProperties": False,
                },
                handler=Main.send_wechat_favorite_sticker,
            )
            context.add_llm_tools(tool)
            logger.info(
                "Akasha 微信收藏表情工具已注册（20 个固定槽位，目录来源=%s）",
                (
                    STATE_DIR_ENV
                    if self.catalog_path != bundled_catalog
                    else "bundled"
                ),
            )
        else:
            logger.info("Akasha 微信收藏表情工具已由配置关闭")

    def _resolve_target(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, int, str] | tuple[None, None, str]:
        if event.is_private_chat():
            if not self.allow_private:
                return None, None, "当前配置禁止在微信私聊发送收藏表情。"
            user_id = _positive_onebot_id(event.get_sender_id())
            if user_id is None:
                return None, None, "当前私聊缺少可信的 OneBot user_id。"
            return "user_id", user_id, f"private:{user_id}"

        group_id = _positive_onebot_id(event.get_group_id())
        if group_id is None:
            return None, None, "收藏表情工具只支持微信私聊或群聊消息事件。"
        if not self.allow_group:
            return None, None, "当前配置禁止在微信群聊发送收藏表情。"
        return "group_id", group_id, f"group:{group_id}"

    async def _reserve_attempt(
        self,
        target_key: str,
    ) -> tuple[bool, float]:
        now = time.monotonic()
        async with self._cooldown_lock:
            previous = self._last_attempt_by_target.get(target_key)
            if previous is not None:
                remaining = self.cooldown_seconds - (now - previous)
                if remaining > 0:
                    return False, remaining
            self._last_attempt_by_target[target_key] = now
            return True, 0.0

    @staticmethod
    def _event_already_used(event: AstrMessageEvent) -> bool:
        return bool(event.get_extra(_EVENT_USED_KEY, False))

    @staticmethod
    def _mark_event_used(
        event: AstrMessageEvent,
        *,
        sticker_id: str,
        request_id: str,
    ) -> None:
        event.set_extra(
            _EVENT_USED_KEY,
            {
                "sticker_id": sticker_id,
                "request_id": request_id,
            },
        )

    @staticmethod
    def _action_failure_text(exc: ActionFailed) -> str:
        result = exc.result if isinstance(exc.result, dict) else {}
        retcode = result.get("retcode", "unknown")
        data = result.get("data")
        error_code = (
            data.get("error_code")
            if isinstance(data, dict)
            else None
        )
        if isinstance(error_code, str) and error_code:
            return (
                "Bridge 拒绝发送收藏表情"
                f"（retcode={retcode}, error_code={error_code}）。"
                "不要在本轮自动重试。"
            )
        return (
            f"Bridge 拒绝发送收藏表情（retcode={retcode}）。"
            "不要在本轮自动重试。"
        )

    async def send_wechat_favorite_sticker(
        self,
        event: AstrMessageEvent,
        sticker_id: str,
    ) -> str:
        """Send one native WeChat favorite sticker to the current conversation."""
        if not self.enabled:
            return "收藏表情工具已关闭。"
        if event.get_platform_name() != "aiocqhttp":
            return "收藏表情工具只允许用于 Akasha 的 aiocqhttp 微信平台。"
        if self._event_already_used(event):
            return "本轮已经尝试过收藏表情；不要再次调用。"

        entry: StickerEntry | None = self._catalog_by_id.get(
            str(sticker_id).strip()
        )
        if entry is None:
            return "无效的 sticker_id；不要猜测或改写目录中的 ID。"

        target_field, target_id, target_key = self._resolve_target(event)
        if target_field is None or target_id is None:
            return target_key

        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return "当前 aiocqhttp 事件没有可用的 OneBot action 通道。"

        reserved, remaining = await self._reserve_attempt(target_key)
        if not reserved:
            return (
                "当前会话仍在收藏表情冷却期，约 "
                f"{max(1, int(remaining + 0.999))} 秒后才能再次尝试。"
            )

        request_id = str(uuid.uuid4())
        self._mark_event_used(
            event,
            sticker_id=entry.sticker_id,
            request_id=request_id,
        )
        params: dict[str, Any] = {
            target_field: target_id,
            "sticker_key": entry.sticker_key,
            "request_id": request_id,
        }

        try:
            result = await asyncio.wait_for(
                call_action(ACTION_NAME, **params),
                timeout=self.action_timeout_seconds,
            )
        except ActionFailed as exc:
            failure_result = (
                exc.result if isinstance(exc.result, dict) else {}
            )
            logger.warning(
                "Akasha 收藏表情 action 被 Bridge 拒绝：retcode=%s",
                failure_result.get("retcode", "unknown"),
            )
            return self._action_failure_text(exc)
        except TimeoutError:
            logger.warning("Akasha 收藏表情 action 等待 Bridge 超时")
            return (
                "收藏表情发送结果未知：等待 Bridge 超时。"
                "不要在本轮或冷却期内自动重试。"
            )
        except Exception as exc:
            logger.warning(
                "Akasha 收藏表情 action 调用异常：%s",
                type(exc).__name__,
            )
            return (
                "收藏表情发送结果未知：OneBot action 通道异常。"
                "不要在本轮或冷却期内自动重试。"
            )

        if (
            isinstance(result, dict)
            and result.get("status") == "failed"
        ):
            retcode = result.get("retcode", "unknown")
            return (
                f"Bridge 拒绝发送收藏表情（retcode={retcode}）。"
                "不要在本轮自动重试。"
            )
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(result.get("data"), dict)
        ):
            result = result["data"]

        if isinstance(result, dict) and result.get("confirmed") is True:
            return (
                f"已确认发送收藏表情 {entry.sticker_id}。"
                "本轮不要再次调用此工具，也无需额外描述发送动作。"
            )
        if isinstance(result, dict) and result.get("confirmed") is False:
            return (
                "Bridge 未确认收藏表情送达。"
                "不要在本轮或冷却期内自动重试。"
            )
        return (
            "Bridge 已接受收藏表情动作，但没有提供送达确认。"
            "结果视为未知，不要在本轮或冷却期内自动重试。"
        )
