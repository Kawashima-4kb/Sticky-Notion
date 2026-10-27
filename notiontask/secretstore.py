"""トークン類を Windows DPAPI（現在のユーザーでのみ復号可能）で暗号化して保存する。"""

import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(func, data):
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI の処理に失敗しました")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    if not text or sys.platform != "win32":
        return text
    data = _call(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
    return PREFIX + base64.b64encode(data).decode("ascii")


def unprotect(value):
    """暗号化されていない値（旧形式・Windows 以外）はそのまま返す。復号できなければ空文字。"""
    if not value or not value.startswith(PREFIX):
        return value or ""
    try:
        data = _call(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(value[len(PREFIX):]))
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8")
