"""
Fixed foreground WeChat sender.

This mode assumes the WeChat main window is already open, maximized, and kept in
front by the operator. It does not call SetForegroundWindow or run the original
UIA contact switching flow. It simply clicks window-relative screen positions:

search box -> first result -> message input -> send button.
"""

import logging
import os
import time

from uia_sender import UiaSender

log = logging.getLogger("weflow-bridge")


class UiaFixedSender(UiaSender):
    def __init__(
        self,
        search_x: float = 0.115,
        search_y: float = 0.055,
        first_result_x: float = 0.145,
        first_result_y: float = 0.165,
        input_x: float = 0.62,
        input_y: float = 0.88,
        send_x: float = 0.945,
        send_y: float = 0.955,
        search_delay: float = 0.45,
        switch_delay: float = 0.75,
        paste_delay: float = 0.15,
        clear_input: bool = True,
        use_enter_to_send: bool = False,
    ):
        self.fixed_search_x = search_x
        self.fixed_search_y = search_y
        self.fixed_first_result_x = first_result_x
        self.fixed_first_result_y = first_result_y
        self.fixed_input_x = input_x
        self.fixed_input_y = input_y
        self.fixed_send_x = send_x
        self.fixed_send_y = send_y
        self.fixed_search_delay = search_delay
        self.fixed_switch_delay = switch_delay
        self.fixed_paste_delay = paste_delay
        self.fixed_clear_input = clear_input
        self.fixed_use_enter_to_send = use_enter_to_send
        super().__init__(search_enabled=False)

    def _ready_for_fixed_send(self) -> bool:
        if not self._ready:
            log.error("UIA fixed sender is not ready")
            return False
        if not self._ensure_window():
            return False
        if not self._find_wechat_hwnd():
            log.error("UIA fixed sender cannot find WeChat hwnd")
            return False
        return True

    def _fixed_click(self, x_ratio: float, y_ratio: float, label: str) -> bool:
        ok = self._click_window_ratio(x_ratio, y_ratio)
        if not ok:
            log.error("UIA fixed sender click failed")
            return False
        time.sleep(0.12)
        return True

    def _press_vk(self, vk: int) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.03)

    def _hotkey_ctrl(self, vk: int) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.03)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.03)

    def _ctrl_a(self) -> None:
        self._hotkey_ctrl(0x41)

    def _ctrl_v(self) -> None:
        self._hotkey_ctrl(0x56)

    def _backspace(self) -> None:
        self._press_vk(0x08)

    def _enter(self) -> None:
        self._press_vk(0x0D)

    def _paste_text(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)
        time.sleep(self.fixed_paste_delay)
        self._ctrl_v()
        time.sleep(self.fixed_paste_delay)

    def _switch_contact_fixed(self, contact: str) -> bool:
        if not contact:
            return True
        if not self._fixed_click(self.fixed_search_x, self.fixed_search_y, "search box"):
            return False
        self._ctrl_a()
        time.sleep(0.05)
        self._paste_text(contact)
        time.sleep(self.fixed_search_delay)
        if not self._fixed_click(
            self.fixed_first_result_x,
            self.fixed_first_result_y,
            "first search result",
        ):
            return False
        time.sleep(self.fixed_switch_delay)
        self._last_contact = contact
        return True

    def _focus_input_fixed(self) -> bool:
        if not self._fixed_click(self.fixed_input_x, self.fixed_input_y, "message input"):
            return False
        if self.fixed_clear_input:
            self._ctrl_a()
            time.sleep(0.05)
            self._backspace()
            time.sleep(0.05)
        return True

    def _submit_fixed(self) -> bool:
        if self.fixed_use_enter_to_send:
            self._enter()
            time.sleep(0.2)
            return True
        ok = self._click_window_ratio(self.fixed_send_x, self.fixed_send_y)
        if not ok:
            log.error("UIA fixed sender failed to click send button")
            return False
        time.sleep(0.2)
        return True

    def send_text(self, contact: str, text: str) -> bool:
        with self._lock:
            if "<PIL." in text or "PIL." in text:
                log.warning("Skip PIL object message; text_length=%d", len(text))
                return False
            if not self._ready_for_fixed_send():
                return False
            try:
                if not self._switch_contact_fixed(contact):
                    return False
                if not self._focus_input_fixed():
                    return False
                self._paste_text(text)
                if not self._submit_fixed():
                    return False
                log.info("[UIA_FIXED] text sent; text_length=%d", len(text))
                return True
            except Exception:
                log.error("[UIA_FIXED] text send failed")
                return False

    def send_image(self, contact: str, image_path: str) -> bool:
        with self._lock:
            if not os.path.isfile(image_path):
                log.error("Image source is unavailable")
                return False
            if not self._ready_for_fixed_send():
                return False
            try:
                if not self._switch_contact_fixed(contact):
                    return False
                self._copy_image_to_clipboard(image_path)
                time.sleep(0.2)
                if not self._focus_input_fixed():
                    return False
                self._ctrl_v()
                time.sleep(0.5)
                if not self._submit_fixed():
                    return False
                log.info("[UIA_FIXED] image sent")
                return True
            except Exception:
                log.error("[UIA_FIXED] image send failed")
                return False
