from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.api.provider import Provider
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.register import (
    provider_cls_map,
    register_provider_adapter,
)

from .runtime import ContactMemoryRuntime

PROVIDER_TYPE = "akasha_qwen_responses"
PROVIDER_ID = "akasha-qwen-contact-memory"
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _register_once(cls):
    if PROVIDER_TYPE in provider_cls_map:
        return cls
    return register_provider_adapter(
        PROVIDER_TYPE,
        "Akasha per-contact Qwen Responses memory provider",
    )(cls)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            return {}
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _content_details(content: Any) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, True
    if content is None:
        return "", True
    if not isinstance(content, list):
        return "", False
    parts: list[str] = []
    supported = True
    for part in content:
        item = _mapping(part)
        if not item:
            supported = False
            continue
        if item.get("type") in {"text", "input_text", "output_text"}:
            value = item.get("text")
            if isinstance(value, str):
                parts.append(value)
        elif item.get("type") == "think":
            continue
        else:
            supported = False
    return "".join(parts), supported


def _current_prompt(prompt: str | None, contexts: Any) -> tuple[str, bool]:
    if isinstance(prompt, str) and prompt.strip():
        return prompt, True
    if isinstance(contexts, list):
        for item in reversed(contexts):
            message = _mapping(item)
            if message.get("role") == "user":
                return _content_details(message.get("content"))
    return "", True


def _system_prompt(explicit: str | None, contexts: Any) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if not isinstance(contexts, list):
        return ""
    parts: list[str] = []
    for item in contexts:
        message = _mapping(item)
        if message.get("role") != "system":
            continue
        text, _ = _content_details(message.get("content"))
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _responses_tools(func_tool: Any) -> tuple[list[dict[str, Any]], bool]:
    if func_tool is None:
        return [], True
    schema = getattr(func_tool, "openai_schema", None)
    if not callable(schema):
        return [], False
    try:
        raw_tools = schema()
    except Exception:
        return [], False
    if not isinstance(raw_tools, list):
        return [], False
    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            return [], False
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            return [], False
        name = str(function.get("name") or "").strip()
        if (
            not name
            or len(name) > 64
            or _TOOL_NAME_RE.fullmatch(name) is None
        ):
            return [], False
        tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "description": str(function.get("description") or ""),
        }
        parameters = function.get("parameters")
        tool["parameters"] = (
            parameters
            if isinstance(parameters, dict)
            else {"type": "object", "properties": {}}
        )
        tools.append(tool)
    return tools, True


def _tool_fingerprint(tools: list[dict[str, Any]]) -> str:
    return json.dumps(
        tools,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_call_id(value: Any) -> str:
    item = _mapping(value)
    return str(item.get("id") or item.get("call_id") or "").strip()


def _tool_result_inputs(contexts: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(contexts, list):
        return [], True
    messages = [_mapping(item) for item in contexts]
    last_user = max(
        (index for index, item in enumerate(messages) if item.get("role") == "user"),
        default=-1,
    )
    assistant_index = -1
    expected: set[str] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            continue
        assistant_index = index
        expected = {
            call_id for call_id in (_tool_call_id(call) for call in raw_calls) if call_id
        }
        break
    if assistant_index < 0 or assistant_index < last_user:
        return [], True
    if not expected:
        return [], False
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages[assistant_index + 1 :]:
        if message.get("role") != "tool":
            return [], False
        call_id = str(message.get("tool_call_id") or "").strip()
        if call_id not in expected or call_id in seen:
            return [], False
        output, supported = _content_details(message.get("content"))
        if not supported:
            return [], False
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            }
        )
        seen.add(call_id)
    if seen != expected:
        return [], False
    return outputs, True


def _current_turn_contexts(contexts: Any) -> list[dict[str, Any]]:
    if not isinstance(contexts, list):
        return []
    messages = [_mapping(item) for item in contexts]
    if not messages:
        return []
    last_assistant = max(
        (
            index
            for index, item in enumerate(messages)
            if item.get("role") == "assistant"
        ),
        default=-1,
    )
    start = -1
    if last_assistant >= 0 and isinstance(
        messages[last_assistant].get("tool_calls"),
        list,
    ):
        start = max(
            (
                index
                for index in range(last_assistant)
                if messages[index].get("role") == "user"
            ),
            default=-1,
        )
    if start < 0:
        start = max(
            (
                index
                for index, item in enumerate(messages)
                if item.get("role") == "user"
            ),
            default=-1,
        )
    if start < 0:
        return []
    return [
        dict(message)
        for message in messages[start:]
        if message and message.get("role") != "system"
    ]


def _extra_text(parts: Any) -> tuple[str, bool]:
    if not parts:
        return "", True
    output: list[str] = []
    for part in parts:
        value = _mapping(part)
        if hasattr(part, "model_dump_for_context"):
            try:
                value = part.model_dump_for_context()
            except Exception:
                return "", False
        if not isinstance(value, dict) or value.get("type") not in {
            "text",
            "input_text",
        }:
            return "", False
        text = value.get("text")
        if isinstance(text, str) and text:
            output.append(text)
    return "\n".join(output), True


@_register_once
class AkashaQwenMemoryProvider(Provider):
    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict | None = None,
        *,
        runtime: ContactMemoryRuntime | None = None,
        fallback_provider: Provider | None = None,
    ) -> None:
        super().__init__(provider_config, provider_settings or {})
        self.runtime = runtime
        self.fallback_provider = fallback_provider
        self.set_model(str(provider_config.get("model") or "qwen3.7-max"))

    def get_current_key(self) -> str:
        if not self.fallback_provider:
            return ""
        return self.fallback_provider.get_current_key()

    def get_keys(self) -> list[str]:
        if not self.fallback_provider:
            return [""]
        return self.fallback_provider.get_keys()

    def set_key(self, key: str) -> None:
        if self.fallback_provider:
            self.fallback_provider.set_key(key)

    async def get_models(self) -> list[str]:
        return [self.get_model()]

    async def test(self, timeout: float = 45.0) -> None:
        if not self.runtime or not self.runtime.qwen_sessions:
            raise RuntimeError("Qwen Responses client is not configured")
        conversation_id = await self.runtime.qwen_sessions.client.create_conversation(
            metadata={"purpose": "akasha_provider_test"}
        )
        await self.runtime.qwen_sessions.client.delete_conversation_fully(
            conversation_id
        )

    async def _fallback(
        self,
        *,
        prompt: str | None,
        session_id: str | None,
        image_urls: list[str] | None,
        audio_urls: list[str] | None,
        func_tool: Any,
        contexts: Any,
        system_prompt: str | None,
        tool_calls_result: Any,
        model: str | None,
        extra_user_content_parts: Any,
        tool_choice: str,
        request_max_retries: int | None,
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        if not self.fallback_provider:
            raise RuntimeError("fallback provider is unavailable")
        current, _ = _current_prompt(prompt, contexts)
        fallback_contexts = contexts
        fallback_prompt = prompt
        fallback_system_prompt = _system_prompt(system_prompt, contexts)
        if self.runtime and session_id:
            local_contexts = await self.runtime.fallback_contexts(
                str(session_id),
                current_prompt=current,
            )
            current_turn = (
                [] if prompt is not None else _current_turn_contexts(contexts)
            )
            fallback_contexts = [*local_contexts, *current_turn]
            if current_turn:
                fallback_prompt = None
            fallback_system_prompt = self.runtime.fallback_system_prompt(
                fallback_system_prompt
            )
        response = await self.fallback_provider.text_chat(
            prompt=fallback_prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=fallback_contexts,
            system_prompt=fallback_system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        )
        if (
            self.runtime
            and session_id
            and response.completion_text
            and not response.tools_call_name
        ):
            await self.runtime.archive_fallback_output(
                str(session_id),
                response.completion_text,
                response_id=str(getattr(response, "id", "") or ""),
            )
        return response

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: Any = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        current, current_supported = _current_prompt(prompt, contexts)
        extra_text, extra_supported = _extra_text(extra_user_content_parts)
        if extra_text and prompt is not None:
            current = f"{current}\n\n{extra_text}" if current else extra_text
        live_system_prompt = _system_prompt(system_prompt, contexts)
        tools, tools_supported = _responses_tools(func_tool)
        tool_inputs, continuation_supported = _tool_result_inputs(contexts)
        skills_like_tools = bool(
            tools
            and str(
                self.provider_settings.get("tool_schema_mode", "full")
            ).lower()
            == "skills_like"
        )
        unsupported_required_choice = bool(
            tool_choice == "required" and len(tools) != 1
        )
        bound = bool(
            self.runtime
            and session_id
            and self.runtime.prepared_for(str(session_id))
        )
        requires_fallback = bool(
            not bound
            or (not current and not tool_inputs)
            or tool_calls_result is not None
            or not extra_supported
            or not current_supported
            or not tools_supported
            or not continuation_supported
            or skills_like_tools
            or unsupported_required_choice
        )
        if requires_fallback:
            return await self._fallback(
                prompt=prompt,
                session_id=session_id,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                tool_choice=tool_choice,
                request_max_retries=request_max_retries,
                kwargs=kwargs,
            )

        try:
            result = await self.runtime.respond(  # type: ignore[union-attr]
                str(session_id),
                prompt=current,
                input_items=tool_inputs or None,
                system_prompt=live_system_prompt,
                tool_fingerprint=_tool_fingerprint(tools),
                tools=tools,
                tool_choice=tool_choice,
                request_max_retries=request_max_retries,
            )
        except Exception as exc:
            if (
                self.runtime
                and session_id
                and not getattr(exc, "akasha_session_safe", False)
            ):
                await self.runtime.mark_dirty(str(session_id))
            return await self._fallback(
                prompt=prompt,
                session_id=session_id,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                tool_choice=tool_choice,
                request_max_retries=request_max_retries,
                kwargs=kwargs,
            )

        return LLMResponse(
            role="tool" if result.tool_calls else "assistant",
            completion_text=result.text,
            tools_call_args=[call.arguments for call in result.tool_calls],
            tools_call_name=[call.name for call in result.tool_calls],
            tools_call_ids=[call.call_id for call in result.tool_calls],
            id=result.response_id,
            usage=TokenUsage(
                input_other=max(0, result.input_tokens - result.cached_tokens),
                input_cached=max(0, result.cached_tokens),
                output=max(0, result.output_tokens),
            ),
        )

    async def text_chat_stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMResponse, None]:
        response = await self.text_chat(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        )
        yield response

    async def terminate(self) -> None:
        if self.runtime:
            await self.runtime.close()
