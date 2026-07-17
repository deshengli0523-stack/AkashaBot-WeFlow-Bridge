import copy
import logging
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import uia_fixed_sender as sender_module
from uia_support import (
    CALIBRATION_INVALID,
    CALIBRATION_WINDOW,
    RECALIBRATION_REQUIRED,
    CalibrationError,
    ClientMetrics,
    validate_runtime_metrics as real_validate_runtime_metrics,
)


VALID_CALIBRATION = {
    "schema_version": 1,
    "completed": True,
    "coordinate_space": "client_area_ratio",
    "points": {
        "search_box": {"x": 0.10, "y": 0.10},
        "first_result": {"x": 0.20, "y": 0.20},
        "message_input": {"x": 0.60, "y": 0.80},
        "send_button": {"x": 0.90, "y": 0.90},
    },
    "reference": {
        "client_width": 1200,
        "client_height": 800,
        "aspect_ratio": 1.5,
        "dpi": 96,
    },
}


def valid_metrics(**changes):
    values = {
        "hwnd": 101,
        "left": 40,
        "top": 30,
        "width": 1200,
        "height": 800,
        "dpi": 96,
        "visible": True,
        "maximized": True,
        "foreground": True,
    }
    values.update(changes)
    return ClientMetrics(**values)


class FakeDriver:
    def __init__(self, metrics=None):
        self.metrics = metrics or valid_metrics()
        self.events = []
        self._point_names = {
            (point["x"], point["y"]): name
            for name, point in VALID_CALIBRATION["points"].items()
        }

    def find_wechat_window(self):
        self.events.append(("find_window",))
        return self.metrics.hwnd

    def get_client_metrics(self, hwnd):
        self.events.append(("get_metrics", hwnd))
        return self.metrics

    def click_ratio(self, hwnd, point):
        name = self._point_names[(point["x"], point["y"])]
        self.events.append(("click", name, hwnd))

    def hotkey_ctrl(self, virtual_key):
        self.events.append(("hotkey_ctrl", virtual_key))

    def press_key(self, virtual_key):
        self.events.append(("press_key", virtual_key))

    def copy_image_to_clipboard(self, image_path):
        self.events.append(("copy_image", image_path))


class UiaFixedSenderTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.sleep_calls = []

    def _sender(self, driver=None):
        return sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=driver or self.driver,
            sleep_fn=self.sleep_calls.append,
        )

    def _runtime_validation(self, calibration, metrics):
        self.driver.events.append(("validate",))
        return real_validate_runtime_metrics(calibration, metrics)

    def _clipboard_boundary(self, side_effect):
        clipboard = types.ModuleType("pyperclip")
        clipboard.copy = mock.Mock(side_effect=side_effect)
        return mock.patch.dict(sys.modules, {"pyperclip": clipboard})

    def _send_text(self, sender, contact="private-contact", text="private-body"):
        def copy_text(value):
            self.driver.events.append(("copy_text", value))

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(copy_text):
            return sender.send_text(contact, text)

    def test_constructor_validates_schema_one_calibration(self):
        invalid = copy.deepcopy(VALID_CALIBRATION)
        invalid["points"]["search_box"]["x"] = 0

        with self.assertRaises(CalibrationError) as raised:
            sender_module.UiaFixedSender(
                calibration=invalid,
                driver=self.driver,
                sleep_fn=self.sleep_calls.append,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_INVALID)

    def test_text_send_uses_preflight_then_exact_fixed_four_point_sequence(self):
        sender = self._sender()

        sent = self._send_text(sender)

        self.assertIs(sent, True)
        self.assertEqual(
            self.driver.events,
            [
                ("find_window",),
                ("get_metrics", 101),
                ("validate",),
                ("click", "search_box", 101),
                ("hotkey_ctrl", 0x41),
                ("copy_text", "private-contact"),
                ("hotkey_ctrl", 0x56),
                ("click", "first_result", 101),
                ("click", "message_input", 101),
                ("hotkey_ctrl", 0x41),
                ("press_key", 0x08),
                ("copy_text", "private-body"),
                ("hotkey_ctrl", 0x56),
                ("click", "send_button", 101),
            ],
        )
        self.assertTrue(self.sleep_calls, "fake sleep boundary was not exercised")

    def test_image_send_uses_same_four_points_and_pastes_at_message_input(self):
        sender = self._sender()
        with tempfile.TemporaryDirectory() as temporary:
            image_path = str(pathlib.Path(temporary) / "private-image-path.png")
            pathlib.Path(image_path).write_bytes(b"fake-driver-does-not-open-this")

            with mock.patch.object(
                sender_module,
                "validate_runtime_metrics",
                side_effect=self._runtime_validation,
            ), self._clipboard_boundary(
                lambda value: self.driver.events.append(("copy_text", value))
            ):
                sent = sender.send_image("private-contact", image_path)

        self.assertIs(sent, True)
        self.assertEqual(
            self.driver.events,
            [
                ("find_window",),
                ("get_metrics", 101),
                ("validate",),
                ("click", "search_box", 101),
                ("hotkey_ctrl", 0x41),
                ("copy_text", "private-contact"),
                ("hotkey_ctrl", 0x56),
                ("click", "first_result", 101),
                ("click", "message_input", 101),
                ("hotkey_ctrl", 0x41),
                ("press_key", 0x08),
                ("copy_image", image_path),
                ("hotkey_ctrl", 0x56),
                ("click", "send_button", 101),
            ],
        )

    def test_invalid_window_state_or_size_never_reaches_mouse_actions(self):
        cases = (
            {"foreground": False},
            {"maximized": False},
            {"visible": False},
            {"width": 799},
            {"height": 599},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                driver = FakeDriver(valid_metrics(**changes))
                self.driver = driver
                sender = self._sender(driver)

                with self.assertLogs("weflow-bridge", logging.ERROR) as captured:
                    sent = self._send_text(sender)

                self.assertIs(sent, False)
                self.assertEqual(
                    captured.output,
                    [f"ERROR:weflow-bridge:{CALIBRATION_WINDOW}"],
                )
                self.assertFalse(
                    any(event[0] == "click" for event in driver.events),
                    driver.events,
                )

    def test_dpi_and_aspect_drift_log_only_recalibration_code(self):
        cases = (
            valid_metrics(dpi=144),
            valid_metrics(width=1600, height=800),
        )
        for metrics in cases:
            with self.subTest(metrics=metrics):
                driver = FakeDriver(metrics)
                self.driver = driver
                sender = self._sender(driver)

                with self.assertLogs("weflow-bridge", logging.ERROR) as captured:
                    sent = self._send_text(
                        sender,
                        contact="do-not-log-contact",
                        text="do-not-log-body",
                    )

                self.assertIs(sent, False)
                self.assertEqual(
                    captured.output,
                    [f"ERROR:weflow-bridge:{RECALIBRATION_REQUIRED}"],
                )
                self.assertFalse(any(event[0] == "click" for event in driver.events))

    def test_unclassified_failure_logs_only_generic_code(self):
        sender = self._sender()

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(
            RuntimeError("do-not-log-private-exception")
        ), self.assertLogs("weflow-bridge", logging.ERROR) as captured:
            sent = sender.send_text("do-not-log-contact", "do-not-log-body")

        self.assertIs(sent, False)
        self.assertEqual(
            captured.output,
            ["ERROR:weflow-bridge:[UIA_FIXED] send failed"],
        )

    def test_missing_image_returns_false_without_ui_or_clipboard_actions(self):
        sender = self._sender()

        with self.assertLogs("weflow-bridge", logging.ERROR) as captured:
            sent = sender.send_image("do-not-log-contact", "missing-private-image.png")

        self.assertIs(sent, False)
        self.assertEqual(
            captured.output,
            ["ERROR:weflow-bridge:[UIA_FIXED] send failed"],
        )
        self.assertEqual(self.driver.events, [])

    def test_each_sender_instance_serializes_complete_send_actions(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()

        class BlockingDriver(FakeDriver):
            def __init__(self):
                super().__init__()
                self.find_calls = 0

            def find_wechat_window(self):
                self.find_calls += 1
                if self.find_calls == 1:
                    first_entered.set()
                    if not release_first.wait(2):
                        raise AssertionError("test did not release first send")
                return super().find_wechat_window()

        driver = BlockingDriver()
        self.driver = driver
        sender = self._sender(driver)

        results = []
        failures = []
        with tempfile.TemporaryDirectory() as temporary:
            image_path = str(pathlib.Path(temporary) / "serialized-image.png")
            pathlib.Path(image_path).write_bytes(b"fake-driver-does-not-open-this")

            def run(action):
                try:
                    results.append(action())
                except BaseException as caught:
                    failures.append(caught)

            def run_second():
                second_attempted.set()
                run(lambda: sender.send_image("second", image_path))

            first = threading.Thread(
                target=run,
                args=(lambda: sender.send_text("first", "one"),),
            )
            second = threading.Thread(
                target=run_second,
            )

            with mock.patch.object(
                sender_module,
                "validate_runtime_metrics",
                side_effect=self._runtime_validation,
            ), self._clipboard_boundary(lambda _value: None):
                first.start()
                self.assertTrue(first_entered.wait(1))
                second.start()
                self.assertTrue(second_attempted.wait(1))
                try:
                    second.join(0.05)
                    self.assertTrue(
                        second.is_alive(),
                        "image send completed while text send held the instance lock",
                    )
                    self.assertEqual(driver.find_calls, 1)
                finally:
                    release_first.set()
                    first.join(2)
                    second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [True, True])
        self.assertEqual(driver.find_calls, 2)

    def test_sender_source_has_no_foreground_activation_or_enter_send_branch(self):
        source = (BRIDGE / "uia_fixed_sender.py").read_text(encoding="utf-8")

        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("use_enter_to_send", source)
        self.assertNotIn("press_key(0x0D)", source)
        self.assertNotIn("VK_RETURN", source)


if __name__ == "__main__":
    unittest.main()
