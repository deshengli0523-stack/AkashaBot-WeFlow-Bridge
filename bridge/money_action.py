"""Incoming red-packet and transfer transaction primitives."""

from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


RED_PACKET_LOCAL_TYPE = "8594229559345"
TRANSFER_LOCAL_TYPE = "8589934592049"
_CORRELATION_TAGS = (
    "paymsgid",
    "transferid",
    "transcationid",
    "sendid",
    "channelid",
)
_TAG_PATTERNS = {
    name: re.compile(
        rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>",
        re.IGNORECASE | re.DOTALL,
    )
    for name in (
        *_CORRELATION_TAGS,
        "paysubtype",
        "type",
        "receiver_username",
        "feedesc",
    )
}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _raw_content(row: Mapping[str, object]) -> str:
    values = []
    for key in (
        "rawContent",
        "raw_content",
        "content",
        "parsedContent",
        "parsed_content",
        "message",
    ):
        value = row.get(key)
        if isinstance(value, str) and value and value not in values:
            decoded = value
            for _ in range(2):
                candidate = html.unescape(decoded)
                if candidate == decoded:
                    break
                decoded = candidate
            values.append(decoded)
    return "\n".join(values)


def _xml_tag(raw: str, name: str) -> str:
    match = _TAG_PATTERNS[name].search(raw)
    if not match:
        return ""
    value = html.unescape(match.group(1)).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3].strip()
    return value


def _sent_by_self(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes"}


def detect_money_marker(data: Mapping[str, object]) -> str | None:
    marker = _text(data.get("content"))
    if marker == "[红包]":
        return "red_packet"
    if marker == "[转账]":
        return "transfer"
    return None


@dataclass(frozen=True)
class MoneyCandidate:
    kind: str
    session_id: str
    source_server_id: str
    source_timestamp: float
    correlation_ids: dict[str, str] = field(default_factory=dict)
    amount_cny: str = ""

    @property
    def source_ref(self) -> str:
        return self.source_server_id


def select_money_candidate(
    sse: Mapping[str, object],
    messages: Sequence[Mapping[str, object]],
    *,
    account_id: str = "",
) -> MoneyCandidate | None:
    """Select only the exact incoming REST row represented by one SSE marker."""

    marker_kind = detect_money_marker(sse)
    raw_id = _text(sse.get("rawid"))
    session_id = _text(sse.get("sessionId"))
    if marker_kind is None or not raw_id or not session_id:
        return None
    matching_rows = [
        item
        for item in messages
        if raw_id
        in {
            _text(item.get("serverId")),
            _text(item.get("localId")),
        }
    ]
    if len(matching_rows) != 1:
        return None
    row = matching_rows[0]
    source_server_id = _text(row.get("serverId"))
    if not source_server_id or _sent_by_self(row.get("isSend")):
        return None

    raw = _raw_content(row)
    local_type = _text(row.get("localType"))
    app_type = _xml_tag(raw, "type")
    if marker_kind == "transfer":
        if local_type != TRANSFER_LOCAL_TYPE and app_type != "2000":
            return None
        if _xml_tag(raw, "paysubtype") != "1":
            return None
        is_group = (
            _text(sse.get("sessionType")) == "group"
            or "@chatroom" in session_id
        )
        receiver = _xml_tag(raw, "receiver_username")
        if is_group and (
            not _text(account_id)
            or not receiver
            or receiver != _text(account_id)
        ):
            return None
    else:
        if local_type != RED_PACKET_LOCAL_TYPE and app_type != "2001":
            return None

    timestamp_value = (
        row.get("createTime")
        if row.get("createTime") is not None
        else sse.get("timestamp")
    )
    try:
        source_timestamp = float(timestamp_value or 0)
    except (TypeError, ValueError):
        source_timestamp = 0.0
    correlations = {
        name: value
        for name in _CORRELATION_TAGS
        if (value := _xml_tag(raw, name))
    }
    if marker_kind == "transfer" and not all(
        correlations.get(name)
        for name in ("transferid", "transcationid")
    ):
        return None
    amount_cny = ""
    if marker_kind == "transfer":
        fee_description = _xml_tag(raw, "feedesc")
        amount_match = re.fullmatch(
            r"\s*[￥¥]\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?\s*",
            fee_description.replace(",", ""),
        )
        if amount_match:
            try:
                amount = Decimal(amount_match.group(1))
                if amount >= 0:
                    amount_cny = format(amount, "f")
            except InvalidOperation:
                pass
    return MoneyCandidate(
        kind=marker_kind,
        session_id=session_id,
        source_server_id=source_server_id,
        source_timestamp=source_timestamp,
        correlation_ids=correlations,
        amount_cny=amount_cny,
    )


def _timestamp(row: Mapping[str, object]) -> float:
    value = (
        row.get("createTime")
        if row.get("createTime") is not None
        else row.get("timestamp")
    )
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def money_receipt_matches(
    candidate: MoneyCandidate,
    messages: Sequence[Mapping[str, object]],
) -> bool:
    """Return true only for a post-candidate WeFlow acceptance record."""

    for row in messages:
        if _text(row.get("serverId")) == candidate.source_server_id:
            continue
        row_timestamp = _timestamp(row)
        if (
            candidate.source_timestamp
            and row_timestamp
            and row_timestamp < candidate.source_timestamp
        ):
            continue
        raw = _raw_content(row)
        if candidate.kind == "red_packet":
            if _text(row.get("localType")) != "10000":
                continue
            without_cdata = re.sub(
                r"<!\[CDATA\[(.*?)\]\]>",
                r"\1",
                raw,
                flags=re.DOTALL,
            )
            without_markup = re.sub(r"<[^>]+>", "", without_cdata)
            normalized = re.sub(r"\s+", "", without_markup)
            if "你领取了" in normalized and "红包" in normalized:
                return True
            continue

        if _xml_tag(raw, "paysubtype") != "3":
            continue
        row_ids = {
            name: value
            for name in _CORRELATION_TAGS
            if (value := _xml_tag(raw, name))
        }
        stable_ids = {
            name: candidate.correlation_ids.get(name, "")
            for name in ("transferid", "transcationid")
        }
        if all(stable_ids.values()) and all(
            row_ids.get(name) == value
            for name, value in stable_ids.items()
        ):
            return True
    return False


class ReceiveTransaction:
    """Thread-safe dual-signal completion state for one receive action."""

    def __init__(
        self,
        *,
        request_id: str,
        generation: int,
        source_ref: str,
        deadline: float,
    ) -> None:
        self.request_id = str(request_id)
        self.generation = int(generation)
        self.source_ref = str(source_ref)
        self.deadline = float(deadline)
        self.visual_success = False
        self.weflow_success = False
        self.status = "active"
        self.failure_reason = ""
        self.cancel_event = threading.Event()
        self._condition = threading.Condition()

    @property
    def completed(self) -> bool:
        with self._condition:
            return self.status == "completed"

    @property
    def terminal(self) -> bool:
        with self._condition:
            return self.status != "active"

    def _matches(self, request_id: str, generation: int) -> bool:
        return (
            self.status == "active"
            and str(request_id) == self.request_id
            and int(generation) == self.generation
        )

    def _complete_if_ready(self) -> None:
        if self.visual_success and self.weflow_success:
            self.status = "completed"

    def mark_visual_success(
        self,
        *,
        request_id: str,
        generation: int,
    ) -> bool:
        with self._condition:
            if not self._matches(request_id, generation):
                return False
            self.visual_success = True
            self._complete_if_ready()
            self._condition.notify_all()
            return True

    def mark_weflow_success(
        self,
        *,
        request_id: str,
        generation: int,
        source_ref: str,
    ) -> bool:
        with self._condition:
            if (
                not self._matches(request_id, generation)
                or str(source_ref) != self.source_ref
            ):
                return False
            self.weflow_success = True
            self._complete_if_ready()
            self._condition.notify_all()
            return True

    def fail(self, reason: str) -> bool:
        with self._condition:
            if self.status != "active":
                return False
            self.status = "failed"
            self.failure_reason = str(reason)
            self.cancel_event.set()
            self._condition.notify_all()
            return True

    def cancel(self, reason: str = "cancelled") -> bool:
        return self.fail(reason)

    def expire(self) -> bool:
        with self._condition:
            if self.status != "active":
                return False
            self.status = "timed_out"
            self.failure_reason = "timeout"
            self.cancel_event.set()
            self._condition.notify_all()
            return True

    def wait(self, poll_seconds: float = 0.1) -> str:
        with self._condition:
            while self.status == "active":
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    self.status = "timed_out"
                    self.failure_reason = "timeout"
                    self.cancel_event.set()
                    break
                self._condition.wait(min(remaining, max(0.01, poll_seconds)))
            return self.status

    def public_status(self) -> dict[str, object]:
        with self._condition:
            return {
                "request_id": self.request_id,
                "status": self.status,
                "visual_success": self.visual_success,
                "weflow_success": self.weflow_success,
            }
