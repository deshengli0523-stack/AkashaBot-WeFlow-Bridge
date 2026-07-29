import copy
import logging
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import uia_fixed_sender as sender_module
import favorite_sticker as favorite_module
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

    def click_calibrated_search_result(self, hwnd, point):
        name = self._point_names[(point["x"], point["y"])]
        self.events.append(("click", name, hwnd))

    def hotkey_ctrl(self, virtual_key):
        self.events.append(("hotkey_ctrl", virtual_key))

    def press_key(self, virtual_key):
        self.events.append(("press_key", virtual_key))

    def move_bound_process_ratio(self, hwnd, point):
        self.events.append(("move_bound", hwnd, dict(point)))

    def press_key_bound_process(self, hwnd, virtual_key):
        self.events.append(("press_key_bound", hwnd, virtual_key))

    def copy_image_to_clipboard(self, image_path):
        self.events.append(("copy_image", image_path))


class UiaFixedSenderTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.sleep_calls = []
        sender_module.state.running = True
        sender_module.state.paused.clear()
        sender_module.state.sender_instance = None
        if hasattr(sender_module.state, "clear_last_send_result"):
            sender_module.state.clear_last_send_result()
        with sender_module.state.send_preview_lock:
            sender_module.state.current_send_cancel_event = None
            sender_module.state.current_send_preview = None

    def tearDown(self):
        sender_module.state.running = False
        sender_module.state.paused.clear()

    def _sender(self, driver=None, **changes):
        kwargs = {
            "calibration": copy.deepcopy(VALID_CALIBRATION),
            "driver": driver or self.driver,
            "sleep_fn": self.sleep_calls.append,
            "pre_paste_preview_delay": changes.get("pre_paste_preview_delay", 0),
            "pre_send_delay": changes.get("pre_send_delay", 0),
            "settle_jitter_max_seconds": changes.get(
                "settle_jitter_max_seconds",
                0,
            ),
        }
        if "rng" in changes:
            kwargs["rng"] = changes["rng"]
        for name in (
            "monotonic_fn",
            "favorite_state_dir",
            "favorite_receipt",
        ):
            if name in changes:
                kwargs[name] = changes[name]
        return sender_module.UiaFixedSender(**kwargs)

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

    def test_default_post_paste_delay_range_is_three_to_five_seconds(self):
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=self.sleep_calls.append,
        )

        self.assertEqual(sender.pre_send_delay, 5.0)
        with mock.patch.object(
            sender_module.random,
            "uniform",
            return_value=4.25,
        ) as uniform:
            self.assertEqual(sender._next_pre_send_delay(), 4.25)
        uniform.assert_called_once_with(3.0, 5.0)

    def test_settle_jitter_is_injected_at_click_and_pre_paste_boundaries(self):
        class FixedRng:
            def __init__(self):
                self.calls = []

            def uniform(self, minimum, maximum):
                self.calls.append((minimum, maximum))
                return 0.2

        rng = FixedRng()
        sender = self._sender(
            settle_jitter_max_seconds=0.3,
            rng=rng,
        )

        sent = self._send_text(sender)

        self.assertIs(sent, True)
        self.assertGreaterEqual(rng.calls.count((0.0, 0.3)), 5)
        self.assertIn(0.32, self.sleep_calls)
        self.assertIn(0.2, self.sleep_calls)
        self.assertIn(
            0.50,
            self.sleep_calls,
            "clipboard ownership retention must remain unchanged",
        )

    def test_text_jitter_precedes_clipboard_and_cancel_prevents_copy(self):
        class FixedRng:
            def uniform(self, minimum, maximum):
                return 0.2

        cancel_event = threading.Event()
        ordered_events = []

        def pause(seconds):
            ordered_events.append(("sleep", seconds))
            if seconds == 0.2:
                cancel_event.set()

        clipboard = types.ModuleType("pyperclip")
        clipboard.copy = lambda value: ordered_events.append(("copy", value))
        clipboard.paste = lambda: ""
        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=pause,
            pre_paste_preview_delay=0,
            pre_send_delay=0,
            settle_jitter_max_seconds=0.3,
            rng=FixedRng(),
        )

        with mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
            pasted = sender._paste_text(
                "private-body",
                before_paste=lambda: not cancel_event.is_set(),
            )

        self.assertIs(pasted, False)
        self.assertEqual(ordered_events, [("sleep", 0.2)])
        self.assertNotIn(("hotkey_ctrl", sender_module.VK_V), self.driver.events)

    def test_text_pause_during_jitter_waits_and_resumes_same_item(self):
        class SequenceRng:
            def __init__(self):
                self.values = iter(
                    (0.0, 0.0, 0.0, 0.0, 0.2, 0.0, 0.0)
                )

            def uniform(self, minimum, maximum):
                return next(self.values)

        paused_once = {"value": False}
        resumed_wait = {"value": False}

        def pause(seconds):
            self.sleep_calls.append(seconds)
            if seconds == 0.2 and not paused_once["value"]:
                paused_once["value"] = True
                sender_module.state.paused.set()
            elif sender_module.state.paused.is_set():
                resumed_wait["value"] = True
                sender_module.state.paused.clear()

        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=pause,
            pre_paste_preview_delay=0,
            pre_send_delay=0,
            settle_jitter_max_seconds=0.3,
            rng=SequenceRng(),
        )

        sent = self._send_text(sender)

        self.assertIs(sent, True)
        self.assertTrue(paused_once["value"])
        self.assertTrue(resumed_wait["value"])
        self.assertEqual(
            sum(
                event == ("copy_text", "private-body")
                for event in self.driver.events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event[:2] == ("click", "send_button")
                for event in self.driver.events
            ),
            1,
        )

    def test_pause_after_body_copy_clears_clipboard_before_retry(self):
        clipboard_value = {"value": ""}
        paused_once = {"value": False}
        cleared_before_resume = {"value": False}
        clipboard = types.ModuleType("pyperclip")

        def copy_text(value):
            clipboard_value["value"] = value
            self.driver.events.append(("copy_text", value))

        clipboard.copy = copy_text
        clipboard.paste = lambda: clipboard_value["value"]

        def pause(seconds):
            self.sleep_calls.append(seconds)
            if (
                seconds == 0.05
                and clipboard_value["value"] == "private-body"
                and not paused_once["value"]
            ):
                paused_once["value"] = True
                sender_module.state.paused.set()
            elif sender_module.state.paused.is_set():
                cleared_before_resume["value"] = (
                    clipboard_value["value"] == ""
                )
                sender_module.state.paused.clear()

        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=pause,
            pre_paste_preview_delay=0,
            pre_send_delay=0,
            settle_jitter_max_seconds=0,
        )

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
            sent = sender.send_text("private-contact", "private-body")

        self.assertIs(sent, True)
        self.assertTrue(paused_once["value"])
        self.assertTrue(cleared_before_resume["value"])
        self.assertEqual(
            sum(
                event == ("copy_text", "private-body")
                for event in self.driver.events
            ),
            2,
            "the same FIFO item should retry once after resume",
        )
        self.assertEqual(
            sum(
                event == ("hotkey_ctrl", sender_module.VK_V)
                for event in self.driver.events
            ),
            2,
            "the aborted body copy must not send an extra paste hotkey",
        )

    def test_rapid_pause_resume_uses_stable_retry_reason(self):
        sender = self._sender()
        original_paste = sender._paste_text
        blocked_once = {"value": False}

        def paste_with_fast_resume(value, before_paste=None):
            if before_paste is not None and not blocked_once["value"]:
                blocked_once["value"] = True
                sender_module.state.paused.set()
                try:
                    self.assertIs(before_paste(), False)
                finally:
                    sender_module.state.paused.clear()
                return False
            return original_paste(value, before_paste=before_paste)

        with mock.patch.object(
            sender,
            "_paste_text",
            side_effect=paste_with_fast_resume,
        ):
            sent = self._send_text(sender)

        self.assertIs(sent, True)
        self.assertTrue(blocked_once["value"])
        self.assertEqual(
            sender_module.state.get_last_send_result()["status"],
            "sent",
        )

    def test_image_jitter_stop_prevents_clipboard_copy_and_paste(self):
        class SequenceRng:
            def __init__(self):
                self.values = iter((0.0, 0.0, 0.0, 0.0, 0.2))

            def uniform(self, minimum, maximum):
                return next(self.values)

        def pause(seconds):
            self.sleep_calls.append(seconds)
            if seconds == 0.2:
                sender_module.state.running = False

        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=self.driver,
            sleep_fn=pause,
            pre_paste_preview_delay=0,
            pre_send_delay=0,
            settle_jitter_max_seconds=0.3,
            rng=SequenceRng(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            image_path = str(pathlib.Path(temporary) / "private-image.png")
            pathlib.Path(image_path).write_bytes(b"fake")
            with mock.patch.object(
                sender_module,
                "validate_runtime_metrics",
                side_effect=self._runtime_validation,
            ), self._clipboard_boundary(
                lambda value: self.driver.events.append(("copy_text", value))
            ):
                sent = sender.send_image("private-contact", image_path)

        self.assertIs(sent, False)
        self.assertFalse(
            any(event[0] == "copy_image" for event in self.driver.events),
            self.driver.events,
        )
        self.assertEqual(
            sum(
                event == ("hotkey_ctrl", sender_module.VK_V)
                for event in self.driver.events
            ),
            1,
            "only the contact search paste may occur before the stop",
        )

    def test_text_send_uses_calibrated_first_result_without_ocr(self):
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

    def test_image_send_uses_calibrated_first_result_without_ocr(self):
        sender = self._sender(
            pre_send_delay=5,
            settle_jitter_max_seconds=0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            image_path = str(pathlib.Path(temporary) / "private-image-path.png")
            pathlib.Path(image_path).write_bytes(b"fake-driver-does-not-open-this")

            with mock.patch.object(
                sender_module,
                "validate_runtime_metrics",
                side_effect=self._runtime_validation,
            ), self._clipboard_boundary(
                lambda value: self.driver.events.append(("copy_text", value))
            ), mock.patch.object(
                sender,
                "_wait_for_review",
                return_value=True,
            ) as review, mock.patch.object(
                sender_module.random,
                "uniform",
                return_value=4.0,
            ) as uniform:
                sent = sender.send_image("private-contact", image_path)

        self.assertIs(sent, True)
        self.assertEqual(review.call_count, 1)
        self.assertEqual(review.call_args.args[0], 4)
        self.assertEqual(review.call_args.args[2], "pasted_waiting")
        uniform.assert_called_once_with(3.0, 5.0)
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

    def test_transient_foreground_loss_at_submit_retains_queue_head(self):
        class InterruptedDriver(FakeDriver):
            def __init__(self):
                super().__init__()
                self.submit_interrupted = False
                self.recovery_checks = 0

            def click_ratio(self, hwnd, point):
                name = self._point_names[(point["x"], point["y"])]
                if name == "send_button" and not self.submit_interrupted:
                    self.events.append(("click_blocked", name, hwnd))
                    self.submit_interrupted = True
                    self.metrics = valid_metrics(foreground=False)
                    raise CalibrationError(CALIBRATION_WINDOW)
                super().click_ratio(hwnd, point)

            def get_client_metrics(self, hwnd):
                self.events.append(("get_metrics", hwnd))
                if self.submit_interrupted:
                    self.recovery_checks += 1
                    if self.recovery_checks >= 2:
                        self.metrics = valid_metrics(foreground=True)
                return self.metrics

        driver = InterruptedDriver()
        self.driver = driver
        sender = self._sender(driver)

        with self.assertLogs("weflow-bridge", logging.WARNING) as captured:
            sent = self._send_text(sender)

        self.assertIs(sent, True, driver.events)
        self.assertEqual(
            captured.output,
            [
                "WARNING:weflow-bridge:[UIA_FIXED] foreground lost "
                "before submit; queue head retained"
            ],
        )
        self.assertLess(
            driver.events.index(("click_blocked", "send_button", 101)),
            driver.events.index(("click", "send_button", 101)),
        )
        self.assertEqual(
            sum(
                event == ("copy_text", "private-body")
                for event in driver.events
            ),
            1,
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

    def test_sender_failure_publishes_safe_reason_for_control_panel(self):
        self.driver.metrics = valid_metrics(foreground=False)
        sender = self._sender()

        with self.assertLogs("weflow-bridge", logging.ERROR):
            sent = self._send_text(
                sender,
                contact="private-contact",
                text="private-body",
            )

        self.assertIs(sent, False)
        result = sender_module.state.get_last_send_result()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], CALIBRATION_WINDOW)
        self.assertEqual(result["stage"], "preflight")
        self.assertTrue(result["message"])
        serialized = str(result)
        self.assertNotIn("private-contact", serialized)
        self.assertNotIn("private-body", serialized)

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
        self.assertEqual(
            sender_module.state.get_last_send_result()["code"],
            "E_UIA_CONTACT_SELECTION_FAILED",
        )

    def test_new_preview_clears_stale_send_result(self):
        sender_module.state.record_send_result(
            False,
            code="E_UIA_SEND_FAILED",
            stage="submit",
        )

        cancel_event = sender_module.state.begin_send_preview(
            "private-contact",
            "private-body",
        )
        try:
            self.assertIsNone(sender_module.state.get_last_send_result())
        finally:
            sender_module.state.end_send_preview(cancel_event)

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

    def test_all_sender_instances_share_one_complete_send_queue(self):
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
        first_sender = self._sender(driver)
        second_sender = self._sender(driver)

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
                run(lambda: second_sender.send_image("second", image_path))

            first = threading.Thread(
                target=run,
                args=(lambda: first_sender.send_text("first", "one"),),
            )
            second = threading.Thread(
                target=run_second,
            )

            with mock.patch.object(
                sender_module,
                "validate_runtime_metrics",
                side_effect=self._runtime_validation,
            ), self._clipboard_boundary(
                lambda value: driver.events.append(("copy_text", value))
            ):
                first.start()
                self.assertTrue(first_entered.wait(1))
                second.start()
                self.assertTrue(second_attempted.wait(1))
                try:
                    second.join(0.05)
                    self.assertTrue(
                        second.is_alive(),
                        "image send completed while text send held the global queue",
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
        searched_contacts = [
            event[1]
            for event in driver.events
            if event[0] == "copy_text" and event[1] in {"first", "second"}
        ]
        self.assertEqual(
            searched_contacts,
            ["first", "second"],
        )

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

    def test_text_clipboard_remains_available_for_delayed_target_read(self):
        clipboard_value = {"value": ""}
        delayed_reads = []
        paste_injected = {"value": False}
        clipboard = types.ModuleType("pyperclip")
        clipboard.copy = lambda value: clipboard_value.__setitem__(
            "value", value
        )
        clipboard.paste = lambda: clipboard_value["value"]

        class DelayedReadDriver(FakeDriver):
            def hotkey_ctrl(self, virtual_key):
                super().hotkey_ctrl(virtual_key)
                if virtual_key == sender_module.VK_V:
                    paste_injected["value"] = True

        driver = DelayedReadDriver()

        def deterministic_sleep(seconds):
            self.sleep_calls.append(seconds)
            if paste_injected["value"] and seconds >= 0.50:
                delayed_reads.append(clipboard_value["value"])
                paste_injected["value"] = False

        sender = sender_module.UiaFixedSender(
            calibration=copy.deepcopy(VALID_CALIBRATION),
            driver=driver,
            sleep_fn=deterministic_sleep,
            pre_paste_preview_delay=0,
            pre_send_delay=0,
        )

        with mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
            self.assertTrue(sender._paste_text("delayed-body"))

        self.assertEqual(delayed_reads, ["delayed-body"])
        self.assertEqual(clipboard_value["value"], "")
        self.assertIn(0.50, self.sleep_calls)

    def test_cancel_after_body_copy_prevents_body_paste_and_send(self):
        sender = self._sender()
        clipboard_value = {"value": ""}
        clipboard = types.ModuleType("pyperclip")

        def copy_text(value):
            clipboard_value["value"] = value
            self.driver.events.append(("copy_text", value))
            if value == "private-body":
                cancel_event = sender_module.state.current_send_cancel_event
                self.assertIsNotNone(cancel_event)
                cancel_event.set()

        clipboard.copy = copy_text
        clipboard.paste = lambda: clipboard_value["value"]

        with mock.patch.object(
            sender_module,
            "validate_runtime_metrics",
            side_effect=self._runtime_validation,
        ), mock.patch.dict(sys.modules, {"pyperclip": clipboard}):
            sent = sender.send_text("private-contact", "private-body")

        self.assertFalse(sent)
        self.assertEqual(
            sum(
                event == ("hotkey_ctrl", sender_module.VK_V)
                for event in self.driver.events
            ),
            1,
        )
        self.assertFalse(
            any(
                event[:2] == ("click", "send_button")
                for event in self.driver.events
            )
        )
        self.assertEqual(
            sender_module.state.get_last_send_result()["code"],
            "E_UIA_SEND_CANCELLED",
        )

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
        cancel_event = sender_module.state.begin_send_preview(
            "target-contact", "committed"
        )
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

    def test_favorite_sticker_clicks_fixed_slot_without_visual_capture(self):
        events = []

        class FixedRng:
            def __init__(self):
                self.calls = []

            def uniform(self, minimum, maximum):
                self.calls.append((minimum, maximum))
                return (minimum + maximum) / 2

        class FavoriteDriver(FakeDriver):
            def click_bound_process_ratio(self, hwnd, point):
                events.append(("bound_click", dict(point)))

            def capture_bound_process_client(self, hwnd):
                raise AssertionError("fixed-slot sending must not capture frames")

            def move_bound_process_ratio(self, hwnd, point):
                raise AssertionError("fixed-slot sending must not move for capture")

            def press_key_bound_process(self, hwnd, virtual_key):
                events.append(("guarded_key", virtual_key))

        class Layout:
            manifest = {
                "reference": {
                    "dpi": 96,
                    "aspect_ratio": 1.5,
                },
                "points": {
                    "smile_entry": {"x": 0.60, "y": 0.80},
                    "favorite_tab": {"x": 0.22, "y": 0.88},
                    "grid_first": {"x": 0.10, "y": 0.20},
                    "grid_last": {"x": 0.90, "y": 0.80},
                },
            }

            def point(self, sticker_key):
                events.append(("fixed_point", sticker_key))
                return {"x": 0.33, "y": 0.44}

        class Receipt:
            def baseline(self, session):
                events.append(("baseline", session))
                return frozenset()

            def confirm(self, session, baseline):
                events.append(("confirm", session))
                return True

        sender_module._FAVORITE_REQUEST_CACHE = favorite_module.RequestIdCache()
        driver = FavoriteDriver()
        layout = Layout()
        rng = FixedRng()
        sender = self._sender(
            driver=driver,
            favorite_receipt=Receipt(),
            settle_jitter_max_seconds=0.3,
            rng=rng,
        )
        sender._favorite_layout = layout
        sender._select_contact = lambda _hwnd, contact: events.append(
            ("select", contact)
        )
        result = sender.send_favorite_sticker(
            "contact",
            "session",
            "slot_07",
            str(uuid.uuid4()),
            time.monotonic() + 25,
        )

        self.assertTrue(result.confirmed)
        self.assertLess(
            events.index(("baseline", "session")),
            events.index(("select", "contact")),
        )
        self.assertEqual(events.count(("baseline", "session")), 2)
        self.assertEqual(events.count(("fixed_point", "slot_07")), 1)
        self.assertFalse(any(event[0] == "capture" for event in events))
        self.assertFalse(any(event[0] == "move_bound" for event in events))
        self.assertEqual(rng.calls, [(0.8, 1.3), (0.9, 1.5)])
        self.assertIn(1.05, self.sleep_calls)
        self.assertIn(1.2, self.sleep_calls)
        self.assertEqual(events[-1], ("confirm", "session"))
        self.assertEqual(
            sum(
                event[0] == "bound_click"
                and event[1] == {"x": 0.33, "y": 0.44}
                for event in events
            ),
            1,
        )

    def test_favorite_click_exception_is_cached_unknown_and_not_retried(self):
        class Driver(FakeDriver):
            def click_bound_process_ratio(self, _hwnd, point):
                if point == {"x": 0.33, "y": 0.44}:
                    self.events.append(("target_click",))
                    raise CalibrationError(CALIBRATION_WINDOW)

            def capture_bound_process_client(self, _hwnd):
                return object()

            def press_key_bound_process(self, _hwnd, _virtual_key):
                self.events.append(("guarded_escape",))

        layout = types.SimpleNamespace(
            manifest={
                "reference": {"dpi": 96, "aspect_ratio": 1.5},
                "points": {
                    "smile_entry": {"x": 0.60, "y": 0.80},
                    "favorite_tab": {"x": 0.22, "y": 0.88},
                },
            },
            point=lambda _key: {"x": 0.33, "y": 0.44},
        )
        receipt = types.SimpleNamespace(
            baseline=lambda _session: frozenset(),
            confirm=lambda _session, _baseline: False,
        )
        sender_module._FAVORITE_REQUEST_CACHE = favorite_module.RequestIdCache()
        driver = Driver()
        sender = self._sender(driver=driver, favorite_receipt=receipt)
        sender._favorite_layout = layout
        sender._select_contact = lambda *_args: None
        request_id = str(uuid.uuid4())

        first = sender.send_favorite_sticker(
            "contact",
            "session",
            "slot_01",
            request_id,
            time.monotonic() + 25,
        )
        second = sender.send_favorite_sticker(
            "contact",
            "session",
            "slot_01",
            request_id,
            time.monotonic() + 25,
        )

        self.assertTrue(first.committed)
        self.assertEqual(
            first.error_code,
            favorite_module.STICKER_COMMIT_UNKNOWN,
        )
        self.assertTrue(second.cached)
        self.assertEqual(driver.events.count(("target_click",)), 1)

    def test_expired_favorite_review_never_touches_wechat(self):
        class Clock:
            def __init__(self):
                self.value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        sender_module._FAVORITE_REQUEST_CACHE = favorite_module.RequestIdCache()
        driver = FakeDriver()
        sender = sender_module.UiaFixedSender(
            copy.deepcopy(VALID_CALIBRATION),
            driver=driver,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            pre_paste_preview_delay=60,
            pre_send_delay=60,
            settle_jitter_max_seconds=0,
        )
        result = sender.send_favorite_sticker(
            "contact",
            "session",
            "slot_01",
            str(uuid.uuid4()),
            5.0,
        )

        self.assertEqual(
            result.error_code,
            favorite_module.STICKER_QUEUE_EXPIRED,
        )
        self.assertEqual(driver.events, [])

    def test_expired_fifo_ticket_is_skipped_for_the_next_sender(self):
        lock = sender_module._FifoSendLock()
        ticks = iter((0.0, 2.0, 2.0, 2.0))

        with lock:
            with lock.reserve_normal(
                deadline=1.0,
                monotonic=lambda: next(ticks),
            ) as acquired:
                self.assertFalse(acquired)
        with lock:
            self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
