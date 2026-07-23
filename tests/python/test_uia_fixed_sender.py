import copy
import logging
import pathlib
import sys
import tempfile
import threading
import time
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
        sender_module.state.running = True
        sender_module.state.paused.clear()
        sender_module.state.sender_instance = None
        with sender_module.state.send_preview_lock:
            sender_module.state.current_send_cancel_event = None
            sender_module.state.current_send_preview = None

    def tearDown(self):
        sender_module.state.running = False
        sender_module.state.paused.clear()

    def _sender(self, driver=None, **changes):
        return sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=driver or self.driver,
            sleep_fn=self.sleep_calls.append,
            pre_paste_preview_delay=changes.get("pre_paste_preview_delay", 0),
            pre_send_delay=changes.get("pre_send_delay", 0),
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

    def test_preview_is_visible_before_ui_and_stale_cancel_cannot_hit_next(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.01)),
            pre_paste_preview_delay=0.5,
            pre_send_delay=0,
        )
        results = []

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            worker = threading.Thread(
                target=lambda: results.append(
                    sender.send_text("private-contact", "first body")
                )
            )
            worker.start()
            self.assertTrue(self._wait_for_preview("before_paste"))
            preview = sender_module.state.get_send_preview()
            first_id = preview["preview_id"]
            self.assertEqual(preview["content"], "first body")
            self.assertEqual(self.driver.events, [])
            self.assertTrue(
                sender_module.state.cancel_current_preview(first_id)
            )
            worker.join(2)

            sender.pre_paste_preview_delay = 0
            self.assertIs(
                self._send_text(sender, text="second body"),
                True,
            )

        self.assertEqual(results, [False])
        self.assertFalse(sender_module.state.cancel_current_preview(first_id))
        sent_bodies = [
            event[1]
            for event in self.driver.events
            if event[0] == "copy_text" and event[1].endswith("body")
        ]
        self.assertEqual(sent_bodies, ["second body"])

    def test_pause_freezes_pasted_countdown_then_resumes_exactly_once(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.01)),
            pre_paste_preview_delay=0,
            pre_send_delay=0.30,
        )
        results = []

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            worker = threading.Thread(
                target=lambda: results.append(
                    sender.send_text("private-contact", "paused body")
                )
            )
            worker.start()
            self.assertTrue(self._wait_for_preview("pasted_waiting"))
            time.sleep(0.03)
            sender_module.state.paused.set()
            paused_preview = sender_module.state.get_send_preview()
            remaining = paused_preview["remaining_seconds"]
            time.sleep(0.20)
            self.assertEqual(
                sum(
                    event[:2] == ("click", "send_button")
                    for event in self.driver.events
                ),
                0,
            )
            self.assertEqual(
                sender_module.state.get_send_preview()["stage"],
                "paused",
            )
            self.assertAlmostEqual(
                sender_module.state.get_send_preview()["remaining_seconds"],
                remaining,
                delta=0.05,
            )

            for _ in range(3):
                sender_module.state.paused.clear()
                time.sleep(0.01)
                sender_module.state.paused.set()
                time.sleep(0.01)
            sender_module.state.paused.clear()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        self.assertEqual(
            sum(
                event[:2] == ("click", "send_button")
                for event in self.driver.events
            ),
            1,
        )

    def test_cancel_first_of_three_true_fifo_items_preserves_later_order(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.005)),
            pre_paste_preview_delay=0.5,
            pre_send_delay=0,
        )
        results = {}

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            workers = [
                threading.Thread(
                    target=lambda label=label: results.setdefault(
                        label,
                        sender.send_text("private-contact", f"{label} body"),
                    )
                )
                for label in ("A", "B", "C")
            ]
            workers[0].start()
            self.assertTrue(
                self._wait_for_preview("before_paste", content="A body")
            )
            first_id = sender_module.state.get_send_preview()["preview_id"]
            workers[1].start()
            time.sleep(0.02)
            workers[2].start()
            sender.pre_paste_preview_delay = 0
            self.assertTrue(
                sender_module.state.cancel_current_preview(first_id)
            )
            for worker in workers:
                worker.join(2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(results, {"A": False, "B": True, "C": True})
        sent_bodies = [
            event[1]
            for event in self.driver.events
            if event[0] == "copy_text" and event[1].endswith("body")
        ]
        self.assertEqual(sent_bodies, ["B body", "C body"])

    def test_stopped_sender_does_not_revive_after_fast_restart(self):
        old_sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.005)),
            pre_paste_preview_delay=0.5,
            pre_send_delay=0,
        )
        new_sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.005)),
            pre_paste_preview_delay=0,
            pre_send_delay=0,
        )
        results = {}

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            old_worker = threading.Thread(
                target=lambda: results.setdefault(
                    "old", old_sender.send_text("private-contact", "old body")
                )
            )
            old_worker.start()
            self.assertTrue(
                self._wait_for_preview("before_paste", content="old body")
            )
            sender_module.state.running = False
            old_sender.stop_pending()
            sender_module.state.running = True
            new_worker = threading.Thread(
                target=lambda: results.setdefault(
                    "new", new_sender.send_text("private-contact", "new body")
                )
            )
            new_worker.start()
            old_worker.join(2)
            new_worker.join(2)

        self.assertFalse(old_worker.is_alive())
        self.assertFalse(new_worker.is_alive())
        self.assertEqual(results, {"old": False, "new": True})
        sent_bodies = [
            event[1]
            for event in self.driver.events
            if event[0] == "copy_text" and event[1].endswith("body")
        ]
        self.assertEqual(sent_bodies, ["new body"])

    def test_old_generation_cannot_capture_new_sender(self):
        old_sender = object()
        new_sender = object()
        with sender_module.state.run_lock:
            sender_module.state.lifecycle_generation = 100
            sender_module.state.running = True
            sender_module.state.sender_instance = old_sender
        self.assertIs(
            sender_module.state.get_sender_for_generation(100),
            old_sender,
        )

        with sender_module.state.run_lock:
            sender_module.state.lifecycle_generation = 101
            sender_module.state.sender_instance = new_sender

        self.assertIsNone(
            sender_module.state.get_sender_for_generation(100)
        )
        self.assertIs(
            sender_module.state.get_sender_for_generation(101),
            new_sender,
        )
        self.assertFalse(
            sender_module.state.deactivate_generation(100)
        )
        self.assertTrue(sender_module.state.running)
        self.assertEqual(sender_module.state.lifecycle_generation, 101)

    def test_text_clipboard_is_cleared_only_while_still_bot_owned(self):
        sender = self._sender()

        def exercise(user_replacement=None):
            clipboard_value = {"value": None}
            clipboard = types.ModuleType("pyperclip")
            clipboard.copy = lambda value: clipboard_value.__setitem__(
                "value", value
            )
            clipboard.paste = lambda: clipboard_value["value"]
            original_hotkey = self.driver.hotkey_ctrl

            def hotkey(virtual_key):
                original_hotkey(virtual_key)
                if virtual_key == sender_module.VK_V and user_replacement:
                    clipboard_value["value"] = user_replacement

            self.driver.hotkey_ctrl = hotkey
            try:
                with mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
                    sender._paste_text("bot-owned")
            finally:
                self.driver.hotkey_ctrl = original_hotkey
            return clipboard_value["value"]

        self.assertEqual(exercise(), "")
        self.assertEqual(exercise("user-owned"), "user-owned")

    def test_cancel_while_paused_drops_only_current_fifo_item(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.01)),
            pre_paste_preview_delay=0.1,
            pre_send_delay=0,
        )
        results = {}

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            first = threading.Thread(
                target=lambda: results.setdefault(
                    "first", sender.send_text("private-contact", "first body")
                )
            )
            second = threading.Thread(
                target=lambda: results.setdefault(
                    "second", sender.send_text("private-contact", "second body")
                )
            )
            first.start()
            self.assertTrue(self._wait_for_preview("before_paste"))
            first_id = sender_module.state.get_send_preview()["preview_id"]
            second.start()
            sender_module.state.paused.set()
            self.assertTrue(
                sender_module.state.cancel_current_preview(first_id)
            )
            first.join(2)
            self.assertTrue(
                self._wait_for_preview("paused", content="second body")
            )
            self.assertFalse(
                sender_module.state.cancel_current_preview(first_id)
            )
            sender_module.state.paused.clear()
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, {"first": False, "second": True})
        self.assertEqual(
            sum(
                event[:2] == ("click", "send_button")
                for event in self.driver.events
            ),
            1,
        )

    def test_cancel_after_paste_clears_input_without_submitting(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=lambda seconds: time.sleep(min(seconds, 0.01)),
            pre_paste_preview_delay=0,
            pre_send_delay=0.5,
        )
        results = []

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), self._clipboard_boundary(lambda value: self.driver.events.append(("copy_text", value))):
            worker = threading.Thread(
                target=lambda: results.append(
                    sender.send_text("private-contact", "cancel body")
                )
            )
            worker.start()
            self.assertTrue(self._wait_for_preview("pasted_waiting"))
            preview_id = sender_module.state.get_send_preview()["preview_id"]
            self.assertTrue(
                sender_module.state.cancel_current_preview(preview_id)
            )
            worker.join(2)

        self.assertEqual(results, [False])
        self.assertEqual(
            sum(
                event[:2] == ("click", "message_input")
                for event in self.driver.events
            ),
            2,
        )
        self.assertFalse(
            any(
                event[:2] == ("click", "send_button")
                for event in self.driver.events
            )
        )

    def test_submitting_preview_cannot_report_cancelled(self):
        cancel_event = sender_module.state.begin_send_preview("committed")
        preview_id = sender_module.state.get_send_preview()["preview_id"]

        self.assertTrue(sender_module.state.try_commit_send(cancel_event))
        self.assertFalse(
            sender_module.state.cancel_current_preview(preview_id)
        )
        self.assertEqual(
            sender_module.state.get_send_preview()["stage"],
            "submitting",
        )
        sender_module.state.end_send_preview(cancel_event)

    def _wait_for_preview(self, stage, content=None, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            preview = sender_module.state.get_send_preview()
            if (
                preview is not None
                and preview.get("stage") == stage
                and (content is None or preview.get("content") == content)
            ):
                return True
            time.sleep(0.005)
        return False

    def test_sender_source_has_no_foreground_activation_or_enter_send_branch(self):
        source = (BRIDGE / "uia_fixed_sender.py").read_text(encoding="utf-8")

        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("use_enter_to_send", source)
        self.assertNotIn("press_key(0x0D)", source)
        self.assertNotIn("VK_RETURN", source)


if __name__ == "__main__":
    unittest.main()
