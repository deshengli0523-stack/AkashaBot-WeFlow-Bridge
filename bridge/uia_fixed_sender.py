"""Fixed four-point foreground WeChat sender."""

import logging
import os
import random
import threading
import time
from collections import deque
from contextlib import contextmanager

import state
from uia_support import (
    CALIBRATION_WINDOW,
    CalibrationError,
    Win32WeChatDriver,
    validate_calibration,
    validate_runtime_metrics,
)
from favorite_sticker import (
    STICKER_COMMIT_UNKNOWN,
    STICKER_CONFIRMATION_UNKNOWN,
    STICKER_KEYS,
    STICKER_QUEUE_EXPIRED,
    FavoriteStickerError,
    FavoriteStickerLayout,
    IdempotentResult,
    RequestIdCache,
)


log = logging.getLogger("weflow-bridge")

VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
VK_BACKSPACE = 0x08
VK_RIGHT = 0x27
VK_ESCAPE = 0x1B

_FAVORITE_ENTRY_TO_TAB_DELAY_RANGE = (0.8, 1.3)
_FAVORITE_TAB_TO_SLOT_DELAY_RANGE = (0.9, 1.5)


class _FifoSendLock:
    """Serialize UI sends while allowing a reserved receive action to preempt."""

    def __init__(self):
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._normal_active = False
        self._priority_active = False
        self._priority_waiters = deque()
        self._cancelled_tickets = set()

    def _advance_cancelled_tickets(self) -> None:
        while self._serving_ticket in self._cancelled_tickets:
            self._cancelled_tickets.remove(self._serving_ticket)
            self._serving_ticket += 1

    def __enter__(self):
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while (
                ticket != self._serving_ticket
                or self._normal_active
                or self._priority_active
                or self._priority_waiters
            ):
                self._condition.wait()
            self._normal_active = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self._condition:
            self._normal_active = False
            self._serving_ticket += 1
            self._advance_cancelled_tickets()
            self._condition.notify_all()
        return False

    def reserve_normal(self, *, deadline: float, monotonic):
        return _NormalReservation(self, deadline=deadline, monotonic=monotonic)

    def priority_waiter_count(self) -> int:
        with self._condition:
            return len(self._priority_waiters)

    def reserve_priority(
        self,
        *,
        cancel_event: threading.Event,
        timeout: float,
    ):
        return _PriorityReservation(
            self,
            cancel_event=cancel_event,
            timeout=timeout,
        )

    @contextmanager
    def priority(
        self,
        *,
        cancel_event: threading.Event,
        timeout: float,
    ):
        """Compatibility helper for callers that reserve at ``with`` time."""

        with self.reserve_priority(
            cancel_event=cancel_event,
            timeout=timeout,
        ) as acquired:
            yield acquired


class _PriorityReservation:
    """A priority waiter whose barrier exists before its worker starts."""

    def __init__(
        self,
        lock: _FifoSendLock,
        *,
        cancel_event: threading.Event,
        timeout: float,
    ):
        self._lock = lock
        self._cancel_event = cancel_event
        self._timeout = max(0.0, float(timeout))
        token = object()
        self._token = token
        self._acquired = False
        self._closed = False
        with lock._condition:
            lock._priority_waiters.append(token)
            lock._condition.notify_all()

    def __enter__(self):
        deadline = time.monotonic() + self._timeout
        lock = self._lock
        with lock._condition:
            while True:
                if self._closed:
                    break
                if self._cancel_event.is_set():
                    if self._token in lock._priority_waiters:
                        lock._priority_waiters.remove(self._token)
                    lock._condition.notify_all()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._token in lock._priority_waiters:
                        lock._priority_waiters.remove(self._token)
                    lock._condition.notify_all()
                    break
                if (
                    lock._priority_waiters
                    and lock._priority_waiters[0] is self._token
                    and not lock._normal_active
                    and not lock._priority_active
                ):
                    lock._priority_waiters.popleft()
                    lock._priority_active = True
                    self._acquired = True
                    break
                lock._condition.wait(min(remaining, 0.05))
        return self._acquired

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self) -> None:
        """Release an acquired lease or remove a reservation not yet entered."""

        if self._closed:
            return
        self._closed = True
        lock = self._lock
        with lock._condition:
            if self._acquired:
                lock._priority_active = False
            elif self._token in lock._priority_waiters:
                lock._priority_waiters.remove(self._token)
            lock._condition.notify_all()


class _NormalReservation:
    """A FIFO ticket that can expire without blocking every later ticket."""

    def __init__(self, lock: _FifoSendLock, *, deadline: float, monotonic):
        self._lock = lock
        self._deadline = float(deadline)
        self._monotonic = monotonic
        self._acquired = False
        with lock._condition:
            self._ticket = lock._next_ticket
            lock._next_ticket += 1

    def __enter__(self) -> bool:
        lock = self._lock
        with lock._condition:
            while True:
                remaining = self._deadline - self._monotonic()
                if remaining <= 0:
                    lock._cancelled_tickets.add(self._ticket)
                    lock._advance_cancelled_tickets()
                    lock._condition.notify_all()
                    return False
                if (
                    self._ticket == lock._serving_ticket
                    and not lock._normal_active
                    and not lock._priority_active
                    and not lock._priority_waiters
                ):
                    lock._normal_active = True
                    self._acquired = True
                    return True
                lock._condition.wait(min(remaining, 0.05))

    def __exit__(self, exc_type, exc, traceback):
        if not self._acquired:
            return False
        lock = self._lock
        with lock._condition:
            lock._normal_active = False
            lock._serving_ticket += 1
            lock._advance_cancelled_tickets()
            lock._condition.notify_all()
        return False


_SHARED_SEND_LOCK = _FifoSendLock()
_FAVORITE_REQUEST_CACHE = RequestIdCache()


class UiaFixedSender:
    def __init__(
        self,
        calibration,
        driver=None,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
        pre_paste_preview_delay: float = 1.0,
        pre_send_delay: float = 5.0,
        settle_jitter_max_seconds: float = 0.25,
        favorite_state_dir: str | None = None,
        favorite_receipt=None,
        rng=None,
    ):
        self.calibration = validate_calibration(calibration)
        self.driver = driver or Win32WeChatDriver()
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.pre_paste_preview_delay = max(0.0, float(pre_paste_preview_delay))
        self.pre_send_delay = max(0.0, float(pre_send_delay))
        self.settle_jitter_max_seconds = min(
            0.5,
            max(0.0, float(settle_jitter_max_seconds)),
        )
        self.rng = rng or random
        self.favorite_state_dir = str(favorite_state_dir or "")
        self.favorite_receipt = favorite_receipt
        self._favorite_layout = None
        self._favorite_layout_lock = threading.Lock()
        self._lock = _SHARED_SEND_LOCK
        self._stopped = threading.Event()

    def _settle_jitter(self) -> float:
        maximum = self.settle_jitter_max_seconds
        if maximum <= 0:
            return 0.0
        return max(0.0, min(maximum, float(self.rng.uniform(0.0, maximum))))

    def _pause_settle_jitter(self) -> None:
        delay = self._settle_jitter()
        if delay > 0:
            self._pause(delay)

    def _next_pre_send_delay(self) -> float:
        """Sample a two-second review range ending at the configured maximum."""
        maximum = self.pre_send_delay
        if maximum < 3.0:
            return maximum
        minimum = maximum - 2.0
        return self.rng.uniform(minimum, maximum)

    def stop_pending(self) -> None:
        """Prevent this sender generation from resuming after a later restart."""
        self._stopped.set()

    def reserve_receive_priority(
        self,
        *,
        cancel_event: threading.Event,
        timeout: float,
    ):
        """Install the receive barrier before its worker thread is scheduled."""

        return self._lock.reserve_priority(
            cancel_event=cancel_event,
            timeout=timeout,
        )

    def _preflight(self) -> int:
        hwnd = self.driver.find_wechat_window()
        metrics = self.driver.get_client_metrics(hwnd)
        validate_runtime_metrics(self.calibration, metrics)
        return hwnd

    def _pause(self, seconds: float) -> None:
        self.sleep(seconds)

    def _click(self, hwnd: int, point_name: str) -> None:
        self.driver.click_ratio(
            hwnd,
            self.calibration["points"][point_name],
        )
        self._pause(0.12 + self._settle_jitter())

    def _paste_text(self, value: str, before_paste=None) -> bool:
        import pyperclip

        self._pause_settle_jitter()
        if before_paste is not None and before_paste() is not True:
            return False
        pyperclip.copy(value)
        try:
            self._pause(0.05)
            if before_paste is not None and before_paste() is not True:
                return False
            self.driver.hotkey_ctrl(VK_V)
            self._pause(0.50)
            return True
        finally:
            try:
                if pyperclip.paste() == value:
                    pyperclip.copy("")
            except Exception:
                pass

    def _select_contact(self, hwnd: int, contact: str) -> None:
        self._click(hwnd, "search_box")
        self.driver.hotkey_ctrl(VK_A)
        self._pause(0.05)
        self._paste_text(contact)
        self._pause(0.45)
        self.driver.click_calibrated_search_result(
            hwnd,
            self.calibration["points"]["first_result"],
        )
        self._pause(0.45 + self._settle_jitter())

    def _focus_and_clear_input(self, hwnd: int) -> None:
        self._click(hwnd, "message_input")
        self.driver.hotkey_ctrl(VK_A)
        self._pause(0.05)
        press_bound = getattr(self.driver, "press_key_bound_process", None)
        if not callable(press_bound):
            raise CalibrationError(CALIBRATION_WINDOW)
        press_bound(hwnd, VK_BACKSPACE)
        self._pause(0.05)

    @staticmethod
    def _canonical_draft_text(value: object) -> str:
        normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip(" \t") for line in normalized.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        output = []
        blank = 0
        for line in lines:
            if line == "":
                blank += 1
                if blank <= 1:
                    output.append("")
            else:
                blank = 0
                output.append(line)
        return "\n".join(output)

    @staticmethod
    def _clipboard_sequence_number() -> int | None:
        if os.name != "nt":
            return None
        try:
            import ctypes

            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception:
            return None

    @staticmethod
    def _snapshot_clipboard() -> dict[str, object] | None:
        """Capture every HGLOBAL clipboard format or fail without mutation."""

        try:
            import pyperclip

            if os.name != "nt":
                return {
                    "mode": "text",
                    "text": str(pyperclip.paste()),
                    "sequence": None,
                }
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
            user32.EnumClipboardFormats.restype = wintypes.UINT
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalSize.restype = ctypes.c_size_t
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            if not user32.OpenClipboard(None):
                return None
            try:
                formats: list[tuple[int, bytes]] = []
                clipboard_format = 0
                while True:
                    clipboard_format = int(
                        user32.EnumClipboardFormats(clipboard_format)
                    )
                    if clipboard_format == 0:
                        break
                    handle = user32.GetClipboardData(clipboard_format)
                    if not handle:
                        return None
                    size = int(kernel32.GlobalSize(handle))
                    if size <= 0:
                        # Bitmap/owner-display handles are not losslessly
                        # restorable through HGLOBAL; fail closed.
                        return None
                    pointer = kernel32.GlobalLock(handle)
                    if not pointer:
                        return None
                    try:
                        formats.append(
                            (clipboard_format, ctypes.string_at(pointer, size))
                        )
                    finally:
                        kernel32.GlobalUnlock(handle)
                return {
                    "mode": "win32",
                    "formats": formats,
                    "sequence": int(user32.GetClipboardSequenceNumber()),
                }
            finally:
                user32.CloseClipboard()
        except Exception:
            return None

    @staticmethod
    def _restore_clipboard(
        snapshot: dict[str, object],
        *,
        expected_sequence: int | None,
        expected_text: str,
    ) -> bool:
        """Restore only if no program changed the probe clipboard."""

        try:
            import pyperclip

            if str(pyperclip.paste()) != expected_text:
                return False
            current_sequence = UiaFixedSender._clipboard_sequence_number()
            if (
                expected_sequence is not None
                and current_sequence != expected_sequence
            ):
                return False
            if snapshot.get("mode") == "text":
                pyperclip.copy(str(snapshot.get("text") or ""))
                return True
            if snapshot.get("mode") != "win32" or os.name != "nt":
                return False
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.SetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            if not user32.OpenClipboard(None):
                return False
            allocated: list[int] = []
            try:
                if not user32.EmptyClipboard():
                    return False
                for clipboard_format, data in snapshot.get("formats", []):
                    handle = kernel32.GlobalAlloc(0x0002, len(data))
                    if not handle:
                        return False
                    pointer = kernel32.GlobalLock(handle)
                    if not pointer:
                        return False
                    try:
                        ctypes.memmove(pointer, data, len(data))
                    finally:
                        kernel32.GlobalUnlock(handle)
                    if not user32.SetClipboardData(int(clipboard_format), handle):
                        return False
                    allocated.append(int(handle))
                return True
            finally:
                user32.CloseClipboard()
        except Exception:
            return False

    def _managed_input_is_empty(self, hwnd: int) -> bool:
        """Inspect the selected draft without ever deleting an unknown draft."""

        snapshot = self._snapshot_clipboard()
        if snapshot is None:
            return False
        try:
            import pyperclip

            self._click(hwnd, "message_input")
            self.driver.hotkey_ctrl(VK_A)
            self._pause(0.05)
            sentinel = f"akasha-empty-probe-{time.monotonic_ns()}"
            pyperclip.copy(sentinel)
            self.driver.hotkey_ctrl(VK_C)
            self._pause(0.10)
            copied = str(pyperclip.paste())
            sequence = self._clipboard_sequence_number()
            metrics = self.driver.get_client_metrics(hwnd)
            empty = copied == sentinel and bool(metrics.foreground)
            restored = self._restore_clipboard(
                snapshot,
                expected_sequence=sequence,
                expected_text=copied,
            )
            press_bound = getattr(self.driver, "press_key_bound_process", None)
            if callable(press_bound):
                # Collapse Ctrl+A without sending Esc.  Recent Weixin builds
                # interpret Esc as "hide the main window to the tray".
                press_bound(hwnd, VK_RIGHT)
            return bool(empty and restored)
        except Exception:
            return False

    def _send_button(self, hwnd: int) -> None:
        self._click(hwnd, "send_button")
        self._pause(0.20)

    def _send_button_when_foreground(
        self,
        hwnd: int,
        lifecycle_event: threading.Event,
        *,
        allow_stopping: bool = False,
    ) -> bool:
        """Keep the FIFO head until a transient foreground loss recovers."""
        while self._send_active(
            lifecycle_event,
            allow_stopping=allow_stopping,
        ):
            try:
                self._send_button(hwnd)
                return True
            except CalibrationError as caught:
                if caught.code != CALIBRATION_WINDOW:
                    raise
                log.warning(
                    "[UIA_FIXED] foreground lost before submit; "
                    "queue head retained"
                )
            while self._send_active(
                lifecycle_event,
                allow_stopping=allow_stopping,
            ):
                metrics = self.driver.get_client_metrics(hwnd)
                if (
                    not metrics.visible
                    or not metrics.maximized
                    or metrics.width < 800
                    or metrics.height < 600
                ):
                    raise CalibrationError(CALIBRATION_WINDOW)
                if metrics.foreground:
                    break
                self._pause(0.05)
            if not self._send_active(
                lifecycle_event,
                allow_stopping=allow_stopping,
            ):
                return False
        return False

    def _send_active(
        self,
        cancel_event: threading.Event,
        *,
        allow_stopping: bool = False,
    ) -> bool:
        return bool(
            state.running
            and not self._stopped.is_set()
            and not cancel_event.is_set()
            and (
                allow_stopping
                or not bool(getattr(state, "stopping", False))
            )
        )

    def _wait_for_review(
        self,
        seconds: float,
        cancel_event: threading.Event,
        stage: str,
        deadline: float | None = None,
    ) -> bool:
        """Wait for active time only; paused time does not consume the timer."""
        remaining = max(0.0, float(seconds))
        last = self.monotonic()
        state.update_send_preview(
            cancel_event,
            stage=stage,
            remaining_seconds=remaining,
        )
        while remaining > 0:
            if (
                not self._send_active(cancel_event)
                or (
                    deadline is not None
                    and self.monotonic() >= deadline
                )
            ):
                return False
            now = self.monotonic()
            if state.paused.is_set():
                last = now
                self._pause(min(0.05, remaining))
                continue
            remaining = max(0.0, remaining - max(0.0, now - last))
            last = now
            state.update_send_preview(
                cancel_event,
                stage=stage,
                remaining_seconds=remaining,
            )
            if remaining <= 0:
                break
            self._pause(min(0.05, remaining))
        return self._send_active(cancel_event)

    def _wait_until_resumed(
        self,
        cancel_event: threading.Event,
        deadline: float | None = None,
    ) -> bool:
        while state.paused.is_set():
            if (
                not self._send_active(cancel_event)
                or (
                    deadline is not None
                    and self.monotonic() >= deadline
                )
            ):
                return False
            self._pause(0.05)
        return bool(
            self._send_active(cancel_event)
            and (
                deadline is None
                or self.monotonic() < deadline
            )
        )

    def _discard_pasted_text(self, hwnd: int, contact: str) -> None:
        try:
            self._select_contact(hwnd, contact)
            self._focus_and_clear_input(hwnd)
        except Exception:
            log.warning("[UIA_FIXED] cancelled text could not be cleared")

    def _discard_owned_pasted_text(
        self,
        hwnd: int,
        contact: str,
        expected_text: str,
        *,
        target_id: object,
        generation: object,
        reply_epoch: object,
        request_id: str,
    ) -> bool:
        """Clear only when clipboard readback proves the draft is ours."""

        snapshot = self._snapshot_clipboard()
        if snapshot is None:
            return False
        try:
            import pyperclip

            self._select_contact(hwnd, contact)
            self._click(hwnd, "message_input")
            self.driver.hotkey_ctrl(VK_A)
            self._pause(0.05)
            sentinel = f"akasha-draft-probe-{time.monotonic_ns()}"
            pyperclip.copy(sentinel)
            self.driver.hotkey_ctrl(VK_C)
            self._pause(0.10)
            copied = pyperclip.paste()
            canonical_copied = str(copied).replace("\r\n", "\n")
            canonical_expected = self._canonical_draft_text(expected_text)
            canonical_copied = self._canonical_draft_text(canonical_copied)
            if canonical_copied != canonical_expected:
                log.warning(
                    "[UIA_FIXED] cancelled draft ownership mismatch; retained"
                )
                return False
            copied_sequence = self._clipboard_sequence_number()
            preview = state.get_send_preview()
            metrics = self.driver.get_client_metrics(hwnd)
            if (
                not metrics.foreground
                or not isinstance(preview, dict)
                or str(preview.get("request_id") or "") != str(request_id)
                or preview.get("target_id") != target_id
                or preview.get("generation") != generation
                or preview.get("reply_epoch") != reply_epoch
                or not state.is_reply_current(
                    target_id,
                    generation,
                    reply_epoch,
                )
                or str(pyperclip.paste()) != copied
                or (
                    copied_sequence is not None
                    and self._clipboard_sequence_number() != copied_sequence
                )
            ):
                log.warning(
                    "[UIA_FIXED] cancelled draft ownership changed; retained"
                )
                return False
            press_bound = getattr(self.driver, "press_key_bound_process", None)
            if not callable(press_bound):
                return False
            press_bound(hwnd, VK_BACKSPACE)
            self._pause(0.05)
            return self._restore_clipboard(
                snapshot,
                expected_sequence=copied_sequence,
                expected_text=str(copied),
            )
        except Exception:
            log.warning(
                "[UIA_FIXED] cancelled draft ownership unavailable; retained"
            )
            return False
        finally:
            try:
                press_bound = getattr(
                    self.driver,
                    "press_key_bound_process",
                    None,
                )
                if callable(press_bound):
                    # Preserve an unknown draft while only collapsing its
                    # selection; Esc can hide the entire Weixin window.
                    press_bound(hwnd, VK_RIGHT)
            except Exception:
                pass

    def _record_failure(self, code: str, stage: str) -> bool:
        state.record_send_result(
            False,
            code=code,
            stage=stage,
        )
        return False

    def _record_inactive_failure(
        self,
        cancel_event: threading.Event,
        stage: str,
    ) -> bool:
        cancel_reason = state.get_send_cancel_reason(cancel_event)
        if cancel_reason == "superseded":
            code = "E_UIA_REPLY_SUPERSEDED"
        elif cancel_event.is_set():
            code = "E_UIA_SEND_CANCELLED"
        else:
            code = "E_UIA_SENDER_STOPPED"
        return self._record_failure(code, stage)

    def _log_failure(self, caught: BaseException, stage: str) -> None:
        if isinstance(caught, CalibrationError):
            log.error(caught.code)
            code = caught.code
        elif isinstance(caught, FavoriteStickerError):
            log.error(caught.code)
            code = caught.code
        else:
            log.error("[UIA_FIXED] send failed")
            code = {
                "select_contact": "E_UIA_CONTACT_SELECTION_FAILED",
                "focus_input": "E_UIA_INPUT_FOCUS_FAILED",
                "paste": "E_UIA_PASTE_FAILED",
                "image": "E_UIA_IMAGE_CLIPBOARD_FAILED",
                "submit": "E_UIA_SUBMIT_FAILED",
            }.get(stage, "E_UIA_SEND_FAILED")
        self._record_failure(code, stage)

    def _get_favorite_layout(self) -> FavoriteStickerLayout:
        with self._favorite_layout_lock:
            if self._favorite_layout is None:
                self._favorite_layout = FavoriteStickerLayout(
                    self.favorite_state_dir
                )
            return self._favorite_layout

    def _validate_favorite_runtime(self, layout, metrics) -> None:
        reference = layout.manifest["reference"]
        if metrics.dpi != reference["dpi"]:
            raise CalibrationError("E_UIA_RECALIBRATION_REQUIRED")
        current_aspect = metrics.width / metrics.height
        expected_aspect = float(reference["aspect_ratio"])
        if not expected_aspect * 0.95 <= current_aspect <= expected_aspect * 1.05:
            raise CalibrationError("E_UIA_RECALIBRATION_REQUIRED")

    def _finish_favorite_request(
        self,
        request_id: str,
        identity: tuple[str, str, str],
        result: IdempotentResult,
    ) -> IdempotentResult:
        _FAVORITE_REQUEST_CACHE.finish(request_id, identity, result)
        state.record_send_result(
            result.confirmed,
            code=result.error_code or "OK",
            stage=result.error_stage,
        )
        return result

    def _favorite_interrupt_code(
        self,
        cancel_event: threading.Event,
        deadline: float,
    ) -> str:
        if self.monotonic() >= deadline:
            return STICKER_QUEUE_EXPIRED
        if cancel_event.is_set():
            return "E_UIA_SEND_CANCELLED"
        return "E_UIA_SENDER_STOPPED"

    def send_favorite_sticker(
        self,
        contact: str,
        session: str,
        sticker_key: str,
        request_id: str,
        deadline: float,
    ) -> IdempotentResult:
        """Click one calibrated native favorite-sticker slot exactly once."""
        identity = (str(contact), str(session), str(sticker_key))
        cached = _FAVORITE_REQUEST_CACHE.begin(request_id, identity)
        if cached is not None:
            return cached
        result = None
        try:
            with self._lock.reserve_normal(
                deadline=deadline,
                monotonic=self.monotonic,
            ) as acquired:
                if not acquired:
                    result = IdempotentResult(
                        False,
                        STICKER_QUEUE_EXPIRED,
                        "request",
                        False,
                    )
                    return self._finish_favorite_request(
                        request_id, identity, result
                    )
                if sticker_key not in STICKER_KEYS:
                    result = IdempotentResult(
                        False, "E_OB_INVALID_REQUEST", "request", False
                    )
                    return self._finish_favorite_request(
                        request_id, identity, result
                    )
                if not state.running or self._stopped.is_set():
                    result = IdempotentResult(
                        False, "E_UIA_SENDER_STOPPED", "request", False
                    )
                    return self._finish_favorite_request(
                        request_id, identity, result
                    )

                cancel_event = state.begin_send_preview(
                    contact,
                    f"[收藏表情 {sticker_key}]",
                    message_type="favorite_sticker",
                )
                hwnd = None
                panel_open = False
                committed = False
                stage = "review"
                try:
                    if not self._wait_for_review(
                        self.pre_paste_preview_delay,
                        cancel_event,
                        "before_paste",
                        deadline,
                    ):
                        code = self._favorite_interrupt_code(
                            cancel_event,
                            deadline,
                        )
                        result = IdempotentResult(False, code, stage, False)
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    if not self._wait_for_review(
                        self._next_pre_send_delay(),
                        cancel_event,
                        "pasted_waiting",
                        deadline,
                    ):
                        code = self._favorite_interrupt_code(
                            cancel_event,
                            deadline,
                        )
                        result = IdempotentResult(False, code, stage, False)
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    if not self._wait_until_resumed(cancel_event, deadline):
                        result = IdempotentResult(
                            False,
                            self._favorite_interrupt_code(
                                cancel_event,
                                deadline,
                            ),
                            stage,
                            False,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )

                    layout = self._get_favorite_layout()
                    if self.favorite_receipt is None:
                        raise FavoriteStickerError(
                            "E_UIA_STICKER_CONFIRMATION_UNAVAILABLE"
                        )
                    baseline = self.favorite_receipt.baseline(session)
                    if self.monotonic() >= deadline:
                        raise FavoriteStickerError(STICKER_QUEUE_EXPIRED)
                    stage = "preflight"
                    hwnd = self._preflight()
                    metrics = self.driver.get_client_metrics(hwnd)
                    self._validate_favorite_runtime(layout, metrics)
                    stage = "select_contact"
                    self._select_contact(hwnd, contact)
                    if not self._wait_until_resumed(cancel_event, deadline):
                        result = IdempotentResult(
                            False,
                            self._favorite_interrupt_code(
                                cancel_event,
                                deadline,
                            ),
                            stage,
                            False,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )

                    stage = "sticker"
                    self.driver.click_ratio(
                        hwnd,
                        layout.manifest["points"]["smile_entry"],
                    )
                    panel_open = True
                    self._pause(
                        self.rng.uniform(
                            *_FAVORITE_ENTRY_TO_TAB_DELAY_RANGE
                        )
                    )
                    if not self._wait_until_resumed(cancel_event, deadline):
                        result = IdempotentResult(
                            False,
                            self._favorite_interrupt_code(
                                cancel_event,
                                deadline,
                            ),
                            stage,
                            False,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    self.driver.click_bound_process_ratio(
                        hwnd,
                        layout.manifest["points"]["favorite_tab"],
                    )
                    self._pause(
                        self.rng.uniform(
                            *_FAVORITE_TAB_TO_SLOT_DELAY_RANGE
                        )
                    )
                    if not self._wait_until_resumed(cancel_event, deadline):
                        result = IdempotentResult(
                            False,
                            self._favorite_interrupt_code(
                                cancel_event,
                                deadline,
                            ),
                            stage,
                            False,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    target_point = layout.point(sticker_key)
                    # Refresh immediately before the fixed slot click so a
                    # concurrent manual sticker sent during UI preparation is
                    # not mistaken for this request.
                    baseline = self.favorite_receipt.baseline(session)
                    if self.monotonic() >= deadline:
                        raise FavoriteStickerError(STICKER_QUEUE_EXPIRED)

                    stage = "submit"
                    if (
                        self.monotonic() >= deadline
                        or not state.try_commit_send(cancel_event)
                    ):
                        result = IdempotentResult(
                            False,
                            self._favorite_interrupt_code(
                                cancel_event,
                                deadline,
                            ),
                            stage,
                            False,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    committed = True
                    try:
                        self.driver.click_bound_process_ratio(hwnd, target_point)
                    except Exception:
                        result = IdempotentResult(
                            False,
                            STICKER_COMMIT_UNKNOWN,
                            stage,
                            True,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    panel_open = False
                    if not self.favorite_receipt.confirm(session, baseline):
                        result = IdempotentResult(
                            False,
                            STICKER_CONFIRMATION_UNKNOWN,
                            "complete",
                            True,
                        )
                        return self._finish_favorite_request(
                            request_id, identity, result
                        )
                    result = IdempotentResult(True, "", "complete", True)
                    return self._finish_favorite_request(
                        request_id, identity, result
                    )
                except Exception as caught:
                    code = (
                        caught.code
                        if isinstance(caught, (CalibrationError, FavoriteStickerError))
                        else (
                            STICKER_COMMIT_UNKNOWN
                            if committed
                            else "E_UIA_STICKER_PANEL_FAILED"
                        )
                    )
                    result = IdempotentResult(
                        False,
                        code,
                        stage,
                        committed,
                    )
                    return self._finish_favorite_request(
                        request_id, identity, result
                    )
                finally:
                    if panel_open and not committed and hwnd is not None:
                        try:
                            self.driver.press_key_bound_process(hwnd, 0x1B)
                        except Exception:
                            pass
                    state.end_send_preview(cancel_event)
        finally:
            if result is None:
                _FAVORITE_REQUEST_CACHE.abandon(request_id, identity)

    def send_text(
        self,
        contact: str,
        text: str,
        *,
        target_id: object = None,
        generation: object = None,
        reply_epoch: object = None,
        request_id: str = "",
    ) -> bool:
        """Preview, paste, and submit one cancellable FIFO text item."""
        managed_reply = all(
            value is not None
            for value in (target_id, generation, reply_epoch)
        )
        with self._lock:
            if (
                not state.running
                or bool(getattr(state, "stopping", False))
                or self._stopped.is_set()
            ):
                log.info("[UIA_FIXED] text send skipped while stopped")
                return self._record_failure("E_UIA_SENDER_STOPPED", "request")
            if managed_reply and state.get_reply_draft_quarantine(target_id):
                log.warning("[UIA_FIXED] managed reply blocked by draft quarantine")
                return self._record_failure(
                    "E_UIA_DRAFT_QUARANTINED",
                    "preflight",
                )
            cancel_event = state.begin_send_preview(
                contact,
                text,
                target_id=target_id,
                generation=generation,
                reply_epoch=reply_epoch,
                request_id=request_id,
            )
            hwnd = None
            pasted = False
            committed = False
            commit_started = False
            stage = "review"
            try:
                if not self._wait_for_review(
                    self.pre_paste_preview_delay,
                    cancel_event,
                    "before_paste",
                ):
                    return self._record_inactive_failure(cancel_event, stage)
                if not self._wait_until_resumed(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)
                stage = "preflight"
                hwnd = self._preflight()
                stage = "select_contact"
                self._select_contact(hwnd, contact)
                if request_id and not state.persist_merged_job_stage(
                    request_id,
                    "ui_selected",
                ):
                    return self._record_failure(
                        "E_UIA_SENDER_STOPPED",
                        stage,
                    )
                if not self._wait_until_resumed(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)
                stage = "focus_input"
                if managed_reply:
                    if not self._managed_input_is_empty(hwnd):
                        state.mark_reply_draft_quarantine(
                            target_id,
                            request_id=request_id,
                        )
                        return self._record_failure(
                            "E_UIA_DRAFT_OWNERSHIP_LOST",
                            stage,
                        )
                else:
                    self._focus_and_clear_input(hwnd)
                if not self._wait_until_resumed(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)

                paste_block = {"reason": ""}

                def before_text_paste() -> bool:
                    if not self._send_active(cancel_event):
                        paste_block["reason"] = "inactive"
                        return False
                    if state.paused.is_set():
                        paste_block["reason"] = "paused"
                        return False
                    paste_block["reason"] = ""
                    return True

                stage = "paste"
                if request_id and not state.persist_merged_job_stage(
                    request_id,
                    "pasted_owned",
                ):
                    return self._record_failure(
                        "E_UIA_SENDER_STOPPED",
                        stage,
                    )
                while True:
                    paste_block["reason"] = ""
                    if self._paste_text(
                        text,
                        before_paste=before_text_paste,
                    ):
                        break
                    reason = paste_block["reason"]
                    if reason == "inactive" or not self._send_active(
                        cancel_event
                    ):
                        return self._record_inactive_failure(
                            cancel_event,
                            stage,
                        )
                    if reason != "paused":
                        return self._record_failure(
                            "E_UIA_PASTE_FAILED",
                            stage,
                        )
                    if not self._wait_until_resumed(cancel_event):
                        return self._record_inactive_failure(
                            cancel_event,
                            stage,
                        )
                pasted = True
                stage = "review"
                if not self._wait_for_review(
                    self._next_pre_send_delay(),
                    cancel_event,
                    "pasted_waiting",
                ):
                    return self._record_inactive_failure(cancel_event, stage)
                if not self._wait_until_resumed(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)
                stage = "submit"
                if not state.try_commit_send(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)
                if request_id and not state.persist_merged_job_stage(
                    request_id,
                    "committing",
                ):
                    return self._record_failure(
                        "E_UIA_SUBMIT_FAILED",
                        stage,
                    )
                commit_started = True
                if not self._send_button_when_foreground(
                    hwnd,
                    cancel_event,
                    allow_stopping=True,
                ):
                    return self._record_failure(
                        "E_UIA_SUBMIT_FAILED",
                        stage,
                    )
                committed = True
                if request_id and not state.persist_merged_job_stage(
                    request_id,
                    "committed",
                ):
                    raise RuntimeError("durable commit state unavailable")
                state.record_send_result(True, code="OK", stage="complete")
                return True
            except Exception as caught:
                if commit_started:
                    log.error("[UIA_FIXED] submit result unknown")
                    self._record_failure("E_UIA_COMMIT_UNKNOWN", "submit")
                else:
                    self._log_failure(caught, stage)
                return False
            finally:
                if pasted and not committed and hwnd is not None:
                    if managed_reply:
                        owned_cleared = False
                        if not commit_started:
                            owned_cleared = self._discard_owned_pasted_text(
                                hwnd,
                                contact,
                                text,
                                target_id=target_id,
                                generation=generation,
                                reply_epoch=reply_epoch,
                                request_id=request_id,
                            )
                        if not owned_cleared:
                            state.mark_reply_draft_quarantine(
                                target_id,
                                request_id=request_id,
                            )
                            if (
                                state.get_send_cancel_reason(cancel_event)
                                == "superseded"
                            ):
                                self._record_failure(
                                    "E_UIA_REPLY_SUPERSEDED_DRAFT_QUARANTINED",
                                    "review",
                                )
                    else:
                        self._discard_pasted_text(hwnd, contact)
                state.end_send_preview(cancel_event)

    def send_image(self, contact: str, image_path: str) -> bool:
        """Send an image through the same fixed four-point sequence."""
        with self._lock:
            state.clear_last_send_result()
            if not os.path.isfile(image_path):
                log.error("[UIA_FIXED] send failed")
                return self._record_failure("E_UIA_IMAGE_MISSING", "image")
            hwnd = None
            pasted = False
            committed = False
            lifecycle_event = threading.Event()
            stage = "request"
            try:
                if not self._wait_until_resumed(lifecycle_event):
                    return self._record_inactive_failure(lifecycle_event, stage)
                stage = "preflight"
                hwnd = self._preflight()
                stage = "select_contact"
                self._select_contact(hwnd, contact)
                if not self._wait_until_resumed(lifecycle_event):
                    return self._record_inactive_failure(lifecycle_event, stage)
                stage = "focus_input"
                self._focus_and_clear_input(hwnd)
                if not self._wait_until_resumed(lifecycle_event):
                    return self._record_inactive_failure(lifecycle_event, stage)
                stage = "image"
                self._pause_settle_jitter()
                if not self._wait_until_resumed(lifecycle_event):
                    return self._record_inactive_failure(lifecycle_event, stage)
                self.driver.copy_image_to_clipboard(image_path)
                self._pause(0.20)
                if not self._wait_until_resumed(lifecycle_event):
                    return self._record_inactive_failure(lifecycle_event, stage)
                if (
                    not self._send_active(lifecycle_event)
                    or state.paused.is_set()
                ):
                    return self._record_inactive_failure(lifecycle_event, stage)
                stage = "paste"
                self.driver.hotkey_ctrl(VK_V)
                self._pause(0.50)
                pasted = True
                stage = "review"
                if not self._wait_for_review(
                    self._next_pre_send_delay(),
                    lifecycle_event,
                    "pasted_waiting",
                ):
                    return self._record_inactive_failure(lifecycle_event, stage)
                if (
                    not self._send_active(lifecycle_event)
                    or state.paused.is_set()
                ):
                    return self._record_inactive_failure(lifecycle_event, stage)
                stage = "submit"
                if not self._send_button_when_foreground(
                    hwnd,
                    lifecycle_event,
                ):
                    return self._record_inactive_failure(lifecycle_event, stage)
                committed = True
                state.record_send_result(True, code="OK", stage="complete")
                return True
            except Exception as caught:
                self._log_failure(caught, stage)
                return False
            finally:
                if pasted and not committed and hwnd is not None:
                    self._discard_pasted_text(hwnd, contact)
