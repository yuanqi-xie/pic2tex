"""Windows clipboard image reading and a polling clipboard watcher.

Only ``ctypes`` and the standard library are used.  The module exposes three
layers:

* :func:`_payload_from_formats` -- a pure, testable seam that turns a mapping
  of clipboard-format names to raw bytes into an
  :class:`pic2tex.models.ImagePayload`.
* :func:`read_clipboard_image` -- a safe one-shot read of the live clipboard.
* :class:`ClipboardWatcher` -- a daemon thread that polls
  ``GetClipboardSequenceNumber`` and delivers newly copied images.

Threading contract
------------------
``ClipboardWatcher`` invokes ``on_image`` and ``on_error`` **from its own
background thread**.  Tkinter is not thread-safe, so GUI code must marshal
those callbacks onto the UI thread (for example with ``root.after(0, ...)``).
All Win32 clipboard reading and DIB-to-PNG conversion happens inside that
background thread, never on the Tk thread.

On non-Windows platforms :meth:`ClipboardWatcher.start` raises
:class:`RuntimeError` and :func:`read_clipboard_image` simply returns ``None``.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Final

from .imageio import (
    ImageError,
    bmp_to_png,
    dib_to_png,
    guess_mime_from_name,
    image_size,
    load_image_bytes,
    load_image_file,
)
from .models import ImagePayload

__all__ = [
    "ClipboardWatcher",
    "read_clipboard_image",
]

logger = logging.getLogger(__name__)

_IS_WINDOWS: Final[bool] = sys.platform == "win32"

# Standard clipboard format identifiers.
CF_DIB: Final[int] = 8
CF_HDROP: Final[int] = 15
CF_DIBV5: Final[int] = 17

#: Registered format name used by browsers/editors to publish PNG bytes.
_CF_PNG_NAME: Final[str] = "PNG"

#: Formats we consume, highest priority first.
_FORMAT_PRIORITY: Final[tuple[str, ...]] = (
    "PNG",
    "CF_DIBV5",
    "CF_DIB",
    "CF_HDROP",
)

# OpenClipboard retry policy (another process can hold the clipboard briefly).
_OPEN_RETRIES: Final[int] = 8
_OPEN_RETRY_DELAY: Final[float] = 0.02


class _ClipboardUnavailable(Exception):
    """Internal error: the clipboard is locked, empty or has no usable data."""


# ---------------------------------------------------------------------------
# Win32 bindings (declared lazily so the module imports everywhere)
# ---------------------------------------------------------------------------

if _IS_WINDOWS:

    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    _user32.EnumClipboardFormats.restype = wintypes.UINT
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT
    _user32.GetClipboardSequenceNumber.argtypes = []
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL

    _shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    _shell32.DragQueryFileW.restype = wintypes.UINT

    _png_format_id: int | None = None
    _png_format_lock = threading.Lock()

    def _registered_png_format() -> int:
        """Return the registered clipboard format id for ``"PNG"`` (cached)."""
        global _png_format_id
        if _png_format_id is None:
            with _png_format_lock:
                if _png_format_id is None:
                    _png_format_id = int(
                        _user32.RegisterClipboardFormatW(_CF_PNG_NAME)
                    )
        return _png_format_id


def _clipboard_sequence_number() -> int:
    """Return the Win32 clipboard sequence number (changes on every update)."""
    if not _IS_WINDOWS:
        raise RuntimeError("clipboard access requires Windows")
    return int(_user32.GetClipboardSequenceNumber())


# ---------------------------------------------------------------------------
# Pure conversion seam
# ---------------------------------------------------------------------------


def _decode_hdrop(value: bytes) -> list[str]:
    """Decode a ``CF_HDROP`` value into its list of file paths.

    The live reader uses ``DragQueryFileW``; this pure helper takes the raw
    ``DROPFILES`` payload shape used by the test seam: UTF-16LE file paths,
    each NUL-terminated, with the list terminated by an extra NUL.  A payload
    that already is just a path list (no ``DROPFILES`` header) is handled too.
    """
    try:
        text = value.decode("utf-16-le")
    except UnicodeDecodeError:
        return []
    # A real DROPFILES header is 20 bytes (pFiles, pt, fNC, fWide); when the
    # payload clearly starts with it, skip the header so the path list begins.
    if len(value) >= 20:
        p_files = int.from_bytes(value[0:4], "little")
        wide = value[16]
        if wide == 1 and 20 <= p_files < len(value):
            text = value[p_files:].decode("utf-16-le", errors="replace")
    return [part for part in text.split("\x00") if part]


def _encode_hdrop(paths: list[str]) -> bytes:
    """Encode a path list back into the UTF-16LE double-NUL seam format."""
    return ("\x00".join(paths) + "\x00\x00").encode("utf-16-le")


def _payload_from_hdrop(value: bytes) -> ImagePayload | None:
    """Return the first supported image file reachable from an HDROP payload."""
    for path in _decode_hdrop(value):
        if guess_mime_from_name(path) is None:
            logger.debug("ignoring non-image clipboard file: %s", path)
            continue
        try:
            return load_image_file(path, source="clipboard")
        except ImageError as exc:
            logger.debug("cannot load clipboard image file %s: %s", path, exc)
            continue
    return None


def _payload_from_formats(formats: Mapping[str, bytes]) -> ImagePayload | None:
    """Convert clipboard format bytes into an image payload.

    Keys are ``"PNG"``, ``"CF_DIBV5"``, ``"CF_DIB"`` and ``"CF_HDROP"`` (the
    latter holds a UTF-16LE, double-NUL-terminated path list).  Formats are
    tried in that priority order; a corrupt higher-priority format falls back
    to the next one.  Returns ``None`` when no usable image is present.
    """
    if not formats:
        return None
    for key in _FORMAT_PRIORITY:
        value = formats.get(key)
        if not value:
            continue
        try:
            payload = _payload_from_one_format(key, bytes(value))
        except Exception as exc:  # malformed clipboard data must not escape
            logger.debug("clipboard format %s could not be converted: %s", key, exc)
            continue
        if payload is not None:
            return payload
    return None


def _payload_from_one_format(key: str, value: bytes) -> ImagePayload | None:
    if key == "PNG":
        return load_image_bytes(value, source="clipboard")
    if key == "CF_HDROP":
        return _payload_from_hdrop(value)
    if key in ("CF_DIBV5", "CF_DIB"):
        # Some producers (rarely) prefix a full BMP file header even under the
        # CF_DIB format id; accepting it costs nothing and avoids a failed copy.
        png = bmp_to_png(value) if value[:2] == b"BM" else dib_to_png(value)
        width, height = image_size(png) or (0, 0)
        return ImagePayload(
            data=png,
            mime="image/png",
            width=width,
            height=height,
            source="clipboard",
        )
    return None


# ---------------------------------------------------------------------------
# Live Win32 clipboard reading
# ---------------------------------------------------------------------------


def _open_clipboard() -> None:
    """Open the clipboard, retrying briefly while another process holds it."""
    last_error: int | None = None
    for _ in range(_OPEN_RETRIES):
        if _user32.OpenClipboard(None):
            return
        last_error = ctypes.get_last_error()
        time.sleep(_OPEN_RETRY_DELAY)
    raise _ClipboardUnavailable(
        f"clipboard is busy or unavailable (Win32 error {last_error})"
    )


def _enumerate_formats() -> list[int]:
    """Return the clipboard format ids currently present."""
    present: list[int] = []
    current = 0
    while True:
        current = int(_user32.EnumClipboardFormats(current))
        if current == 0:
            return present
        present.append(current)


def _read_global_bytes(format_id: int) -> bytes:
    """Copy the bytes of a clipboard HGLOBAL without taking ownership."""
    handle = _user32.GetClipboardData(format_id)
    if not handle:
        raise _ClipboardUnavailable(
            f"GetClipboardData({format_id}) failed: {ctypes.get_last_error()}"
        )
    size = int(_kernel32.GlobalSize(handle))
    if size <= 0:
        raise _ClipboardUnavailable(f"clipboard format {format_id} has no data")
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        raise _ClipboardUnavailable(
            f"GlobalLock failed for clipboard format {format_id}"
        )
    try:
        return ctypes.string_at(pointer, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def _read_hdrop_paths(format_id: int) -> list[str]:
    """Read the file list of a ``CF_HDROP`` handle with ``DragQueryFileW``."""
    handle = _user32.GetClipboardData(format_id)
    if not handle:
        raise _ClipboardUnavailable(
            f"GetClipboardData(CF_HDROP) failed: {ctypes.get_last_error()}"
        )
    count = int(_shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
    paths: list[str] = []
    for index in range(count):
        length = int(_shell32.DragQueryFileW(handle, index, None, 0))
        if length <= 0:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = int(_shell32.DragQueryFileW(handle, index, buffer, length + 1))
        if copied:
            paths.append(buffer.value)
    return paths


def _read_formats_live() -> dict[str, bytes]:
    """Read the raw bytes of every image format currently on the clipboard."""
    if not _IS_WINDOWS:
        raise _ClipboardUnavailable("clipboard access requires Windows")
    formats: dict[str, bytes] = {}
    png_id = _registered_png_format()
    wanted: list[tuple[str, int]] = [
        ("PNG", png_id),
        ("CF_DIBV5", CF_DIBV5),
        ("CF_DIB", CF_DIB),
        ("CF_HDROP", CF_HDROP),
    ]
    _open_clipboard()
    try:
        present = set(_enumerate_formats())
        for key, format_id in wanted:
            if not format_id or format_id not in present:
                continue
            try:
                if key == "CF_HDROP":
                    paths = _read_hdrop_paths(format_id)
                    if paths:
                        formats[key] = _encode_hdrop(paths)
                else:
                    formats[key] = _read_global_bytes(format_id)
            except _ClipboardUnavailable as exc:
                logger.debug("could not read clipboard format %s: %s", key, exc)
    finally:
        _user32.CloseClipboard()
    return formats


def read_clipboard_image() -> ImagePayload | None:
    """Read the current clipboard image once, returning ``None`` if unavailable.

    This function never raises: a locked clipboard, missing image or malformed
    content all result in ``None``.  It is safe to call from any thread, but
    like any clipboard access it briefly excludes other processes.
    """
    if not _IS_WINDOWS:
        return None
    try:
        formats = _read_formats_live()
    except Exception as exc:  # locked clipboard, access denied, ...
        logger.debug("clipboard read failed: %s", exc)
        return None
    return _payload_from_formats(formats)


# ---------------------------------------------------------------------------
# Polling watcher
# ---------------------------------------------------------------------------


class ClipboardWatcher:
    """Watch the Windows clipboard for new images on a daemon polling thread.

    ``on_image`` fires once per clipboard update that yields an image;
    ``on_error`` receives exceptions raised by the callbacks or by a clipboard
    read.  Both callbacks run on the watcher thread -- see the module docstring
    about Tkinter.

    With ``start_with_current=False`` (the default) whatever is already on the
    clipboard when :meth:`start` is called is ignored, so starting the app does
    not silently convert an old screenshot.
    """

    def __init__(
        self,
        on_image: Callable[[ImagePayload], None],
        on_error: Callable[[Exception], None] | None = None,
        *,
        poll_interval: float = 0.35,
        start_with_current: bool = False,
    ) -> None:
        if not callable(on_image):
            raise TypeError("on_image must be callable")
        if on_error is not None and not callable(on_error):
            raise TypeError("on_error must be callable or None")
        interval = float(poll_interval)
        if interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self._on_image = on_image
        self._on_error = on_error
        self._poll_interval = interval
        self._start_with_current = bool(start_with_current)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._last_sequence: int | None = None
        # Windows may bump the clipboard sequence more than once for one logical
        # copy (e.g. Set-Clipboard writes the file list and its metadata).  Keep
        # a short duplicate-suppression window so the GUI/CLI converts once.
        self._last_image_digest: str | None = None
        self._last_image_at = 0.0
        self._duplicate_window = 1.5

    # -- public API ---------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the polling thread is currently alive."""
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the polling thread; a no-op when already running.

        Raises :class:`RuntimeError` on non-Windows platforms.
        """
        if not _IS_WINDOWS:
            raise RuntimeError("ClipboardWatcher requires Windows")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            baseline: int | None = None
            prime_silently = False
            if not self._start_with_current:
                try:
                    baseline = int(_clipboard_sequence_number())
                except Exception as exc:
                    # Cannot snapshot the current state: adopt the first value
                    # we observe instead of firing for a pre-existing image.
                    logger.debug("cannot read initial clipboard sequence: %s", exc)
                    prime_silently = True
            self._last_sequence = baseline
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(stop_event, prime_silently),
                name="pic2tex-clipboard-watcher",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the watcher and join the thread; safe to call repeatedly."""
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            self._stop_event = None
            self._thread = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- internals ----------------------------------------------------------

    def _run(self, stop_event: threading.Event, prime_silently: bool) -> None:
        last = self._last_sequence
        try:
            while not stop_event.is_set():
                try:
                    sequence = int(_clipboard_sequence_number())
                except Exception as exc:  # pragma: no cover - rare Win32 failure
                    self._report_error(exc)
                    sequence = None
                if sequence is not None and sequence != last:
                    last = sequence
                    self._last_sequence = sequence
                    if prime_silently:
                        prime_silently = False
                    else:
                        self._deliver()
                if stop_event.wait(self._poll_interval):
                    break
        except Exception as exc:  # defensive: a watcher must never die silently
            self._report_error(exc)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _deliver(self) -> None:
        try:
            payload = read_clipboard_image()
        except Exception as exc:  # read_clipboard_image is safe, but be defensive
            self._report_error(exc)
            return
        if payload is None:
            return
        digest = hashlib.sha1(payload.mime.encode("ascii", "replace") + payload.data).hexdigest()
        now = time.monotonic()
        if (
            digest == self._last_image_digest
            and now - self._last_image_at < self._duplicate_window
        ):
            return
        self._last_image_digest = digest
        self._last_image_at = now
        try:
            self._on_image(payload)
        except Exception as exc:
            self._report_error(exc)

    def _report_error(self, error: Exception) -> None:
        callback = self._on_error
        if callback is None:
            logger.debug("clipboard watcher error: %s", error)
            return
        try:
            callback(error)
        except Exception:
            logger.exception("clipboard watcher on_error callback raised")
