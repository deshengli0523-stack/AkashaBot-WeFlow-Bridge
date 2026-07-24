from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.provider.entities import ProviderType

from .akasha_memory.context_builder import ContextBuilder
from .akasha_memory.provider import (
    PROVIDER_ID,
    PROVIDER_TYPE,
    AkashaQwenMemoryProvider,
)
from .akasha_memory.qwen_client import QwenClient
from .akasha_memory.qwen_session import QwenSessionManager
from .akasha_memory.runtime import ContactMemoryRuntime
from .akasha_memory.security import SecretManager
from .akasha_memory.store import MemoryStore
from .akasha_memory.weflow_sync import WeFlowSync

PLUGIN_NAME = "astrbot_plugin_akasha_contact_memory"


def _value(config: dict, key: str, default: Any) -> Any:
    value = config.get(key, default)
    return default if value is None else value


def _base_url(source_provider: Provider | None, override: str) -> str:
    value = override.strip()
    if not value and source_provider:
        value = str(source_provider.provider_config.get("api_base") or "").strip()
    for suffix in ("/chat/completions", "/responses"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.runtime: ContactMemoryRuntime | None = None
        self.memory_provider: AkashaQwenMemoryProvider | None = None
        self.source_provider: Provider | None = None
        self._effective_mode = "off"

    def _source_provider(self) -> Provider | None:
        source_id = str(_value(self.config, "source_provider_id", "")).strip()
        if source_id:
            candidate = self.context.get_provider_by_id(source_id)
            if (
                isinstance(candidate, Provider)
                and candidate.provider_config.get("id") != PROVIDER_ID
            ):
                return candidate
            logger.error(
                "Akasha 联系人记忆配置的 source_provider_id 不存在或不可用；"
                "不会改用其他 Provider。"
            )
            return None

        candidate = self.context.get_using_provider()
        if isinstance(candidate, Provider) and candidate.provider_config.get(
            "id"
        ) != PROVIDER_ID:
            return candidate
        for provider in self.context.get_all_providers():
            if (
                isinstance(provider, Provider)
                and provider.provider_config.get("id") != PROVIDER_ID
            ):
                return provider
        return None

    async def _route_to_source_provider(self, event: AstrMessageEvent) -> None:
        if not self.source_provider:
            return
        source_id = str(
            self.source_provider.provider_config.get("id") or ""
        ).strip()
        if source_id:
            try:
                await self.context.provider_manager.set_provider(
                    source_id,
                    ProviderType.CHAT_COMPLETION,
                    event.unified_msg_origin,
                )
            except ValueError:
                logger.error(
                    "Akasha 联系人记忆无法恢复源 Provider；"
                    "该 Provider 可能已被移除。"
                )

    async def initialize(self) -> None:
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        secrets = SecretManager(data_dir)
        state_dir_value = os.environ.get("AKASHABOT_STATE_DIR", "").strip()
        backup_dir = (
            Path(state_dir_value) / "akasha-contact-memory-backups"
            if state_dir_value
            else None
        )
        store = MemoryStore(data_dir, backup_dir=backup_dir)
        await store.initialize()

        mode = str(_value(self.config, "mode", "shadow")).lower()
        context_builder = ContextBuilder(
            store,
            seed_max_tokens=int(
                _value(self.config, "seed_max_tokens", 150_000)
            ),
        )
        synchronizer = WeFlowSync(
            store,
            bridge_config_path=str(
                _value(self.config, "bridge_config_path", "")
            ),
            allow_non_loopback=bool(
                _value(self.config, "allow_non_loopback_weflow", False)
            ),
            cold_limit=int(_value(self.config, "cold_sync_messages", 2000)),
            fallback_limit=int(
                _value(self.config, "fallback_sync_messages", 500)
            ),
            sync_budget_seconds=float(
                _value(self.config, "sync_budget_seconds", 3.0)
            ),
        )

        self.source_provider = self._source_provider()
        model = str(_value(self.config, "qwen_model", "qwen3.7-max")).strip()
        base_url = _base_url(
            self.source_provider,
            str(_value(self.config, "qwen_responses_base_url", "")),
        )
        qwen_sessions: QwenSessionManager | None = None
        source_has_key = False
        if self.source_provider:
            try:
                source_has_key = bool(self.source_provider.get_current_key().strip())
            except Exception:
                source_has_key = False
        if self.source_provider and source_has_key and base_url:
            try:
                client = QwenClient(
                    base_url=base_url,
                    api_key=self.source_provider.get_current_key,
                    request_timeout=float(
                        _value(self.config, "request_timeout_seconds", 120)
                    ),
                    enable_session_cache=bool(
                        _value(self.config, "enable_session_cache", True)
                    ),
                )
                qwen_sessions = QwenSessionManager(
                    store=store,
                    context_builder=context_builder,
                    client=client,
                    model=model,
                    soft_context_tokens=int(
                        _value(self.config, "soft_context_tokens", 700_000)
                    ),
                    cloud_ttl_days=float(
                        _value(self.config, "cloud_ttl_days", 7)
                    ),
                )
            except (TypeError, ValueError):
                qwen_sessions = None

        effective_mode = mode if mode in ContactMemoryRuntime.VALID_MODES else "shadow"
        if effective_mode == "active" and qwen_sessions is None:
            effective_mode = "shadow"
            logger.error(
                "Akasha 联系人记忆缺少可用的源 Provider 或 Responses base URL，"
                "已降级为 shadow 模式。"
            )
        self._effective_mode = effective_mode
        self.runtime = ContactMemoryRuntime(
            mode=effective_mode,
            secret_manager=secrets,
            store=store,
            synchronizer=synchronizer,
            context_builder=context_builder,
            qwen_sessions=qwen_sessions,
            fallback_context_tokens=int(
                _value(self.config, "fallback_context_tokens", 120_000)
            ),
        )

        if (
            effective_mode == "active"
            and qwen_sessions
            and self.source_provider
        ):
            manager = self.context.provider_manager
            existing = manager.inst_map.get(PROVIDER_ID)
            if existing is not None:
                await manager.terminate_provider(PROVIDER_ID)
            self.memory_provider = AkashaQwenMemoryProvider(
                {
                    "id": PROVIDER_ID,
                    "type": PROVIDER_TYPE,
                    "model": model,
                    "enable": True,
                },
                getattr(self.source_provider, "provider_settings", {}),
                runtime=self.runtime,
                fallback_provider=self.source_provider,
            )
            self.context.register_provider(self.memory_provider)
            # Context.register_provider() does not populate inst_map in 4.26.6.
            manager.inst_map[PROVIDER_ID] = self.memory_provider
        logger.info(
            "Akasha 联系人记忆插件已启动：mode=%s, qwen_ready=%s",
            self._effective_mode,
            bool(qwen_sessions),
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=10_000,
    )
    async def route_private_contact(self, event: AstrMessageEvent) -> None:
        if not self.runtime:
            return
        prepared, sync_result = await self.runtime.prepare_request(
            event,
            event.get_message_str(),
        )
        if prepared:
            event.set_extra(
                "akasha_memory_contact_key",
                prepared.request_key,
            )
        elif sync_result and sync_result.error_kind == "tombstoned":
            await self._route_to_source_provider(event)
        if (
            prepared
            and self._effective_mode == "active"
            and self.memory_provider
        ):
            await self.context.provider_manager.set_provider(
                PROVIDER_ID,
                ProviderType.CHAT_COMPLETION,
                event.unified_msg_origin,
            )

    @filter.on_llm_request()
    async def prepare_contact_memory(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.runtime or self._effective_mode == "off":
            return
        prompt = req.prompt or event.get_message_str()
        cached_key = str(
            event.get_extra("akasha_memory_contact_key", "") or ""
        )
        if cached_key:
            prepared = await self.runtime.validate_request(event, cached_key)
        else:
            prepared, _ = await self.runtime.prepare_request(event, prompt)
        if not prepared or self._effective_mode != "active":
            return
        contact_key = prepared.request_key
        req.session_id = contact_key
        # The agent chooses its Provider before this hook. If another plugin
        # changed it after our routing handler, still prevent mixed AstrBot
        # history by replacing only this request's contexts.
        selected = self.context.get_using_provider(event.unified_msg_origin)
        if selected is not self.memory_provider:
            local_contexts = await self.runtime.fallback_contexts(
                contact_key,
                current_prompt=prompt,
            )
            if local_contexts:
                req.contexts = local_contexts
                req.system_prompt = self.runtime.fallback_system_prompt(
                    req.system_prompt
                )

    async def _finish_pending_request(self, event: AstrMessageEvent) -> None:
        if not self.runtime:
            return
        contact_key = str(
            event.get_extra("akasha_memory_contact_key", "") or ""
        )
        if contact_key and await self.runtime.finish_request(contact_key):
            logger.warning(
                "Akasha 联系人记忆检测到未回传的工具调用；"
                "已将该云端会话标记为需要重建。"
            )

    @filter.on_agent_done()
    async def finish_contact_agent(
        self,
        event: AstrMessageEvent,
        _run_context: Any,
        _response: Any,
    ) -> None:
        await self._finish_pending_request(event)

    @filter.after_message_sent()
    async def finish_contact_after_send(
        self,
        event: AstrMessageEvent,
    ) -> None:
        await self._finish_pending_request(event)

    @filter.command_group("akasha_memory")
    def akasha_memory(self) -> None:
        """Akasha 联系人记忆管理。"""

    @akasha_memory.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_status(self, event: AstrMessageEvent):
        if not self.runtime:
            yield event.plain_result("联系人记忆尚未初始化。")
            return
        status = await self.runtime.status(event)
        yield event.plain_result(
            "\n".join(
                (
                    f"mode: {status['mode']}",
                    f"qwen_ready: {status['qwen_ready']}",
                    f"contacts: {status['contacts']}",
                    f"messages: {status['messages']}",
                    f"contact_messages: {status.get('contact_messages', 0)}",
                    f"sessions: {status['sessions']}",
                    f"dirty_sessions: {status['dirty_sessions']}",
                    f"full_backfill_complete: "
                    f"{status.get('full_backfill_complete', False)}",
                    f"contact_tombstoned: "
                    f"{status.get('contact_tombstoned', False)}",
                )
            )
        )

    @akasha_memory.command("rebuild")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_rebuild(self, event: AstrMessageEvent):
        if not self.runtime:
            yield event.plain_result("联系人记忆尚未初始化。")
            return
        count = await self.runtime.rebuild(event)
        if count is None:
            yield event.plain_result("当前消息不是 Akasha 私聊，或 Qwen 尚未配置。")
            return
        yield event.plain_result(
            f"已标记重建；下次回复会从本地记录重建会话（旧会话 {count} 个）。"
        )

    @akasha_memory.command("forget")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_forget(
        self,
        event: AstrMessageEvent,
        confirmation: str = "",
    ):
        if confirmation != "CONFIRM":
            yield event.plain_result(
                "此操作会删除当前联系人的本地记忆及云端会话。"
                "请发送 /akasha_memory forget CONFIRM 确认。"
            )
            return
        if not self.runtime:
            yield event.plain_result("联系人记忆尚未初始化。")
            return
        result = await self.runtime.forget(event)
        if result is None:
            yield event.plain_result("当前消息不是 Akasha 私聊，或 Qwen 尚未配置。")
        elif not result[0]:
            await self._route_to_source_provider(event)
            yield event.plain_result(
                f"云端仍有 {result[1]} 个会话未删除，本地数据保持不变，请稍后重试。"
            )
        else:
            await self._route_to_source_provider(event)
            yield event.plain_result("当前联系人的本地记忆与云端会话已删除。")

    async def terminate(self) -> None:
        runtime = self.runtime
        if self.memory_provider:
            manager = self.context.provider_manager
            if manager.inst_map.get(PROVIDER_ID) is self.memory_provider:
                await manager.terminate_provider(PROVIDER_ID)
            self.memory_provider = None
        if runtime:
            await runtime.close()
        self.runtime = None
