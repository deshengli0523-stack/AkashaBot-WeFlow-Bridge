"""Bridge-owned receive coordinator and narrow UI capability surface."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import quote

from money_action import (
    MoneyCandidate,
    ReceiveTransaction,
    detect_money_marker,
    money_receipt_matches,
    select_money_candidate,
)
from uia_support import (
    ScreenRect,
    VK_ESCAPE,
    validate_receive_runtime_metrics,
    validate_runtime_metrics,
)


log = logging.getLogger("ob11-bridge")


def _capture_png_bytes(rect: ScreenRect) -> bytes:
    from PIL import ImageGrab

    buffer = io.BytesIO()
    try:
        image = ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom),
            all_screens=True,
        )
        try:
            image.save(buffer, format="PNG")
        finally:
            image.close()
        return buffer.getvalue()
    finally:
        buffer.close()


class MoneyRequestError(Exception):
    def __init__(self, code: str, http_status: int = 400):
        super().__init__(code)
        self.code = code
        self.http_status = int(http_status)


class WeFlowMoneySource:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        request_get: Callable | None = None,
        warm_retry_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if request_get is None:
            import requests

            request_get = requests.get
        self.base_url = str(base_url).rstrip("/")
        self.access_token = str(access_token)
        self.request_get = request_get
        self.warm_retry_seconds = max(0.1, float(warm_retry_seconds))
        self.monotonic = monotonic
        self._last_warm_at: dict[str, float] = {}
        self._warm_lock = threading.Lock()

    def fetch_messages(self, session_id: str) -> list[Mapping[str, object]]:
        session = str(session_id)
        params = {"talker": session, "limit": 100, "offset": 0}
        headers = {"Authorization": f"Bearer {self.access_token}"}

        def query():
            response = self.request_get(
                f"{self.base_url}/api/v1/messages",
                params=params,
                headers=headers,
                timeout=10,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"WeFlow message query failed: {response.status_code}"
                )
            return response.json()

        payload = query()
        messages = (
            payload
            if isinstance(payload, list)
            else payload.get("messages", payload.get("data", []))
            if isinstance(payload, dict)
            else []
        )
        rows = [item for item in messages if isinstance(item, dict)]
        with self._warm_lock:
            now = self.monotonic()
            last_warm = self._last_warm_at.get(session)
            should_warm = (
                last_warm is None
                or now - last_warm >= self.warm_retry_seconds
            )
            if should_warm:
                self._last_warm_at[session] = now
        if not should_warm:
            return rows
        warm = self.request_get(
            (
                f"{self.base_url}/api/v1/sessions/"
                f"{quote(session, safe='')}/messages"
            ),
            params={"limit": 1, "offset": 0},
            headers=headers,
            timeout=10,
        )
        if warm.status_code != 200:
            return rows
        payload = query()
        messages = (
            payload
            if isinstance(payload, list)
            else payload.get("messages", payload.get("data", []))
            if isinstance(payload, dict)
            else []
        )
        refreshed_rows = [
            item for item in messages if isinstance(item, dict)
        ]
        return refreshed_rows or rows


class MoneyUiController:
    """Capture and act only inside the validated foreground WeChat client."""

    def __init__(
        self,
        sender,
        *,
        capture_fn=None,
    ) -> None:
        self.sender = sender
        self.capture_fn = capture_fn or _capture_png_bytes
        self.target_contact = ""

    def _validated_window(self):
        driver = self.sender.driver
        hwnd = driver.find_wechat_window()
        driver.activate_receive_window(hwnd)
        metrics = driver.get_client_metrics(hwnd)
        validate_receive_runtime_metrics(self.sender.calibration, metrics)
        return driver, hwnd, metrics

    def open_contact(self, contact: str) -> None:
        normalized = str(contact or "").strip()
        if not normalized:
            raise ValueError("target contact is required")
        driver = self.sender.driver
        hwnd = driver.find_wechat_window()
        driver.activate_receive_window(hwnd)
        metrics = driver.get_client_metrics(hwnd)
        validate_runtime_metrics(self.sender.calibration, metrics)
        self.sender._select_contact(hwnd, normalized)
        validate_runtime_metrics(
            self.sender.calibration,
            driver.get_client_metrics(hwnd),
        )
        self.target_contact = normalized

    def reselect_target_contact(self) -> None:
        if not self.target_contact:
            raise ValueError("target contact was not selected")
        self.open_contact(self.target_contact)

    def verify_normal_chat_window(self) -> None:
        if not self.target_contact:
            raise ValueError("target contact was not selected")
        driver = self.sender.driver
        hwnd = driver.find_wechat_window()
        driver.activate_receive_window(hwnd)
        metrics = driver.get_client_metrics(hwnd)
        validate_runtime_metrics(self.sender.calibration, metrics)

    def capture_frame(self) -> tuple[str, str]:
        _driver, _hwnd, metrics = self._validated_window()
        frame = self.capture_fn(
            ScreenRect(
                metrics.left,
                metrics.top,
                metrics.left + metrics.width,
                metrics.top + metrics.height,
            )
        )
        digest = hashlib.sha256(frame).hexdigest()
        encoded = base64.b64encode(frame).decode("ascii")
        return f"data:image/png;base64,{encoded}", digest

    def click(self, x: float, y: float) -> None:
        driver, hwnd, _metrics = self._validated_window()
        driver.click_receive_ratio(
            hwnd,
            {"x": float(x), "y": float(y)},
            self.sender.calibration,
        )

    def press_escape(self) -> None:
        driver, _hwnd, _metrics = self._validated_window()
        driver.press_key(VK_ESCAPE)


@dataclass
class _ActiveReceive:
    transaction: ReceiveTransaction
    candidate: MoneyCandidate
    token: str
    controller: MoneyUiController
    last_frame_digest: str = ""
    last_frame_nonce: str = ""
    baseline_server_ids: set[str] | None = None
    action_started: bool = False
    action_epoch: int = 0


class MoneyActionService:
    """Own the FIFO barrier until AstrBot vision and WeFlow both succeed."""

    _MAX_PENDING_RECEIVES = 32
    _CANDIDATE_CONFIRM_SECONDS = 5.0

    def __init__(
        self,
        *,
        sender,
        generation: int,
        source: WeFlowMoneySource,
        notifier: Callable[[dict[str, object]], bool],
        controller_factory: Callable[[object], MoneyUiController] = MoneyUiController,
        timeout_seconds: float = 180.0,
        receipt_poll_seconds: float = 1.0,
        account_id: str = "",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sender = sender
        self.generation = int(generation)
        self.source = source
        self.notifier = notifier
        self.controller_factory = controller_factory
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.receipt_poll_seconds = max(0.05, float(receipt_poll_seconds))
        self.account_id = str(account_id).strip()
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._ui_lock = threading.Lock()
        self._active: _ActiveReceive | None = None
        self._finished: OrderedDict[str, tuple[str, dict[str, object]]] = (
            OrderedDict()
        )
        self._last_result: dict[str, object] | None = None
        self._pending_cancels: set[threading.Event] = set()
        self._workers: set[threading.Thread] = set()
        self._seen_raw_ids: OrderedDict[str, None] = OrderedDict()
        self._stopped = threading.Event()

    def handle_sse(self, data: Mapping[str, object]) -> bool:
        if self._stopped.is_set() or detect_money_marker(data) is None:
            return False
        raw_id = str(data.get("rawid") or "").strip()
        session_id = str(data.get("sessionId") or "").strip()
        if not raw_id or not session_id:
            return False
        with self._lock:
            if raw_id in self._seen_raw_ids:
                return True
            if len(self._pending_cancels) >= self._MAX_PENDING_RECEIVES:
                log.error("红包/转账接收候选已达上限；拒绝继续占用 FIFO 优先位")
                return False
            self._seen_raw_ids[raw_id] = None
            while len(self._seen_raw_ids) > 2048:
                self._seen_raw_ids.popitem(last=False)
            cancel_event = threading.Event()
            self._pending_cancels.add(cancel_event)
            deadline = self.monotonic() + self.timeout_seconds
            lease = self.sender.reserve_receive_priority(
                cancel_event=cancel_event,
                timeout=max(0.0, deadline - self.monotonic()),
            )
            worker = None
            try:
                worker = threading.Thread(
                    target=self._run_candidate,
                    args=(dict(data), cancel_event, lease, deadline),
                    daemon=True,
                    name=f"money-receive-{raw_id[-8:]}",
                )
                self._workers.add(worker)
                worker.start()
            except Exception:
                if worker is not None:
                    self._workers.discard(worker)
                self._pending_cancels.discard(cancel_event)
                self._seen_raw_ids.pop(raw_id, None)
                cancel_event.set()
                lease.close()
                log.exception("红包/转账接收线程启动失败")
                return False
        return True

    @staticmethod
    def _target_contact(sse: Mapping[str, object]) -> str:
        session_id = str(sse.get("sessionId") or "").strip()
        group_name = str(sse.get("groupName") or "").strip()
        source_name = str(
            sse.get("sourceName")
            or sse.get("talkerName")
            or ""
        ).strip()
        is_group = (
            str(sse.get("sessionType") or "") == "group"
            or bool(group_name)
            or "@chatroom" in session_id
        )
        target = (group_name or source_name) if is_group else source_name
        if is_group:
            import re

            target = re.sub(r"\s*\(\d+\)\s*$", "", target).strip()
        return target

    def _run_candidate(self, sse, cancel_event, lease, deadline) -> None:
        transaction = None
        active = None
        worker = threading.current_thread()
        try:
            with lease as acquired:
                if not acquired or self._stopped.is_set():
                    return
                try:
                    candidate = None
                    rows = []
                    confirm_deadline = min(
                        deadline,
                        self.monotonic()
                        + self._CANDIDATE_CONFIRM_SECONDS,
                    )
                    while self.monotonic() < confirm_deadline:
                        try:
                            rows = self.source.fetch_messages(
                                str(sse["sessionId"])
                            )
                        except Exception:
                            if (
                                self._stopped.is_set()
                                or cancel_event.is_set()
                            ):
                                return
                            self._stopped.wait(0.1)
                            continue
                        if (
                            self._stopped.is_set()
                            or cancel_event.is_set()
                            or self.monotonic() >= deadline
                        ):
                            return
                        candidate = select_money_candidate(
                            sse,
                            rows,
                            account_id=self.account_id,
                        )
                        if candidate is not None:
                            break
                        self._stopped.wait(0.1)
                    if candidate is None:
                        return
                    transaction = ReceiveTransaction(
                        request_id=uuid.uuid4().hex,
                        generation=self.generation,
                        source_ref=candidate.source_ref,
                        deadline=deadline,
                    )
                    active = _ActiveReceive(
                        transaction=transaction,
                        candidate=candidate,
                        token=secrets.token_urlsafe(32),
                        controller=self.controller_factory(self.sender),
                        baseline_server_ids={
                            str(row.get("serverId") or "").strip()
                            for row in rows
                            if str(row.get("serverId") or "").strip()
                        },
                    )
                    with self._lock:
                        if self._stopped.is_set() or cancel_event.is_set():
                            transaction.cancel("bridge_stopped")
                            return
                        self._active = active
                    target_contact = self._target_contact(sse)
                    if not target_contact:
                        transaction.fail("target_contact_unavailable")
                        return
                    try:
                        active.controller.open_contact(target_contact)
                    except Exception:
                        transaction.fail("contact_selection_failed")
                        return
                    notice = {
                        "request_id": transaction.request_id,
                        "capability_token": active.token,
                        "money_kind": candidate.kind,
                        "amount_cny": candidate.amount_cny,
                        "expires_in_seconds": max(
                            0,
                            int(deadline - self.monotonic()),
                        ),
                    }
                    if not self.notifier(notice):
                        transaction.fail("astrbot_notice_unavailable")
                    while not transaction.terminal:
                        if self._stopped.is_set() or cancel_event.is_set():
                            transaction.cancel("bridge_stopped")
                            break
                        if self.monotonic() >= transaction.deadline:
                            with self._ui_lock:
                                transaction.expire()
                            break
                        with self._lock:
                            poll_epoch = active.action_epoch
                        try:
                            rows = self.source.fetch_messages(
                                candidate.session_id
                            )
                            with self._ui_lock:
                                with self._lock:
                                    self._check_active(active)
                                    if active.action_epoch != poll_epoch:
                                        rows = []
                                    row_ids = {
                                        str(
                                            row.get("serverId") or ""
                                        ).strip()
                                        for row in rows
                                        if str(
                                            row.get("serverId") or ""
                                        ).strip()
                                    }
                                    if not active.action_started:
                                        active.baseline_server_ids.update(
                                            row_ids
                                        )
                                    action_started = (
                                        active.action_started
                                    )
                                    baseline = set(
                                        active.baseline_server_ids
                                    )
                                    new_rows = [
                                        row
                                        for row in rows
                                        if (
                                            str(
                                                row.get("serverId") or ""
                                            ).strip()
                                            and str(
                                                row.get("serverId") or ""
                                            ).strip()
                                            not in baseline
                                        )
                                    ]
                                    if (
                                        action_started
                                        and money_receipt_matches(
                                            candidate,
                                            new_rows,
                                        )
                                    ):
                                        transaction.mark_weflow_success(
                                            request_id=(
                                                transaction.request_id
                                            ),
                                            generation=self.generation,
                                            source_ref=(
                                                candidate.source_ref
                                            ),
                                        )
                        except Exception:
                            with self._ui_lock:
                                with self._lock:
                                    try:
                                        self._check_active(active)
                                    except MoneyRequestError:
                                        pass
                            if not transaction.terminal:
                                log.warning(
                                    "WeFlow 收款回执轮询失败；"
                                    "将在事务期限内重试"
                                )
                        if not transaction.terminal:
                            self._stopped.wait(
                                self.receipt_poll_seconds
                            )
                finally:
                    # The lease must outlive any UI action already admitted by
                    # submit_step, even when Stop/fail/timeout ends the
                    # transaction in another thread.
                    with self._ui_lock:
                        pass
        except Exception:
            if transaction is not None:
                transaction.fail("receive_worker_error")
            log.exception("红包/转账接收事务异常")
        finally:
            if transaction is not None and not transaction.terminal:
                transaction.fail("receive_worker_stopped")
            with self._lock:
                self._pending_cancels.discard(cancel_event)
                if active is not None:
                    status = active.transaction.public_status()
                    self._last_result = {
                        "status": status["status"],
                        "visual_success": status["visual_success"],
                        "weflow_success": status["weflow_success"],
                        "money_kind": active.candidate.kind,
                    }
                    self._finished[active.transaction.request_id] = (
                        active.token,
                        status,
                    )
                    while len(self._finished) > 16:
                        self._finished.popitem(last=False)
                    if self._active is active:
                        self._active = None
                self._workers.discard(worker)

    def _authenticate(self, request_id: object, token: object):
        request_key = str(request_id or "")
        supplied = str(token or "")
        with self._lock:
            active = self._active
            if (
                active is not None
                and active.transaction.request_id == request_key
                and hmac.compare_digest(active.token, supplied)
            ):
                return active
            finished = self._finished.get(request_key)
            if finished and hmac.compare_digest(finished[0], supplied):
                return finished[1]
        raise MoneyRequestError("E_MONEY_CAPABILITY", 403)

    def _check_active(self, active: _ActiveReceive) -> None:
        if (
            self._active is not active
            or self._stopped.is_set()
            or active.transaction.terminal
        ):
            raise MoneyRequestError("E_MONEY_STALE_REQUEST", 409)
        if self.monotonic() >= active.transaction.deadline:
            active.transaction.expire()
            raise MoneyRequestError("E_MONEY_EXPIRED", 409)

    def get_frame(self, *, request_id: object, token: object) -> dict[str, object]:
        authenticated = self._authenticate(request_id, token)
        if isinstance(authenticated, dict):
            return dict(authenticated)
        active = authenticated
        with self._ui_lock:
            with self._lock:
                self._check_active(active)
            try:
                data_url, digest = active.controller.capture_frame()
            except Exception as error:
                active.transaction.fail("frame_capture_failed")
                raise MoneyRequestError("E_MONEY_FRAME", 409) from error
            with self._lock:
                self._check_active(active)
                active.last_frame_digest = digest
                active.last_frame_nonce = secrets.token_urlsafe(18)
            return {
                **active.transaction.public_status(),
                "image_data_url": data_url,
                "frame_sha256": digest,
                "frame_nonce": active.last_frame_nonce,
                "money_kind": active.candidate.kind,
                "target_contact": active.controller.target_contact,
            }

    @staticmethod
    def _coordinate(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MoneyRequestError("E_MONEY_ACTION")
        normalized = float(value)
        if not 0.0 < normalized < 1.0:
            raise MoneyRequestError("E_MONEY_ACTION")
        return normalized

    def submit_step(
        self,
        *,
        token: object,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise MoneyRequestError("E_MONEY_ACTION")
        action = payload.get("action")
        if not isinstance(action, dict) or "type" not in action:
            raise MoneyRequestError("E_MONEY_ACTION")
        authenticated = self._authenticate(payload.get("request_id"), token)
        if isinstance(authenticated, dict):
            return dict(authenticated)
        active = authenticated
        action_type = str(action.get("type") or "")
        if action_type == "fail":
            if (
                set(payload) != {"request_id", "action"}
                or set(action) - {"type", "reason"}
            ):
                raise MoneyRequestError("E_MONEY_ACTION")
            with self._ui_lock:
                with self._lock:
                    if self._active is not active:
                        raise MoneyRequestError(
                            "E_MONEY_STALE_REQUEST",
                            409,
                        )
                    active.transaction.fail("astrbot_agent_failed")
            return active.transaction.public_status()
        if set(payload) != {
            "request_id",
            "frame_sha256",
            "frame_nonce",
            "action",
        }:
            raise MoneyRequestError("E_MONEY_ACTION")
        if action_type == "click":
            if set(action) != {"type", "x", "y"}:
                raise MoneyRequestError("E_MONEY_ACTION")
            x = self._coordinate(action.get("x"))
            y = self._coordinate(action.get("y"))
        elif action_type == "done":
            if set(action) != {"type", "normal_chat"} or action.get(
                "normal_chat"
            ) is not True:
                raise MoneyRequestError("E_MONEY_ACTION")
        elif (
            action_type
            not in {"press_escape", "wait", "reselect_contact"}
            or set(action) != {"type"}
        ):
            raise MoneyRequestError("E_MONEY_ACTION")

        frame_digest = str(payload.get("frame_sha256") or "")
        frame_nonce = str(payload.get("frame_nonce") or "")
        with self._ui_lock:
            with self._lock:
                self._check_active(active)
                if (
                    not frame_digest
                    or not frame_nonce
                    or not hmac.compare_digest(
                        active.last_frame_digest,
                        frame_digest,
                    )
                    or not hmac.compare_digest(
                        active.last_frame_nonce,
                        frame_nonce,
                    )
                ):
                    raise MoneyRequestError("E_MONEY_STALE_FRAME", 409)
                active.last_frame_digest = ""
                active.last_frame_nonce = ""
            try:
                if action_type == "click":
                    with self._lock:
                        needs_baseline = not active.action_started
                    if needs_baseline:
                        try:
                            current_rows = self.source.fetch_messages(
                                active.candidate.session_id
                            )
                        except Exception as error:
                            active.transaction.fail(
                                "receipt_baseline_failed"
                            )
                            raise MoneyRequestError(
                                "E_MONEY_RECEIPT_BASELINE",
                                409,
                            ) from error
                        with self._lock:
                            if (
                                self._active is not active
                                or active.transaction.terminal
                            ):
                                raise MoneyRequestError(
                                    "E_MONEY_STALE_REQUEST",
                                    409,
                                )
                            if not any(
                                str(
                                    row.get("serverId") or ""
                                ).strip()
                                == active.candidate.source_server_id
                                for row in current_rows
                            ):
                                active.transaction.fail(
                                    "receipt_baseline_incomplete"
                                )
                                raise MoneyRequestError(
                                    "E_MONEY_RECEIPT_BASELINE",
                                    409,
                                )
                            active.baseline_server_ids.update(
                                str(
                                    row.get("serverId") or ""
                                ).strip()
                                for row in current_rows
                                if str(
                                    row.get("serverId") or ""
                                ).strip()
                            )
                    _data_url, current_digest = (
                        active.controller.capture_frame()
                    )
                    if not hmac.compare_digest(
                        current_digest,
                        frame_digest,
                    ):
                        raise MoneyRequestError(
                            "E_MONEY_STALE_FRAME",
                            409,
                        )
                    with self._lock:
                        self._check_active(active)
                        active.action_epoch += 1
                        active.action_started = True
                    active.controller.click(x, y)
                elif action_type == "press_escape":
                    _data_url, current_digest = (
                        active.controller.capture_frame()
                    )
                    if not hmac.compare_digest(
                        current_digest,
                        frame_digest,
                    ):
                        raise MoneyRequestError(
                            "E_MONEY_STALE_FRAME",
                            409,
                        )
                    with self._lock:
                        self._check_active(active)
                    active.controller.press_escape()
                elif action_type == "reselect_contact":
                    _data_url, current_digest = (
                        active.controller.capture_frame()
                    )
                    if not hmac.compare_digest(
                        current_digest,
                        frame_digest,
                    ):
                        raise MoneyRequestError(
                            "E_MONEY_STALE_FRAME",
                            409,
                        )
                    with self._lock:
                        self._check_active(active)
                    active.controller.reselect_target_contact()
                elif action_type == "done":
                    _data_url, current_digest = (
                        active.controller.capture_frame()
                    )
                    if not hmac.compare_digest(
                        current_digest,
                        frame_digest,
                    ):
                        raise MoneyRequestError(
                            "E_MONEY_STALE_FRAME",
                            409,
                        )
                    with self._lock:
                        self._check_active(active)
                    active.controller.verify_normal_chat_window()
                    active.transaction.mark_visual_success(
                        request_id=active.transaction.request_id,
                        generation=self.generation,
                    )
            except MoneyRequestError:
                raise
            except Exception as error:
                active.transaction.fail("ui_action_failed")
                raise MoneyRequestError("E_MONEY_UI_ACTION", 409) from error
        return active.transaction.public_status()

    def get_status(
        self,
        *,
        request_id: object,
        token: object,
    ) -> dict[str, object]:
        authenticated = self._authenticate(request_id, token)
        if isinstance(authenticated, dict):
            return dict(authenticated)
        with self._ui_lock:
            with self._lock:
                if (
                    not authenticated.transaction.terminal
                    and self.monotonic()
                    >= authenticated.transaction.deadline
                ):
                    authenticated.transaction.expire()
        return authenticated.transaction.public_status()

    def public_status(self) -> dict[str, object]:
        with self._lock:
            active = self._active
            if active is None:
                status: dict[str, object] = {"active": False}
                if self._last_result is not None:
                    status["last_result"] = dict(self._last_result)
                return status
            return {
                "active": True,
                "status": active.transaction.status,
                "visual_success": active.transaction.visual_success,
                "weflow_success": active.transaction.weflow_success,
                "money_kind": active.candidate.kind,
            }

    def stop(self, join_timeout: float = 2.0) -> None:
        with self._ui_lock:
            self._stopped.set()
            with self._lock:
                pending = list(self._pending_cancels)
                active = self._active
                workers = list(self._workers)
            for cancel_event in pending:
                cancel_event.set()
            if active is not None:
                active.transaction.cancel("bridge_stopped")
        deadline = time.monotonic() + max(0.0, join_timeout)
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if worker.ident is not None:
                worker.join(remaining)
