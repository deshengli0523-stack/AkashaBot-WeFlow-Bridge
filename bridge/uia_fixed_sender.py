"""Fixed four-point foreground WeChat sender."""

import logging
import os
import threading
import time

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


class UiaFixedSender:
    def __init__(self, calibration, driver=None, sleep_fn=time.sleep):
        self.calibration = validate_calibration(calibration)
        self.driver = driver or Win32WeChatDriver()
        self.sleep = sleep_fn
        self._lock = threading.Lock()

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
        self._pause(0.05)
        self.driver.hotkey_ctrl(VK_V)
        self._pause(0.05)

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

    def _log_failure(self, caught: BaseException) -> None:
        if isinstance(caught, CalibrationError):
            log.error(caught.code)
        else:
            log.error("[UIA_FIXED] send failed")

    def send_text(self, contact: str, text: str) -> bool:
        """Send through the fixed four-point sequence."""
        with self._lock:
            try:
                hwnd = self._preflight()
                self._select_contact(hwnd, contact)
                self._focus_and_clear_input(hwnd)
                self._paste_text(text)
                self._send_button(hwnd)
                return True
            except Exception as caught:
                self._log_failure(caught)
                return False

    def send_image(self, contact: str, image_path: str) -> bool:
        """Send an image through the same fixed four-point sequence."""
        with self._lock:
            if not os.path.isfile(image_path):
                log.error("[UIA_FIXED] send failed")
                return False
            try:
                hwnd = self._preflight()
                self._select_contact(hwnd, contact)
                self._focus_and_clear_input(hwnd)
                self.driver.copy_image_to_clipboard(image_path)
                self._pause(0.20)
                self.driver.hotkey_ctrl(VK_V)
                self._pause(0.50)
                self._send_button(hwnd)
                return True
            except Exception as caught:
                self._log_failure(caught)
                return False
