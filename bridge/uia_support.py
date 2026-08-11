import ctypes
import io
import math
import ntpath
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass


POINT_NAMES = ("search_box", "first_result", "message_input", "send_button")
CALIBRATION_REQUIRED = "E_UIA_CALIBRATION_REQUIRED"
CALIBRATION_INVALID = "E_UIA_CALIBRATION_INVALID"
CALIBRATION_WINDOW = "E_UIA_CALIBRATION_WINDOW"
CALIBRATION_BUSY = "E_UIA_CALIBRATION_BUSY"
RECALIBRATION_REQUIRED = "E_UIA_RECALIBRATION_REQUIRED"
CONTACT_SELECTION_FAILED = "E_UIA_CONTACT_SELECTION_FAILED"

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
GA_ROOT = 2
SW_MAXIMIZE = 3
SW_RESTORE = 9
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GMEM_MOVEABLE = 0x0002
CF_DIB = 8
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_IMAGE_BUFFER_SIZE = 32768
WECHAT_PROCESS_BASENAMES = frozenset({"wechat.exe", "weixin.exe"})
WECHAT_MAIN_WINDOW_CLASSES = frozenset(
    {"Qt51514QWindowIcon", "WeChatMainWndForPC"}
)
WECHAT_SEARCH_POPUP_CLASS = "Qt51514QWindowToolSaveBits"
PER_MONITOR_AWARE = ctypes.c_void_p(-3)
PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)
_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class CalibrationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _ensure_per_monitor_dpi_awareness(user32) -> None:
    """Keep window metrics, captured points, and injected clicks in physical pixels."""
    try:
        if user32.SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2):
            return
        current = user32.GetDpiAwarenessContextForProcess(None)
        if user32.AreDpiAwarenessContextsEqual(
            current, PER_MONITOR_AWARE_V2
        ) or user32.AreDpiAwarenessContextsEqual(
            current, PER_MONITOR_AWARE
        ):
            return
    except Exception:
        pass
    raise CalibrationError(CALIBRATION_WINDOW)


@dataclass(frozen=True)
class ClientMetrics:
    hwnd: int
    left: int
    top: int
    width: int
    height: int
    dpi: int
    visible: bool
    maximized: bool
    foreground: bool


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class _WindowIdentity:
    hwnd: int
    pid: int
    image: str


class Win32WeChatDriver:
    def __init__(self, user32=None, kernel32=None, hook_runner=None):
        native_user32 = user32 is None
        native_kernel32 = kernel32 is None
        self.user32 = user32 or ctypes.windll.user32
        self.kernel32 = kernel32 or ctypes.windll.kernel32
        self._hook_runner = hook_runner or self._run_low_level_hooks
        self._bound_window_identity = None
        if native_user32:
            self._configure_user32_abi()
            _ensure_per_monitor_dpi_awareness(self.user32)
        if native_kernel32:
            self._configure_kernel32_abi()

    def _configure_user32_abi(self) -> None:
        signatures = {
            "EnumWindows": (
                [_WNDENUMPROC, wintypes.LPARAM],
                wintypes.BOOL,
            ),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "GetClassNameW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetForegroundWindow": ([], wintypes.HWND),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "BringWindowToTop": ([wintypes.HWND], wintypes.BOOL),
            "AttachThreadInput": (
                [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL],
                wintypes.BOOL,
            ),
            "SetProcessDpiAwarenessContext": (
                [ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "GetDpiAwarenessContextForProcess": (
                [wintypes.HANDLE],
                ctypes.c_void_p,
            ),
            "AreDpiAwarenessContextsEqual": (
                [ctypes.c_void_p, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            "GetClientRect": (
                [wintypes.HWND, ctypes.POINTER(_RECT)],
                wintypes.BOOL,
            ),
            "GetWindowRect": (
                [wintypes.HWND, ctypes.POINTER(_RECT)],
                wintypes.BOOL,
            ),
            "ClientToScreen": (
                [wintypes.HWND, ctypes.POINTER(_POINT)],
                wintypes.BOOL,
            ),
            "GetDpiForWindow": ([wintypes.HWND], wintypes.UINT),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "WindowFromPoint": ([_POINT], wintypes.HWND),
            "GetAncestor": (
                [wintypes.HWND, wintypes.UINT],
                wintypes.HWND,
            ),
            "PostQuitMessage": ([ctypes.c_int], None),
            "SetCursorPos": (
                [ctypes.c_int, ctypes.c_int],
                wintypes.BOOL,
            ),
            "mouse_event": (
                [
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_size_t,
                ],
                None,
            ),
            "keybd_event": (
                [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t],
                None,
            ),
            "SetWindowsHookExW": (
                [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD],
                wintypes.HHOOK,
            ),
            "CallNextHookEx": (
                [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM],
                ctypes.c_ssize_t,
            ),
            "UnhookWindowsHookEx": ([wintypes.HHOOK], wintypes.BOOL),
            "GetMessageW": (
                [
                    ctypes.POINTER(wintypes.MSG),
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.UINT,
                ],
                wintypes.BOOL,
            ),
            "TranslateMessage": (
                [ctypes.POINTER(wintypes.MSG)],
                wintypes.BOOL,
            ),
            "DispatchMessageW": (
                [ctypes.POINTER(wintypes.MSG)],
                ctypes.c_ssize_t,
            ),
            "OpenClipboard": ([wintypes.HWND], wintypes.BOOL),
            "EmptyClipboard": ([], wintypes.BOOL),
            "SetClipboardData": (
                [wintypes.UINT, wintypes.HANDLE],
                wintypes.HANDLE,
            ),
            "CloseClipboard": ([], wintypes.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes = argtypes
            function.restype = restype

    def _configure_kernel32_abi(self) -> None:
        signatures = {
            "GetModuleHandleW": ([wintypes.LPCWSTR], wintypes.HMODULE),
            "GlobalAlloc": (
                [wintypes.UINT, ctypes.c_size_t],
                wintypes.HGLOBAL,
            ),
            "GlobalLock": ([wintypes.HGLOBAL], wintypes.LPVOID),
            "GlobalUnlock": ([wintypes.HGLOBAL], wintypes.BOOL),
            "GlobalFree": ([wintypes.HGLOBAL], wintypes.HGLOBAL),
            "OpenProcess": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "QueryFullProcessImageNameW": (
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPWSTR,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetCurrentThreadId": ([], wintypes.DWORD),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.kernel32, name)
            function.argtypes = argtypes
            function.restype = restype

    def _window_class(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def _window_title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(length + 1, 2))
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _query_window_identity(self, hwnd: int) -> _WindowIdentity:
        process_id = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(process_id)
        ) or not process_id.value:
            raise CalibrationError(CALIBRATION_WINDOW)

        process = self.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
        )
        if not process:
            raise CalibrationError(CALIBRATION_WINDOW)
        try:
            capacity = wintypes.DWORD(PROCESS_IMAGE_BUFFER_SIZE)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not self.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(capacity)
            ):
                raise CalibrationError(CALIBRATION_WINDOW)
            image = ntpath.normcase(ntpath.normpath(buffer.value.strip()))
        finally:
            self.kernel32.CloseHandle(process)

        if not image or ntpath.basename(image).casefold() not in (
            WECHAT_PROCESS_BASENAMES
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        return _WindowIdentity(int(hwnd), int(process_id.value), image)

    def _ensure_bound_window_identity(self, hwnd: int) -> _WindowIdentity:
        current = self._query_window_identity(hwnd)
        if self._bound_window_identity is None:
            self._bound_window_identity = current
        if current != self._bound_window_identity:
            raise CalibrationError(CALIBRATION_WINDOW)
        return current

    def _point_targets_bound_window(
        self, hwnd: int, point: tuple[int, int]
    ) -> bool:
        bound = self._ensure_bound_window_identity(hwnd)
        child = int(self.user32.WindowFromPoint(_POINT(*point)) or 0)
        if not child:
            return False
        root = int(self.user32.GetAncestor(child, GA_ROOT) or 0)
        if root != hwnd:
            return False
        return self._query_window_identity(root) == bound

    def _point_targets_bound_process(
        self, hwnd: int, point: tuple[int, int]
    ) -> bool:
        """Accept the main window or one visible same-process WeChat popup."""
        bound = self._ensure_bound_window_identity(hwnd)
        child = int(self.user32.WindowFromPoint(_POINT(*point)) or 0)
        if not child:
            return False
        root = int(self.user32.GetAncestor(child, GA_ROOT) or 0)
        if not root or not self.user32.IsWindowVisible(root):
            return False
        try:
            target = self._query_window_identity(root)
        except CalibrationError:
            return False
        return target.pid == bound.pid and target.image == bound.image

    def _foreground_targets_bound_process(self, hwnd: int) -> bool:
        bound = self._ensure_bound_window_identity(hwnd)
        foreground = int(self.user32.GetForegroundWindow() or 0)
        root = int(self.user32.GetAncestor(foreground, GA_ROOT) or 0)
        if not root:
            return False
        try:
            target = self._query_window_identity(root)
        except CalibrationError:
            return False
        return target.pid == bound.pid and target.image == bound.image

    def find_wechat_window(self) -> int:
        """Return a visible WeChat main-window HWND or raise CALIBRATION_WINDOW."""
        class_candidates = []
        title_candidates = []

        @_WNDENUMPROC
        def collect(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            class_name = self._window_class(hwnd)
            title = self._window_title(hwnd)
            supported_class = class_name in WECHAT_MAIN_WINDOW_CLASSES
            supported_title = "微信" in title or "WeChat" in title
            if not supported_class and not supported_title:
                return True
            try:
                identity = self._query_window_identity(int(hwnd))
            except CalibrationError:
                return True
            if supported_class:
                class_candidates.append(identity)
            else:
                title_candidates.append(identity)
            return True

        if not self.user32.EnumWindows(collect, 0):
            raise CalibrationError(CALIBRATION_WINDOW)

        candidates = class_candidates or title_candidates
        if len(candidates) == 1:
            selected = candidates[0]
            self._bound_window_identity = selected
            return selected.hwnd
        if len(candidates) > 1:
            foreground = int(self.user32.GetForegroundWindow() or 0)
            selected = next(
                (item for item in candidates if item.hwnd == foreground), None
            )
            if selected is not None:
                self._bound_window_identity = selected
                return selected.hwnd
        raise CalibrationError(CALIBRATION_WINDOW)

    def find_calibration_window(self) -> int:
        """Find the one plausible WeChat main window, including a tray-hidden one.

        Normal send/receive paths deliberately continue to use
        ``find_wechat_window`` and therefore never select a hidden window.  The
        calibration launcher is the only caller that may recover a tray-hidden
        main window after its console takes focus.
        """
        try:
            return self.find_wechat_window()
        except CalibrationError as error:
            if error.code != CALIBRATION_WINDOW:
                raise

        candidates: list[tuple[_WindowIdentity, int]] = []

        @_WNDENUMPROC
        def collect(hwnd, _lparam):
            if self._window_class(hwnd) not in WECHAT_MAIN_WINDOW_CLASSES:
                return True
            rect = _RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            # Exclude login/toast/helper windows that share the generic Qt
            # class.  A selected window will still be revalidated at >=800x600
            # after it is maximized.
            if width < 640 or height < 480:
                return True
            try:
                identity = self._query_window_identity(int(hwnd))
            except CalibrationError:
                return True
            candidates.append((identity, width * height))
            return True

        if not self.user32.EnumWindows(collect, 0) or not candidates:
            raise CalibrationError(CALIBRATION_WINDOW)

        candidates.sort(key=lambda item: item[1], reverse=True)
        if len(candidates) > 1 and candidates[0][1] < candidates[1][1] * 2:
            # Two similarly sized main windows are ambiguous.  Fail closed so
            # calibration cannot bind to the wrong signed-in account.
            raise CalibrationError(CALIBRATION_WINDOW)

        selected = candidates[0][0]
        self._bound_window_identity = selected
        return selected.hwnd

    def prepare_calibration_window(self) -> int:
        """Restore, maximize, and foreground one identity-validated main window."""
        hwnd = self.find_calibration_window()
        self._ensure_bound_window_identity(hwnd)
        if not self.user32.IsWindowVisible(hwnd):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
        if not self.user32.IsZoomed(hwnd):
            self.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        if not self.user32.IsWindowVisible(hwnd) or not self.user32.IsZoomed(hwnd):
            raise CalibrationError(CALIBRATION_WINDOW)
        self.activate_receive_window(hwnd)
        return hwnd

    def get_client_metrics(self, hwnd: int) -> ClientMetrics:
        """Return client origin, size, DPI, visible/maximized/foreground flags."""
        self._ensure_bound_window_identity(hwnd)
        rect = _RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise CalibrationError(CALIBRATION_WINDOW)

        top_left = _POINT(rect.left, rect.top)
        bottom_right = _POINT(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            raise CalibrationError(CALIBRATION_WINDOW)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            raise CalibrationError(CALIBRATION_WINDOW)

        return ClientMetrics(
            hwnd=hwnd,
            left=top_left.x,
            top=top_left.y,
            width=bottom_right.x - top_left.x,
            height=bottom_right.y - top_left.y,
            dpi=int(self.user32.GetDpiForWindow(hwnd)),
            visible=bool(self.user32.IsWindowVisible(hwnd)),
            maximized=bool(self.user32.IsZoomed(hwnd)),
            foreground=int(self.user32.GetForegroundWindow() or 0) == hwnd,
        )

    def activate_receive_window(self, hwnd: int) -> None:
        """Bring one identity-validated WeChat receive window to the foreground."""
        self._ensure_bound_window_identity(hwnd)
        if int(self.user32.GetForegroundWindow() or 0) == hwnd:
            return

        process_id = wintypes.DWORD()
        target_thread = int(
            self.user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(process_id),
            )
            or 0
        )
        foreground = int(self.user32.GetForegroundWindow() or 0)
        foreground_thread = 0
        if foreground:
            foreground_process_id = wintypes.DWORD()
            foreground_thread = int(
                self.user32.GetWindowThreadProcessId(
                    foreground,
                    ctypes.byref(foreground_process_id),
                )
                or 0
            )
        current_thread = int(self.kernel32.GetCurrentThreadId() or 0)
        attached_threads = []
        try:
            for thread_id in {foreground_thread, target_thread}:
                if (
                    thread_id
                    and current_thread
                    and thread_id != current_thread
                    and self.user32.AttachThreadInput(
                        current_thread,
                        thread_id,
                        True,
                    )
                ):
                    attached_threads.append(thread_id)
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
        finally:
            for thread_id in reversed(attached_threads):
                self.user32.AttachThreadInput(
                    current_thread,
                    thread_id,
                    False,
                )
        if int(self.user32.GetForegroundWindow() or 0) != hwnd:
            raise CalibrationError(CALIBRATION_WINDOW)

    def _get_screen_rect(self, hwnd: int) -> ScreenRect:
        rect = _RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        result = ScreenRect(rect.left, rect.top, rect.right, rect.bottom)
        if result.width <= 0 or result.height <= 0:
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        return result

    def find_search_popup(
        self,
        main_hwnd: int,
        search_point: Mapping[str, object],
    ) -> tuple[int, ScreenRect]:
        """Return the one same-process WeChat search popup near the search box."""
        main_identity = self._ensure_bound_window_identity(main_hwnd)
        metrics = self._validated_click_metrics(main_hwnd, search_point)
        search_x, search_y = screen_point_from_ratio(search_point, metrics)
        candidates: list[tuple[int, ScreenRect]] = []

        @_WNDENUMPROC
        def collect(hwnd, _lparam):
            popup_hwnd = int(hwnd)
            if (
                popup_hwnd == main_hwnd
                or not self.user32.IsWindowVisible(popup_hwnd)
                or self._window_class(popup_hwnd) != WECHAT_SEARCH_POPUP_CLASS
            ):
                return True
            try:
                identity = self._query_window_identity(popup_hwnd)
                rect = self._get_screen_rect(popup_hwnd)
            except CalibrationError:
                return True
            if (
                identity.pid != main_identity.pid
                or identity.image != main_identity.image
                or rect.width < 160
                or rect.height < 80
                or rect.width > metrics.width * 0.60
                or rect.height > metrics.height * 0.90
                or rect.left < metrics.left - metrics.width * 0.05
                or rect.right > metrics.left + metrics.width * 1.05
                or rect.top < metrics.top - metrics.height * 0.05
                or rect.bottom > metrics.top + metrics.height * 1.05
                or not rect.left <= search_x <= rect.right
                or rect.top > search_y + metrics.height * 0.20
            ):
                return True
            candidates.append((popup_hwnd, rect))
            return True

        if not self.user32.EnumWindows(collect, 0) or len(candidates) != 1:
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        return candidates[0]

    def click_search_popup_point(
        self,
        main_hwnd: int,
        popup_hwnd: int,
        point: tuple[int, int],
    ) -> None:
        """Click one point only after revalidating the same-process search popup."""
        main_identity = self._ensure_bound_window_identity(main_hwnd)
        self.get_client_metrics(main_hwnd)
        if (
            not self.user32.IsWindowVisible(popup_hwnd)
            or self._window_class(popup_hwnd) != WECHAT_SEARCH_POPUP_CLASS
        ):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        popup_identity = self._query_window_identity(popup_hwnd)
        rect = self._get_screen_rect(popup_hwnd)
        if (
            popup_identity.pid != main_identity.pid
            or popup_identity.image != main_identity.image
            or not rect.left <= point[0] < rect.right
            or not rect.top <= point[1] < rect.bottom
        ):
            raise CalibrationError(CONTACT_SELECTION_FAILED)

        child = int(self.user32.WindowFromPoint(_POINT(*point)) or 0)
        root = int(self.user32.GetAncestor(child, GA_ROOT) or 0)
        if not child or root != popup_hwnd:
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        if not self.user32.SetCursorPos(*point):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _capture_point_is_valid(
        self,
        hwnd: int,
        point: tuple[int, int],
        *,
        allow_same_process_popup: bool = False,
    ) -> bool:
        point_validator = (
            self._point_targets_bound_process
            if allow_same_process_popup
            else self._point_targets_bound_window
        )
        if not point_validator(hwnd, point):
            return False
        metrics = self.get_client_metrics(hwnd)
        return (
            metrics.left <= point[0] < metrics.left + metrics.width
            and metrics.top <= point[1] < metrics.top + metrics.height
        )

    def capture_swallowed_click(
        self,
        hwnd: int,
        *,
        allow_same_process_popup: bool = False,
        passthrough: bool = False,
    ) -> tuple[int, int] | None:
        """Capture one left-click; optionally let safe navigation clicks through."""
        self._ensure_bound_window_identity(hwnd)
        state = {
            "active_inputs": set(),
            "pending": None,
            "finished": False,
            "result": None,
            "error": None,
        }

        def queue_outcome(kind, value=None):
            if kind == "cancel" or state["pending"] is None:
                state["pending"] = (kind, value)

        def finish_if_idle():
            if (
                state["finished"]
                or state["pending"] is None
                or state["active_inputs"]
            ):
                return
            kind, value = state["pending"]
            state["finished"] = True
            if kind == "click":
                state["result"] = value
            elif kind == "error":
                state["error"] = CalibrationError(CALIBRATION_WINDOW)
            self.user32.PostQuitMessage(0)

        def mouse_callback(message: int, point: tuple[int, int]) -> int:
            if message not in {WM_LBUTTONDOWN, WM_LBUTTONUP}:
                return 0
            if state["finished"]:
                return 0 if passthrough else 1
            if message == WM_LBUTTONDOWN:
                state["active_inputs"].add("mouse")
                return 0 if passthrough else 1
            if "mouse" in state["active_inputs"]:
                state["active_inputs"].remove("mouse")
                if not (
                    state["pending"] is not None
                    and state["pending"][0] == "cancel"
                ):
                    try:
                        valid = self._capture_point_is_valid(
                            hwnd,
                            point,
                            allow_same_process_popup=allow_same_process_popup,
                        )
                    except Exception:
                        queue_outcome("error")
                    else:
                        if valid:
                            queue_outcome("click", point)
                        else:
                            queue_outcome("error")
                finish_if_idle()
            return 0 if passthrough else 1

        def keyboard_callback(message: int, virtual_key: int) -> int:
            if virtual_key != VK_ESCAPE or message not in {
                WM_KEYDOWN,
                WM_KEYUP,
                WM_SYSKEYDOWN,
                WM_SYSKEYUP,
            }:
                return 0
            if state["finished"]:
                return 1
            if message in {WM_KEYDOWN, WM_SYSKEYDOWN}:
                state["active_inputs"].add("escape")
            elif "escape" in state["active_inputs"]:
                state["active_inputs"].remove("escape")
                queue_outcome("cancel")
                finish_if_idle()
            return 1

        self._hook_runner(mouse_callback, keyboard_callback)
        if state["error"] is not None:
            raise state["error"]
        if not state["finished"]:
            raise CalibrationError(CALIBRATION_WINDOW)
        return state["result"]

    def _validated_click_metrics(
        self, hwnd: int, point: Mapping[str, object]
    ) -> ClientMetrics:
        try:
            x = point["x"]
            y = point["y"]
        except (KeyError, TypeError):
            raise CalibrationError(CALIBRATION_INVALID) from None
        if (
            not _is_finite_number(x)
            or not _is_finite_number(y)
            or not 0 < x < 1
            or not 0 < y < 1
        ):
            raise CalibrationError(CALIBRATION_INVALID)
        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or not metrics.foreground
            or metrics.width < 800
            or metrics.height < 600
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        return metrics

    def click_ratio(self, hwnd: int, point: Mapping[str, object]) -> None:
        """Revalidate the window, then move and click inside its client area."""
        metrics = self._validated_click_metrics(hwnd, point)
        x, y = screen_point_from_ratio(point, metrics)
        if not self._point_targets_bound_window(hwnd, (x, y)):
            raise CalibrationError(CALIBRATION_WINDOW)
        if not self.user32.SetCursorPos(x, y):
            raise CalibrationError(CALIBRATION_WINDOW)
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def click_bound_process_ratio(
        self, hwnd: int, point: Mapping[str, object]
    ) -> None:
        """Click a ratio only inside the bound WeChat process and client bounds."""
        try:
            ratio_x = point["x"]
            ratio_y = point["y"]
        except (KeyError, TypeError):
            raise CalibrationError(CALIBRATION_INVALID) from None
        if (
            not _is_finite_number(ratio_x)
            or not _is_finite_number(ratio_y)
            or not 0 < ratio_x < 1
            or not 0 < ratio_y < 1
        ):
            raise CalibrationError(CALIBRATION_INVALID)
        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or metrics.width < 800
            or metrics.height < 600
            or not self._foreground_targets_bound_process(hwnd)
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        screen_point = screen_point_from_ratio(point, metrics)
        if not self._point_targets_bound_process(hwnd, screen_point):
            raise CalibrationError(CALIBRATION_WINDOW)
        if not self.user32.SetCursorPos(*screen_point):
            raise CalibrationError(CALIBRATION_WINDOW)
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def move_bound_process_ratio(
        self, hwnd: int, point: Mapping[str, object]
    ) -> None:
        """Move without clicking, only to a bound-process client point."""
        try:
            ratio_x = point["x"]
            ratio_y = point["y"]
        except (KeyError, TypeError):
            raise CalibrationError(CALIBRATION_INVALID) from None
        if (
            not _is_finite_number(ratio_x)
            or not _is_finite_number(ratio_y)
            or not 0 < ratio_x < 1
            or not 0 < ratio_y < 1
        ):
            raise CalibrationError(CALIBRATION_INVALID)
        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or not self._foreground_targets_bound_process(hwnd)
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        screen_point = screen_point_from_ratio(point, metrics)
        if not self._point_targets_bound_process(hwnd, screen_point):
            raise CalibrationError(CALIBRATION_WINDOW)
        if not self.user32.SetCursorPos(*screen_point):
            raise CalibrationError(CALIBRATION_WINDOW)

    def capture_bound_process_client(self, hwnd: int):
        """Capture the main client rectangle while its same-process popup is active."""
        from PIL import ImageGrab

        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or metrics.width < 800
            or metrics.height < 600
            or not self._foreground_targets_bound_process(hwnd)
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        try:
            return ImageGrab.grab(
                bbox=(
                    metrics.left,
                    metrics.top,
                    metrics.left + metrics.width,
                    metrics.top + metrics.height,
                ),
                all_screens=True,
            ).convert("RGB")
        except Exception:
            raise CalibrationError(CALIBRATION_WINDOW) from None

    def click_receive_ratio(
        self,
        hwnd: int,
        point: Mapping[str, object],
        calibration: object,
    ) -> None:
        """Click inside a validated foreground receive window or popup."""
        try:
            ratio_x = point["x"]
            ratio_y = point["y"]
        except (KeyError, TypeError):
            raise CalibrationError(CALIBRATION_INVALID) from None
        if (
            not _is_finite_number(ratio_x)
            or not _is_finite_number(ratio_y)
            or not 0 < ratio_x < 1
            or not 0 < ratio_y < 1
        ):
            raise CalibrationError(CALIBRATION_INVALID)
        metrics = self.get_client_metrics(hwnd)
        validate_receive_runtime_metrics(calibration, metrics)
        x, y = screen_point_from_ratio(point, metrics)
        if not self._point_targets_bound_window(hwnd, (x, y)):
            raise CalibrationError(CALIBRATION_WINDOW)
        if not self.user32.SetCursorPos(x, y):
            raise CalibrationError(CALIBRATION_WINDOW)
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def click_calibrated_search_result(
        self,
        hwnd: int,
        point: Mapping[str, object],
    ) -> None:
        """Click a calibrated result in the bound WeChat process."""
        try:
            ratio_x = point["x"]
            ratio_y = point["y"]
        except (KeyError, TypeError):
            raise CalibrationError(CALIBRATION_INVALID) from None
        if (
            not _is_finite_number(ratio_x)
            or not _is_finite_number(ratio_y)
            or not 0 < ratio_x < 1
            or not 0 < ratio_y < 1
        ):
            raise CalibrationError(CALIBRATION_INVALID)
        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or metrics.width < 800
            or metrics.height < 600
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        bound = self._ensure_bound_window_identity(hwnd)
        foreground = int(self.user32.GetForegroundWindow() or 0)
        foreground_root = int(
            self.user32.GetAncestor(foreground, GA_ROOT) or 0
        )
        if not foreground_root:
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        foreground_identity = self._query_window_identity(foreground_root)
        if (
            foreground_identity.pid != bound.pid
            or foreground_identity.image != bound.image
        ):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        x, y = screen_point_from_ratio(point, metrics)
        child = int(self.user32.WindowFromPoint(_POINT(x, y)) or 0)
        root = int(self.user32.GetAncestor(child, GA_ROOT) or 0)
        if not child or not root or not self.user32.IsWindowVisible(root):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        target = self._query_window_identity(root)
        if target.pid != bound.pid or target.image != bound.image:
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        if not self.user32.SetCursorPos(x, y):
            raise CalibrationError(CONTACT_SELECTION_FAILED)
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def hotkey_ctrl(self, virtual_key: int) -> None:
        """Press Ctrl with one virtual key and release both keys."""
        self.user32.keybd_event(VK_CONTROL, 0, 0, 0)
        self.user32.keybd_event(virtual_key, 0, 0, 0)
        self.user32.keybd_event(virtual_key, 0, KEYEVENTF_KEYUP, 0)
        self.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def press_key(self, virtual_key: int) -> None:
        """Press and release one virtual key."""
        self.user32.keybd_event(virtual_key, 0, 0, 0)
        self.user32.keybd_event(virtual_key, 0, KEYEVENTF_KEYUP, 0)

    def press_key_bound_process(
        self,
        hwnd: int,
        virtual_key: int,
    ) -> None:
        """Press a key only while the bound WeChat process owns foreground."""
        metrics = self.get_client_metrics(hwnd)
        if (
            not metrics.visible
            or not metrics.maximized
            or not self._foreground_targets_bound_process(hwnd)
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        self.press_key(virtual_key)

    def copy_image_to_clipboard(self, image_path: str) -> None:
        """Copy one image as DIB using Pillow without logging the path."""
        from PIL import Image

        bitmap = io.BytesIO()
        with Image.open(image_path) as source:
            source.convert("RGB").save(bitmap, "BMP")
        dib = bitmap.getvalue()[14:]

        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        if not handle:
            raise OSError("GlobalAlloc failed")
        transferred = False
        clipboard_open = False
        try:
            address = self.kernel32.GlobalLock(handle)
            if not address:
                raise OSError("GlobalLock failed")
            try:
                ctypes.memmove(address, dib, len(dib))
            finally:
                self.kernel32.GlobalUnlock(handle)

            if not self.user32.OpenClipboard(0):
                raise OSError("OpenClipboard failed")
            clipboard_open = True
            if not self.user32.EmptyClipboard():
                raise OSError("EmptyClipboard failed")
            if not self.user32.SetClipboardData(CF_DIB, handle):
                raise OSError("SetClipboardData failed")
            transferred = True
        finally:
            if clipboard_open:
                self.user32.CloseClipboard()
            if not transferred:
                self.kernel32.GlobalFree(handle)

    def _run_low_level_hooks(self, mouse_callback, keyboard_callback) -> None:
        mouse_hook = None
        keyboard_hook = None

        @_HOOKPROC
        def mouse_proc(code, message, data):
            if code >= 0:
                hook_data = ctypes.cast(
                    data, ctypes.POINTER(_MSLLHOOKSTRUCT)
                ).contents
                if mouse_callback(
                    int(message), (hook_data.pt.x, hook_data.pt.y)
                ):
                    return 1
            return self.user32.CallNextHookEx(
                mouse_hook, code, message, data
            )

        @_HOOKPROC
        def keyboard_proc(code, message, data):
            if code >= 0:
                hook_data = ctypes.cast(
                    data, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                ).contents
                if keyboard_callback(int(message), int(hook_data.vkCode)):
                    return 1
            return self.user32.CallNextHookEx(
                keyboard_hook, code, message, data
            )

        module = self.kernel32.GetModuleHandleW(None)
        try:
            mouse_hook = self.user32.SetWindowsHookExW(
                WH_MOUSE_LL, mouse_proc, module, 0
            )
            if not mouse_hook:
                raise OSError("SetWindowsHookExW mouse failed")
            keyboard_hook = self.user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, keyboard_proc, module, 0
            )
            if not keyboard_hook:
                raise OSError("SetWindowsHookExW keyboard failed")

            message = wintypes.MSG()
            while True:
                status = self.user32.GetMessageW(
                    ctypes.byref(message), 0, 0, 0
                )
                if status == -1:
                    raise OSError("GetMessageW failed")
                if status == 0:
                    break
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if keyboard_hook:
                self.user32.UnhookWindowsHookEx(keyboard_hook)
            if mouse_hook:
                self.user32.UnhookWindowsHookEx(mouse_hook)


def ratio_from_screen_point(
    point: tuple[int, int], metrics: ClientMetrics
) -> dict[str, float]:
    return {
        "x": (point[0] - metrics.left) / metrics.width,
        "y": (point[1] - metrics.top) / metrics.height,
    }


def screen_point_from_ratio(
    point: Mapping[str, object], metrics: ClientMetrics
) -> tuple[int, int]:
    x_offset = min(
        metrics.width - 1,
        max(0, round(float(point["x"]) * metrics.width)),
    )
    y_offset = min(
        metrics.height - 1,
        max(0, round(float(point["y"]) * metrics.height)),
    )
    return (
        metrics.left + x_offset,
        metrics.top + y_offset,
    )


def build_calibration(
    points: Mapping[str, tuple[int, int]], metrics: ClientMetrics
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "completed": True,
        "coordinate_space": "client_area_ratio",
        "points": {
            name: ratio_from_screen_point(points[name], metrics)
            for name in POINT_NAMES
        },
        "reference": {
            "client_width": metrics.width,
            "client_height": metrics.height,
            "aspect_ratio": metrics.width / metrics.height,
            "dpi": metrics.dpi,
        },
    }


def _raise_invalid() -> None:
    raise CalibrationError(CALIBRATION_INVALID)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_calibration(value: object) -> dict[str, object]:
    """Return the validated mapping or raise CalibrationError with a fixed code."""
    if not isinstance(value, Mapping) or not value:
        raise CalibrationError(CALIBRATION_REQUIRED)

    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool):
        _raise_invalid()
    if schema_version != 1:
        raise CalibrationError(CALIBRATION_REQUIRED)
    if not isinstance(schema_version, int):
        _raise_invalid()

    completed = value.get("completed")
    if completed is False:
        raise CalibrationError(CALIBRATION_REQUIRED)
    if completed is not True:
        _raise_invalid()

    if set(value) != {
        "schema_version",
        "completed",
        "coordinate_space",
        "points",
        "reference",
    }:
        _raise_invalid()

    if value.get("coordinate_space") != "client_area_ratio":
        _raise_invalid()

    points = value.get("points")
    reference = value.get("reference")
    if not isinstance(points, Mapping) or not isinstance(reference, Mapping):
        _raise_invalid()
    if set(points) != set(POINT_NAMES):
        _raise_invalid()
    if set(reference) != {
        "client_width",
        "client_height",
        "aspect_ratio",
        "dpi",
    }:
        _raise_invalid()

    for name in POINT_NAMES:
        point = points.get(name)
        if not isinstance(point, Mapping) or set(point) != {"x", "y"}:
            _raise_invalid()
        for axis in ("x", "y"):
            coordinate = point.get(axis)
            if not _is_finite_number(coordinate) or not 0 < coordinate < 1:
                _raise_invalid()

    for name in ("client_width", "client_height", "dpi"):
        number = reference.get(name)
        if (
            not _is_finite_number(number)
            or not isinstance(number, int)
            or number <= 0
        ):
            _raise_invalid()

    aspect_ratio = reference.get("aspect_ratio")
    if not _is_finite_number(aspect_ratio) or aspect_ratio <= 0:
        _raise_invalid()

    return value


def validate_runtime_metrics(
    calibration: object, metrics: ClientMetrics, tolerance: float = 0.05
) -> dict[str, object]:
    """Validate schema, window state, minimum client size, DPI, and aspect drift."""
    validated = validate_calibration(calibration)

    if (
        not metrics.visible
        or not metrics.maximized
        or not metrics.foreground
        or metrics.width < 800
        or metrics.height < 600
    ):
        raise CalibrationError(CALIBRATION_WINDOW)

    reference = validated["reference"]
    if metrics.dpi != reference["dpi"]:
        raise CalibrationError(RECALIBRATION_REQUIRED)

    reference_aspect = float(reference["aspect_ratio"])
    current_aspect = metrics.width / metrics.height
    minimum_aspect = reference_aspect * (1 - tolerance)
    maximum_aspect = reference_aspect * (1 + tolerance)
    if current_aspect < minimum_aspect or current_aspect > maximum_aspect:
        raise CalibrationError(RECALIBRATION_REQUIRED)

    return validated


def validate_receive_runtime_metrics(
    calibration: object,
    metrics: ClientMetrics,
    tolerance: float = 0.05,
) -> dict[str, object]:
    """Allow the calibrated main window or a foreground WeChat money popup."""
    try:
        return validate_runtime_metrics(calibration, metrics, tolerance)
    except CalibrationError:
        validated = validate_calibration(calibration)
        is_receive_popup = (
            metrics.visible
            and metrics.foreground
            and not metrics.maximized
            and 240 <= metrics.width < 800
            and 320 <= metrics.height <= 900
        )
        if not is_receive_popup:
            raise
        if metrics.dpi != validated["reference"]["dpi"]:
            raise CalibrationError(RECALIBRATION_REQUIRED)
        return validated
