from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from .models import QwenResult, QwenToolCall

_SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class QwenAPIError(RuntimeError):
    def __init__(self, status: int, code: str = "request_failed") -> None:
        self.status = int(status)
        self.code = _SAFE_CODE_RE.sub("_", code)[:80] or "request_failed"
        super().__init__(f"Qwen API request failed ({self.status}, {self.code})")


def _output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
    return "".join(parts).strip()


def _tool_calls(payload: dict[str, Any]) -> tuple[QwenToolCall, ...]:
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ()
    calls: list[QwenToolCall] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not call_id or not name:
            continue
        raw_arguments = item.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str):
            try:
                decoded = json.loads(raw_arguments)
            except (TypeError, ValueError):
                decoded = {}
            arguments = decoded if isinstance(decoded, dict) else {}
        else:
            arguments = {}
        calls.append(
            QwenToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


class QwenClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | Callable[[], str],
        request_timeout: float = 120,
        enable_session_cache: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid Qwen API base URL")
        if parsed.scheme != "https" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Qwen API base URL must use HTTPS")
        self._api_key = api_key
        self.request_timeout = max(10.0, float(request_timeout))
        self.enable_session_cache = bool(enable_session_cache)
        self._session: aiohttp.ClientSession | None = None

    def _current_key(self) -> str:
        key = self._api_key() if callable(self._api_key) else self._api_key
        key = str(key or "").strip()
        if not key:
            raise QwenAPIError(401, "missing_api_key")
        return key

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout, connect=10)
            )
        return self._session

    @staticmethod
    async def _error_code(response: aiohttp.ClientResponse) -> str:
        try:
            payload = await response.json(content_type=None)
        except Exception:
            return "http_error"
        if not isinstance(payload, dict):
            return "http_error"
        error = payload.get("error")
        if isinstance(error, dict):
            value = error.get("code") or error.get("type")
            if value:
                return str(value)
        value = payload.get("code")
        return str(value) if value else "http_error"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ignore_not_found: bool = False,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        client = await self._client()
        headers = {
            "Authorization": f"Bearer {self._current_key()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.enable_session_cache:
            headers["x-dashscope-session-cache"] = "enable"
        attempts = 3 if max_attempts is None else max(
            1,
            min(10, int(max_attempts)),
        )
        for attempt in range(attempts):
            async with client.request(
                method,
                f"{self.base_url}{path}",
                json=body,
                params=params,
                headers=headers,
            ) as response:
                if response.status == 404 and ignore_not_found:
                    return {}
                if 200 <= response.status < 300:
                    if response.status == 204:
                        return {}
                    payload = await response.json(content_type=None)
                    if not isinstance(payload, dict):
                        raise QwenAPIError(response.status, "invalid_json_shape")
                    return payload
                code = await self._error_code(response)
                if response.status not in {408, 409, 429, 500, 502, 503, 504}:
                    raise QwenAPIError(response.status, code)
                if attempt == attempts - 1:
                    raise QwenAPIError(response.status, code)
            await asyncio.sleep(0.5 * (2**attempt))
        raise QwenAPIError(500, "retry_exhausted")

    async def create_conversation(
        self,
        *,
        metadata: dict[str, str] | None = None,
    ) -> str:
        safe_metadata = {
            str(key)[:64]: str(value)[:512]
            for key, value in (metadata or {}).items()
            if str(key) and str(value)
        }
        safe_metadata.setdefault("purpose", "akasha_contact_memory")
        payload = await self._request(
            "POST",
            "/conversations",
            body={"metadata": safe_metadata},
        )
        conversation_id = payload.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise QwenAPIError(200, "missing_conversation_id")
        return conversation_id

    async def add_items(
        self,
        conversation_id: str,
        items: Iterable[dict[str, Any]],
    ) -> list[str]:
        prepared = [
            {
                "type": "message",
                "role": str(item.get("role", "user")),
                "content": str(item.get("content", "")),
            }
            for item in items
            if str(item.get("content", ""))
        ]
        if not prepared:
            return []
        if len(prepared) > 20:
            raise ValueError("Qwen conversation item batch exceeds 20")
        cid = quote(conversation_id, safe="")
        payload = await self._request(
            "POST",
            f"/conversations/{cid}/items",
            body={"items": prepared},
        )
        data = payload.get("data", [])
        if not isinstance(data, list):
            return []
        return [
            str(item["id"])
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    async def respond(
        self,
        *,
        conversation_id: str,
        model: str,
        prompt: str = "",
        input_items: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
    ) -> QwenResult:
        if input_items is not None:
            response_input: str | list[dict[str, Any]] = input_items
        else:
            response_input = prompt
        body: dict[str, Any] = {
            "conversation": conversation_id,
            "model": model,
            "input": response_input,
        }
        if tools:
            body["tools"] = tools
            if tool_choice == "required" and len(tools) != 1:
                raise ValueError(
                    "Qwen Responses requires exactly one tool for tool_choice=required"
                )
            body["tool_choice"] = (
                tool_choice if tool_choice in {"auto", "required", "none"} else "auto"
            )
        payload = await self._request(
            "POST",
            "/responses",
            body=body,
            max_attempts=request_max_retries,
        )
        if payload.get("status") not in {None, "completed"}:
            raise QwenAPIError(200, f"response_{payload.get('status')}")
        response_id = payload.get("id")
        text = _output_text(payload)
        tool_calls = _tool_calls(payload)
        if not isinstance(response_id, str) or not response_id:
            raise QwenAPIError(200, "missing_response_id")
        if not text and not tool_calls:
            raise QwenAPIError(200, "empty_output")
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        details = usage.get("input_tokens_details", {})
        if not isinstance(details, dict):
            details = {}
        return QwenResult(
            response_id=response_id,
            text=text,
            tool_calls=tool_calls,
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw_usage=usage,
        )

    async def list_item_ids(self, conversation_id: str) -> list[str]:
        cid = quote(conversation_id, safe="")
        item_ids: list[str] = []
        after = ""
        while True:
            params: dict[str, Any] = {"limit": 100, "order": "asc"}
            if after:
                params["after"] = after
            payload = await self._request(
                "GET",
                f"/conversations/{cid}/items",
                params=params,
                ignore_not_found=True,
            )
            data = payload.get("data", [])
            if not isinstance(data, list):
                break
            page_ids = [
                str(item["id"])
                for item in data
                if isinstance(item, dict) and item.get("id")
            ]
            item_ids.extend(page_ids)
            if not payload.get("has_more") or not page_ids:
                break
            next_after = str(payload.get("last_id") or page_ids[-1])
            if next_after == after:
                break
            after = next_after
        return item_ids

    async def delete_conversation_fully(self, conversation_id: str) -> None:
        cid = quote(conversation_id, safe="")
        for item_id in await self.list_item_ids(conversation_id):
            await self._request(
                "DELETE",
                f"/conversations/{cid}/items/{quote(item_id, safe='')}",
                ignore_not_found=True,
            )
        await self._request(
            "DELETE",
            f"/conversations/{cid}",
            ignore_not_found=True,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
