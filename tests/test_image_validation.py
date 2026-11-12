"""Regression tests for corrupt/oversized image validation (Lead-owned).

The QA pass found that files with a valid magic number but a corrupt header
were passed to the API.  These tests pin the fix.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from pic2tex.imageio import ImageError, encode_png, load_image_bytes, load_image_file

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_with_ihdr(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IEND", b"")


class CorruptImageTests(unittest.TestCase):
    def test_valid_png_still_loads(self) -> None:
        png = encode_png(3, 2, bytes(3 * 2 * 4))
        payload = load_image_bytes(png)
        self.assertEqual((payload.width, payload.height), (3, 2))
        self.assertEqual(payload.mime, "image/png")

    def test_png_signature_with_garbage_raises(self) -> None:
        with self.assertRaises(ImageError):
            load_image_bytes(PNG_SIGNATURE + b"\x00" * 64)

    def test_truncated_jpeg_raises(self) -> None:
        with self.assertRaises(ImageError):
            load_image_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 40)

    def test_gif_header_with_zero_dimensions_raises(self) -> None:
        with self.assertRaises(ImageError):
            load_image_bytes(b"GIF89a" + b"\x00" * 16)

    def test_corrupt_webp_raises(self) -> None:
        data = b"RIFF" + struct.pack("<I", 20) + b"WEBPVP8 " + b"\x00" * 12
        with self.assertRaises(ImageError):
            load_image_bytes(data)

    def test_oversized_dimensions_raise(self) -> None:
        with self.assertRaises(ImageError) as ctx:
            load_image_bytes(_png_with_ihdr(9000, 100))
        self.assertIn("8192", str(ctx.exception))

    def test_corrupt_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.png"
            path.write_bytes(PNG_SIGNATURE + b"nope")
            with self.assertRaises(ImageError):
                load_image_file(path)


if __name__ == "__main__":
    unittest.main()
