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


log = logging.getLogger("weflow-bridge")

VK_A = 0x41
VK_V = 0x56
VK_BACKSPACE = 0x08


class _FifoSendLock:
    """Serialize UI sends while allowing a reserved receive action to preempt."""

    def __init__(self):
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._normal_active = False
        self._priority_active = False
        self._priority_waiters = deque()

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
            self._condition.notify_all()
        return False

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


_SHARED_SEND_LOCK = _FifoSendLock()


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
        self.driver.press_key(VK_BACKSPACE)
        self._pause(0.05)

    def _send_button(self, hwnd: int) -> None:
        self._click(hwnd, "send_button")
        self._pause(0.20)

    def _send_button_when_foreground(
        self,
        hwnd: int,
        lifecycle_event: threading.Event,
    ) -> bool:
        """Keep the FIFO head until a transient foreground loss recovers."""
        while self._send_active(lifecycle_event):
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
            while self._send_active(lifecycle_event):
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
            if not self._send_active(lifecycle_event):
                return False
        return False

    def _send_active(self, cancel_event: threading.Event) -> bool:
        return bool(
            state.running
            and not self._stopped.is_set()
            and not cancel_event.is_set()
        )

    def _wait_for_review(
        self,
        seconds: float,
        cancel_event: threading.Event,
        stage: str,
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
            if not self._send_active(cancel_event):
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

    def _wait_until_resumed(self, cancel_event: threading.Event) -> bool:
        while state.paused.is_set():
            if not self._send_active(cancel_event):
                return False
            self._pause(0.05)
        return self._send_active(cancel_event)

    def _discard_pasted_text(self, hwnd: int, contact: str) -> None:
        try:
            self._select_contact(hwnd, contact)
            self._focus_and_clear_input(hwnd)
        except Exception:
            log.warning("[UIA_FIXED] cancelled text could not be cleared")

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
        code = (
            "E_UIA_SEND_CANCELLED"
            if cancel_event.is_set()
            else "E_UIA_SENDER_STOPPED"
        )
        return self._record_failure(code, stage)

    def _log_failure(self, caught: BaseException, stage: str) -> None:
        if isinstance(caught, CalibrationError):
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

    def send_text(self, contact: str, text: str) -> bool:
        """Preview, paste, and submit one cancellable FIFO text item."""
        with self._lock:
            if not state.running or self._stopped.is_set():
                log.info("[UIA_FIXED] text send skipped while stopped")
                return self._record_failure("E_UIA_SENDER_STOPPED", "request")
            cancel_event = state.begin_send_preview(contact, text)
            hwnd = None
            pasted = False
            committed = False
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
                if not self._wait_until_resumed(cancel_event):
                    return self._record_inactive_failure(cancel_event, stage)
                stage = "focus_input"
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
                if not self._send_button_when_foreground(
                    hwnd,
                    cancel_event,
                ):
                    return self._record_inactive_failure(cancel_event, stage)
                committed = True
                state.record_send_result(True, code="OK", stage="complete")
                return True
            except Exception as caught:
                self._log_failure(caught, stage)
                return False
            finally:
                if pasted and not committed and hwnd is not None:
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
