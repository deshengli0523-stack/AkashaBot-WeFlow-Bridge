"""Fixed four-point foreground WeChat sender."""

import logging
import os
import threading
import time

import state
from uia_support import (
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
    """Serialize UI sends in caller arrival order."""

    def __init__(self):
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0

    def __enter__(self):
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving_ticket:
                self._condition.wait()
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self._condition:
            self._serving_ticket += 1
            self._condition.notify_all()
        return False


_SHARED_SEND_LOCK = _FifoSendLock()


class UiaFixedSender:
    def __init__(
        self,
        calibration,
        driver=None,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
        pre_paste_preview_delay: float = 1.0,
        pre_send_delay: float = 10.0,
    ):
        self.calibration = validate_calibration(calibration)
        self.driver = driver or Win32WeChatDriver()
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.pre_paste_preview_delay = max(0.0, float(pre_paste_preview_delay))
        self.pre_send_delay = max(0.0, float(pre_send_delay))
        self._lock = _SHARED_SEND_LOCK
        self._stopped = threading.Event()

    def stop_pending(self) -> None:
        """Prevent this sender generation from resuming after a later restart."""
        self._stopped.set()

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
        self._pause(0.12)

    def _paste_text(self, value: str) -> None:
        import pyperclip

        pyperclip.copy(value)
        try:
            self._pause(0.05)
            self.driver.hotkey_ctrl(VK_V)
            self._pause(0.05)
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
        self._click(hwnd, "first_result")
        self._pause(0.75)

    def _focus_and_clear_input(self, hwnd: int) -> None:
        self._click(hwnd, "message_input")
        self.driver.hotkey_ctrl(VK_A)
        self._pause(0.05)
        self.driver.press_key(VK_BACKSPACE)
        self._pause(0.05)

    def _send_button(self, hwnd: int) -> None:
        self._click(hwnd, "send_button")
        self._pause(0.20)

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

    def _discard_pasted_text(self, hwnd: int) -> None:
        try:
            self._focus_and_clear_input(hwnd)
        except Exception:
            log.warning("[UIA_FIXED] cancelled text could not be cleared")

    def _log_failure(self, caught: BaseException) -> None:
        if isinstance(caught, CalibrationError):
            log.error(caught.code)
        else:
            log.error("[UIA_FIXED] send failed")

    def send_text(self, contact: str, text: str) -> bool:
        """Preview, paste, and submit one cancellable FIFO text item."""
        with self._lock:
            if not state.running or self._stopped.is_set():
                log.info("[UIA_FIXED] text send skipped while stopped")
                return False
            cancel_event = state.begin_send_preview(contact, text)
            hwnd = None
            pasted = False
            committed = False
            try:
                if not self._wait_for_review(
                    self.pre_paste_preview_delay,
                    cancel_event,
                    "before_paste",
                ):
                    return False
                if not self._wait_until_resumed(cancel_event):
                    return False
                hwnd = self._preflight()
                self._select_contact(hwnd, contact)
                if not self._wait_until_resumed(cancel_event):
                    return False
                self._focus_and_clear_input(hwnd)
                if not self._wait_until_resumed(cancel_event):
                    return False
                self._paste_text(text)
                pasted = True
                if not self._wait_for_review(
                    self.pre_send_delay,
                    cancel_event,
                    "pasted_waiting",
                ):
                    return False
                if not self._wait_until_resumed(cancel_event):
                    return False
                if not state.try_commit_send(cancel_event):
                    return False
                committed = True
                self._send_button(hwnd)
                return True
            except Exception as caught:
                self._log_failure(caught)
                return False
            finally:
                if pasted and not committed and hwnd is not None:
                    self._discard_pasted_text(hwnd)
                state.end_send_preview(cancel_event)

    def send_image(self, contact: str, image_path: str) -> bool:
        """Send an image through the same fixed four-point sequence."""
        with self._lock:
            if not os.path.isfile(image_path):
                log.error("[UIA_FIXED] send failed")
                return False
            lifecycle_event = threading.Event()
            try:
                if not self._wait_until_resumed(lifecycle_event):
                    return False
                hwnd = self._preflight()
                self._select_contact(hwnd, contact)
                if not self._wait_until_resumed(lifecycle_event):
                    return False
                self._focus_and_clear_input(hwnd)
                if not self._wait_until_resumed(lifecycle_event):
                    return False
                self.driver.copy_image_to_clipboard(image_path)
                self._pause(0.20)
                self.driver.hotkey_ctrl(VK_V)
                self._pause(0.50)
                if not self._wait_until_resumed(lifecycle_event):
                    return False
                self._send_button(hwnd)
                return True
            except Exception as caught:
                self._log_failure(caught)
                return False
