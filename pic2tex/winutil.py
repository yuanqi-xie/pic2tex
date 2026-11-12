"""Small Win32 helpers used by the GUI: DPI awareness, clipboard text, snipping."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def resource_path(relative: str) -> Path:
    """Resolve a bundled resource path both in source and in a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent.parent / relative


def get_work_area() -> tuple[int, int, int, int] | None:
    """Return ``(x, y, width, height)`` of the primary monitor work area.

    The work area excludes the taskbar, which keeps the window fully visible.
    Returns None on non-Windows or on failure.
    """
    if not IS_WINDOWS:
        return None
    try:
        rect = _RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
        if not ok:
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        return rect.left, rect.top, width, height
    except Exception:
        return None


def enable_high_dpi() -> None:
    """Make the process DPI aware so Tk windows are not blurry on scaled displays."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def copy_text(text: str) -> bool:
    """Copy *text* to the Windows clipboard as CF_UNICODETEXT.

    Unlike Tk's clipboard this keeps working after the app exits.  Returns True
    on success; on non-Windows it returns False and callers can fall back to the
    Tk clipboard.
    """
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_bool

    payload = (text + "\x00").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return False
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(locked, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)

    opened = False
    for _ in range(15):  # clipboard can be briefly locked by another app
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        kernel32.GlobalFree(handle)
        return False

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        handle = None  # ownership transferred to the clipboard
        return True
    finally:
        user32.CloseClipboard()


def launch_screen_clip() -> bool:
    """Open the Windows 11 "Win+Shift+S" style screen clipping overlay."""
    if not IS_WINDOWS:
        return False
    try:
        os.startfile("ms-screenclip:")  # type: ignore[attr-defined]
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(["SnippingTool.exe", "/clip"], close_fds=True)
        return True
    except Exception:
        return False


def message_box(text: str, title: str = "Pic2TeX") -> None:
    """Native message box (useful before Tk exists / after it is destroyed)."""
    if not IS_WINDOWS:
        print(f"{title}: {text}")
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00000040)
    except Exception:
        print(f"{title}: {text}")
