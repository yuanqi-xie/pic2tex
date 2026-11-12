"""Global hotkey support (RegisterHotKey + a private message loop thread)."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

from .winutil import IS_WINDOWS

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_HOTKEY_ID = 0x50494332  # 'PIC2'

_NAMED_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "num0": 0x60,
    "num1": 0x61,
    "num2": 0x62,
    "num3": 0x63,
    "num4": 0x64,
    "num5": 0x65,
    "num6": 0x66,
    "num7": 0x67,
    "num8": 0x68,
    "num9": 0x69,
    "num*": 0x6A,
    "num+": 0x6B,
    "num-": 0x6D,
    "num.": 0x6E,
    "num/": 0x6F,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse ``"ctrl+alt+s"`` into ``(modifiers, virtual_key)``.

    Supports ctrl/control, alt, shift, win/super, letters A-Z, digits 0-9,
    F1-F24 and the common named keys.  Raises ValueError for bad input.
    """
    if not spec or not spec.strip():
        raise ValueError("快捷键不能为空")
    parts = [part.strip().lower() for part in spec.replace(" ", "").split("+") if part.strip()]
    if len(parts) < 2:
        raise ValueError("快捷键至少需要一个修饰键和一个主键，例如 ctrl+alt+s")
    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in ("ctrl", "control"):
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            modifiers |= MOD_WIN
        elif key is not None:
            raise ValueError(f"快捷键只能有一个主键: {spec!r}")
        else:
            key = part
    if key is None:
        raise ValueError("快捷键缺少主键")
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"无法识别的快捷键主键: {key!r}")
    if modifiers == 0:
        raise ValueError("快捷键必须包含修饰键（Ctrl/Alt/Shift/Win）")
    return modifiers, vk


class HotkeyManager:
    """Register one global hotkey and run *callback* on a dedicated thread.

    The callback runs on the hotkey thread, so GUI code must marshal it back to
    the Tk thread (the GUI pushes it onto a queue).
    """

    def __init__(self, callback: Callable[[], None], hotkey: str = "ctrl+alt+s") -> None:
        self._callback = callback
        self._hotkey = hotkey
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._started_ok = False
        self._last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._started_ok)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> bool:
        """Register the hotkey.  Returns False when Windows refuses it."""
        if not IS_WINDOWS:
            self._last_error = "当前系统不支持全局快捷键（仅 Windows）"
            return False
        if self.running:
            return True
        try:
            modifiers, vk = parse_hotkey(self._hotkey)
        except ValueError as exc:
            self._last_error = str(exc)
            return False

        with self._lock:
            self._ready.clear()
            self._started_ok = False
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run, args=(modifiers, vk), name="pic2tex-hotkey", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._started_ok

    def stop(self) -> None:
        thread = self._thread
        thread_id = self._thread_id
        if thread is None:
            return
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        thread.join(timeout=3.0)
        self._thread = None
        self._thread_id = None
        self._started_ok = False

    def _run(self, modifiers: int, vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self._thread_id = kernel32.GetCurrentThreadId()
        registered = bool(user32.RegisterHotKey(None, _HOTKEY_ID, modifiers | MOD_NOREPEAT, vk))
        self._started_ok = registered
        if not registered:
            self._last_error = (
                f"快捷键 {self._hotkey} 注册失败，可能已被其他程序占用；"
                "可在设置中更换快捷键。"
            )
        self._ready.set()
        if not registered:
            return

        msg = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT or error
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == _HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception as exc:  # pragma: no cover - defensive
                        self._last_error = f"快捷键回调出错: {exc}"
        finally:
            try:
                user32.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:
                pass
