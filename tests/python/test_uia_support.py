import copy
import ctypes
import pathlib
import sys
import types
import unittest
from dataclasses import replace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))

from uia_support import (  # noqa: E402
    CALIBRATION_INVALID,
    CALIBRATION_REQUIRED,
    CALIBRATION_WINDOW,
    RECALIBRATION_REQUIRED,
    CalibrationError,
    ClientMetrics,
    Win32WeChatDriver,
    build_calibration,
    ratio_from_screen_point,
    screen_point_from_ratio,
    validate_calibration,
    validate_runtime_metrics,
)


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002


class FakeUser32:
    def __init__(self):
        self.windows = {
            10: {
                "class": "Qt51514QWindowIcon",
                "title": "微信",
                "visible": True,
            }
        }
        self.window_order = [10]
        self.pids = {10: 100}
        self.foreground = 10
        self.client_rect = (0, 0, 1600, 900)
        self.client_rect_result = 1
        self.client_origin = (100, 50)
        self.dpi = 120
        self.maximized = True
        self.window_from_point_result = 11
        self.roots = {11: 10}
        self.calls = []
        self.set_cursor_result = 1
        self.set_clipboard_result = 1

    def EnumWindows(self, callback, lparam):
        self.calls.append(("EnumWindows",))
        for hwnd in self.window_order:
            if not callback(hwnd, lparam):
                break
        return 1

    def IsWindowVisible(self, hwnd):
        self.calls.append(("IsWindowVisible", hwnd))
        return int(self.windows.get(hwnd, {}).get("visible", False))

    def GetClassNameW(self, hwnd, buffer, length):
        self.calls.append(("GetClassNameW", hwnd))
        value = self.windows.get(hwnd, {}).get("class", "")
        buffer.value = value[: length - 1]
        return len(buffer.value)

    def GetWindowTextLengthW(self, hwnd):
        self.calls.append(("GetWindowTextLengthW", hwnd))
        return len(self.windows.get(hwnd, {}).get("title", ""))

    def GetWindowTextW(self, hwnd, buffer, length):
        self.calls.append(("GetWindowTextW", hwnd))
        value = self.windows.get(hwnd, {}).get("title", "")
        buffer.value = value[: length - 1]
        return len(buffer.value)

    def GetForegroundWindow(self):
        self.calls.append(("GetForegroundWindow",))
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, process_id_pointer):
        pid = self.pids.get(hwnd, 100)
        self.calls.append(("GetWindowThreadProcessId", hwnd, pid))
        process_id_pointer._obj.value = pid
        return 1 if pid else 0

    def GetClientRect(self, hwnd, rect_pointer):
        self.calls.append(("GetClientRect", hwnd))
        if not self.client_rect_result:
            return 0
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.client_rect
        return 1

    def ClientToScreen(self, hwnd, point_pointer):
        point = point_pointer._obj
        self.calls.append(("ClientToScreen", hwnd, point.x, point.y))
        point.x += self.client_origin[0]
        point.y += self.client_origin[1]
        return 1

    def GetWindowRect(self, hwnd, rect_pointer):
        raise AssertionError("GetWindowRect must not be used")

    def GetDpiForWindow(self, hwnd):
        self.calls.append(("GetDpiForWindow", hwnd))
        return self.dpi

    def IsZoomed(self, hwnd):
        self.calls.append(("IsZoomed", hwnd))
        return int(self.maximized)

    def WindowFromPoint(self, point):
        self.calls.append(("WindowFromPoint", point.x, point.y))
        return self.window_from_point_result

    def GetAncestor(self, hwnd, flag):
        self.calls.append(("GetAncestor", hwnd, flag))
        return self.roots.get(hwnd, hwnd)

    def PostQuitMessage(self, exit_code):
        self.calls.append(("PostQuitMessage", exit_code))

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", x, y))
        return self.set_cursor_result

    def mouse_event(self, flags, dx, dy, data, extra_info):
        self.calls.append(("mouse_event", flags, dx, dy, data, extra_info))

    def keybd_event(self, virtual_key, scan_code, flags, extra_info):
        self.calls.append(
            ("keybd_event", virtual_key, scan_code, flags, extra_info)
        )

    def OpenClipboard(self, owner):
        self.calls.append(("OpenClipboard", owner))
        return 1

    def EmptyClipboard(self):
        self.calls.append(("EmptyClipboard",))
        return 1

    def SetClipboardData(self, format_id, handle):
        self.calls.append(("SetClipboardData", format_id, handle))
        return self.set_clipboard_result

    def CloseClipboard(self):
        self.calls.append(("CloseClipboard",))
        return 1


class FakeKernel32:
    def __init__(self):
        self.calls = []
        self.buffers = {}
        self.next_handle = 1000
        self.process_images = {
            100: r"C:\Program Files\Tencent\WeChat.exe",
            200: r"C:\Program Files\Tencent\Weixin.exe",
        }
        self.process_handles = {}

    def GetModuleHandleW(self, module_name):
        self.calls.append(("GetModuleHandleW", module_name))
        return 1

    def OpenProcess(self, access, inherit_handle, process_id):
        self.calls.append(("OpenProcess", access, inherit_handle, process_id))
        if process_id not in self.process_images:
            return 0
        handle = process_id + 5000
        self.process_handles[handle] = process_id
        return handle

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size_pointer):
        self.calls.append(("QueryFullProcessImageNameW", handle, flags))
        process_id = self.process_handles.get(handle)
        value = self.process_images.get(process_id)
        if value is None:
            return 0
        size = size_pointer._obj.value
        buffer.value = value[: max(0, size - 1)]
        size_pointer._obj.value = len(buffer.value)
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        self.process_handles.pop(handle, None)
        return 1

    def GlobalAlloc(self, flags, size):
        handle = self.next_handle
        self.next_handle += 1
        self.buffers[handle] = ctypes.create_string_buffer(size)
        self.calls.append(("GlobalAlloc", flags, size, handle))
        return handle

    def GlobalLock(self, handle):
        self.calls.append(("GlobalLock", handle))
        return ctypes.addressof(self.buffers[handle])

    def GlobalUnlock(self, handle):
        self.calls.append(("GlobalUnlock", handle))
        return 1

    def GlobalFree(self, handle):
        self.calls.append(("GlobalFree", handle))
        self.buffers.pop(handle, None)
        return 0


class FakeHookRunner:
    def __init__(self, events):
        self.events = events
        self.mouse_results = []
        self.keyboard_results = []

    def __call__(self, mouse_callback, keyboard_callback):
        for kind, message, value in self.events:
            if kind == "mouse":
                self.mouse_results.append(mouse_callback(message, value))
            else:
                self.keyboard_results.append(
                    keyboard_callback(message, value)
                )


class FakeHookUser32(FakeUser32):
    def __init__(self):
        super().__init__()
        self.next_hook = 2000

    def SetWindowsHookExW(self, hook_id, callback, module, thread_id):
        handle = self.next_hook
        self.next_hook += 1
        self.calls.append(
            ("SetWindowsHookExW", hook_id, callback, module, thread_id, handle)
        )
        return handle

    def UnhookWindowsHookEx(self, handle):
        self.calls.append(("UnhookWindowsHookEx", handle))
        return 1

    def GetMessageW(self, message_pointer, hwnd, minimum, maximum):
        self.calls.append(("GetMessageW", hwnd, minimum, maximum))
        return -1

    def CallNextHookEx(self, hook, code, message, data):
        self.calls.append(("CallNextHookEx", hook, code, message, data))
        return 0

    def TranslateMessage(self, message_pointer):
        self.calls.append(("TranslateMessage",))
        return 1

    def DispatchMessageW(self, message_pointer):
        self.calls.append(("DispatchMessageW",))
        return 0


class FakeImage:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def convert(self, mode):
        self.mode = mode
        return self

    def save(self, stream, format):
        self.format = format
        stream.write(b"0" * 14 + b"DIB-BYTES")


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

VALID_POINTS = {
    "search_box": (260, 140),
    "first_result": (300, 230),
    "message_input": (1060, 770),
    "send_button": (1540, 860),
}


class CoordinateConversionTests(unittest.TestCase):
    def test_ratio_from_screen_point_uses_client_area(self):
        ratio = ratio_from_screen_point((260, 140), METRICS)

        self.assertAlmostEqual(ratio["x"], 0.1, delta=1e-9)
        self.assertAlmostEqual(ratio["y"], 0.1, delta=1e-9)

    def test_screen_point_from_ratio_reverses_conversion(self):
        for name, point in VALID_POINTS.items():
            with self.subTest(name=name):
                ratio = ratio_from_screen_point(point, METRICS)
                converted = screen_point_from_ratio(ratio, METRICS)

                self.assertEqual(converted, point)

    def test_screen_point_from_ratio_clamps_rounding_inside_client_area(self):
        point = screen_point_from_ratio(
            {"x": 0.999999999999, "y": 0.999999999999}, METRICS
        )

        self.assertEqual(
            point,
            (METRICS.left + METRICS.width - 1, METRICS.top + METRICS.height - 1),
        )

    def test_build_calibration_uses_schema_one_and_fixed_point_order(self):
        reversed_points = dict(reversed(tuple(VALID_POINTS.items())))

        calibration = build_calibration(reversed_points, METRICS)

        self.assertEqual(calibration["schema_version"], 1)
        self.assertIs(calibration["completed"], True)
        self.assertEqual(calibration["coordinate_space"], "client_area_ratio")
        self.assertEqual(
            tuple(calibration["points"]),
            ("search_box", "first_result", "message_input", "send_button"),
        )
        self.assertEqual(
            calibration["reference"],
            {
                "client_width": 1600,
                "client_height": 900,
                "aspect_ratio": 1600 / 900,
                "dpi": 120,
            },
        )


class CalibrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.valid = build_calibration(VALID_POINTS, METRICS)

    def assert_calibration_error(self, expected_code, value):
        with self.assertRaises(CalibrationError) as raised:
            validate_calibration(value)
        self.assertEqual(raised.exception.code, expected_code)
        self.assertEqual(str(raised.exception), expected_code)

    def test_valid_calibration_is_returned_unchanged(self):
        self.assertIs(validate_calibration(self.valid), self.valid)

    def test_missing_incomplete_or_other_schema_requires_calibration(self):
        cases = (
            None,
            {},
            {"schema_version": 1, "completed": False},
            {**self.valid, "schema_version": 2},
        )

        for value in cases:
            with self.subTest(value=value):
                self.assert_calibration_error(CALIBRATION_REQUIRED, value)

    def test_missing_schema_one_fields_are_invalid(self):
        paths = (
            ("coordinate_space",),
            ("points",),
            ("reference",),
            ("points", "search_box"),
            ("points", "search_box", "x"),
            ("reference", "client_width"),
            ("reference", "client_height"),
            ("reference", "aspect_ratio"),
            ("reference", "dpi"),
        )

        for path in paths:
            with self.subTest(path=path):
                value = copy.deepcopy(self.valid)
                target = value
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_boolean_values_cannot_masquerade_as_numbers(self):
        paths = (
            ("schema_version",),
            ("points", "search_box", "x"),
            ("points", "search_box", "y"),
            ("reference", "client_width"),
            ("reference", "client_height"),
            ("reference", "aspect_ratio"),
            ("reference", "dpi"),
        )

        for path in paths:
            for boolean in (True, False):
                with self.subTest(path=path, value=boolean):
                    value = copy.deepcopy(self.valid)
                    target = value
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = boolean
                    self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_point_ratios_must_be_finite_and_strictly_inside_client_area(self):
        for invalid in (float("nan"), float("inf"), float("-inf"), 0, 1, -0.1):
            with self.subTest(value=invalid):
                value = copy.deepcopy(self.valid)
                value["points"]["search_box"]["x"] = invalid
                self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_oversized_integer_is_reported_as_invalid(self):
        value = copy.deepcopy(self.valid)
        value["points"]["search_box"]["x"] = 10**10000

        self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_all_point_coordinates_are_validated(self):
        for point_name in VALID_POINTS:
            for axis in ("x", "y"):
                with self.subTest(point=point_name, axis=axis):
                    value = copy.deepcopy(self.valid)
                    value["points"][point_name][axis] = float("nan")
                    self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_reference_dimensions_and_dpi_must_be_positive_integers(self):
        for key in ("client_width", "client_height", "dpi"):
            for invalid in (0, -1, 1.5, float("nan"), float("inf")):
                with self.subTest(key=key, value=invalid):
                    value = copy.deepcopy(self.valid)
                    value["reference"][key] = invalid
                    self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_one_is_valid_for_positive_reference_numbers(self):
        value = copy.deepcopy(self.valid)
        value["reference"] = {
            "client_width": 1,
            "client_height": 1,
            "aspect_ratio": 1,
            "dpi": 1,
        }

        self.assertIs(validate_calibration(value), value)

    def test_reference_aspect_ratio_must_be_positive_and_finite(self):
        for invalid in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=invalid):
                value = copy.deepcopy(self.valid)
                value["reference"]["aspect_ratio"] = invalid
                self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_wrong_coordinate_space_is_invalid(self):
        value = copy.deepcopy(self.valid)
        value["coordinate_space"] = "screen_pixels"

        self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_top_level_properties_are_exact(self):
        value = copy.deepcopy(self.valid)
        value["unexpected"] = "must be rejected"

        self.assert_calibration_error(CALIBRATION_INVALID, value)

    def test_reference_properties_are_exact(self):
        value = copy.deepcopy(self.valid)
        value["reference"]["unexpected"] = 1

        self.assert_calibration_error(CALIBRATION_INVALID, value)


class RuntimeMetricsValidationTests(unittest.TestCase):
    def setUp(self):
        self.calibration = build_calibration(VALID_POINTS, METRICS)

    def assert_runtime_error(self, expected_code, metrics):
        with self.assertRaises(CalibrationError) as raised:
            validate_runtime_metrics(self.calibration, metrics)
        self.assertEqual(raised.exception.code, expected_code)

    def test_compatible_runtime_metrics_return_validated_calibration(self):
        self.assertIs(
            validate_runtime_metrics(self.calibration, METRICS), self.calibration
        )

    def test_runtime_validation_requires_a_valid_schema(self):
        with self.assertRaises(CalibrationError) as raised:
            validate_runtime_metrics(None, METRICS)
        self.assertEqual(raised.exception.code, CALIBRATION_REQUIRED)

    def test_window_must_be_visible_maximized_foreground_and_large_enough(self):
        cases = (
            replace(METRICS, visible=False),
            replace(METRICS, maximized=False),
            replace(METRICS, foreground=False),
            replace(METRICS, width=799),
            replace(METRICS, height=599),
        )

        for metrics in cases:
            with self.subTest(metrics=metrics):
                self.assert_runtime_error(CALIBRATION_WINDOW, metrics)

    def test_exact_minimum_client_size_is_allowed(self):
        metrics = replace(METRICS, width=800, height=600)
        points = {
            "search_box": (180, 110),
            "first_result": (260, 170),
            "message_input": (580, 470),
            "send_button": (820, 590),
        }
        calibration = build_calibration(points, metrics)

        self.assertIs(validate_runtime_metrics(calibration, metrics), calibration)

    def test_dpi_change_requires_recalibration(self):
        self.assert_runtime_error(
            RECALIBRATION_REQUIRED, replace(METRICS, dpi=121)
        )

    def test_aspect_ratio_drift_above_five_percent_requires_recalibration(self):
        for width in (1696, 1504):
            with self.subTest(width=width):
                self.assert_runtime_error(
                    RECALIBRATION_REQUIRED, replace(METRICS, width=width)
                )

    def test_aspect_ratio_drift_slightly_above_five_percent_is_rejected(self):
        current_aspect = METRICS.width / METRICS.height
        drift = 0.0500000000005

        for reference_aspect in (
            current_aspect / (1 + drift),
            current_aspect / (1 - drift),
        ):
            with self.subTest(reference_aspect=reference_aspect):
                calibration = copy.deepcopy(self.calibration)
                calibration["reference"]["aspect_ratio"] = reference_aspect
                with self.assertRaises(CalibrationError) as raised:
                    validate_runtime_metrics(calibration, METRICS)
                self.assertEqual(raised.exception.code, RECALIBRATION_REQUIRED)

    def test_aspect_ratio_drift_of_exactly_five_percent_is_allowed(self):
        for width in (1680, 1520):
            with self.subTest(width=width):
                metrics = replace(METRICS, width=width)
                self.assertIs(
                    validate_runtime_metrics(self.calibration, metrics),
                    self.calibration,
                )


class Win32WindowBoundaryTests(unittest.TestCase):
    def make_driver(self, user32=None, hook_runner=None, kernel32=None):
        return Win32WeChatDriver(
            user32=user32 or FakeUser32(),
            kernel32=kernel32 or FakeKernel32(),
            hook_runner=hook_runner or FakeHookRunner([]),
        )

    def assert_window_error(self, operation):
        with self.assertRaises(CalibrationError) as raised:
            operation()
        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertEqual(str(raised.exception), CALIBRATION_WINDOW)

    def test_get_client_metrics_uses_two_client_to_screen_conversions(self):
        user32 = FakeUser32()
        driver = self.make_driver(user32=user32)

        metrics = driver.get_client_metrics(10)

        self.assertEqual(metrics, METRICS)
        self.assertEqual(
            [call for call in user32.calls if call[0] == "ClientToScreen"],
            [
                ("ClientToScreen", 10, 0, 0),
                ("ClientToScreen", 10, 1600, 900),
            ],
        )
        self.assertFalse(
            any(call[0] == "GetWindowRect" for call in user32.calls)
        )

    def test_find_wechat_window_prefers_visible_supported_class(self):
        user32 = FakeUser32()
        user32.windows = {
            10: {"class": "Other", "title": "WeChat helper", "visible": True},
            20: {
                "class": "WeChatMainWndForPC",
                "title": "not localized",
                "visible": True,
            },
        }
        user32.window_order = [10, 20]
        user32.foreground = 10

        self.assertEqual(
            self.make_driver(user32=user32).find_wechat_window(), 20
        )

    def test_find_wechat_window_uses_title_only_as_compatibility_path(self):
        user32 = FakeUser32()
        user32.windows = {
            30: {"class": "Other", "title": "聊天 - 微信", "visible": True}
        }
        user32.window_order = [30]
        user32.foreground = 30

        self.assertEqual(
            self.make_driver(user32=user32).find_wechat_window(), 30
        )

    def test_find_wechat_window_rejects_hidden_or_missing_candidates(self):
        user32 = FakeUser32()
        user32.windows[10]["visible"] = False

        self.assert_window_error(
            self.make_driver(user32=user32).find_wechat_window
        )

    def test_multiple_candidates_require_the_foreground_candidate(self):
        user32 = FakeUser32()
        user32.windows[20] = {
            "class": "WeChatMainWndForPC",
            "title": "WeChat",
            "visible": True,
        }
        user32.window_order = [10, 20]
        user32.foreground = 20
        driver = self.make_driver(user32=user32)

        self.assertEqual(driver.find_wechat_window(), 20)

        user32.foreground = 99
        self.assert_window_error(driver.find_wechat_window)

    def test_generic_qt_or_wechat_title_from_other_process_is_rejected(self):
        for window in (
            {"class": "Qt51514QWindowIcon", "title": "unrelated", "visible": True},
            {"class": "Other", "title": "WeChat login helper", "visible": True},
        ):
            with self.subTest(window=window):
                user32 = FakeUser32()
                user32.windows = {10: window}
                kernel32 = FakeKernel32()
                kernel32.process_images[100] = r"C:\Other\NotWeChat.exe"

                self.assert_window_error(
                    self.make_driver(
                        user32=user32, kernel32=kernel32
                    ).find_wechat_window
                )

    def test_weixin_process_is_allowed_and_process_handle_is_closed(self):
        user32 = FakeUser32()
        user32.pids[10] = 200
        kernel32 = FakeKernel32()

        hwnd = self.make_driver(
            user32=user32, kernel32=kernel32
        ).find_wechat_window()

        self.assertEqual(hwnd, 10)
        self.assertIn(("OpenProcess", 0x1000, False, 200), kernel32.calls)
        self.assertIn(("CloseHandle", 5200), kernel32.calls)

    def test_bound_window_rejects_pid_or_image_identity_change(self):
        for change in ("pid", "image"):
            with self.subTest(change=change):
                user32 = FakeUser32()
                kernel32 = FakeKernel32()
                driver = self.make_driver(user32=user32, kernel32=kernel32)
                self.assertEqual(driver.find_wechat_window(), 10)
                if change == "pid":
                    user32.pids[10] = 200
                else:
                    kernel32.process_images[100] = r"D:\Portable\WeChat.exe"

                self.assert_window_error(lambda: driver.get_client_metrics(10))

    def test_bound_image_identity_is_canonicalized_for_case_and_separators(self):
        user32 = FakeUser32()
        kernel32 = FakeKernel32()
        driver = self.make_driver(user32=user32, kernel32=kernel32)
        self.assertEqual(driver.find_wechat_window(), 10)
        kernel32.process_images[100] = r"c:/program files/tencent/WECHAT.EXE"

        self.assertEqual(driver.get_client_metrics(10), METRICS)


class Win32CaptureTests(unittest.TestCase):
    def make_driver(self, user32, hook_runner):
        return Win32WeChatDriver(
            user32=user32,
            kernel32=FakeKernel32(),
            hook_runner=hook_runner,
        )

    def test_valid_click_is_swallowed_until_up_then_returned(self):
        user32 = FakeUser32()
        swallowed = []

        def runner(mouse_callback, keyboard_callback):
            swallowed.append(mouse_callback(WM_LBUTTONDOWN, (260, 140)))
            self.assertFalse(
                any(call[0] == "WindowFromPoint" for call in user32.calls)
            )
            swallowed.append(mouse_callback(WM_LBUTTONUP, (260, 140)))

        point = self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertEqual(point, (260, 140))
        self.assertEqual(swallowed, [1, 1])
        self.assertEqual(
            [call for call in user32.calls if call[0] == "WindowFromPoint"],
            [("WindowFromPoint", 260, 140)],
        )
        self.assertIn(("PostQuitMessage", 0), user32.calls)

    def test_other_window_click_swallows_complete_pair_then_raises(self):
        user32 = FakeUser32()
        user32.roots[11] = 99
        runner = FakeHookRunner(
            [
                ("mouse", WM_LBUTTONDOWN, (260, 140)),
                ("mouse", WM_LBUTTONUP, (260, 140)),
            ]
        )

        with self.assertRaises(CalibrationError) as raised:
            self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertEqual(runner.mouse_results, [1, 1])
        self.assertIn(("PostQuitMessage", 0), user32.calls)

    def test_capture_revalidates_bound_process_identity(self):
        user32 = FakeUser32()
        kernel32 = FakeKernel32()
        runner = FakeHookRunner(
            [
                ("mouse", WM_LBUTTONDOWN, (260, 140)),
                ("mouse", WM_LBUTTONUP, (260, 140)),
            ]
        )
        driver = Win32WeChatDriver(
            user32=user32, kernel32=kernel32, hook_runner=runner
        )
        self.assertEqual(driver.find_wechat_window(), 10)
        user32.pids[10] = 200

        with self.assertRaises(CalibrationError) as raised:
            driver.capture_swallowed_click(10)

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)

    def test_outside_client_click_is_swallowed_then_raises(self):
        user32 = FakeUser32()
        runner = FakeHookRunner(
            [
                ("mouse", WM_LBUTTONDOWN, (99, 140)),
                ("mouse", WM_LBUTTONUP, (99, 140)),
            ]
        )

        with self.assertRaises(CalibrationError) as raised:
            self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertEqual(runner.mouse_results, [1, 1])

    def test_metric_failure_on_up_is_still_swallowed_and_fixed(self):
        user32 = FakeUser32()
        user32.client_rect_result = 0
        runner = FakeHookRunner(
            [
                ("mouse", WM_LBUTTONDOWN, (260, 140)),
                ("mouse", WM_LBUTTONUP, (260, 140)),
            ]
        )

        with self.assertRaises(CalibrationError) as raised:
            self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertEqual(runner.mouse_results, [1, 1])
        self.assertIn(("PostQuitMessage", 0), user32.calls)

    def test_escape_is_swallowed_through_key_up_and_cancels(self):
        user32 = FakeUser32()
        runner = FakeHookRunner(
            [
                ("keyboard", WM_KEYDOWN, VK_ESCAPE),
                ("keyboard", WM_KEYUP, VK_ESCAPE),
            ]
        )

        result = self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertIsNone(result)
        self.assertEqual(runner.keyboard_results, [1, 1])
        self.assertIn(("PostQuitMessage", 0), user32.calls)

    def test_mouse_up_waits_for_overlapping_escape_up_and_cancel_wins(self):
        user32 = FakeUser32()
        swallowed = []

        def runner(mouse_callback, keyboard_callback):
            swallowed.append(mouse_callback(WM_LBUTTONDOWN, (260, 140)))
            swallowed.append(keyboard_callback(WM_KEYDOWN, VK_ESCAPE))
            swallowed.append(mouse_callback(WM_LBUTTONUP, (260, 140)))
            self.assertNotIn(("PostQuitMessage", 0), user32.calls)
            swallowed.append(keyboard_callback(WM_KEYUP, VK_ESCAPE))

        result = self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertIsNone(result)
        self.assertEqual(swallowed, [1, 1, 1, 1])
        self.assertEqual(user32.calls.count(("PostQuitMessage", 0)), 1)

    def test_escape_up_waits_for_overlapping_mouse_up_and_cancel_wins(self):
        user32 = FakeUser32()
        swallowed = []

        def runner(mouse_callback, keyboard_callback):
            swallowed.append(keyboard_callback(WM_KEYDOWN, VK_ESCAPE))
            swallowed.append(mouse_callback(WM_LBUTTONDOWN, (260, 140)))
            swallowed.append(keyboard_callback(WM_KEYUP, VK_ESCAPE))
            self.assertNotIn(("PostQuitMessage", 0), user32.calls)
            swallowed.append(mouse_callback(WM_LBUTTONUP, (260, 140)))

        result = self.make_driver(user32, runner).capture_swallowed_click(10)

        self.assertIsNone(result)
        self.assertEqual(swallowed, [1, 1, 1, 1])
        self.assertEqual(user32.calls.count(("PostQuitMessage", 0)), 1)

    def test_native_hook_runner_unhooks_both_hooks_on_message_error(self):
        user32 = FakeHookUser32()
        driver = Win32WeChatDriver(
            user32=user32,
            kernel32=FakeKernel32(),
        )

        with self.assertRaises(OSError):
            driver._hook_runner(lambda message, point: 0, lambda message, key: 0)

        installed = [
            call[-1] for call in user32.calls if call[0] == "SetWindowsHookExW"
        ]
        unhooked = [
            call[1] for call in user32.calls if call[0] == "UnhookWindowsHookEx"
        ]
        self.assertEqual(installed, [2000, 2001])
        self.assertEqual(unhooked, [2001, 2000])


class Win32InputAndClipboardTests(unittest.TestCase):
    def make_driver(self, user32=None, kernel32=None):
        return Win32WeChatDriver(
            user32=user32 or FakeUser32(),
            kernel32=kernel32 or FakeKernel32(),
            hook_runner=FakeHookRunner([]),
        )

    def test_click_ratio_does_not_move_or_click_before_window_validation(self):
        user32 = FakeUser32()
        user32.maximized = False
        driver = self.make_driver(user32=user32)

        with self.assertRaises(CalibrationError) as raised:
            driver.click_ratio(10, {"x": 0.1, "y": 0.1})

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertFalse(
            any(
                call[0] in {"SetCursorPos", "mouse_event"}
                for call in user32.calls
            )
        )

    def test_click_ratio_moves_and_clicks_only_after_validation(self):
        user32 = FakeUser32()

        self.make_driver(user32=user32).click_ratio(
            10, {"x": 0.1, "y": 0.1}
        )

        input_calls = [
            call
            for call in user32.calls
            if call[0] in {"SetCursorPos", "mouse_event"}
        ]
        self.assertEqual(
            input_calls,
            [
                ("SetCursorPos", 260, 140),
                ("mouse_event", 0x0002, 0, 0, 0, 0),
                ("mouse_event", 0x0004, 0, 0, 0, 0),
            ],
        )
        self.assertFalse(
            any(call[0] == "SetForegroundWindow" for call in user32.calls)
        )

    def test_click_ratio_stops_when_set_cursor_pos_fails(self):
        user32 = FakeUser32()
        user32.set_cursor_result = 0

        with self.assertRaises(CalibrationError) as raised:
            self.make_driver(user32=user32).click_ratio(
                10, {"x": 0.1, "y": 0.1}
            )

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertIn(("SetCursorPos", 260, 140), user32.calls)
        self.assertFalse(
            any(call[0] == "mouse_event" for call in user32.calls)
        )

    def test_click_ratio_rejects_wrong_window_at_calculated_point(self):
        user32 = FakeUser32()
        user32.roots[11] = 99

        with self.assertRaises(CalibrationError) as raised:
            self.make_driver(user32=user32).click_ratio(
                10, {"x": 0.1, "y": 0.1}
            )

        self.assertEqual(raised.exception.code, CALIBRATION_WINDOW)
        self.assertFalse(
            any(call[0] == "mouse_event" for call in user32.calls)
        )

    def test_every_click_revalidates_bound_process_identity(self):
        user32 = FakeUser32()
        kernel32 = FakeKernel32()
        driver = self.make_driver(user32=user32, kernel32=kernel32)
        self.assertEqual(driver.find_wechat_window(), 10)
        driver.click_ratio(10, {"x": 0.1, "y": 0.1})
        first_query_count = sum(
            call[0] == "QueryFullProcessImageNameW" for call in kernel32.calls
        )
        driver.click_ratio(10, {"x": 0.2, "y": 0.2})
        second_query_count = sum(
            call[0] == "QueryFullProcessImageNameW" for call in kernel32.calls
        )

        self.assertGreater(second_query_count, first_query_count)

    def test_keyboard_helpers_release_pressed_keys(self):
        user32 = FakeUser32()
        driver = self.make_driver(user32=user32)

        driver.hotkey_ctrl(0x56)
        driver.press_key(0x0D)

        calls = [call for call in user32.calls if call[0] == "keybd_event"]
        self.assertEqual(
            calls,
            [
                ("keybd_event", 0x11, 0, 0, 0),
                ("keybd_event", 0x56, 0, 0, 0),
                ("keybd_event", 0x56, 0, KEYEVENTF_KEYUP, 0),
                ("keybd_event", 0x11, 0, KEYEVENTF_KEYUP, 0),
                ("keybd_event", 0x0D, 0, 0, 0),
                ("keybd_event", 0x0D, 0, KEYEVENTF_KEYUP, 0),
            ],
        )

    def test_copy_image_transfers_dib_ownership_to_clipboard(self):
        user32 = FakeUser32()
        kernel32 = FakeKernel32()
        image_module = types.ModuleType("PIL.Image")
        image_module.open = mock.Mock(return_value=FakeImage())
        pil_module = types.ModuleType("PIL")
        pil_module.Image = image_module

        with mock.patch.dict(
            sys.modules, {"PIL": pil_module, "PIL.Image": image_module}
        ):
            self.make_driver(user32, kernel32).copy_image_to_clipboard(
                r"C:\private\never-log-this.png"
            )

        self.assertEqual(
            image_module.open.call_args.args[0],
            r"C:\private\never-log-this.png",
        )
        self.assertTrue(
            any(call[:2] == ("SetClipboardData", 8) for call in user32.calls)
        )
        self.assertFalse(
            any(call[0] == "GlobalFree" for call in kernel32.calls)
        )

    def test_copy_image_frees_untransferred_memory_on_failure(self):
        user32 = FakeUser32()
        user32.set_clipboard_result = 0
        kernel32 = FakeKernel32()
        image_module = types.ModuleType("PIL.Image")
        image_module.open = mock.Mock(return_value=FakeImage())
        pil_module = types.ModuleType("PIL")
        pil_module.Image = image_module

        with mock.patch.dict(
            sys.modules, {"PIL": pil_module, "PIL.Image": image_module}
        ):
            with self.assertRaises(OSError):
                self.make_driver(user32, kernel32).copy_image_to_clipboard(
                    r"C:\private\never-log-this.png"
                )

        allocated_handle = next(
            call[3] for call in kernel32.calls if call[0] == "GlobalAlloc"
        )
        self.assertIn(("GlobalFree", allocated_handle), kernel32.calls)
        self.assertIn(("CloseClipboard",), user32.calls)


if __name__ == "__main__":
    unittest.main()
