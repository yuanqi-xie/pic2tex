"""Tests for :mod:`pic2tex.clipboard` (stdlib ``unittest`` only).

The clipboard format-conversion seam, the ``CF_HDROP`` decoding and the
watcher lifecycle (with the Win32 sequence-number call patched out) are all
tested without touching a real interactive clipboard.  One read-only live
clipboard test is included and degrades gracefully to ``None``.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pic2tex import clipboard, imageio
from pic2tex.clipboard import ClipboardWatcher, _payload_from_formats
from pic2tex.imageio import encode_png
from pic2tex.models import ImagePayload

WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "requires Windows")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png(width: int, height: int) -> bytes:
    """Build a small but valid PNG whose dimensions are easy to assert."""
    rgba = bytearray()
    for y in range(height):
        for x in range(width):
            rgba += bytes(
                (
                    (x * 31 + y * 7) & 0xFF,
                    (x * 11 + y * 53) & 0xFF,
                    (x * 71 + y * 3) & 0xFF,
                    (x * 17 + y * 29 + 5) & 0xFF,
                )
            )
    return encode_png(width, height, bytes(rgba))


def _make_dib(width: int, height: int, *, top_down: bool = False) -> bytes:
    """Build a 32-bit BI_RGB DIB (BGRX byte order, alpha byte = 0)."""
    stride = width * 4
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r = (x * 40 + y * 7) & 0xFF
            g = (x * 3 + y * 50) & 0xFF
            b = (x * 90 + y * 11) & 0xFF
            row += bytes((b, g, r, 0))
        rows.append(bytes(row) + b"\x00" * (stride - len(row)))
    body = b"".join(rows if top_down else reversed(rows))
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        -height if top_down else height,
        1,
        32,
        0,
        len(body),
        2835,
        2835,
        0,
        0,
    )
    return header + body


def _hdrop(paths: list[str], *, with_header: bool = False) -> bytes:
    text = ("\x00".join(paths) + "\x00\x00").encode("utf-16-le")
    if not with_header:
        return text
    # Real DROPFILES header: pFiles, POINT, fNC, fWide.
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + text


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self._tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path


# ---------------------------------------------------------------------------
# Pure format seam
# ---------------------------------------------------------------------------


class TestPayloadFromFormats(_TempDirTest):
    def test_empty_or_missing_mapping(self) -> None:
        self.assertIsNone(_payload_from_formats({}))
        self.assertIsNone(_payload_from_formats(None))  # type: ignore[arg-type]

    def test_no_usable_image(self) -> None:
        self.assertIsNone(_payload_from_formats({"PNG": b""}))
        self.assertIsNone(_payload_from_formats({"PNG": b"garbage"}))
        self.assertIsNone(_payload_from_formats({"CF_DIB": b"garbage"}))
        self.assertIsNone(
            _payload_from_formats({"PNG": b"garbage", "CF_DIB": b"garbage"})
        )

    def test_png_has_highest_priority(self) -> None:
        png = _png(2, 1)
        formats = {
            "PNG": png,
            "CF_DIBV5": _make_dib(9, 9),
            "CF_DIB": _make_dib(8, 8),
            "CF_HDROP": _hdrop([self.write("ignored.png", _png(7, 7))]),
        }
        payload = _payload_from_formats(formats)
        assert payload is not None
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual(payload.data, png)
        self.assertEqual((payload.width, payload.height), (2, 1))
        self.assertEqual(payload.source, "clipboard")
        self.assertIsNone(payload.name)

    def test_dibv5_beats_dib(self) -> None:
        formats = {"CF_DIBV5": _make_dib(3, 2), "CF_DIB": _make_dib(5, 4)}
        payload = _payload_from_formats(formats)
        assert payload is not None
        self.assertEqual((payload.width, payload.height), (3, 2))
        self.assertEqual(payload.mime, "image/png")

    def test_dib_converted_to_png(self) -> None:
        dib = _make_dib(4, 3, top_down=True)
        payload = _payload_from_formats({"CF_DIB": dib})
        assert payload is not None
        self.assertEqual((payload.width, payload.height), (4, 3))
        self.assertTrue(payload.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(payload.source, "clipboard")
        # Verify the decoded pixels are the opaque BGR->RGBA conversion.
        width, height, rgba = imageio.decode_png_rgba(payload.data)
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(rgba[0:4], bytes((0, 0, 0, 255)))
        self.assertEqual(rgba[3::4], b"\xff" * (4 * 3))

    def test_corrupt_png_falls_back_to_dib(self) -> None:
        payload = _payload_from_formats({"PNG": b"not a real png", "CF_DIB": _make_dib(2, 3)})
        assert payload is not None
        self.assertEqual((payload.width, payload.height), (2, 3))

    def test_cf_dib_accepts_bmp_file_header(self) -> None:
        dib = _make_dib(2, 2, top_down=True)
        bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + 40) + dib
        payload = _payload_from_formats({"CF_DIB": bmp})
        assert payload is not None
        self.assertEqual((payload.width, payload.height), (2, 2))

    def test_hdrop_picks_first_supported_image(self) -> None:
        text_path = self.write("notes.txt", b"not an image")
        png = _png(6, 5)
        png_path = self.write("screenshot.png", png)
        formats = {"CF_HDROP": _hdrop([text_path, png_path])}
        payload = _payload_from_formats(formats)
        assert payload is not None
        self.assertEqual(payload.data, png)
        self.assertEqual(payload.name, "screenshot.png")
        self.assertEqual(payload.source, "clipboard")

    def test_hdrop_with_dropfiles_header(self) -> None:
        png = _png(1, 1)
        path = self.write("one.png", png)
        payload = _payload_from_formats({"CF_HDROP": _hdrop([path], with_header=True)})
        assert payload is not None
        self.assertEqual(payload.data, png)

    def test_hdrop_converts_bmp(self) -> None:
        dib = _make_dib(2, 1, top_down=True)
        bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + 40) + dib
        path = self.write("capture.bmp", bmp)
        payload = _payload_from_formats({"CF_HDROP": _hdrop([path])})
        assert payload is not None
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual((payload.width, payload.height), (2, 1))
        self.assertEqual(payload.name, "capture.bmp")

    def test_hdrop_ignores_non_image_files_and_missing_files(self) -> None:
        text_path = self.write("notes.txt", b"hello")
        missing = os.path.join(self._tmp.name, "missing.png")
        self.assertIsNone(
            _payload_from_formats({"CF_HDROP": _hdrop([text_path, missing])})
        )

    def test_hdrop_is_lowest_priority(self) -> None:
        path = self.write("shot.png", _png(8, 8))
        formats = {"CF_HDROP": _hdrop([path]), "CF_DIB": _make_dib(2, 2)}
        payload = _payload_from_formats(formats)
        assert payload is not None
        self.assertEqual((payload.width, payload.height), (2, 2))

    def test_unknown_format_keys_are_ignored(self) -> None:
        self.assertIsNone(_payload_from_formats({"CF_UNICODETEXT": b"hello"}))


# ---------------------------------------------------------------------------
# Watcher lifecycle
# ---------------------------------------------------------------------------


def _changing_sequence() -> object:
    """Return a fake sequence-number function: 100 first, then 200 forever."""

    calls = {"count": 0}

    def sequence() -> int:
        calls["count"] += 1
        return 100 if calls["count"] == 1 else 200

    return sequence


class TestClipboardWatcher(_TempDirTest):
    def test_poll_interval_validation(self) -> None:
        with self.assertRaises(ValueError):
            ClipboardWatcher(lambda payload: None, poll_interval=0)
        with self.assertRaises(ValueError):
            ClipboardWatcher(lambda payload: None, poll_interval=-1)
        with self.assertRaises(TypeError):
            ClipboardWatcher("not callable")  # type: ignore[arg-type]

    @WINDOWS_ONLY
    def test_start_stop_and_idempotency(self) -> None:
        watcher = ClipboardWatcher(lambda payload: None, poll_interval=0.01)
        self.assertFalse(watcher.running)
        with patch.object(clipboard, "_clipboard_sequence_number", lambda: 7):
            watcher.start()
            watcher.start()  # second start must be a no-op
            self.assertTrue(watcher.running)
            watcher.stop()
            self.assertFalse(watcher.running)
            watcher.stop()  # stop must be idempotent
            self.assertFalse(watcher.running)

    @WINDOWS_ONLY
    def test_stop_before_start_is_safe(self) -> None:
        watcher = ClipboardWatcher(lambda payload: None, poll_interval=0.01)
        watcher.stop()
        self.assertFalse(watcher.running)

    @WINDOWS_ONLY
    def test_initial_clipboard_state_is_ignored(self) -> None:
        reads: list[int] = []

        def fake_read() -> ImagePayload:
            reads.append(1)
            return ImagePayload(b"x", "image/png", 1, 1, source="clipboard")

        watcher = ClipboardWatcher(lambda payload: None, poll_interval=0.01)
        with patch.object(
            clipboard, "_clipboard_sequence_number", lambda: 41
        ), patch.object(clipboard, "read_clipboard_image", fake_read):
            watcher.start()
            time.sleep(0.12)
            watcher.stop()
        self.assertEqual(reads, [])

    @WINDOWS_ONLY
    def test_fires_once_per_clipboard_change(self) -> None:
        payload = ImagePayload(b"png", "image/png", 2, 2, source="clipboard")
        received: list[ImagePayload] = []
        fired = threading.Event()

        def on_image(item: ImagePayload) -> None:
            received.append(item)
            fired.set()

        watcher = ClipboardWatcher(on_image, poll_interval=0.01)
        with patch.object(
            clipboard, "_clipboard_sequence_number", _changing_sequence()
        ), patch.object(clipboard, "read_clipboard_image", lambda: payload):
            watcher.start()
            self.assertTrue(fired.wait(2.0), "watcher did not fire")
            time.sleep(0.08)  # allow any duplicate delivery to show up
            watcher.stop()
        self.assertEqual(received, [payload])

    @WINDOWS_ONLY
    def test_start_with_current_fires_immediately(self) -> None:
        payload = ImagePayload(b"png", "image/png", 1, 1, source="clipboard")
        received: list[ImagePayload] = []
        fired = threading.Event()

        watcher = ClipboardWatcher(
            lambda item: (received.append(item), fired.set()),
            poll_interval=0.01,
            start_with_current=True,
        )
        with patch.object(
            clipboard, "_clipboard_sequence_number", lambda: 9
        ), patch.object(clipboard, "read_clipboard_image", lambda: payload):
            watcher.start()
            self.assertTrue(fired.wait(2.0), "watcher did not fire")
            watcher.stop()
        self.assertEqual(received, [payload])

    @WINDOWS_ONLY
    def test_none_payload_does_not_fire(self) -> None:
        fired: list[object] = []
        watcher = ClipboardWatcher(lambda item: fired.append(item), poll_interval=0.01)
        with patch.object(
            clipboard, "_clipboard_sequence_number", _changing_sequence()
        ), patch.object(clipboard, "read_clipboard_image", lambda: None):
            watcher.start()
            time.sleep(0.15)
            watcher.stop()
        self.assertEqual(fired, [])

    @WINDOWS_ONLY
    def test_callback_exception_goes_to_on_error(self) -> None:
        errors: list[Exception] = []
        reported = threading.Event()

        def on_image(payload: ImagePayload) -> None:
            raise RuntimeError("callback exploded")

        def on_error(error: Exception) -> None:
            errors.append(error)
            reported.set()

        watcher = ClipboardWatcher(on_image, on_error, poll_interval=0.01)
        with patch.object(
            clipboard, "_clipboard_sequence_number", _changing_sequence()
        ), patch.object(
            clipboard, "read_clipboard_image", lambda: ImagePayload(b"x", "image/png", 1, 1)
        ):
            watcher.start()
            self.assertTrue(reported.wait(2.0), "on_error was not called")
            # The watcher must survive a callback exception.
            self.assertTrue(watcher.running)
            watcher.stop()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    @WINDOWS_ONLY
    def test_read_exception_goes_to_on_error(self) -> None:
        errors: list[Exception] = []
        reported = threading.Event()

        def on_error(error: Exception) -> None:
            errors.append(error)
            reported.set()

        def bad_read() -> ImagePayload:
            raise RuntimeError("clipboard read failed")

        watcher = ClipboardWatcher(
            lambda payload: None, on_error, poll_interval=0.01
        )
        with patch.object(
            clipboard, "_clipboard_sequence_number", _changing_sequence()
        ), patch.object(clipboard, "read_clipboard_image", bad_read):
            watcher.start()
            self.assertTrue(reported.wait(2.0), "on_error was not called")
            self.assertTrue(watcher.running)
            watcher.stop()
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], RuntimeError)

    @WINDOWS_ONLY
    def test_stop_from_inside_callback(self) -> None:
        stopped = threading.Event()
        watcher: ClipboardWatcher

        def on_image(payload: ImagePayload) -> None:
            watcher.stop()
            stopped.set()

        watcher = ClipboardWatcher(on_image, poll_interval=0.01)
        with patch.object(
            clipboard, "_clipboard_sequence_number", _changing_sequence()
        ), patch.object(
            clipboard, "read_clipboard_image", lambda: ImagePayload(b"x", "image/png", 1, 1)
        ):
            watcher.start()
            self.assertTrue(stopped.wait(2.0), "callback stop did not run")
            watcher.stop()
        self.assertFalse(watcher.running)

    def test_non_windows_behaviour(self) -> None:
        with patch.object(clipboard, "_IS_WINDOWS", False):
            self.assertIsNone(clipboard.read_clipboard_image())
            watcher = ClipboardWatcher(lambda payload: None, poll_interval=0.01)
            with self.assertRaises(RuntimeError):
                watcher.start()


# ---------------------------------------------------------------------------
# Live clipboard (read-only, must never fail the suite)
# ---------------------------------------------------------------------------


@WINDOWS_ONLY
class TestLiveClipboard(unittest.TestCase):
    def test_sequence_number_and_safe_read(self) -> None:
        self.assertIsInstance(clipboard._clipboard_sequence_number(), int)
        result = clipboard.read_clipboard_image()
        self.assertTrue(result is None or isinstance(result, ImagePayload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
