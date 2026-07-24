from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
from ctypes import wintypes
from pathlib import Path

_SECRET_FILE_VERSION = b"AKASHA_SECRET_V1\n"
_DPAPI_PREFIX = b"DPAPI\n"
_LOCAL_PREFIX = b"LOCAL\n"
_CIPHER_PREFIX = b"A1"


class SecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _as_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, source_buffer = _as_blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN prevents an unexpected desktop prompt.
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "Akasha contact memory",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, source_buffer = _as_blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _atomic_write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class SecretManager:
    """Protects the stable HMAC key and encrypts raw contact identifiers.

    Windows installations protect the master key with the current user's DPAPI
    profile.  The small authenticated stream construction below is only used
    to encrypt individual database fields with that protected master key.  The
    LOCAL envelope exists for non-Windows development and test environments.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / "secrets.dpapi"
        self._master_key = self._load_or_create()

    def _load_or_create(self) -> bytes:
        if self._path.exists():
            payload = self._path.read_bytes()
            if not payload.startswith(_SECRET_FILE_VERSION):
                raise SecretError("unsupported secret file format")
            envelope = payload[len(_SECRET_FILE_VERSION) :]
            if envelope.startswith(_DPAPI_PREFIX):
                protected = base64.b64decode(
                    envelope[len(_DPAPI_PREFIX) :],
                    validate=True,
                )
                key = _dpapi_unprotect(protected)
            elif envelope.startswith(_LOCAL_PREFIX):
                key = base64.b64decode(
                    envelope[len(_LOCAL_PREFIX) :],
                    validate=True,
                )
            else:
                raise SecretError("invalid secret envelope")
            if len(key) != 32:
                raise SecretError("invalid secret length")
            return key

        key = secrets.token_bytes(32)
        if os.name == "nt":
            envelope = _DPAPI_PREFIX + base64.b64encode(_dpapi_protect(key))
        else:
            envelope = _LOCAL_PREFIX + base64.b64encode(key)
        _atomic_write_private(self._path, _SECRET_FILE_VERSION + envelope)
        return key

    @staticmethod
    def _canonical_part(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return len(encoded).to_bytes(4, "big") + encoded

    def contact_hmac(self, account: str, session: str) -> str:
        canonical = (
            b"akasha-contact-v1"
            + self._canonical_part(account)
            + self._canonical_part(session)
        )
        return hmac.new(self._master_key, canonical, hashlib.sha256).hexdigest()

    def _stream(self, nonce: bytes, size: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < size:
            output.extend(
                hmac.new(
                    self._master_key,
                    b"stream" + nonce + counter.to_bytes(8, "big"),
                    hashlib.sha256,
                ).digest()
            )
            counter += 1
        return bytes(output[:size])

    def encrypt_text(self, value: str) -> str:
        plaintext = value.encode("utf-8")
        nonce = secrets.token_bytes(16)
        stream = self._stream(nonce, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream, strict=True))
        tag = hmac.new(
            self._master_key,
            b"tag" + nonce + ciphertext,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(
            _CIPHER_PREFIX + nonce + tag + ciphertext
        ).decode("ascii")

    def decrypt_text(self, value: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value.encode("ascii"))
        except Exception as exc:
            raise SecretError("invalid encrypted value") from exc
        if not payload.startswith(_CIPHER_PREFIX) or len(payload) < 50:
            raise SecretError("invalid encrypted value")
        nonce = payload[2:18]
        supplied_tag = payload[18:50]
        ciphertext = payload[50:]
        expected_tag = hmac.new(
            self._master_key,
            b"tag" + nonce + ciphertext,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_tag, expected_tag):
            raise SecretError("encrypted value authentication failed")
        stream = self._stream(nonce, len(ciphertext))
        plaintext = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
        return plaintext.decode("utf-8")
