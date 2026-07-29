"""Private favorite-sticker calibration, matching, receipt, and idempotency helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageOps


STICKER_KEYS = tuple(f"slot_{index:02d}" for index in range(1, 21))
FAVORITE_POINT_NAMES = (
    "smile_entry",
    "favorite_tab",
    "grid_first",
    "grid_last",
)
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
GRID_ROWS = 4
GRID_COLUMNS = 5
TEMPLATE_FRAME_COUNT = 4
TEMPLATE_CROP_SCALE = 0.78
MIN_MATCH_SIMILARITY = 0.76
MIN_MATCH_MARGIN = 0.055
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
MAX_TEMPLATE_DIMENSION = 512
MAX_CAPTURE_DIMENSION = 8192

STICKER_CALIBRATION_REQUIRED = "E_UIA_STICKER_CALIBRATION_REQUIRED"
STICKER_CALIBRATION_INVALID = "E_UIA_STICKER_CALIBRATION_INVALID"
STICKER_PANEL_FAILED = "E_UIA_STICKER_PANEL_FAILED"
STICKER_TEMPLATE_MISSING = "E_UIA_STICKER_TEMPLATE_MISSING"
STICKER_MATCH_LOW_CONFIDENCE = "E_UIA_STICKER_MATCH_LOW_CONFIDENCE"
STICKER_MATCH_AMBIGUOUS = "E_UIA_STICKER_MATCH_AMBIGUOUS"
STICKER_CONFIRMATION_UNAVAILABLE = "E_UIA_STICKER_CONFIRMATION_UNAVAILABLE"
STICKER_CONFIRMATION_UNKNOWN = "E_UIA_STICKER_CONFIRMATION_UNKNOWN"
STICKER_COMMIT_UNKNOWN = "E_UIA_STICKER_COMMIT_UNKNOWN"
STICKER_REQUEST_IN_PROGRESS = "E_UIA_STICKER_REQUEST_IN_PROGRESS"
STICKER_REQUEST_CAPACITY = "E_UIA_STICKER_REQUEST_CAPACITY"
STICKER_QUEUE_EXPIRED = "E_UIA_STICKER_QUEUE_EXPIRED"


class FavoriteStickerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


def favorite_bundle_root(state_dir: str | os.PathLike[str]) -> Path:
    state_root = Path(state_dir).resolve()
    candidate = state_root / "favorite-sticker-templates"
    if os.path.lexists(candidate):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID) from None
        if resolved.parent != state_root:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    return candidate


def _is_reparse(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & 0x400)


def _require_plain_path(path: Path, *, directory: bool) -> None:
    if not os.path.lexists(path):
        raise FavoriteStickerError(
            STICKER_CALIBRATION_INVALID
            if directory
            else STICKER_TEMPLATE_MISSING
        )
    if _is_reparse(path):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    if directory and not path.is_dir():
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    if not directory and not path.is_file():
        raise FavoriteStickerError(STICKER_TEMPLATE_MISSING)


def _finite_ratio(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(normalized) and 0.0 < normalized < 1.0


def _validate_point(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y"}:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    if not _finite_ratio(value.get("x")) or not _finite_ratio(value.get("y")):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    return {"x": float(value["x"]), "y": float(value["y"])}


def _validate_reference(value: object) -> dict[str, object]:
    names = {"client_width", "client_height", "aspect_ratio", "dpi"}
    if not isinstance(value, Mapping) or set(value) != names:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    for name in ("client_width", "client_height", "dpi"):
        number = value.get(name)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    aspect = value.get("aspect_ratio")
    try:
        normalized_aspect = float(aspect)
    except (OverflowError, TypeError, ValueError):
        normalized_aspect = float("nan")
    if (
        isinstance(aspect, bool)
        or not isinstance(aspect, (int, float))
        or not math.isfinite(normalized_aspect)
        or normalized_aspect <= 0
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    return {
        "client_width": int(value["client_width"]),
        "client_height": int(value["client_height"]),
        "aspect_ratio": normalized_aspect,
        "dpi": int(value["dpi"]),
    }


def validate_manifest(value: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "completed",
        "coordinate_space",
        "points",
        "reference",
        "grid",
        "templates",
    }
    if not isinstance(value, Mapping) or not value:
        raise FavoriteStickerError(STICKER_CALIBRATION_REQUIRED)
    if set(value) != expected:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MANIFEST_SCHEMA_VERSION
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_REQUIRED)
    if value.get("completed") is not True:
        raise FavoriteStickerError(STICKER_CALIBRATION_REQUIRED)
    if value.get("coordinate_space") != "client_area_ratio":
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)

    raw_points = value.get("points")
    if not isinstance(raw_points, Mapping) or set(raw_points) != set(
        FAVORITE_POINT_NAMES
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    points = {name: _validate_point(raw_points[name]) for name in FAVORITE_POINT_NAMES}
    first = points["grid_first"]
    last = points["grid_last"]
    if first["x"] >= last["x"] or first["y"] >= last["y"]:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)

    raw_grid = value.get("grid")
    if not isinstance(raw_grid, Mapping) or set(raw_grid) != {
        "rows",
        "columns",
        "crop_scale",
        "template_frames",
    }:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    try:
        crop_scale = float(raw_grid.get("crop_scale"))
    except (OverflowError, TypeError, ValueError):
        crop_scale = float("nan")
    if (
        raw_grid.get("rows") != GRID_ROWS
        or raw_grid.get("columns") != GRID_COLUMNS
        or raw_grid.get("template_frames") != TEMPLATE_FRAME_COUNT
        or isinstance(raw_grid.get("crop_scale"), bool)
        or not isinstance(raw_grid.get("crop_scale"), (int, float))
        or not math.isfinite(crop_scale)
        or not 0.5 <= crop_scale <= 0.95
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)

    raw_templates = value.get("templates")
    if not isinstance(raw_templates, Mapping) or set(raw_templates) != set(
        STICKER_KEYS
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    templates: dict[str, tuple[str, ...]] = {}
    for key in STICKER_KEYS:
        names = raw_templates.get(key)
        if (
            not isinstance(names, (list, tuple))
            or len(names) != TEMPLATE_FRAME_COUNT
            or any(not isinstance(name, str) for name in names)
        ):
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        expected_names = [
            f"{key}-frame_{index:02d}.png"
            for index in range(1, TEMPLATE_FRAME_COUNT + 1)
        ]
        if list(names) != expected_names:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        templates[key] = tuple(names)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "completed": True,
        "coordinate_space": "client_area_ratio",
        "points": points,
        "reference": _validate_reference(value.get("reference")),
        "grid": {
            "rows": GRID_ROWS,
            "columns": GRID_COLUMNS,
            "crop_scale": crop_scale,
            "template_frames": TEMPLATE_FRAME_COUNT,
        },
        "templates": templates,
    }


def _ratio_point(point: tuple[int, int], metrics) -> dict[str, float]:
    return {
        "x": (point[0] - metrics.left) / metrics.width,
        "y": (point[1] - metrics.top) / metrics.height,
    }


def build_manifest(
    points: Mapping[str, tuple[int, int]],
    metrics,
) -> dict[str, object]:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "completed": True,
        "coordinate_space": "client_area_ratio",
        "points": {
            name: _ratio_point(points[name], metrics)
            for name in FAVORITE_POINT_NAMES
        },
        "reference": {
            "client_width": int(metrics.width),
            "client_height": int(metrics.height),
            "aspect_ratio": metrics.width / metrics.height,
            "dpi": int(metrics.dpi),
        },
        "grid": {
            "rows": GRID_ROWS,
            "columns": GRID_COLUMNS,
            "crop_scale": TEMPLATE_CROP_SCALE,
            "template_frames": TEMPLATE_FRAME_COUNT,
        },
        "templates": {
            key: [
                f"{key}-frame_{frame:02d}.png"
                for frame in range(1, TEMPLATE_FRAME_COUNT + 1)
            ]
            for key in STICKER_KEYS
        },
    }
    return validate_manifest(manifest)


def grid_centers(
    manifest: Mapping[str, object],
    width: int,
    height: int,
) -> tuple[tuple[int, int], ...]:
    validated = validate_manifest(manifest)
    first = validated["points"]["grid_first"]
    last = validated["points"]["grid_last"]
    start_x = float(first["x"]) * width
    start_y = float(first["y"]) * height
    step_x = (float(last["x"]) * width - start_x) / (GRID_COLUMNS - 1)
    step_y = (float(last["y"]) * height - start_y) / (GRID_ROWS - 1)
    if step_x < 20 or step_y < 20:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    return tuple(
        (
            round(start_x + column * step_x),
            round(start_y + row * step_y),
        )
        for row in range(GRID_ROWS)
        for column in range(GRID_COLUMNS)
    )


def crop_grid(
    image: Image.Image,
    manifest: Mapping[str, object],
) -> tuple[Image.Image, ...]:
    validated = validate_manifest(manifest)
    centers = grid_centers(validated, image.width, image.height)
    first, last = centers[0], centers[-1]
    step_x = (last[0] - first[0]) / (GRID_COLUMNS - 1)
    step_y = (last[1] - first[1]) / (GRID_ROWS - 1)
    scale = float(validated["grid"]["crop_scale"])
    half_width = max(8, round(step_x * scale / 2))
    half_height = max(8, round(step_y * scale / 2))
    crops = []
    for center_x, center_y in centers:
        box = (
            center_x - half_width,
            center_y - half_height,
            center_x + half_width,
            center_y + half_height,
        )
        if (
            box[0] < 0
            or box[1] < 0
            or box[2] > image.width
            or box[3] > image.height
        ):
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        crops.append(image.crop(box).convert("RGB"))
    return tuple(crops)


def _content_crop(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    corners = (
        gray.getpixel((0, 0)),
        gray.getpixel((gray.width - 1, 0)),
        gray.getpixel((0, gray.height - 1)),
        gray.getpixel((gray.width - 1, gray.height - 1)),
    )
    background = sorted(corners)[len(corners) // 2]
    difference = ImageChops.difference(gray, Image.new("L", gray.size, background))
    mask = difference.point(lambda value: 255 if value >= 10 else 0)
    box = mask.getbbox()
    if box is None:
        return image
    left, top, right, bottom = box
    if right - left < 6 or bottom - top < 6:
        return image
    padding_x = max(1, round((right - left) * 0.05))
    padding_y = max(1, round((bottom - top) * 0.05))
    return image.crop(
        (
            max(0, left - padding_x),
            max(0, top - padding_y),
            min(image.width, right + padding_x),
            min(image.height, bottom + padding_y),
        )
    )


@dataclass(frozen=True)
class PerceptualFingerprint:
    difference_bits: tuple[int, ...]
    average_bits: tuple[int, ...]
    color_histogram: tuple[float, ...]


def _pixel_values(image: Image.Image) -> tuple[int, ...]:
    flattened = getattr(image, "get_flattened_data", None)
    return tuple(flattened() if callable(flattened) else image.getdata())


def fingerprint(image: Image.Image) -> PerceptualFingerprint:
    content = _content_crop(image.convert("RGB"))
    gray = ImageOps.autocontrast(content.convert("L"))
    difference = gray.resize((17, 16), Image.Resampling.LANCZOS)
    difference_pixels = _pixel_values(difference)
    difference_bits = tuple(
        int(
            difference_pixels[row * 17 + column]
            > difference_pixels[row * 17 + column + 1]
        )
        for row in range(16)
        for column in range(16)
    )
    average = gray.resize((16, 16), Image.Resampling.LANCZOS)
    average_pixels = _pixel_values(average)
    average_mean = sum(average_pixels) / len(average_pixels)
    average_bits = tuple(int(value >= average_mean) for value in average_pixels)

    color = content.resize((32, 32), Image.Resampling.LANCZOS)
    channels = color.split()
    histogram_values: list[float] = []
    for channel in channels:
        raw = channel.histogram()
        bins = [sum(raw[index : index + 32]) for index in range(0, 256, 32)]
        total = float(sum(bins)) or 1.0
        histogram_values.extend(value / total for value in bins)
    return PerceptualFingerprint(
        difference_bits,
        average_bits,
        tuple(histogram_values),
    )


def fingerprint_similarity(
    left: PerceptualFingerprint,
    right: PerceptualFingerprint,
) -> float:
    difference_similarity = 1.0 - (
        sum(a != b for a, b in zip(left.difference_bits, right.difference_bits))
        / len(left.difference_bits)
    )
    average_similarity = 1.0 - (
        sum(a != b for a, b in zip(left.average_bits, right.average_bits))
        / len(left.average_bits)
    )
    histogram_similarity = sum(
        min(a, b) for a, b in zip(left.color_histogram, right.color_histogram)
    ) / 3.0
    return (
        difference_similarity * 0.55
        + average_similarity * 0.30
        + histogram_similarity * 0.15
    )


def _safe_write_json(path: Path, value: Mapping[str, object]) -> None:
    rendered = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_bundle(
    state_dir: str | os.PathLike[str],
    manifest: Mapping[str, object],
    screenshots: Sequence[Image.Image],
) -> Path:
    validated = validate_manifest(manifest)
    if len(screenshots) != TEMPLATE_FRAME_COUNT:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    expected_size = (
        int(validated["reference"]["client_width"]),
        int(validated["reference"]["client_height"]),
    )
    if (
        expected_size[0] > MAX_CAPTURE_DIMENSION
        or expected_size[1] > MAX_CAPTURE_DIMENSION
        or any(image.size != expected_size for image in screenshots)
    ):
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    root = favorite_bundle_root(state_dir)
    root.parent.mkdir(parents=True, exist_ok=True)
    _require_plain_path(root.parent, directory=True)
    if root.exists():
        _require_plain_path(root, directory=True)
    temporary = root.with_name(f".{root.name}.{uuid.uuid4().hex}.tmp")
    previous = root.with_name(f".{root.name}.{uuid.uuid4().hex}.old")
    temporary.mkdir()
    try:
        frames = [crop_grid(image, validated) for image in screenshots]
        for slot_index, key in enumerate(STICKER_KEYS):
            for frame_index in range(TEMPLATE_FRAME_COUNT):
                name = validated["templates"][key][frame_index]
                frames[frame_index][slot_index].save(
                    temporary / name,
                    format="PNG",
                    optimize=True,
                )
        _safe_write_json(temporary / MANIFEST_FILENAME, validated)
        if root.exists():
            os.replace(root, previous)
        try:
            os.replace(temporary, root)
        except Exception:
            if previous.exists() and not root.exists():
                os.replace(previous, root)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        return root
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def write_layout(
    state_dir: str | os.PathLike[str],
    manifest: Mapping[str, object],
) -> Path:
    """Persist only calibrated geometry without creating or deleting templates."""
    validated = validate_manifest(manifest)
    root = favorite_bundle_root(state_dir)
    root.parent.mkdir(parents=True, exist_ok=True)
    _require_plain_path(root.parent, directory=True)
    if root.exists():
        _require_plain_path(root, directory=True)
    else:
        root.mkdir()
    path = root / MANIFEST_FILENAME
    if path.exists():
        _require_plain_path(path, directory=False)
    _safe_write_json(path, validated)
    return root


def load_manifest(state_dir: str | os.PathLike[str]) -> tuple[Path, dict[str, object]]:
    root = favorite_bundle_root(state_dir)
    if not root.exists():
        raise FavoriteStickerError(STICKER_CALIBRATION_REQUIRED)
    _require_plain_path(root, directory=True)
    path = root / MANIFEST_FILENAME
    if not path.exists():
        raise FavoriteStickerError(STICKER_CALIBRATION_REQUIRED)
    _require_plain_path(path, directory=False)
    try:
        manifest_size = path.stat().st_size
    except OSError:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID) from None
    if not 0 < manifest_size <= MAX_MANIFEST_BYTES:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:
        raise FavoriteStickerError(STICKER_CALIBRATION_INVALID) from None
    return root, validate_manifest(raw)


class FavoriteStickerLayout:
    """Load calibrated favorite-panel points and map a fixed slot to a click."""

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.root, self.manifest = load_manifest(state_dir)

    def point(self, sticker_key: str) -> dict[str, float]:
        if sticker_key not in STICKER_KEYS:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        slot_index = STICKER_KEYS.index(sticker_key)
        row, column = divmod(slot_index, GRID_COLUMNS)
        first = self.manifest["points"]["grid_first"]
        last = self.manifest["points"]["grid_last"]
        return {
            "x": float(first["x"])
            + column
            * (float(last["x"]) - float(first["x"]))
            / (GRID_COLUMNS - 1),
            "y": float(first["y"])
            + row
            * (float(last["y"]) - float(first["y"]))
            / (GRID_ROWS - 1),
        }


class FavoriteStickerMatcher(FavoriteStickerLayout):
    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        minimum_similarity: float = MIN_MATCH_SIMILARITY,
        minimum_margin: float = MIN_MATCH_MARGIN,
    ) -> None:
        super().__init__(state_dir)
        self.minimum_similarity = float(minimum_similarity)
        self.minimum_margin = float(minimum_margin)
        if (
            not math.isfinite(self.minimum_similarity)
            or not 0.0 < self.minimum_similarity <= 1.0
            or not math.isfinite(self.minimum_margin)
            or not 0.0 < self.minimum_margin <= 1.0
        ):
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        self._templates: dict[str, tuple[PerceptualFingerprint, ...]] = {}
        for key in STICKER_KEYS:
            values = []
            for name in self.manifest["templates"][key]:
                path = self.root / name
                if path.parent != self.root:
                    raise FavoriteStickerError(STICKER_TEMPLATE_MISSING)
                _require_plain_path(path, directory=False)
                try:
                    template_size = path.stat().st_size
                    if not 0 < template_size <= MAX_TEMPLATE_BYTES:
                        raise FavoriteStickerError(STICKER_TEMPLATE_MISSING)
                    with Image.open(path) as source:
                        if (
                            source.width < 8
                            or source.height < 8
                            or source.width > MAX_TEMPLATE_DIMENSION
                            or source.height > MAX_TEMPLATE_DIMENSION
                        ):
                            raise FavoriteStickerError(
                                STICKER_TEMPLATE_MISSING
                            )
                        values.append(fingerprint(source.convert("RGB")))
                except FavoriteStickerError:
                    raise
                except Exception:
                    raise FavoriteStickerError(STICKER_TEMPLATE_MISSING) from None
            self._templates[key] = tuple(values)

    def point(self, image_frames: Sequence[Image.Image], sticker_key: str) -> dict[str, float]:
        if sticker_key not in STICKER_KEYS:
            raise FavoriteStickerError(STICKER_CALIBRATION_INVALID)
        if not image_frames:
            raise FavoriteStickerError(STICKER_PANEL_FAILED)
        frame_size = image_frames[0].size
        if (
            frame_size[0] < 800
            or frame_size[1] < 600
            or frame_size[0] > MAX_CAPTURE_DIMENSION
            or frame_size[1] > MAX_CAPTURE_DIMENSION
            or any(image.size != frame_size for image in image_frames)
        ):
            raise FavoriteStickerError(STICKER_PANEL_FAILED)
        candidate_frame_sets = [crop_grid(image, self.manifest) for image in image_frames]
        candidate_fingerprints = [
            [
                fingerprint(candidate_frame_sets[frame][slot])
                for frame in range(len(candidate_frame_sets))
            ]
            for slot in range(len(STICKER_KEYS))
        ]
        template_values = self._templates[sticker_key]
        scores = []
        for candidates in candidate_fingerprints:
            scores.append(
                max(
                    fingerprint_similarity(template, candidate)
                    for template in template_values
                    for candidate in candidates
                )
            )
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        best_index, best_score = ranked[0]
        second_score = ranked[1][1]
        if best_score < self.minimum_similarity:
            raise FavoriteStickerError(STICKER_MATCH_LOW_CONFIDENCE)
        if best_score - second_score < self.minimum_margin:
            raise FavoriteStickerError(STICKER_MATCH_AMBIGUOUS)
        center = grid_centers(
            self.manifest,
            image_frames[-1].width,
            image_frames[-1].height,
        )[best_index]
        return {
            "x": center[0] / image_frames[-1].width,
            "y": center[1] / image_frames[-1].height,
        }


def _message_identity(row: Mapping[str, object]) -> str:
    stable = str(
        row.get("serverId")
        or row.get("rawid")
        or row.get("localId")
        or row.get("id")
        or ""
    ).strip()
    if stable:
        return stable
    canonical = json.dumps(
        {
            key: row.get(key)
            for key in (
                "timestamp",
                "localType",
                "mediaType",
                "content",
                "rawContent",
                "isSend",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sent_sticker(row: Mapping[str, object]) -> bool:
    sent = str(row.get("isSend") or "").strip().casefold()
    if sent not in {"1", "true", "yes", "on"}:
        return False
    local_type = str(row.get("localType") or row.get("type") or "").strip()
    media_type = str(row.get("mediaType") or "").strip().casefold()
    return local_type == "47" or media_type in {"emoji", "sticker"}


class WeFlowStickerReceipt:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        request_get: Callable | None = None,
        timeout_seconds: float = 8.0,
        poll_seconds: float = 0.35,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_get is None:
            import requests

            request_get = requests.get
        self.base_url = str(base_url).rstrip("/")
        self.access_token = str(access_token)
        self.request_get = request_get
        try:
            normalized_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            normalized_timeout = 8.0
        if not math.isfinite(normalized_timeout):
            normalized_timeout = 8.0
        try:
            normalized_poll = float(poll_seconds)
        except (TypeError, ValueError):
            normalized_poll = 0.35
        if not math.isfinite(normalized_poll):
            normalized_poll = 0.35
        self.timeout_seconds = min(10.0, max(1.0, normalized_timeout))
        self.poll_seconds = min(2.0, max(0.1, normalized_poll))
        self.monotonic = monotonic
        self.sleep = sleep

    def _fetch(
        self,
        session: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> list[Mapping[str, object]]:
        try:
            request_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            request_timeout = 5.0
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            request_timeout = 5.0
        request_timeout = min(5.0, request_timeout)
        response = self.request_get(
            f"{self.base_url}/api/v1/messages",
            params={
                "access_token": self.access_token,
                "talker": str(session),
                "media": "true",
                "image": "false",
                "voice": "false",
                "video": "false",
                "emoji": "true",
                "limit": 30,
                "offset": 0,
            },
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=request_timeout,
        )
        if response.status_code != 200:
            raise FavoriteStickerError(STICKER_CONFIRMATION_UNAVAILABLE)
        payload = response.json()
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("messages", payload.get("data", []))
            if isinstance(payload, Mapping)
            else []
        )
        if not isinstance(rows, list):
            raise FavoriteStickerError(STICKER_CONFIRMATION_UNAVAILABLE)
        return [row for row in rows if isinstance(row, Mapping)]

    def baseline(self, session: str) -> frozenset[str]:
        try:
            return frozenset(
                _message_identity(row)
                for row in self._fetch(session)
                if _sent_sticker(row)
            )
        except FavoriteStickerError:
            raise
        except Exception:
            raise FavoriteStickerError(STICKER_CONFIRMATION_UNAVAILABLE) from None

    def confirm(self, session: str, baseline: frozenset[str]) -> bool:
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            try:
                rows = self._fetch(
                    session,
                    timeout_seconds=min(5.0, remaining),
                )
            except Exception:
                rows = []
            if any(
                _sent_sticker(row) and _message_identity(row) not in baseline
                for row in rows
            ):
                return True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            self.sleep(min(self.poll_seconds, remaining))


@dataclass(frozen=True)
class IdempotentResult:
    confirmed: bool
    error_code: str
    error_stage: str
    committed: bool
    in_progress: bool = False
    cached: bool = False


class RequestIdCache:
    """Bounded process-local cache; committed/unknown requests are never re-clicked."""

    def __init__(
        self,
        *,
        capacity: int = 512,
        ttl_seconds: float = 3600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            normalized_capacity = int(capacity)
        except (TypeError, ValueError, OverflowError):
            normalized_capacity = 512
        self.capacity = max(32, min(4096, normalized_capacity))
        try:
            normalized_ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            normalized_ttl = 3600.0
        if not math.isfinite(normalized_ttl):
            normalized_ttl = 3600.0
        self.ttl_seconds = max(60.0, min(86400.0, normalized_ttl))
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: OrderedDict[
            str, tuple[float, tuple[str, str, str], IdempotentResult | None]
        ] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [
            request_id
            for request_id, (created, _, result) in self._entries.items()
            if result is not None and now - created > self.ttl_seconds
        ]
        for request_id in expired:
            self._entries.pop(request_id, None)
        while len(self._entries) > self.capacity:
            removable = next(
                (
                    request_id
                    for request_id, (_, _, result) in self._entries.items()
                    if result is not None
                ),
                None,
            )
            if removable is None:
                break
            self._entries.pop(removable, None)

    def begin(
        self,
        request_id: str,
        identity: tuple[str, str, str],
    ) -> IdempotentResult | None:
        now = self.monotonic()
        with self._lock:
            self._prune(now)
            existing = self._entries.get(request_id)
            if existing is not None:
                _, previous_identity, result = existing
                if previous_identity != identity:
                    return IdempotentResult(
                        False,
                        "E_OB_INVALID_REQUEST",
                        "request",
                        False,
                        cached=True,
                    )
                if result is None:
                    return IdempotentResult(
                        False,
                        STICKER_REQUEST_IN_PROGRESS,
                        "request",
                        False,
                        in_progress=True,
                        cached=True,
                    )
                return IdempotentResult(
                    result.confirmed,
                    result.error_code,
                    result.error_stage,
                    result.committed,
                    cached=True,
                )
            if len(self._entries) >= self.capacity:
                removable = next(
                    (
                        existing_id
                        for existing_id, (_, _, existing_result)
                        in self._entries.items()
                        if existing_result is not None
                    ),
                    None,
                )
                if removable is None:
                    return IdempotentResult(
                        False,
                        STICKER_REQUEST_CAPACITY,
                        "request",
                        False,
                        cached=True,
                    )
                self._entries.pop(removable, None)
            self._entries[request_id] = (now, identity, None)
            return None

    def finish(
        self,
        request_id: str,
        identity: tuple[str, str, str],
        result: IdempotentResult,
    ) -> None:
        now = self.monotonic()
        with self._lock:
            self._entries[request_id] = (now, identity, result)
            self._entries.move_to_end(request_id)
            self._prune(now)

    def abandon(self, request_id: str, identity: tuple[str, str, str]) -> None:
        with self._lock:
            existing = self._entries.get(request_id)
            if existing is not None and existing[1] == identity and existing[2] is None:
                self._entries.pop(request_id, None)
