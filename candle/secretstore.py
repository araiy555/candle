"""APIパスワードの保存（Windows専用）。

Windowsの暗号化機能（DPAPI）で暗号化して保存する。復号できるのは、保存したのと同じ
Windowsユーザーだけ。保存先はOneDriveで同期されない場所（%LOCALAPPDATA%\\candle）。
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _path() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "candle" / "api_password.bin" if base else None


def available() -> bool:
    return sys.platform == "win32" and _path() is not None


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("暗号化処理に失敗しました")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def save(password: str) -> bool:
    if not available():
        return False
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_crypt(password.encode("utf-8"), True))
        return True
    except Exception:
        return False


def load() -> str:
    if not available():
        return ""
    try:
        return _crypt(_path().read_bytes(), False).decode("utf-8")
    except Exception:
        return ""


def delete() -> bool:
    path = _path()
    if path and path.exists():
        path.unlink()
        return True
    return False
