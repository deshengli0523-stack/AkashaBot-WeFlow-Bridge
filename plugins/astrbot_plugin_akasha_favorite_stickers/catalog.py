from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CATALOG_SIZE = 20
STATE_DIR_ENV = "AKASHABOT_STATE_DIR"
PERSISTENT_CATALOG_NAME = "favorite-sticker-catalog.json"
EXPECTED_STICKER_KEYS = tuple(
    f"slot_{slot:02d}" for slot in range(1, CATALOG_SIZE + 1)
)
_STICKER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CatalogError(ValueError):
    """The local semantic catalog is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class StickerEntry:
    sticker_id: str
    sticker_key: str
    description: str
    use_when: str
    avoid_when: str


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    if path.is_symlink():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validate_state_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise CatalogError(f"{STATE_DIR_ENV} must be an absolute path")

    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if not current.exists() or not current.is_dir():
                raise CatalogError(f"{STATE_DIR_ENV} must name an existing directory")
            if _is_reparse_point(current):
                raise CatalogError(
                    f"{STATE_DIR_ENV} cannot contain symlink/reparse components"
                )
        return path.resolve(strict=True)
    except OSError as exc:
        raise CatalogError(f"cannot validate {STATE_DIR_ENV}") from exc


def _validate_persistent_catalog(path: Path, state_dir: Path) -> None:
    if path.parent != state_dir or path.name != PERSISTENT_CATALOG_NAME:
        raise CatalogError("persistent catalog must be a direct state file")
    try:
        if not path.exists() or not path.is_file():
            raise CatalogError("persistent catalog is not a regular file")
        if _is_reparse_point(path):
            raise CatalogError("persistent catalog cannot be a symlink/reparse point")
    except OSError as exc:
        raise CatalogError("cannot validate persistent catalog") from exc


def _seed_catalog_atomically(source: Path, destination: Path) -> None:
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise CatalogError("cannot read bundled catalog seed") from exc

    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{PERSISTENT_CATALOG_NAME}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        with os.fdopen(descriptor, "wb") as temporary_file:
            descriptor = None
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        try:
            # A hard-link publish is atomic and never replaces an existing
            # user catalog. A concurrent first start can safely win the race.
            os.link(temporary_name, destination)
        except FileExistsError:
            pass
    except OSError as exc:
        raise CatalogError("cannot seed persistent catalog") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def resolve_catalog_path(
    bundled_catalog: Path,
    state_dir_value: str | None = None,
) -> Path:
    """Return a persistent user catalog, seeding it once from the bundle."""
    if state_dir_value is None:
        state_dir_value = os.environ.get(STATE_DIR_ENV, "")
    if not state_dir_value.strip():
        return bundled_catalog

    state_dir = _validate_state_directory(Path(state_dir_value.strip()))
    destination = state_dir / PERSISTENT_CATALOG_NAME
    if not destination.exists():
        _seed_catalog_atomically(bundled_catalog, destination)
    _validate_persistent_catalog(destination, state_dir)
    return destination


def _required_text(
    record: dict[str, Any],
    key: str,
    *,
    index: int,
    max_length: int = 500,
) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"catalog item {index} has invalid {key}")
    value = value.strip()
    if len(value) > max_length:
        raise CatalogError(f"catalog item {index} {key} is too long")
    return value


def load_catalog(path: Path) -> tuple[StickerEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("cannot read catalog.json") from exc

    if not isinstance(payload, list) or len(payload) != CATALOG_SIZE:
        raise CatalogError(f"catalog must contain exactly {CATALOG_SIZE} items")

    entries: list[StickerEntry] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for index, record in enumerate(payload, start=1):
        if not isinstance(record, dict):
            raise CatalogError(f"catalog item {index} must be an object")
        sticker_id = _required_text(record, "id", index=index, max_length=64)
        if not _STICKER_ID_RE.fullmatch(sticker_id):
            raise CatalogError(
                f"catalog item {index} id must match "
                "[a-z][a-z0-9_-]{0,63}"
            )
        sticker_key = _required_text(
            record,
            "sticker_key",
            index=index,
            max_length=7,
        )
        if sticker_key not in EXPECTED_STICKER_KEYS:
            raise CatalogError(
                f"catalog item {index} has invalid sticker_key"
            )
        if sticker_id in seen_ids:
            raise CatalogError(f"duplicate sticker id: {sticker_id}")
        if sticker_key in seen_keys:
            raise CatalogError(f"duplicate sticker_key: {sticker_key}")

        seen_ids.add(sticker_id)
        seen_keys.add(sticker_key)
        entries.append(
            StickerEntry(
                sticker_id=sticker_id,
                sticker_key=sticker_key,
                description=_required_text(
                    record,
                    "description",
                    index=index,
                ),
                use_when=_required_text(record, "use_when", index=index),
                avoid_when=_required_text(record, "avoid_when", index=index),
            )
        )

    if seen_keys != set(EXPECTED_STICKER_KEYS):
        raise CatalogError("catalog must map every slot_01..slot_20 exactly once")
    return tuple(entries)


def build_tool_description(entries: tuple[StickerEntry, ...]) -> str:
    lines = [
        "向当前微信会话发送一张原生收藏表情。",
        "仅在表情能自然加强语气时调用；正式、严肃、敏感或含义不确定时不要调用。",
        "每轮最多调用一次，不能用它代替必要的文字答复。",
        "只能从下面的语义目录选择 sticker_id：",
    ]
    for entry in entries:
        lines.append(
            f"- {entry.sticker_id}：{entry.description}；"
            f"适用：{entry.use_when}；避免：{entry.avoid_when}"
        )
    return "\n".join(lines)
