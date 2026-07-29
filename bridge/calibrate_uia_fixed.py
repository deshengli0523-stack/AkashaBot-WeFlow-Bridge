import argparse
import ctypes
import json
import msvcrt
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from uia_support import (
    CALIBRATION_BUSY,
    CALIBRATION_INVALID,
    CALIBRATION_REQUIRED,
    CALIBRATION_WINDOW,
    RECALIBRATION_REQUIRED,
    POINT_NAMES,
    CalibrationError,
    ClientMetrics,
    Win32WeChatDriver,
    build_calibration,
    validate_calibration,
)
from favorite_sticker import (
    FAVORITE_POINT_NAMES,
    build_manifest as build_favorite_manifest,
    write_layout as write_favorite_layout,
)


STEP_LABELS = {
    "search_box": "搜索框",
    "first_result": "第一条搜索结果或会话",
    "message_input": "消息输入框",
    "send_button": "发送按钮",
}

FAVORITE_STEP_LABELS = {
    "smile_entry": "左下角表情入口（笑脸）",
    "favorite_tab": "收藏表情标签（心形）",
    "grid_first": "收藏网格第 1 格中心",
    "grid_last": "收藏网格第 20 格中心",
}

_EXIT_CODES = {
    CALIBRATION_INVALID: 20,
    CALIBRATION_WINDOW: 21,
    CALIBRATION_REQUIRED: 22,
    CALIBRATION_BUSY: 23,
    RECALIBRATION_REQUIRED: 24,
}

_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x0080
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_DUPLICATE_SAME_ACCESS = 0x00000002
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CREATE_FILE_W = _KERNEL32.CreateFileW
_CREATE_FILE_W.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CREATE_FILE_W.restype = wintypes.HANDLE
_GET_FILE_INFORMATION = _KERNEL32.GetFileInformationByHandle
_GET_FILE_INFORMATION.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
]
_GET_FILE_INFORMATION.restype = wintypes.BOOL
_GET_CURRENT_PROCESS = _KERNEL32.GetCurrentProcess
_GET_CURRENT_PROCESS.argtypes = []
_GET_CURRENT_PROCESS.restype = wintypes.HANDLE
_DUPLICATE_HANDLE = _KERNEL32.DuplicateHandle
_DUPLICATE_HANDLE.argtypes = [
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
_DUPLICATE_HANDLE.restype = wintypes.BOOL
_SET_FILE_INFORMATION = _KERNEL32.SetFileInformationByHandle
_SET_FILE_INFORMATION.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_SET_FILE_INFORMATION.restype = wintypes.BOOL
_CLOSE_HANDLE = _KERNEL32.CloseHandle
_CLOSE_HANDLE.argtypes = [wintypes.HANDLE]
_CLOSE_HANDLE.restype = wintypes.BOOL


def _capture_signature(metrics: ClientMetrics) -> tuple[int, int, int, int, int, int]:
    return (
        metrics.hwnd,
        metrics.left,
        metrics.top,
        metrics.width,
        metrics.height,
        metrics.dpi,
    )


def _capture_metrics(driver) -> ClientMetrics:
    hwnd = driver.find_wechat_window()
    metrics = driver.get_client_metrics(hwnd)
    if (
        metrics.hwnd != hwnd
        or not metrics.visible
        or not metrics.maximized
        or not metrics.foreground
        or metrics.width < 800
        or metrics.height < 600
        or metrics.dpi <= 0
    ):
        raise CalibrationError(CALIBRATION_WINDOW)
    return metrics


def _report_window_failure(output_fn) -> None:
    output_fn(f"失败: {CALIBRATION_WINDOW}")


def collect_calibration(
    driver,
    confirm_fn=input,
    output_fn=print,
    *,
    reactivate_after_confirm=False,
) -> dict[str, object] | None:
    """Capture four swallowed clicks in POINT_NAMES order and ask for final confirmation."""
    points: dict[str, tuple[int, int]] = {}
    reference_metrics = None
    signature = None
    step_index = 0

    while True:
        try:
            metrics = _capture_metrics(driver)
        except CalibrationError as error:
            if error.code != CALIBRATION_WINDOW:
                raise
            _report_window_failure(output_fn)
            continue

        current_signature = _capture_signature(metrics)
        if signature is None:
            signature = current_signature
            reference_metrics = metrics
        elif current_signature != signature:
            points.clear()
            signature = current_signature
            reference_metrics = metrics
            step_index = 0
            _report_window_failure(output_fn)

        if step_index == len(POINT_NAMES):
            calibration = build_calibration(points, reference_metrics)
            output_fn("成功: 四点标定")
            answer = confirm_fn("输入 y 确认保存，其他输入取消: ")
            if str(answer).strip().lower() not in {"y", "yes"}:
                output_fn("失败: 用户取消")
                return None
            if reactivate_after_confirm:
                output_fn("步骤: 正在切回目标微信窗口继续收藏表情标定")
                driver.activate_receive_window(reference_metrics.hwnd)
            return calibration

        point_name = POINT_NAMES[step_index]
        label = STEP_LABELS[point_name]
        output_fn(f"步骤: {label}")
        try:
            point = driver.capture_swallowed_click(metrics.hwnd)
        except CalibrationError as error:
            if error.code != CALIBRATION_WINDOW:
                raise
            _report_window_failure(output_fn)
            continue

        if point is None:
            output_fn("失败: 用户取消")
            return None

        points[point_name] = point
        output_fn(f"成功: {label}")
        step_index += 1


def collect_favorite_sticker_calibration(
    driver,
    confirm_fn=input,
    output_fn=print,
    sleep_fn=time.sleep,
):
    """Capture fixed entry/grid points without selecting a sticker."""
    metrics = _capture_metrics(driver)
    signature = _capture_signature(metrics)
    points: dict[str, tuple[int, int]] = {}

    output_fn("步骤: 请关闭表情面板并保持目标微信窗口前台最大化")
    for point_name in FAVORITE_POINT_NAMES:
        current = driver.get_client_metrics(metrics.hwnd)
        if (
            not current.visible
            or not current.maximized
            or current.width < 800
            or current.height < 600
            or _capture_signature(current) != signature
        ):
            raise CalibrationError(CALIBRATION_WINDOW)
        output_fn(f"步骤: {FAVORITE_STEP_LABELS[point_name]}")
        point = driver.capture_swallowed_click(
            metrics.hwnd,
            allow_same_process_popup=point_name != "smile_entry",
        )
        if point is None:
            output_fn("失败: 用户取消")
            return None
        points[point_name] = point
        output_fn(f"成功: {FAVORITE_STEP_LABELS[point_name]}")

        ratio = {
            "x": (point[0] - metrics.left) / metrics.width,
            "y": (point[1] - metrics.top) / metrics.height,
        }
        if point_name == "smile_entry":
            driver.click_ratio(metrics.hwnd, ratio)
            sleep_fn(0.45)
        elif point_name == "favorite_tab":
            driver.click_bound_process_ratio(metrics.hwnd, ratio)
            sleep_fn(0.55)

    manifest = build_favorite_manifest(points, metrics)
    answer = confirm_fn("输入 y 确认保存收藏表情槽位标定，其他输入取消: ")
    if str(answer).strip().lower() not in {"y", "yes"}:
        output_fn("失败: 用户取消")
        return None
    return manifest


def _backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _close_handle(handle: int) -> None:
    if not _CLOSE_HANDLE(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _get_handle_identity(handle: int) -> tuple[int, int]:
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _GET_FILE_INFORMATION(
        wintypes.HANDLE(handle), ctypes.byref(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return (information.dwVolumeSerialNumber, file_index)


def _create_temp_handle(path: Path) -> int:
    handle = _CREATE_FILE_W(
        str(path),
        _GENERIC_WRITE | _FILE_READ_ATTRIBUTES | _DELETE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _CREATE_NEW,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    value = int(handle or 0)
    if value == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "temporary file already exists", str(path))
        raise ctypes.WinError(error)
    return value


def _duplicate_handle(handle: int) -> int:
    process = _GET_CURRENT_PROCESS()
    duplicate = wintypes.HANDLE()
    if not _DUPLICATE_HANDLE(
        process,
        wintypes.HANDLE(handle),
        process,
        ctypes.byref(duplicate),
        0,
        False,
        _DUPLICATE_SAME_ACCESS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(duplicate.value)


def _delete_and_close_owned_handle(handle: int) -> None:
    cleanup_error = None
    try:
        _mark_handle_for_deletion(handle)
    except Exception as error:
        cleanup_error = error
    try:
        _close_handle(handle)
    except Exception as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        raise cleanup_error


def _open_owned_temp(path: Path):
    cleanup_handle = _create_temp_handle(path)
    try:
        cleanup_identity = _get_handle_identity(cleanup_handle)
    except Exception:
        _close_handle(cleanup_handle)
        raise

    try:
        write_handle = _duplicate_handle(cleanup_handle)
    except Exception:
        _delete_and_close_owned_handle(cleanup_handle)
        raise

    try:
        descriptor = msvcrt.open_osfhandle(
            write_handle, os.O_WRONLY | os.O_BINARY
        )
    except Exception:
        _close_handle(write_handle)
        _delete_and_close_owned_handle(cleanup_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "wb")
    except Exception:
        os.close(descriptor)
        _delete_and_close_owned_handle(cleanup_handle)
        raise

    try:
        write_identity = _get_handle_identity(
            msvcrt.get_osfhandle(stream.fileno())
        )
        if write_identity != cleanup_identity:
            raise OSError("temporary file identity mismatch")
        return stream, cleanup_handle
    except Exception:
        stream.close()
        _close_handle(cleanup_handle)
        raise


def _mark_handle_for_deletion(handle: int) -> None:
    disposition = _FILE_DISPOSITION_INFO(1)
    if not _SET_FILE_INFORMATION(
        wintypes.HANDLE(handle),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _reject_json_constant(_value: str):
    raise ValueError


def _create_backup(
    config_path: Path, backup_dir: Path, original_bytes: bytes
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _backup_stamp()
    candidate = backup_dir / stamp
    while True:
        try:
            candidate.mkdir()
            break
        except FileExistsError:
            candidate = backup_dir / f"{stamp}-{_uuid_hex()[:8]}"

    backup_path = candidate / config_path.name
    try:
        with backup_path.open("xb") as stream:
            stream.write(original_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            backup_path.unlink(missing_ok=True)
            candidate.rmdir()
        finally:
            raise
    return backup_path


def persist_calibration(
    config_path: str,
    backup_dir: str,
    calibration: Mapping[str, object],
    replace_fn=os.replace,
) -> str:
    """Back up the old config, fsync a same-directory temp file, and atomically replace."""
    validated = validate_calibration(calibration)
    target = Path(config_path)
    original_bytes = target.read_bytes()
    try:
        config = json.loads(
            original_bytes.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise CalibrationError(CALIBRATION_INVALID) from None
    if not isinstance(config, dict):
        raise CalibrationError(CALIBRATION_INVALID)

    replacement = dict(config)
    replacement["uia_fixed_calibration"] = validated
    try:
        rendered = (
            json.dumps(
                replacement,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise CalibrationError(CALIBRATION_INVALID) from None

    backup_path = _create_backup(target, Path(backup_dir), original_bytes)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    cleanup_handle = None
    cleanup_owned = False
    try:
        try:
            stream, cleanup_handle = _open_owned_temp(temporary)
        except FileExistsError:
            raise
        except Exception:
            raise CalibrationError(CALIBRATION_INVALID) from None
        cleanup_owned = True
        with stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        replace_fn(str(temporary), str(target))
        cleanup_owned = False
    finally:
        if cleanup_handle is not None:
            cleanup_error = None
            if cleanup_owned:
                try:
                    _mark_handle_for_deletion(cleanup_handle)
                except Exception:
                    cleanup_error = CalibrationError(CALIBRATION_INVALID)
            try:
                _close_handle(cleanup_handle)
            except Exception:
                cleanup_error = CalibrationError(CALIBRATION_INVALID)
            if cleanup_error is not None:
                raise cleanup_error from None
    return str(backup_path)


def run_calibration(
    config_path: str,
    backup_dir: str,
    favorite_state_dir: str | None = None,
    driver=None,
    confirm_fn=input,
    output_fn=print,
) -> int:
    """Return 0 on save, 2 on user cancellation, and raise CalibrationError otherwise."""
    try:
        active_driver = Win32WeChatDriver() if driver is None else driver
        calibration = collect_calibration(
            active_driver,
            confirm_fn,
            output_fn,
            reactivate_after_confirm=bool(favorite_state_dir),
        )
        if calibration is None:
            return 2
        favorite_manifest = None
        if favorite_state_dir:
            favorite_manifest = collect_favorite_sticker_calibration(
                active_driver,
                confirm_fn,
                output_fn,
            )
            if favorite_manifest is None:
                return 2
        persist_calibration(config_path, backup_dir, calibration)
        if favorite_manifest is not None:
            write_favorite_layout(
                favorite_state_dir,
                favorite_manifest,
            )
            output_fn("成功: 收藏表情槽位标定已保存到本机状态目录")
        output_fn("成功: 标定已保存")
        return 0
    except CalibrationError:
        raise
    except Exception:
        raise CalibrationError(CALIBRATION_INVALID) from None


class _FixedCodeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CalibrationError(CALIBRATION_INVALID)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse required --config and --backup-dir arguments and emit only fixed codes on error."""
    try:
        parser = _FixedCodeArgumentParser(add_help=False)
        parser.add_argument("--config", required=True)
        parser.add_argument("--backup-dir", required=True)
        parser.add_argument("--favorite-state-dir")
        arguments = parser.parse_args(argv)
        return run_calibration(
            arguments.config,
            arguments.backup_dir,
            favorite_state_dir=arguments.favorite_state_dir,
        )
    except CalibrationError as error:
        exit_code = _EXIT_CODES.get(error.code, 20)
        code = error.code if error.code in _EXIT_CODES else CALIBRATION_INVALID
        print(code)
        return exit_code
    except Exception:
        print(CALIBRATION_INVALID)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
