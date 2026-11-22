"""API Key 的安全保存：Windows DPAPI（CryptProtectData）。

- 密文只能由同一 Windows 用户的登录凭据解密；
- 保存在 ``%LOCALAPPDATA%\\WindowsHelperPlus\\credentials.bin``；
- 环境变量 ``DEEPSEEK_API_KEY`` 优先级最高，且不会写盘；
- 非 Windows 平台或 DPAPI 失败时明确返回失败，不回退到明文存储。
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

from .config import app_data_dir, env_api_key


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_ENTROPY = b"WindowsHelperPlus:deepseek-api-key:v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi(protect: bool, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI 仅在 Windows 上可用。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, in_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(_ENTROPY)
    out_blob = _DataBlob()
    description = "Windows Helper Plus API Key"
    description_ptr = ctypes.c_wchar_p(description) if protect else None

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            description_ptr,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    del in_buffer, entropy_buffer
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败。")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def credentials_path() -> Path:
    return app_data_dir() / "credentials.bin"


def has_stored_api_key() -> bool:
    return credentials_path().exists()


def save_api_key(api_key: str) -> None:
    """保存 API Key（DPAPI 加密）。空字符串等同于清除。"""
    key = api_key.strip()
    if not key:
        clear_api_key()
        return
    encrypted = _dpapi(True, key.encode("utf-8"))
    payload = {
        "version": 1,
        "protection": "windows-dpapi-cryptprotectdata",
        "blob": base64.b64encode(encrypted).decode("ascii"),
    }
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_stored_api_key() -> str:
    path = credentials_path()
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blob = base64.b64decode(str(payload.get("blob", "")))
        if not blob:
            return ""
        return _dpapi(False, blob).decode("utf-8", errors="replace").strip()
    except (OSError, ValueError, KeyError):
        return ""


def get_api_key(session_key: str = "") -> str:
    """优先级：本次会话输入 > DPAPI 保存值 > 环境变量。

    这样用户在设置页中明确填写并保存的 Key 会覆盖外部环境变量，避免“填写后
    看起来没有保存/仍然使用旧 Key”的困惑；没有应用内 Key 时才回退到环境变量。
    """
    return session_key.strip() or load_stored_api_key() or env_api_key()


def clear_api_key() -> None:
    try:
        credentials_path().unlink(missing_ok=True)
    except OSError:
        pass


def api_key_source(session_key: str = "") -> str:
    if session_key.strip():
        return "本次会话（未保存）"
    if load_stored_api_key():
        return "Windows DPAPI 已保存"
    if env_api_key():
        return "环境变量 DEEPSEEK_API_KEY（应用内未保存 Key）"
    return "未配置"
