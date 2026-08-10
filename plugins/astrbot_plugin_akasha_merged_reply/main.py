from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from astrbot.core.utils.active_event_registry import active_event_registry
except (ImportError, AttributeError):
    active_event_registry = None


PLUGIN_NAME = "astrbot_plugin_akasha_merged_reply"
ACTION_NAME = "send_akasha_merged_reply"
REGISTER_ACTION = "register_akasha_merged_reply"
RENEW_ACTION = "renew_akasha_merged_reply"
ACTIVATE_ACTION = "activate_akasha_merged_reply"
ACK_ACTION = "ack_akasha_send_result"
RELEASE_ACTION = "release_akasha_reply_admission"
FINISH_ACTION = "finish_akasha_reply_admission"
GET_ADMISSION_ACTION = "get_akasha_reply_admission"

PROTOCOL_VERSION = 1
ACTION_VERSION = 1
RESULT_SCHEMA = 2
MEMORY_SCHEMA = 1
LEASE_RENEW_SECONDS = 5.0
LEASE_SECONDS = 15.0
ACTION_TIMEOUT_SECONDS = 5.0
LEASE_INVALID_ERROR = "E_MERGED_REPLY_LEASE_INVALID"
MAX_CODEPOINTS = 2000
MAX_UTF8_BYTES = 16 * 1024

REQUEST_ID_KEY = "akasha_merged_request_id"
CLAIMED_KEY = "akasha_merged_claimed"
NORMALIZED_TEXT_KEY = "akasha_merged_normalized_text"
NORMALIZE_ERROR_KEY = "akasha_merged_normalize_error"
MEMORY_BIND_STATUS_KEY = "akasha_memory_bind_status"
MEMORY_RESULT_STATUS_KEY = "akasha_memory_result_status"
TRACKER_KEY = "akasha_admission_tracker"

PROMPT_MARKER = "[AKASHA_MERGED_REPLY_V1]"
PROMPT = (
    "[AKASHA_MERGED_REPLY_V1]\n"
    "请只回复当前最新一轮消息。默认用一条简洁自然的微信消息回答；"
    "删除不影响含义的总结、客套和追问，不要为了凑完整而追加低价值尾句。"
)
_on_astrbot_loaded = getattr(
    filter,
    "on_astrbot_loaded",
    lambda: (lambda function: function),
)
_event_message_type = getattr(
    filter,
    "event_message_type",
    lambda *args, **kwargs: (lambda function: function),
)
_on_agent_done = getattr(
    filter,
    "on_agent_done",
    lambda *args, **kwargs: (lambda function: function),
)
_after_message_sent = getattr(
    filter,
    "after_message_sent",
    lambda *args, **kwargs: (lambda function: function),
)
_PRIVATE_MESSAGE = getattr(
    getattr(filter, "EventMessageType", None),
    "PRIVATE_MESSAGE",
    "private",
)
_TRACKER_HOOKS_READY = all(
    callable(getattr(filter, name, None))
    for name in ("on_agent_done", "after_message_sent")
)


def _config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    return value if isinstance(value, bool) else default


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_reply_text(value: str) -> str:
    """Deterministic normalization used for fingerprint, paste and ownership."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    output: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                output.append("")
        else:
            blank_run = 0
            output.append(line)
    return "\n".join(output)


def validate_reply_text(value: str) -> str:
    normalized = normalize_reply_text(value)
    if not normalized.strip():
        raise ValueError("E_MERGED_REPLY_EMPTY")
    if any(ord(character) < 32 and character not in "\t\n" for character in normalized):
        raise ValueError("E_MERGED_REPLY_CONTROL_CHARACTER")
    if len(normalized) > MAX_CODEPOINTS:
        raise ValueError("E_MERGED_REPLY_CODEPOINT_LIMIT")
    if len(normalized.encode("utf-8")) > MAX_UTF8_BYTES:
        raise ValueError("E_MERGED_REPLY_UTF8_LIMIT")
    return normalized


def _response_data(response: object) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    nested = response.get("data")
    return nested if isinstance(nested, dict) else response


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.enabled = _config_bool(self.config, "enabled", True)
        self.plugin_instance_id = str(uuid.uuid4())
        self._lease_lock = asyncio.Lock()
        self._lease: dict[str, Any] | None = None
        self._lease_task: asyncio.Task | None = None
        self._lease_wake = asyncio.Event()
        self._tracker_tasks: dict[str, asyncio.Task] = {}
        self._acceptance_tasks: dict[str, asyncio.Task] = {}
        self._recovered_generation: int | None = None
        self._terminated = False

    def _managed_bot(self):
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        if not callable(get_insts):
            return None
        try:
            instances = list(get_insts())
        except Exception:
            return None
        for instance in instances:
            try:
                meta = instance.meta()
                platform_name = str(getattr(meta, "name", "") or "")
                platform_config = getattr(instance, "config", {})
                platform_id = str(platform_config.get("id") or "")
                if platform_name == "aiocqhttp" and platform_id == "akasha_ob11":
                    return getattr(instance, "bot", None)
            except Exception:
                continue
        return None

    def _streaming_disabled(self) -> bool:
        get_config = getattr(self.context, "get_config", None)
        if not callable(get_config):
            return False
        try:
            root = get_config()
            provider_settings = root.get("provider_settings", {})
            return provider_settings.get("streaming_response") is False
        except Exception:
            return False

    @staticmethod
    def _registry_ready() -> bool:
        events = getattr(active_event_registry, "_events", None)
        return isinstance(events, dict) and callable(
            getattr(active_event_registry, "request_agent_stop_all", None)
        )

    def _memory_ready(self) -> bool:
        return all(
            callable(getattr(self.context, name, None))
            for name in (
                "akasha_memory_pre_action_terminal",
                "akasha_memory_acceptance",
                "akasha_memory_recovery_records",
                "akasha_memory_recovery_acceptance",
                "akasha_memory_recovery_terminal",
            )
        )

    def _ensure_supervisor(self) -> None:
        if self._terminated or not self.enabled:
            return
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(self._lease_supervisor())

    async def _pause_lease_supervisor(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._lease_wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        finally:
            self._lease_wake.clear()

    async def _discard_lease_snapshot(self, lease: dict[str, Any]) -> bool:
        """Discard only the rejected snapshot, preserving a concurrent renewal."""

        discarded = False
        async with self._lease_lock:
            if (
                self._lease is not None
                and self._lease.get("lease_id") == lease.get("lease_id")
                and self._lease.get("bridge_generation")
                == lease.get("bridge_generation")
            ):
                self._lease = None
                discarded = True
        if discarded:
            self._lease_wake.set()
            self._ensure_supervisor()
        return discarded

    async def initialize(self) -> None:
        self._ensure_supervisor()

    @_on_astrbot_loaded()
    async def initialize_after_astrbot_loaded(self) -> None:
        self._ensure_supervisor()

    async def _lease_supervisor(self) -> None:
        while not self._terminated:
            try:
                bot = self._managed_bot()
                call_action = getattr(bot, "call_action", None)
                if (
                    not callable(call_action)
                    or not self._streaming_disabled()
                    or not _TRACKER_HOOKS_READY
                    or not self._registry_ready()
                    or not self._memory_ready()
                ):
                    async with self._lease_lock:
                        self._lease = None
                    await self._pause_lease_supervisor(1.0)
                    continue
                async with self._lease_lock:
                    lease = None if self._lease is None else dict(self._lease)
                if lease is None or float(lease.get("expires_at") or 0.0) <= time.monotonic() + 5.0:
                    was_activated = bool(lease and lease.get("activated"))
                    was_activation_confirmed = bool(
                        lease and lease.get("activation_confirmed")
                    )
                    lease_request_started = time.monotonic()
                    if lease is None:
                        response = await asyncio.wait_for(
                            call_action(
                                REGISTER_ACTION,
                                plugin_instance_id=self.plugin_instance_id,
                                protocol_version=PROTOCOL_VERSION,
                                action_version=ACTION_VERSION,
                                result_schema=RESULT_SCHEMA,
                                memory_schema=MEMORY_SCHEMA,
                                streaming_response=False,
                                lifecycle_tracker_ready=(
                                    _TRACKER_HOOKS_READY
                                    and self._registry_ready()
                                ),
                            ),
                            timeout=ACTION_TIMEOUT_SECONDS,
                        )
                    else:
                        response = await asyncio.wait_for(
                            call_action(
                                RENEW_ACTION,
                                plugin_instance_id=self.plugin_instance_id,
                                lease_id=str(lease["lease_id"]),
                                bridge_generation=int(lease["bridge_generation"]),
                            ),
                            timeout=ACTION_TIMEOUT_SECONDS,
                        )
                    data = _response_data(response)
                    if data.get("error_code") == LEASE_INVALID_ERROR:
                        if lease is not None:
                            await self._discard_lease_snapshot(lease)
                        raise RuntimeError("merged reply lease rejected")
                    lease_id = str(data.get("lease_id") or "")
                    generation = _positive_int(data.get("bridge_generation"))
                    if not lease_id or generation is None:
                        raise RuntimeError("invalid merged reply lease response")
                    async with self._lease_lock:
                        self._lease = {
                            "lease_id": lease_id,
                            "bridge_generation": generation,
                            # Bridge starts this duration before the response
                            # reaches AstrBot.  Base the local deadline on the
                            # request start so the plugin never overestimates it.
                            "expires_at": lease_request_started + float(
                                data.get("expires_in_seconds") or LEASE_SECONDS
                            ),
                            "activated": was_activated,
                            "activation_confirmed": was_activation_confirmed,
                        }
                async with self._lease_lock:
                    current_lease = (
                        None if self._lease is None else dict(self._lease)
                    )
                if current_lease is None:
                    raise RuntimeError("merged reply lease disappeared")
                if not bool(current_lease.get("activated")):
                    recovered = await self._recover_memory_bindings(
                        call_action,
                        current_lease,
                    )
                    if recovered:
                        async with self._lease_lock:
                            if (
                                self._lease is not None
                                and self._lease.get("lease_id")
                                == current_lease.get("lease_id")
                            ):
                                self._lease["activated"] = True
                                self._lease["activation_confirmed"] = False
                        current_lease["activated"] = True
                        current_lease["activation_confirmed"] = False
                        self._recovered_generation = int(
                            current_lease["bridge_generation"]
                        )
                    else:
                        await self._pause_lease_supervisor(1.0)
                        continue
                if not bool(current_lease.get("activation_confirmed")):
                    activation = _response_data(
                        await asyncio.wait_for(
                            call_action(
                                ACTIVATE_ACTION,
                                plugin_instance_id=self.plugin_instance_id,
                                lease_id=str(current_lease["lease_id"]),
                                bridge_generation=int(
                                    current_lease["bridge_generation"]
                                ),
                            ),
                            timeout=ACTION_TIMEOUT_SECONDS,
                        )
                    )
                    if activation.get("ready") is not True:
                        raise RuntimeError("merged reply activation failed")
                    async with self._lease_lock:
                        if (
                            self._lease is not None
                            and self._lease.get("lease_id")
                            == current_lease.get("lease_id")
                        ):
                            self._lease["activation_confirmed"] = True
                await self._pause_lease_supervisor(LEASE_RENEW_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lease_lock:
                    if (
                        self._lease is None
                        or self._lease.get("activated") is not True
                        or float(self._lease.get("expires_at") or 0.0)
                        <= time.monotonic()
                    ):
                        self._lease = None
                logger.warning(
                    "Akasha 合并回复 readiness 未就绪：%s",
                    type(exc).__name__,
                )
                await self._pause_lease_supervisor(1.0)

    async def _lease_snapshot(self) -> dict[str, Any] | None:
        async with self._lease_lock:
            if (
                self._lease is None
                or float(self._lease.get("expires_at") or 0.0) <= time.monotonic()
                or self._lease.get("activated") is not True
                or self._lease.get("activation_confirmed") is not True
            ):
                return None
            return dict(self._lease)

    async def _recover_memory_bindings(
        self,
        call_action: Any,
        lease: dict[str, Any],
    ) -> bool:
        records_callback = getattr(
            self.context,
            "akasha_memory_recovery_records",
            None,
        )
        acceptance_callback = getattr(
            self.context,
            "akasha_memory_recovery_acceptance",
            None,
        )
        terminal_callback = getattr(
            self.context,
            "akasha_memory_recovery_terminal",
            None,
        )
        if not all(
            callable(callback)
            for callback in (
                records_callback,
                acceptance_callback,
                terminal_callback,
            )
        ):
            return False
        for _page in range(32):
            try:
                records = await records_callback()
            except Exception:
                logger.error("Akasha 联系人记忆恢复清单读取失败。")
                return False
            if not isinstance(records, list):
                return False
            if not records:
                return True
            all_resolved = True
            for record in records[:512]:
                if not isinstance(record, dict):
                    all_resolved = False
                    continue
                request_id = str(record.get("request_id") or "")
                token = str(record.get("admission_token") or "")
                if not request_id or not token:
                    all_resolved = False
                    continue
                try:
                    response = await asyncio.wait_for(
                        call_action(
                            GET_ADMISSION_ACTION,
                            plugin_instance_id=self.plugin_instance_id,
                            lease_id=str(lease["lease_id"]),
                            bridge_generation=int(lease["bridge_generation"]),
                            admission_token=token,
                        ),
                        timeout=ACTION_TIMEOUT_SECONDS,
                    )
                    admission = _response_data(response)
                    state = str(admission.get("state") or "")
                    stored_request_id = str(admission.get("request_id") or "")
                    if state == "consumed":
                        if stored_request_id != request_id:
                            all_resolved = False
                            continue
                        if await acceptance_callback(request_id) is not True:
                            all_resolved = False
                    elif state in {
                        "released",
                        "terminal",
                        "superseded_before_action",
                    }:
                        stored_outcome = str(admission.get("outcome") or "")
                        outcome = (
                            "superseded"
                            if state == "superseded_before_action"
                            or stored_outcome == "superseded"
                            else "failed"
                        )
                        reason = str(
                            admission.get("reason")
                            or "E_ADMISSION_RECOVERED_TERMINAL"
                        )
                        if (
                            await terminal_callback(
                                request_id,
                                outcome,
                                reason,
                            )
                            is not True
                        ):
                            all_resolved = False
                    else:
                        all_resolved = False
                except Exception:
                    all_resolved = False
            if len(records) > 512 or not all_resolved:
                return False
            if len(records) < 128:
                return True
            await asyncio.sleep(0)
        return False

    def _reply_context(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        if event.get_platform_name() != "aiocqhttp" or not event.is_private_chat():
            return None
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if (
            not isinstance(raw, dict)
            or raw.get("akasha_reply_schema") != 1
            or str(raw.get("type") or "") != "private"
        ):
            return None
        target_id = _positive_int(event.get_sender_id())
        generation = _positive_int(raw.get("bridge_generation"))
        reply_epoch = _positive_int(raw.get("reply_epoch"))
        plugin_instance_id = str(raw.get("plugin_instance_id") or "")
        admission_token = str(raw.get("admission_token") or "")
        if (
            target_id is None
            or generation is None
            or reply_epoch is None
            or _positive_int(raw.get("user_id")) != target_id
            or plugin_instance_id != self.plugin_instance_id
            or len(admission_token) < 20
        ):
            return None
        return {
            "target_id": target_id,
            "bridge_generation": generation,
            "reply_epoch": reply_epoch,
            "plugin_instance_id": plugin_instance_id,
            "admission_token": admission_token,
        }

    @_event_message_type(
        _PRIVATE_MESSAGE,
        priority=40_000,
    )
    async def consume_send_result(self, event: AstrMessageEvent) -> None:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if (
            not isinstance(raw, dict)
            or raw.get("notice_type") != "akasha_send_result"
            or raw.get("akasha_send_result_schema") != 2
        ):
            return
        try:
            status = str(event.get_extra(MEMORY_RESULT_STATUS_KEY, "") or "")
            if status not in {"applied", "not_applicable"}:
                logger.error(
                    "Akasha 合并回复结果未持久应用，保留 Bridge outbox：status=%s",
                    status or "missing",
                )
                return
            lease = await self._lease_snapshot()
            bot = getattr(event, "bot", None) or self._managed_bot()
            call_action = getattr(bot, "call_action", None)
            if lease is None or not callable(call_action):
                return
            response = await asyncio.wait_for(
                call_action(
                    ACK_ACTION,
                    plugin_instance_id=self.plugin_instance_id,
                    lease_id=str(lease["lease_id"]),
                    bridge_generation=int(lease["bridge_generation"]),
                    notice_id=str(raw.get("notice_id") or ""),
                    result_digest=str(raw.get("result_digest") or ""),
                ),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
            if _response_data(response).get("acked") is not True:
                logger.error("Akasha 合并回复结果 ACK 未确认，将等待重放。")
        except Exception as exc:
            logger.warning(
                "Akasha 合并回复结果 ACK 失败，将等待重放：%s",
                type(exc).__name__,
            )
        finally:
            event.stop_event()

    @_event_message_type(
        _PRIVATE_MESSAGE,
        priority=5_000,
    )
    async def track_admission(self, event: AstrMessageEvent) -> None:
        context = self._reply_context(event)
        if context is None:
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if origin and self._registry_ready():
            active_event_registry.request_agent_stop_all(
                origin,
                exclude=event,
            )
        token = str(context["admission_token"])
        event.set_extra(TRACKER_KEY, context)
        previous = self._tracker_tasks.pop(token, None)
        if previous is not None:
            previous.cancel()
        self._tracker_tasks[token] = asyncio.create_task(
            self._admission_deadline(event, context)
        )

    async def _admission_deadline(
        self,
        event: AstrMessageEvent,
        context: dict[str, Any],
    ) -> None:
        try:
            deadline = time.monotonic() + 10 * 60
            while not self._terminated:
                events = getattr(active_event_registry, "_events", {})
                origin = str(getattr(event, "unified_msg_origin", "") or "")
                active = bool(origin and event in events.get(origin, set()))
                if not active:
                    if await self._finalize_unconsumed(
                        event,
                        context,
                        outcome="failed",
                        reason="E_PROVIDER_NO_RESULT",
                    ):
                        return
                    await asyncio.sleep(1.0)
                    continue
                if time.monotonic() >= deadline:
                    event.set_extra("agent_stop_requested", True)
                    if await self._finalize_unconsumed(
                        event,
                        context,
                        outcome="failed",
                        reason="E_PROVIDER_TIMEOUT",
                    ):
                        return
                    await asyncio.sleep(1.0)
                    continue
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    async def _finalize_unconsumed(
        self,
        event: AstrMessageEvent,
        context: dict[str, Any] | None,
        *,
        outcome: str,
        reason: str,
    ) -> bool:
        if context is None:
            return True
        token = str(context["admission_token"])
        lease = await self._lease_snapshot()
        bot = getattr(event, "bot", None) or self._managed_bot()
        call_action = getattr(bot, "call_action", None)
        if lease is None or not callable(call_action):
            return False
        request_id = str(event.get_extra(REQUEST_ID_KEY, "") or "")
        state = ""
        try:
            status_response = await asyncio.wait_for(
                call_action(
                    GET_ADMISSION_ACTION,
                    plugin_instance_id=self.plugin_instance_id,
                    lease_id=str(lease["lease_id"]),
                    bridge_generation=int(lease["bridge_generation"]),
                    admission_token=token,
                ),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
            admission = _response_data(status_response)
            state = str(admission.get("state") or "")
            if state == "consumed":
                self._complete_lifecycle_tasks(token, request_id)
                return True
            if state == "superseded_before_action":
                outcome = "superseded"
                reason = str(admission.get("reason") or "E_REPLY_SUPERSEDED")
            elif state in {"released", "terminal"}:
                stored_outcome = str(admission.get("outcome") or "")
                if stored_outcome in {"failed", "superseded"}:
                    outcome = stored_outcome
                reason = str(admission.get("reason") or reason)
                if request_id:
                    if not await self._record_pre_action_terminal(
                        event,
                        outcome=outcome,
                        reason=reason,
                    ):
                        return False
                self._complete_lifecycle_tasks(token, request_id)
                return True
        except Exception:
            logger.warning("Akasha admission 状态暂不可查，将执行幂等终结。")
        action = FINISH_ACTION if request_id else RELEASE_ACTION
        params: dict[str, Any] = {
            "plugin_instance_id": self.plugin_instance_id,
            "lease_id": str(lease["lease_id"]),
            "bridge_generation": int(lease["bridge_generation"]),
            "admission_token": token,
            "reason": reason,
        }
        if request_id:
            params.update({"request_id": request_id, "outcome": outcome})
        try:
            response = await asyncio.wait_for(
                call_action(action, **params),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
            result = _response_data(response)
            state = str(result.get("state") or "")
            if state == "consumed":
                self._complete_lifecycle_tasks(token, request_id)
                return True
            if state not in {
                "released",
                "terminal",
                "superseded_before_action",
            }:
                return False
            stored_outcome = str(result.get("outcome") or "")
            if state == "superseded_before_action":
                outcome = "superseded"
            elif stored_outcome in {"failed", "superseded"}:
                outcome = stored_outcome
            reason = str(result.get("reason") or reason)
            if request_id:
                if not await self._record_pre_action_terminal(
                    event,
                    outcome=outcome,
                    reason=reason,
                ):
                    return False
            self._complete_lifecycle_tasks(token, request_id)
            return True
        except Exception:
            logger.warning("Akasha admission 终结暂未确认，将由 generation 恢复清理。")
            return False

    @filter.on_llm_request(priority=-30_000)
    async def prepare_merged_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.enabled or self._reply_context(event) is None:
            return
        request_id = str(event.get_extra(REQUEST_ID_KEY, "") or "")
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            request_id = str(uuid.uuid4())
            event.set_extra(REQUEST_ID_KEY, request_id)
        system_prompt = str(req.system_prompt or "")
        if PROMPT_MARKER not in system_prompt:
            req.system_prompt = (
                f"{system_prompt.rstrip()}\n\n{PROMPT}"
                if system_prompt.strip()
                else PROMPT
            )

    @filter.on_decorating_result(priority=-20_000)
    async def claim_and_normalize(self, event: AstrMessageEvent) -> None:
        context = self._reply_context(event)
        if not self.enabled or context is None:
            return
        try:
            result = event.get_result()
            if result is None or not result.is_llm_result() or not result.chain:
                await self._finalize_unconsumed(
                    event,
                    context,
                    outcome="failed",
                    reason="E_PROVIDER_NO_RESULT",
                )
                return
            if not all(isinstance(component, Plain) for component in result.chain):
                await self._finalize_unconsumed(
                    event,
                    context,
                    outcome="released",
                    reason="E_RESULT_NOT_PURE_TEXT",
                )
                return
            # Claim before touching component.text so malformed Plain objects
            # still fail closed in the final guard.
            event.set_extra(CLAIMED_KEY, True)
            request_id = str(event.get_extra(REQUEST_ID_KEY, "") or "")
            try:
                request_id = str(uuid.UUID(request_id))
            except (ValueError, AttributeError):
                request_id = str(uuid.uuid4())
                event.set_extra(REQUEST_ID_KEY, request_id)
            raw_text = "".join(
                component.text
                for component in result.chain
                if isinstance(component.text, str)
            )
            event.set_extra(NORMALIZED_TEXT_KEY, validate_reply_text(raw_text))
            event.set_extra(NORMALIZE_ERROR_KEY, "")
        except Exception as exc:
            code = str(exc) if str(exc).startswith("E_") else "E_MERGED_REPLY_NORMALIZE"
            event.set_extra(NORMALIZE_ERROR_KEY, code)
            logger.warning("Akasha 合并回复规范化失败：%s", code)

    def _eligible_plain_result(self, event: AstrMessageEvent) -> bool:
        if self._reply_context(event) is None:
            return False
        try:
            result = event.get_result()
            return bool(
                result is not None
                and result.is_llm_result()
                and result.chain
                and all(isinstance(component, Plain) for component in result.chain)
            )
        except Exception:
            return True

    async def _record_pre_action_terminal(
        self,
        event: AstrMessageEvent,
        *,
        outcome: str,
        reason: str,
    ) -> bool:
        callback = getattr(self.context, "akasha_memory_pre_action_terminal", None)
        if callable(callback):
            try:
                return await callback(event, outcome, reason) is True
            except Exception:
                logger.error("Akasha 联系人记忆 pre-action 终态落盘失败。")
        return False

    async def _mark_acceptance(
        self,
        event: AstrMessageEvent,
        status: str,
    ) -> None:
        callback = getattr(self.context, "akasha_memory_acceptance", None)
        if not callable(callback):
            return
        try:
            if await callback(event, status) is not True:
                logger.error("Akasha 联系人记忆受理状态未持久更新。")
        except Exception:
            logger.error("Akasha 联系人记忆受理状态更新失败。")

    def _cancel_tracker(self, admission_token: str) -> None:
        tracker = self._tracker_tasks.pop(admission_token, None)
        if tracker is not None and tracker is not asyncio.current_task():
            tracker.cancel()

    def _complete_lifecycle_tasks(
        self,
        admission_token: str,
        request_id: str,
    ) -> None:
        self._cancel_tracker(admission_token)
        acceptance_task = self._acceptance_tasks.pop(request_id, None)
        if (
            acceptance_task is not None
            and acceptance_task is not asyncio.current_task()
        ):
            acceptance_task.cancel()

    async def _retry_unknown_acceptance(
        self,
        event: AstrMessageEvent,
        context: dict[str, Any],
        request_id: str,
        text: str,
    ) -> None:
        """Resolve a lost action response without ever creating a new request."""

        try:
            while not self._terminated:
                await asyncio.sleep(30.0)
                lease = await self._lease_snapshot()
                if lease is None:
                    continue
                if int(lease["bridge_generation"]) != int(
                    context["bridge_generation"]
                ):
                    await self._record_pre_action_terminal(
                        event,
                        outcome="superseded",
                        reason="E_BRIDGE_GENERATION_CHANGED",
                    )
                    self._cancel_tracker(str(context["admission_token"]))
                    return
                bot = getattr(event, "bot", None) or self._managed_bot()
                call_action = getattr(bot, "call_action", None)
                if not callable(call_action):
                    continue
                try:
                    response = await asyncio.wait_for(
                        call_action(
                            ACTION_NAME,
                            user_id=int(context["target_id"]),
                            bridge_generation=int(context["bridge_generation"]),
                            reply_epoch=int(context["reply_epoch"]),
                            request_id=request_id,
                            text=text,
                            plugin_instance_id=self.plugin_instance_id,
                            lease_id=str(lease["lease_id"]),
                            admission_token=str(context["admission_token"]),
                        ),
                        timeout=ACTION_TIMEOUT_SECONDS,
                    )
                except Exception:
                    continue
                data = _response_data(response)
                if data.get("error_code") == LEASE_INVALID_ERROR:
                    await self._discard_lease_snapshot(lease)
                    continue
                if not data:
                    continue
                if data.get("pre_action_terminal") is True:
                    await self._record_pre_action_terminal(
                        event,
                        outcome=str(data.get("outcome") or "failed"),
                        reason=str(
                            data.get("reason") or "E_PRE_ACTION_TERMINAL"
                        ),
                    )
                elif data.get("accepted") is True:
                    await self._mark_acceptance(event, "accepted")
                else:
                    await self._record_pre_action_terminal(
                        event,
                        outcome="failed",
                        reason=str(
                            data.get("error_code") or "E_ACCEPTANCE_REJECTED"
                        ),
                    )
                self._cancel_tracker(str(context["admission_token"]))
                return
        except asyncio.CancelledError:
            return
        finally:
            if self._acceptance_tasks.get(request_id) is asyncio.current_task():
                self._acceptance_tasks.pop(request_id, None)

    @filter.on_decorating_result(priority=-30_000)
    async def send_merged_reply(self, event: AstrMessageEvent) -> None:
        claimed = bool(event.get_extra(CLAIMED_KEY, False))
        if not claimed and self._eligible_plain_result(event):
            # Independent final guard: even an exception before the normalizer
            # set its marker may not fall through to segmented/default send.
            claimed = True
            event.set_extra(CLAIMED_KEY, True)
            event.set_extra(
                NORMALIZE_ERROR_KEY,
                "E_MERGED_REPLY_NORMALIZE_GUARD",
            )
        if not claimed:
            return
        context = self._reply_context(event)
        try:
            if context is None:
                return
            request_id = str(event.get_extra(REQUEST_ID_KEY, "") or "")
            text = str(event.get_extra(NORMALIZED_TEXT_KEY, "") or "")
            normalize_error = str(event.get_extra(NORMALIZE_ERROR_KEY, "") or "")
            memory_status = str(event.get_extra(MEMORY_BIND_STATUS_KEY, "") or "")
            if normalize_error or not text:
                reason = normalize_error or "E_MERGED_REPLY_EMPTY"
                await self._record_pre_action_terminal(
                    event,
                    outcome="failed",
                    reason=reason,
                )
                await self._finalize_unconsumed(
                    event,
                    context,
                    outcome="failed",
                    reason=reason,
                )
                return
            if memory_status not in {"bound", "not_applicable"}:
                await self._record_pre_action_terminal(
                    event,
                    outcome="failed",
                    reason="E_MEMORY_BIND_REQUIRED",
                )
                await self._finalize_unconsumed(
                    event,
                    context,
                    outcome="failed",
                    reason="E_MEMORY_BIND_REQUIRED",
                )
                return

            bot = getattr(event, "bot", None) or self._managed_bot()
            call_action = getattr(bot, "call_action", None)
            if not callable(call_action):
                raise RuntimeError("OneBot action channel unavailable")
            last_error: BaseException | None = None
            data: dict[str, Any] = {}
            for _attempt in range(3):
                lease = await self._lease_snapshot()
                if lease is None:
                    await asyncio.sleep(0.2)
                    continue
                try:
                    response = await asyncio.wait_for(
                        call_action(
                            ACTION_NAME,
                            user_id=int(context["target_id"]),
                            bridge_generation=int(context["bridge_generation"]),
                            reply_epoch=int(context["reply_epoch"]),
                            request_id=request_id,
                            text=text,
                            plugin_instance_id=self.plugin_instance_id,
                            lease_id=str(lease["lease_id"]),
                            admission_token=str(context["admission_token"]),
                        ),
                        timeout=ACTION_TIMEOUT_SECONDS,
                    )
                    data = _response_data(response)
                    if data.get("error_code") == LEASE_INVALID_ERROR:
                        last_error = RuntimeError("merged reply lease rejected")
                        await self._discard_lease_snapshot(lease)
                        data = {}
                        await asyncio.sleep(0.2)
                        continue
                    if data:
                        break
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(0.2)
            if not data:
                event.set_extra("akasha_acceptance_unknown", True)
                await self._mark_acceptance(event, "acceptance_unknown")
                previous = self._acceptance_tasks.pop(request_id, None)
                if previous is not None:
                    previous.cancel()
                self._acceptance_tasks[request_id] = asyncio.create_task(
                    self._retry_unknown_acceptance(
                        event,
                        dict(context),
                        request_id,
                        text,
                    )
                )
                logger.warning(
                    "Akasha 合并回复受理结果未知；保留同一 request/admission，禁止默认发送：%s",
                    type(last_error).__name__ if last_error else "lease_unavailable",
                )
                return
            if data.get("pre_action_terminal") is True:
                outcome = str(data.get("outcome") or "failed")
                reason = str(data.get("reason") or "E_PRE_ACTION_TERMINAL")
                await self._record_pre_action_terminal(
                    event,
                    outcome=outcome,
                    reason=reason,
                )
            elif data.get("accepted") is not True:
                await self._record_pre_action_terminal(
                    event,
                    outcome="failed",
                    reason=str(data.get("error_code") or "E_ACCEPTANCE_REJECTED"),
                )
            else:
                await self._mark_acceptance(event, "accepted")
            self._cancel_tracker(str(context["admission_token"]))
            logger.info(
                "Akasha 合并回复已由 Bridge 持久受理：outcome=%s epoch=%s",
                str(data.get("outcome") or "accepted"),
                int(context["reply_epoch"]),
            )
        except Exception as exc:
            logger.warning(
                "Akasha 合并回复 action 异常：%s；已阻止默认重复发送。",
                type(exc).__name__,
            )
        finally:
            try:
                event.clear_result()
            finally:
                event.stop_event()

    @_on_agent_done()
    async def finalize_agent(
        self,
        event: AstrMessageEvent,
        _run_context: Any,
        _response: Any,
    ) -> None:
        # AstrBot invokes this hook before on_decorating_result, so CLAIMED_KEY
        # cannot distinguish a valid model result here.  Later result hooks and
        # the admission tracker own every terminal path without ending a normal
        # reply before it can be claimed.
        return

    @_after_message_sent()
    async def finalize_after_default_send(self, event: AstrMessageEvent) -> None:
        if event.get_extra(CLAIMED_KEY, False):
            return
        await self._finalize_unconsumed(
            event,
            self._reply_context(event),
            outcome="released",
            reason="E_RESULT_NOT_CLAIMED",
        )

    async def terminate(self) -> None:
        self._terminated = True
        if self._lease_task is not None:
            self._lease_task.cancel()
        for task in tuple(self._tracker_tasks.values()):
            task.cancel()
        self._tracker_tasks.clear()
        for task in tuple(self._acceptance_tasks.values()):
            task.cancel()
        self._acceptance_tasks.clear()
        async with self._lease_lock:
            self._lease = None
