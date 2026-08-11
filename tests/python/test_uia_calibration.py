import contextlib
import copy
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
import uuid
from collections import deque
from dataclasses import replace
from unittest import mock
from PIL import Image, ImageDraw


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))

import calibrate_uia_fixed as calibration_module  # noqa: E402
from calibrate_uia_fixed import (  # noqa: E402
    STEP_LABELS,
    collect_calibration,
    main,
    persist_calibration,
    run_calibration,
)
from uia_support import (  # noqa: E402
    CALIBRATION_BUSY,
    CALIBRATION_INVALID,
    CALIBRATION_REQUIRED,
    CALIBRATION_WINDOW,
    RECALIBRATION_REQUIRED,
    POINT_NAMES,
    CalibrationError,
    ClientMetrics,
)
import favorite_sticker as favorite_module  # noqa: E402


METRICS = ClientMetrics(
    hwnd=10,
    left=100,
    top=50,
    width=1600,
    height=900,
    dpi=120,
    visible=True,
    maximized=True,
    foreground=True,
)

POINTS = {
    "search_box": (260, 140),
    "first_result": (300, 230),
    "message_input": (1060, 770),
    "send_button": (1540, 860),
}

FORBIDDEN_OUTPUT = (
    "100,50",
    "1600x900",
    "dpi=120",
    "0.1",
    "0.2",
    "0.6",
    "0.8",
    "0.9",
)


class FakeDriver:
    def __init__(self, captures, metrics=None):
        self.captures = deque(captures)
        self.metrics = deque(metrics or (METRICS,))
        self.last_metrics = self.metrics[-1]
        self.calls = []

    def prepare_calibration_window(self):
        metrics = self.metrics[0] if self.metrics else self.last_metrics
        hwnd = self.last_metrics.hwnd if isinstance(metrics, BaseException) else metrics.hwnd
        self.calls.append(("prepare", hwnd))
        return hwnd

    def get_client_metrics(self, hwnd):
        metrics = self.metrics.popleft() if self.metrics else self.last_metrics
        if isinstance(metrics, BaseException):
            self.calls.append(("metrics", hwnd, "error"))
            raise metrics
        self.last_metrics = metrics
        self.calls.append(("metrics", hwnd, metrics.hwnd))
        return metrics

    def capture_swallowed_click(self, hwnd):
        self.calls.append(("capture", hwnd))
        outcome = self.captures.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def activate_receive_window(self, hwnd):
        self.calls.append(("activate", hwnd))


def ordered_points(*, left=100, top=50):
    return (
        (left + 160, top + 90),
        (left + 200, top + 180),
        (left + 960, top + 720),
        (left + 1440, top + 810),
    )


class OutputPrivacyMixin:
    def assert_safe_output(self, messages, config_path=None):
        output = "\n".join(str(message) for message in messages)
        for forbidden in FORBIDDEN_OUTPUT:
            self.assertNotIn(forbidden, output)
        if config_path is not None:
            self.assertNotIn(str(pathlib.Path(config_path).resolve()), output)


class CalibrationCollectionTests(OutputPrivacyMixin, unittest.TestCase):
    def collect(self, driver, answer="y"):
        messages = []
        calibration = collect_calibration(
            driver=driver,
            confirm_fn=lambda _prompt: answer,
            output_fn=messages.append,
        )
        self.assert_safe_output(messages)
        return calibration, messages

    def test_captures_fixed_four_step_order_with_fresh_metrics(self):
        driver = FakeDriver(ordered_points())

        calibration, messages = self.collect(driver)

        self.assertEqual(tuple(calibration["points"]), POINT_NAMES)
        successful_labels = [
            label
            for message in messages
            for label in STEP_LABELS.values()
            if "成功" in message and label in message
        ]
        self.assertEqual(successful_labels, list(STEP_LABELS.values()))
        capture_indices = [
            index for index, call in enumerate(driver.calls) if call[0] == "capture"
        ]
        self.assertEqual(len(capture_indices), 4)
        for index in capture_indices:
            self.assertEqual(driver.calls[index - 1][0], "metrics")
            self.assertEqual(driver.calls[index - 2][0], "prepare")

    def test_window_error_retries_the_same_step_without_advancing(self):
        driver = FakeDriver(
            (
                CalibrationError(CALIBRATION_WINDOW),
                *ordered_points(),
            )
        )

        calibration, messages = self.collect(driver)

        self.assertIsNotNone(calibration)
        search_success = [
            message
            for message in messages
            if "成功" in message and STEP_LABELS["search_box"] in message
        ]
        self.assertEqual(len(search_success), 1)
        self.assertTrue(
            any("失败" in message and CALIBRATION_WINDOW in message for message in messages)
        )
        self.assertEqual(
            sum(call[0] == "capture" for call in driver.calls),
            5,
        )

    def test_metrics_window_error_retries_current_step_without_losing_points(self):
        driver = FakeDriver(
            (*ordered_points(), (1500, 800)),
            metrics=(
                METRICS,
                CalibrationError(CALIBRATION_WINDOW),
                METRICS,
            ),
        )

        calibration, messages = self.collect(driver)

        self.assertIsNotNone(calibration)
        self.assertEqual(sum(call[0] == "capture" for call in driver.calls), 4)
        successful_labels = [
            label
            for message in messages
            for label in STEP_LABELS.values()
            if "成功" in message and label in message
        ]
        self.assertEqual(successful_labels, list(STEP_LABELS.values()))
        self.assertTrue(
            any("失败" in message and CALIBRATION_WINDOW in message for message in messages)
        )

    def test_any_capture_signature_change_discards_all_points(self):
        changes = {
            "hwnd": replace(METRICS, hwnd=20),
            "left": replace(METRICS, left=120),
            "top": replace(METRICS, top=70),
            "width": replace(METRICS, width=1500),
            "height": replace(METRICS, height=850),
            "dpi": replace(METRICS, dpi=144),
        }
        for field, changed in changes.items():
            with self.subTest(field=field):
                new_points = ordered_points(left=changed.left, top=changed.top)
                driver = FakeDriver(
                    (
                        POINTS["search_box"],
                        POINTS["first_result"],
                        *new_points,
                    ),
                    metrics=(METRICS, METRICS, changed),
                )

                calibration, messages = self.collect(driver)

                success_labels = [
                    label
                    for message in messages
                    for label in STEP_LABELS.values()
                    if "成功" in message and label in message
                ]
                self.assertEqual(
                    success_labels,
                    [
                        STEP_LABELS["search_box"],
                        STEP_LABELS["first_result"],
                        *STEP_LABELS.values(),
                    ],
                )
                self.assertEqual(calibration["reference"]["dpi"], changed.dpi)
                self.assertEqual(
                    calibration["reference"]["client_width"], changed.width
                )
                self.assertTrue(
                    any(
                        "失败" in message and CALIBRATION_WINDOW in message
                        for message in messages
                    )
                )

    def test_final_signature_check_restarts_after_last_click_drift(self):
        changed = replace(METRICS, dpi=144)
        new_points = ordered_points()
        driver = FakeDriver(
            (*ordered_points(), *new_points),
            metrics=(METRICS, METRICS, METRICS, METRICS, changed),
        )

        calibration, messages = self.collect(driver)

        self.assertEqual(sum(call[0] == "capture" for call in driver.calls), 8)
        self.assertEqual(calibration["reference"]["dpi"], 144)
        self.assertTrue(
            any("失败" in message and CALIBRATION_WINDOW in message for message in messages)
        )

    def test_escape_cancels_without_asking_for_confirmation(self):
        confirmation_calls = []
        messages = []
        driver = FakeDriver((None,))

        calibration = collect_calibration(
            driver=driver,
            confirm_fn=lambda prompt: confirmation_calls.append(prompt),
            output_fn=messages.append,
        )

        self.assertIsNone(calibration)
        self.assertEqual(confirmation_calls, [])
        self.assert_safe_output(messages)

    def test_confirmation_accepts_only_trimmed_case_insensitive_y_or_yes(self):
        for answer in ("y", "Y", " yes ", "YeS"):
            with self.subTest(answer=answer):
                calibration, _messages = self.collect(
                    FakeDriver(ordered_points()), answer=answer
                )
                self.assertIsNotNone(calibration)

        for answer in ("", "n", "是", "1", "y e s"):
            with self.subTest(answer=answer):
                calibration, _messages = self.collect(
                    FakeDriver(ordered_points()), answer=answer
                )
                self.assertIsNone(calibration)

    def test_confirmation_reactivates_exact_calibrated_window_when_requested(self):
        driver = FakeDriver(ordered_points())
        messages = []

        calibration = collect_calibration(
            driver,
            confirm_fn=lambda _prompt: "y",
            output_fn=messages.append,
            reactivate_after_confirm=True,
        )

        self.assertIsNotNone(calibration)
        self.assertEqual(driver.calls[-1], ("activate", METRICS.hwnd))
        self.assertTrue(any("正在切回目标微信窗口" in item for item in messages))


class CalibrationPersistenceTests(OutputPrivacyMixin, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.backup_dir = self.root / "backups"
        self.original = {
            "access_token": "opaque-token-value",
            "其他键": {"保留": True},
            "uia_fixed_calibration": {"completed": False},
        }
        self.original_bytes = json.dumps(
            self.original, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.config_path.write_bytes(self.original_bytes)
        self.calibration = collect_calibration(
            FakeDriver(ordered_points()),
            confirm_fn=lambda _prompt: "y",
            output_fn=lambda _message: None,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_confirmed_calibration_replaces_only_target_and_backs_up_first(self):
        replace_observations = []

        def observing_replace(source, destination):
            backups = list(self.backup_dir.rglob("config.json"))
            replace_observations.append(
                (pathlib.Path(source), pathlib.Path(destination), backups)
            )
            os.replace(source, destination)

        with mock.patch(
            "calibrate_uia_fixed.os.fsync", wraps=os.fsync
        ) as fsync_spy:
            backup_path = pathlib.Path(
                persist_calibration(
                    config_path=str(self.config_path),
                    backup_dir=str(self.backup_dir),
                    calibration=self.calibration,
                    replace_fn=observing_replace,
                )
            )

        self.assertEqual(backup_path.read_bytes(), self.original_bytes)
        self.assertEqual(backup_path.name, self.config_path.name)
        self.assertEqual(backup_path.parent.parent, self.backup_dir)
        self.assertRegex(backup_path.parent.name, r"^\d{8}-\d{6}-\d{3}$")
        self.assertEqual(len(replace_observations), 1)
        source, destination, backups_at_replace = replace_observations[0]
        self.assertEqual(source.parent, self.config_path.parent)
        self.assertEqual(destination, self.config_path)
        self.assertEqual(backups_at_replace, [backup_path])
        self.assertGreaterEqual(fsync_spy.call_count, 2)

        saved_bytes = self.config_path.read_bytes()
        self.assertTrue(saved_bytes.endswith(b"\n"))
        self.assertIn("其他键".encode("utf-8"), saved_bytes)
        self.assertNotIn(b"\\u5176\\u4ed6", saved_bytes)
        saved = json.loads(saved_bytes)
        self.assertEqual(saved["access_token"], self.original["access_token"])
        self.assertEqual(saved["其他键"], self.original["其他键"])
        self.assertEqual(saved["uia_fixed_calibration"], self.calibration)
        self.assertEqual(set(saved), set(self.original))

    def test_backup_timestamp_collision_uses_uuid_suffix_without_overwrite(self):
        fixed_stamp = "20260717-160102-345"
        with mock.patch(
            "calibrate_uia_fixed._backup_stamp", return_value=fixed_stamp
        ), mock.patch(
            "calibrate_uia_fixed._uuid_hex",
            side_effect=("0123456789abcdef0123456789abcdef",),
        ):
            first_backup = pathlib.Path(
                persist_calibration(
                    str(self.config_path),
                    str(self.backup_dir),
                    self.calibration,
                )
            )
            first_bytes = first_backup.read_bytes()
            second_backup = pathlib.Path(
                persist_calibration(
                    str(self.config_path),
                    str(self.backup_dir),
                    self.calibration,
                )
            )

        self.assertEqual(first_backup.parent.name, fixed_stamp)
        self.assertEqual(
            second_backup.parent.name,
            fixed_stamp + "-01234567",
        )
        self.assertEqual(first_backup.read_bytes(), first_bytes)
        self.assertNotEqual(first_backup, second_backup)

    def test_replace_failure_preserves_original_and_removes_same_directory_temp(self):
        replace_calls = []

        def failing_replace(source, destination):
            replace_calls.append((pathlib.Path(source), pathlib.Path(destination)))
            raise OSError("injected replacement failure")

        with self.assertRaises(OSError):
            persist_calibration(
                config_path=str(self.config_path),
                backup_dir=str(self.backup_dir),
                calibration=self.calibration,
                replace_fn=failing_replace,
            )

        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertEqual(len(replace_calls), 1)
        temporary_path, destination = replace_calls[0]
        self.assertEqual(destination, self.config_path)
        self.assertEqual(temporary_path.parent, self.config_path.parent)
        self.assertRegex(temporary_path.name, r"^[.]config[.]json[.][0-9a-f]{32}[.]tmp$")
        self.assertFalse(temporary_path.exists())
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["backups", "config.json"],
        )
        backups = list(self.backup_dir.rglob("config.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_bytes)

    def test_temp_name_collision_never_deletes_an_unowned_file(self):
        collision_hex = "f" * 32
        collision = self.root / f".config.json.{collision_hex}.tmp"
        sentinel_bytes = b"unowned sentinel"
        collision.write_bytes(sentinel_bytes)
        uuid_value = mock.Mock(hex=collision_hex)

        with mock.patch(
            "calibrate_uia_fixed.uuid.uuid4", return_value=uuid_value
        ), self.assertRaises(FileExistsError):
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
            )

        self.assertTrue(collision.exists())
        self.assertEqual(collision.read_bytes(), sentinel_bytes)
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)

    def test_successful_replace_does_not_delete_recreated_temp_path(self):
        recreated_bytes = b"replacement-owned sentinel"
        observed_temp = []

        def replace_and_recreate(source, destination):
            source_path = pathlib.Path(source)
            observed_temp.append(source_path)
            os.replace(source, destination)
            source_path.write_bytes(recreated_bytes)

        persist_calibration(
            str(self.config_path),
            str(self.backup_dir),
            self.calibration,
            replace_fn=replace_and_recreate,
        )

        self.assertEqual(len(observed_temp), 1)
        self.assertTrue(observed_temp[0].exists())
        self.assertEqual(observed_temp[0].read_bytes(), recreated_bytes)
        self.assertEqual(
            json.loads(self.config_path.read_bytes())["uia_fixed_calibration"],
            self.calibration,
        )

    def test_failed_replace_does_not_delete_recreated_temp_path(self):
        recreated_bytes = b"failure-owned sentinel"
        observed_temp = []
        moved_temp = self.root / "moved-away.tmp"

        def move_recreate_and_fail(source, _destination):
            source_path = pathlib.Path(source)
            observed_temp.append(source_path)
            os.replace(source_path, moved_temp)
            source_path.write_bytes(recreated_bytes)
            raise OSError("injected post-move failure")

        with self.assertRaises(OSError):
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
                replace_fn=move_recreate_and_fail,
            )

        self.assertEqual(len(observed_temp), 1)
        self.assertTrue(observed_temp[0].exists())
        self.assertEqual(observed_temp[0].read_bytes(), recreated_bytes)
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertFalse(moved_temp.exists())

    def test_handle_cleanup_cannot_delete_path_recreated_just_before_delete(self):
        recreated_bytes = b"race-owned sentinel"
        observed_temp = []
        moved_temp = self.root / "moved-during-cleanup.tmp"

        def fail_before_cleanup(source, _destination):
            observed_temp.append(pathlib.Path(source))
            raise OSError("injected replace failure")

        real_mark = getattr(
            calibration_module,
            "_mark_handle_for_deletion",
            lambda _handle: None,
        )

        def replace_path_then_mark(handle):
            temporary = observed_temp[0]
            os.replace(temporary, moved_temp)
            temporary.write_bytes(recreated_bytes)
            real_mark(handle)

        with mock.patch(
            "calibrate_uia_fixed._mark_handle_for_deletion",
            side_effect=replace_path_then_mark,
            create=True,
        ), self.assertRaises(OSError):
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
                replace_fn=fail_before_cleanup,
            )

        self.assertEqual(len(observed_temp), 1)
        self.assertTrue(observed_temp[0].exists())
        self.assertEqual(observed_temp[0].read_bytes(), recreated_bytes)
        self.assertFalse(moved_temp.exists())
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)

    def test_file_index_failure_keeps_orphan_and_returns_fixed_invalid(self):
        replace_spy = mock.Mock()

        with mock.patch(
            "calibrate_uia_fixed._get_handle_identity",
            side_effect=OSError("injected file-index failure"),
            create=True,
        ), self.assertRaises(CalibrationError) as raised:
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
                replace_fn=replace_spy,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
        replace_spy.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        orphaned = list(self.root.glob(".config.json.*.tmp"))
        self.assertEqual(len(orphaned), 1)
        self.assertTrue(orphaned[0].is_file())

    def test_duplicate_handle_failure_cleans_known_owned_temp(self):
        with mock.patch(
            "calibrate_uia_fixed._duplicate_handle",
            side_effect=OSError("injected duplicate failure"),
        ), self.assertRaises(CalibrationError) as raised:
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
        self.assertEqual(list(self.root.glob(".config.json.*.tmp")), [])
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)

    def test_open_osfhandle_failure_cleans_both_owned_handles(self):
        with mock.patch(
            "calibrate_uia_fixed.msvcrt.open_osfhandle",
            side_effect=OSError("injected osfhandle failure"),
        ), self.assertRaises(CalibrationError) as raised:
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
        self.assertEqual(list(self.root.glob(".config.json.*.tmp")), [])
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)

    def test_fdopen_failure_closes_descriptor_and_cleans_owned_temp(self):
        with mock.patch(
            "calibrate_uia_fixed.os.fdopen",
            side_effect=OSError("injected fdopen failure"),
        ), mock.patch(
            "calibrate_uia_fixed.os.close", wraps=os.close
        ) as close_spy, self.assertRaises(CalibrationError) as raised:
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
        close_spy.assert_called_once()
        self.assertEqual(list(self.root.glob(".config.json.*.tmp")), [])
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)

    def test_backup_failure_preserves_original_and_creates_no_temp(self):
        self.backup_dir.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(OSError):
            persist_calibration(
                str(self.config_path),
                str(self.backup_dir),
                self.calibration,
            )

        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["backups", "config.json"],
        )

    def test_non_finite_constants_in_input_json_are_rejected_before_backup(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as root:
                root_path = pathlib.Path(root)
                config_path = root_path / "config.json"
                backup_dir = root_path / "backups"
                original_bytes = (
                    '{"access_token":"opaque","unexpected":' + constant + "}"
                ).encode("utf-8")
                config_path.write_bytes(original_bytes)

                with self.assertRaises(CalibrationError) as raised:
                    persist_calibration(
                        str(config_path),
                        str(backup_dir),
                        self.calibration,
                    )

                self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
                self.assertEqual(config_path.read_bytes(), original_bytes)
                self.assertFalse(backup_dir.exists())
                self.assertEqual(
                    sorted(path.name for path in root_path.iterdir()),
                    ["config.json"],
                )

    def test_non_finite_pending_values_are_rejected_before_backup(self):
        values = (float("nan"), float("inf"), float("-inf"))
        for value in values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                root_path = pathlib.Path(root)
                config_path = root_path / "config.json"
                backup_dir = root_path / "backups"
                original_bytes = b'{"access_token":"opaque","other":true}'
                config_path.write_bytes(original_bytes)
                calibration = copy.deepcopy(self.calibration)
                calibration["unexpected"] = {"value": value}

                with self.assertRaises(CalibrationError) as raised:
                    persist_calibration(
                        str(config_path),
                        str(backup_dir),
                        calibration,
                    )

                self.assertEqual(raised.exception.code, CALIBRATION_INVALID)
                self.assertEqual(config_path.read_bytes(), original_bytes)
                self.assertFalse(backup_dir.exists())
                self.assertEqual(
                    sorted(path.name for path in root_path.iterdir()),
                    ["config.json"],
                )


class CalibrationRunAndMainTests(OutputPrivacyMixin, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.config_path = self.root / "private config.json"
        self.backup_dir = self.root / "backups"
        self.original_bytes = b'{"access_token":"opaque","other":true}'
        self.config_path.write_bytes(self.original_bytes)

    def tearDown(self):
        self.temporary.cleanup()

    def test_run_cancellation_does_not_write_config_or_backup(self):
        messages = []

        result = run_calibration(
            config_path=str(self.config_path),
            backup_dir=str(self.backup_dir),
            driver=FakeDriver((None,)),
            confirm_fn=lambda _prompt: "y",
            output_fn=messages.append,
        )

        self.assertEqual(result, 2)
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertFalse(self.backup_dir.exists())
        self.assert_safe_output(messages, self.config_path)

    def test_run_rejected_confirmation_does_not_write_config_or_backup(self):
        messages = []

        result = run_calibration(
            config_path=str(self.config_path),
            backup_dir=str(self.backup_dir),
            driver=FakeDriver(ordered_points()),
            confirm_fn=lambda _prompt: "n",
            output_fn=messages.append,
        )

        self.assertEqual(result, 2)
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertFalse(self.backup_dir.exists())
        self.assert_safe_output(messages, self.config_path)

    def test_run_success_writes_without_disclosing_path_or_metadata(self):
        messages = []
        driver = FakeDriver(ordered_points())

        result = run_calibration(
            config_path=str(self.config_path),
            backup_dir=str(self.backup_dir),
            driver=driver,
            confirm_fn=lambda _prompt: "yes",
            output_fn=messages.append,
        )

        self.assertEqual(result, 0)
        self.assertFalse(any(call[0] == "activate" for call in driver.calls))
        self.assertTrue(any("成功" in message for message in messages))
        self.assert_safe_output(messages, self.config_path)

    def test_run_reactivates_before_starting_favorite_calibration(self):
        messages = []
        driver = FakeDriver(ordered_points())
        state_dir = self.root / "state"
        state_dir.mkdir()
        favorite_manifest = {"layout": "opaque"}

        def collect_favorite(*_args, **_kwargs):
            self.assertEqual(driver.calls[-1], ("activate", METRICS.hwnd))
            return favorite_manifest

        with mock.patch(
            "calibrate_uia_fixed.collect_favorite_sticker_calibration",
            side_effect=collect_favorite,
        ) as collect_spy, mock.patch(
            "calibrate_uia_fixed.write_favorite_layout"
        ) as write_spy:
            result = run_calibration(
                config_path=str(self.config_path),
                backup_dir=str(self.backup_dir),
                favorite_state_dir=str(state_dir),
                driver=driver,
                confirm_fn=lambda _prompt: "yes",
                output_fn=messages.append,
            )

        self.assertEqual(result, 0)
        collect_spy.assert_called_once()
        write_spy.assert_called_once_with(str(state_dir), favorite_manifest)
        self.assert_safe_output(messages, self.config_path)

    def test_reactivation_failure_writes_nothing(self):
        class FailingActivationDriver(FakeDriver):
            def activate_receive_window(self, hwnd):
                super().activate_receive_window(hwnd)
                raise CalibrationError(CALIBRATION_WINDOW)

        state_dir = self.root / "state"
        state_dir.mkdir()

        with self.assertRaises(CalibrationError) as raised:
            run_calibration(
                config_path=str(self.config_path),
                backup_dir=str(self.backup_dir),
                favorite_state_dir=str(state_dir),
                driver=FailingActivationDriver(ordered_points()),
                confirm_fn=lambda _prompt: "yes",
                output_fn=lambda _message: None,
            )

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertEqual(self.config_path.read_bytes(), self.original_bytes)
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(list(state_dir.iterdir()), [])

    def test_main_maps_only_fixed_error_codes_to_integer_exit_codes(self):
        cases = (
            (CALIBRATION_INVALID, 20),
            (CALIBRATION_WINDOW, 21),
            (CALIBRATION_REQUIRED, 22),
            (CALIBRATION_BUSY, 23),
            (RECALIBRATION_REQUIRED, 24),
        )
        arguments = [
            "--config",
            str(self.config_path),
            "--backup-dir",
            str(self.backup_dir),
        ]
        for code, expected_exit in cases:
            with self.subTest(code=code), mock.patch(
                "calibrate_uia_fixed.run_calibration",
                side_effect=CalibrationError(code),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = main(arguments)
                self.assertEqual(result, expected_exit)
                self.assertEqual(output.getvalue(), code + "\n")
                self.assert_safe_output([output.getvalue()], self.config_path)

    def test_main_hides_unclassified_exception_text_and_unknown_codes(self):
        arguments = [
            "--config",
            str(self.config_path),
            "--backup-dir",
            str(self.backup_dir),
        ]
        failures = (
            RuntimeError("secret exception body"),
            CalibrationError("E_PRIVATE_INTERNAL_DETAIL"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "calibrate_uia_fixed.run_calibration", side_effect=failure
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = main(arguments)
                self.assertEqual(result, 20)
                self.assertEqual(output.getvalue(), CALIBRATION_INVALID + "\n")
                self.assertNotIn(str(failure), output.getvalue())
                self.assert_safe_output([output.getvalue()], self.config_path)

    def test_main_parser_failures_emit_only_invalid_code_and_return_twenty(self):
        cases = (
            (),
            ("--help",),
            ("-h",),
            ("--unknown",),
            ("--config", str(self.config_path)),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch(
                "calibrate_uia_fixed.run_calibration"
            ) as run_spy:
                output = io.StringIO()
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(
                    errors
                ):
                    try:
                        result = main(arguments)
                    except SystemExit as error:
                        self.fail(f"SystemExit escaped with code {error.code}")

                self.assertEqual(result, 20)
                self.assertEqual(output.getvalue(), CALIBRATION_INVALID + "\n")
                self.assertEqual(errors.getvalue(), "")
                run_spy.assert_not_called()


class FavoriteStickerBundleTests(unittest.TestCase):
    def _manifest(self):
        points = {
            "smile_entry": (140, 900),
            "favorite_tab": (260, 900),
            "grid_first": (300, 220),
            "grid_last": (1100, 670),
        }
        metrics = ClientMetrics(
            hwnd=1,
            left=0,
            top=0,
            width=1400,
            height=1000,
            dpi=96,
            visible=True,
            maximized=True,
            foreground=True,
        )
        return favorite_module.build_manifest(points, metrics)

    def _frame(self, order):
        image = Image.new("RGB", (1400, 1000), "white")
        draw = ImageDraw.Draw(image)
        for cell, source in enumerate(order):
            row, column = divmod(cell, 5)
            center_x = 300 + column * 200
            center_y = 220 + row * 150
            color = (
                (source * 47) % 220 + 20,
                (source * 83) % 220 + 20,
                (source * 131) % 220 + 20,
            )
            draw.rectangle(
                (
                    center_x - 58,
                    center_y - 48,
                    center_x + 58,
                    center_y + 48,
                ),
                fill=color,
                outline="black",
                width=4,
            )
            draw.line(
                (
                    center_x - 45,
                    center_y - 25 + source % 11,
                    center_x + 42,
                    center_y + 22 - source % 7,
                ),
                fill="black",
                width=4,
            )
            draw.text(
                (center_x - 12, center_y + 24),
                f"{source + 1:02d}",
                fill="black",
            )
        return image

    def test_bundle_contains_exactly_twenty_times_four_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            frames = [self._frame(list(range(20))) for _ in range(4)]
            root = favorite_module.write_bundle(
                temporary,
                self._manifest(),
                frames,
            )
            images = sorted(root.glob("slot_*-frame_*.png"))
            self.assertEqual(len(images), 80)
            _, manifest = favorite_module.load_manifest(temporary)
            self.assertEqual(set(manifest["templates"]), set(favorite_module.STICKER_KEYS))
            self.assertTrue(all(len(names) == 4 for names in manifest["templates"].values()))

    def test_favorite_calibration_collects_points_without_template_capture(self):
        class Driver:
            def __init__(self):
                self.points = deque(
                    (
                        (180, 820),
                        (260, 820),
                        (300, 220),
                        (1100, 670),
                    )
                )
                self.events = []

            def prepare_calibration_window(self):
                return METRICS.hwnd

            def get_client_metrics(self, _hwnd):
                return METRICS

            def capture_swallowed_click(
                self,
                _hwnd,
                *,
                allow_same_process_popup=False,
                passthrough=False,
            ):
                self.events.append(
                    ("capture_point", allow_same_process_popup, passthrough)
                )
                return self.points.popleft()

            def click_ratio(self, _hwnd, _point):
                self.events.append(("open_panel",))

            def click_bound_process_ratio(self, _hwnd, _point):
                self.events.append(("favorite_tab",))

            def move_bound_process_ratio(self, _hwnd, _point):
                raise AssertionError("fixed-slot calibration must not move for capture")

            def capture_bound_process_client(self, _hwnd):
                raise AssertionError("fixed-slot calibration must not capture frames")

        driver = Driver()
        captured = calibration_module.collect_favorite_sticker_calibration(
            driver,
            confirm_fn=lambda _prompt: "y",
            output_fn=lambda _message: None,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(len(captured["templates"]), 20)
        self.assertFalse(any(event[0] == "capture_frame" for event in driver.events))
        self.assertFalse(any(event[0] == "park_pointer" for event in driver.events))
        capture_events = [event for event in driver.events if event[0] == "capture_point"]
        self.assertEqual(
            [event[2] for event in capture_events],
            [True, True, False, False],
        )
        self.assertFalse(any(event[0] in {"open_panel", "favorite_tab"} for event in driver.events))

    def test_favorite_calibration_refocuses_and_retries_an_invalid_click(self):
        driver = mock.Mock()
        driver.prepare_calibration_window.return_value = METRICS.hwnd
        driver.get_client_metrics.return_value = METRICS
        driver.capture_swallowed_click.side_effect = (
            CalibrationError(CALIBRATION_WINDOW),
            (180, 820),
            (260, 820),
            (300, 220),
            (1100, 670),
        )
        messages = []
        sleeps = []

        captured = calibration_module.collect_favorite_sticker_calibration(
            driver,
            confirm_fn=lambda _prompt: "y",
            output_fn=messages.append,
            sleep_fn=sleeps.append,
        )

        self.assertEqual(len(captured["templates"]), 20)
        self.assertEqual(driver.capture_swallowed_click.call_count, 5)
        self.assertGreaterEqual(driver.prepare_calibration_window.call_count, 3)
        self.assertIn(0.25, sleeps)
        self.assertTrue(
            any("失败" in message and CALIBRATION_WINDOW in message for message in messages)
        )

    def test_runtime_matching_finds_a_template_after_grid_reordering(self):
        original = list(range(20))
        reordered = original[9:] + original[:9]
        with tempfile.TemporaryDirectory() as temporary:
            favorite_module.write_bundle(
                temporary,
                self._manifest(),
                [self._frame(original) for _ in range(4)],
            )
            matcher = favorite_module.FavoriteStickerMatcher(temporary)
            point = matcher.point(
                [self._frame(reordered), self._frame(reordered)],
                "slot_07",
            )
            cell = reordered.index(6)
            row, column = divmod(cell, 5)
            self.assertAlmostEqual(point["x"], (300 + column * 200) / 1400)
            self.assertAlmostEqual(point["y"], (220 + row * 150) / 1000)

    def test_fixed_layout_maps_slot_without_reading_template_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = favorite_module.write_layout(
                temporary,
                self._manifest(),
            )

            layout = favorite_module.FavoriteStickerLayout(temporary)
            self.assertEqual(list(root.glob("slot_*-frame_*.png")), [])
            expected = {
                "slot_01": (300, 220),
                "slot_05": (1100, 220),
                "slot_07": (500, 370),
                "slot_13": (700, 520),
                "slot_16": (300, 670),
                "slot_20": (1100, 670),
            }
            for sticker_key, (x, y) in expected.items():
                with self.subTest(sticker_key=sticker_key):
                    point = layout.point(sticker_key)
                    self.assertAlmostEqual(point["x"], x / 1400)
                    self.assertAlmostEqual(point["y"], y / 1000)
            with self.assertRaises(favorite_module.FavoriteStickerError):
                layout.point("slot_21")

    def test_matching_rejects_nonfinite_threshold_and_different_frame_sizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            frames = [self._frame(list(range(20))) for _ in range(4)]
            favorite_module.write_bundle(temporary, self._manifest(), frames)
            with self.assertRaises(favorite_module.FavoriteStickerError):
                favorite_module.FavoriteStickerMatcher(
                    temporary,
                    minimum_similarity=float("nan"),
                )
            matcher = favorite_module.FavoriteStickerMatcher(temporary)
            with self.assertRaises(favorite_module.FavoriteStickerError) as caught:
                matcher.point(
                    [frames[0], frames[0].resize((1200, 900))],
                    "slot_01",
                )
            self.assertEqual(
                caught.exception.code,
                favorite_module.STICKER_PANEL_FAILED,
            )

    def test_bundle_reparse_is_rejected_before_template_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "favorite-sticker-templates"
            root.mkdir()
            with mock.patch("favorite_sticker._is_reparse", return_value=True):
                with self.assertRaises(favorite_module.FavoriteStickerError):
                    favorite_module.load_manifest(temporary)

    def test_manifest_rejects_boolean_schema_and_overflowing_numbers(self):
        manifest = self._manifest()
        invalid_schema = copy.deepcopy(manifest)
        invalid_schema["schema_version"] = True
        invalid_ratio = copy.deepcopy(manifest)
        invalid_ratio["points"]["smile_entry"]["x"] = 10**10000
        invalid_aspect = copy.deepcopy(manifest)
        invalid_aspect["reference"]["aspect_ratio"] = "1.4"
        for value in (invalid_schema, invalid_ratio, invalid_aspect):
            with self.subTest(value=value):
                with self.assertRaises(favorite_module.FavoriteStickerError):
                    favorite_module.validate_manifest(value)

    def test_legacy_five_row_manifest_requires_recalibration(self):
        manifest = self._manifest()
        manifest["schema_version"] = 1
        manifest["grid"]["rows"] = 5

        with self.assertRaises(favorite_module.FavoriteStickerError) as caught:
            favorite_module.validate_manifest(manifest)

        self.assertEqual(
            caught.exception.code,
            favorite_module.STICKER_CALIBRATION_REQUIRED,
        )

    def test_low_confidence_and_ambiguous_matches_fail_closed(self):
        original = list(range(20))
        with tempfile.TemporaryDirectory() as temporary:
            favorite_module.write_bundle(
                temporary,
                self._manifest(),
                [self._frame(original) for _ in range(4)],
            )
            matcher = favorite_module.FavoriteStickerMatcher(temporary)
            blank = Image.new("RGB", (1400, 1000), "white")
            with self.assertRaises(favorite_module.FavoriteStickerError) as low:
                matcher.point([blank, blank.copy()], "slot_01")
            self.assertIn(
                low.exception.code,
                {
                    favorite_module.STICKER_MATCH_LOW_CONFIDENCE,
                    favorite_module.STICKER_MATCH_AMBIGUOUS,
                },
            )

        identical = self._frame([0] * 20)
        with tempfile.TemporaryDirectory() as temporary:
            favorite_module.write_bundle(
                temporary,
                self._manifest(),
                [identical.copy() for _ in range(4)],
            )
            matcher = favorite_module.FavoriteStickerMatcher(temporary)
            with self.assertRaises(favorite_module.FavoriteStickerError) as tied:
                matcher.point([identical, identical.copy()], "slot_01")
            self.assertEqual(
                tied.exception.code,
                favorite_module.STICKER_MATCH_AMBIGUOUS,
            )

    def test_request_cache_is_bounded_when_all_entries_are_in_progress(self):
        cache = favorite_module.RequestIdCache(capacity=32, ttl_seconds=60)
        identity = ("contact", "session", "slot_01")
        request_ids = [str(uuid.uuid4()) for _ in range(33)]
        for request_id in request_ids[:32]:
            self.assertIsNone(cache.begin(request_id, identity))
        capacity = cache.begin(request_ids[32], identity)
        self.assertEqual(
            capacity.error_code,
            favorite_module.STICKER_REQUEST_CAPACITY,
        )
        cache.finish(
            request_ids[0],
            identity,
            favorite_module.IdempotentResult(
                False,
                favorite_module.STICKER_QUEUE_EXPIRED,
                "request",
                False,
            ),
        )
        self.assertIsNone(cache.begin(request_ids[32], identity))

    def test_weflow_receipt_requires_a_new_outgoing_type_47_in_same_session(self):
        calls = []
        payloads = deque(
            (
                [
                    {"serverId": "old", "isSend": 1, "localType": 47},
                    {"serverId": "incoming", "isSend": 0, "localType": 47},
                    {"serverId": "image", "isSend": 1, "localType": 3},
                ],
                [
                    {"serverId": "old", "isSend": 1, "localType": 47},
                    {"serverId": "incoming-new", "isSend": 0, "localType": 47},
                ],
                [
                    {"serverId": "old", "isSend": 1, "localType": 47},
                    {"serverId": "new", "isSend": True, "mediaType": "emoji"},
                ],
            )
        )

        class Response:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        def request_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response(payloads.popleft())

        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        receipt = favorite_module.WeFlowStickerReceipt(
            "http://127.0.0.1:5031",
            "token",
            request_get=request_get,
            timeout_seconds=2,
            poll_seconds=0.2,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        baseline = receipt.baseline("same-session")

        self.assertEqual(baseline, frozenset({"old"}))
        self.assertTrue(receipt.confirm("same-session", baseline))
        self.assertTrue(
            all(
                call[1]["params"]["talker"] == "same-session"
                for call in calls
            )
        )
        self.assertTrue(
            all(
                call[1]["headers"]["Authorization"] == "Bearer token"
                for call in calls
            )
        )
        self.assertTrue(
            all(0 < call[1]["timeout"] <= 5 for call in calls)
        )

    def test_weflow_receipt_baseline_fails_closed_and_poll_errors_time_out(self):
        def request_get(*_args, **_kwargs):
            raise OSError("synthetic offline failure")

        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        receipt = favorite_module.WeFlowStickerReceipt(
            "http://127.0.0.1:5031",
            "token",
            request_get=request_get,
            timeout_seconds=1,
            poll_seconds=0.25,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with self.assertRaises(favorite_module.FavoriteStickerError) as caught:
            receipt.baseline("same-session")
        self.assertEqual(
            caught.exception.code,
            favorite_module.STICKER_CONFIRMATION_UNAVAILABLE,
        )
        self.assertFalse(receipt.confirm("same-session", frozenset()))
        self.assertGreaterEqual(clock.value, 1.0)


if __name__ == "__main__":
    unittest.main()
