"""Fail-closed contact selection using Windows OCR and WeChat process identity."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import subprocess
import time
from pathlib import Path

from uia_support import (
    CONTACT_SELECTION_FAILED,
    CalibrationError,
    ScreenRect,
)


_FILE_HELPER = "".join(chr(value) for value in (0x6587, 0x4EF6, 0x4F20, 0x8F93, 0x52A9, 0x624B))
_CONTACTS_SECTION = "".join(chr(value) for value in (0x8054, 0x7CFB, 0x4EBA))
_FEATURES_SECTION = "".join(chr(value) for value in (0x529F, 0x80FD))
_TITLE_LEFT_RATIO = 0.18
_TITLE_RIGHT_RATIO = 0.55
_TITLE_HEIGHT_RATIO = 0.09
_TITLE_MIN_HEIGHT = 96


class OcrContactSelector:
    def __init__(
        self,
        driver,
        *,
        script_path: str | None = None,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
        subprocess_run=subprocess.run,
        capture_fn=None,
        selection_timeout: float = 8.0,
    ):
        self.driver = driver
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.subprocess_run = subprocess_run
        self.capture_fn = capture_fn or self._capture_png_bytes
        self.selection_timeout = max(1.0, float(selection_timeout))
        self.script_path = Path(
            script_path
            or Path(__file__).with_name("windows_ocr_selector.ps1")
        )
        system_root = os.environ.get("SystemRoot", "")
        self.powershell_path = (
            Path(
                system_root,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            if system_root
            else None
        )

    @staticmethod
    def _fail() -> None:
        raise CalibrationError(CONTACT_SELECTION_FAILED)

    @staticmethod
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
        except Exception:
            OcrContactSelector._fail()
        finally:
            buffer.close()

    def _invoke_ocr(
        self,
        rect: ScreenRect,
        *,
        mode: str,
        expected_text: str,
        section_text: str = "",
    ) -> dict:
        if (
            not self.script_path.is_file()
            or self.powershell_path is None
            or not self.powershell_path.is_file()
        ):
            self._fail()
        try:
            image_bytes = self.capture_fn(rect)
        except Exception:
            self._fail()
        if (
            not isinstance(image_bytes, bytes)
            or not image_bytes
            or len(image_bytes) > 3 * 1024 * 1024
        ):
            self._fail()
        request = json.dumps(
            {
                "mode": mode,
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "expected_text": expected_text,
                "section_text": section_text,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if len(request) > 4 * 1024 * 1024:
            self._fail()
        try:
            completed = self.subprocess_run(
                [
                    str(self.powershell_path),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.script_path),
                ],
                input=request,
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self._fail()
        if (
            completed.returncode != 0
            or not isinstance(completed.stdout, str)
            or len(completed.stdout) > 4096
        ):
            self._fail()
        try:
            response = json.loads(completed.stdout.lstrip("\ufeff").strip())
        except (TypeError, ValueError):
            self._fail()
        if (
            not isinstance(response, dict)
            or response.get("status") != "ok"
            or not isinstance(response.get("matched"), bool)
        ):
            self._fail()
        return response

    def _locate_candidate(
        self,
        main_hwnd: int,
        search_point: dict,
        contact: str,
        section: str,
    ) -> tuple[int, ScreenRect, tuple[int, int]] | None:
        try:
            popup_hwnd, rect = self.driver.find_search_popup(
                main_hwnd,
                search_point,
            )
        except CalibrationError:
            return None
        response = self._invoke_ocr(
            rect,
            mode="search",
            expected_text=contact,
            section_text=section,
        )
        if response["matched"] is not True:
            return None
        candidate = response.get("candidate")
        if not isinstance(candidate, dict) or set(candidate) != {"x", "y"}:
            self._fail()
        x = candidate.get("x")
        y = candidate.get("y")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
        ):
            self._fail()
        point = (rect.left + round(float(x)), rect.top + round(float(y)))
        if (
            not rect.left <= point[0] < rect.right
            or not rect.top <= point[1] < rect.bottom
        ):
            self._fail()
        return popup_hwnd, rect, point

    def _title_matches(self, main_hwnd: int, contact: str) -> bool:
        metrics = self.driver.get_client_metrics(main_hwnd)
        header = ScreenRect(
            metrics.left + round(metrics.width * _TITLE_LEFT_RATIO),
            metrics.top,
            metrics.left + round(metrics.width * _TITLE_RIGHT_RATIO),
            metrics.top
            + max(
                _TITLE_MIN_HEIGHT,
                round(metrics.height * _TITLE_HEIGHT_RATIO),
            ),
        )
        response = self._invoke_ocr(
            header,
            mode="title",
            expected_text=contact,
        )
        return response["matched"] is True

    def verify_selected_contact(self, main_hwnd: int, contact: str) -> None:
        normalized = contact.strip() if isinstance(contact, str) else ""
        if not normalized:
            self._fail()
        for _ in range(3):
            if self._title_matches(main_hwnd, normalized):
                return
            self.sleep(0.20)
        self._fail()

    @staticmethod
    def _same_candidate(
        first: tuple[int, ScreenRect, tuple[int, int]],
        second: tuple[int, ScreenRect, tuple[int, int]],
    ) -> bool:
        return (
            first[0] == second[0]
            and first[1] == second[1]
            and max(
                abs(first[2][0] - second[2][0]),
                abs(first[2][1] - second[2][1]),
            )
            <= 4
        )

    def select_contact(
        self,
        main_hwnd: int,
        search_point: dict,
        contact: str,
    ) -> None:
        if not isinstance(contact, str) or not contact.strip():
            self._fail()
        section = (
            _FEATURES_SECTION
            if contact.strip() == _FILE_HELPER
            else _CONTACTS_SECTION
        )
        deadline = self.monotonic() + self.selection_timeout
        previous = None
        while self.monotonic() < deadline:
            candidate = self._locate_candidate(
                main_hwnd,
                search_point,
                contact.strip(),
                section,
            )
            if candidate is not None:
                if previous is not None and self._same_candidate(
                    previous,
                    candidate,
                ):
                    self.driver.click_search_popup_point(
                        main_hwnd,
                        candidate[0],
                        candidate[2],
                    )
                    self.sleep(0.45)
                    self.verify_selected_contact(main_hwnd, contact)
                    return
                previous = candidate
            else:
                previous = None
            self.sleep(0.12)
        self._fail()
