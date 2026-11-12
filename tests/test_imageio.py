"""Tests for :mod:`pic2tex.imageio` (stdlib ``unittest`` only).

Covers magic-byte sniffing, header dimension parsing, the minimal PNG codec
(round-trip plus every PNG filter type), Windows DIB/BMP conversion for the
layouts screenshot tools produce, and the high-level loaders.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
import zlib

from pic2tex import imageio
from pic2tex.imageio import (
    ImageError,
    bmp_to_png,
    decode_png_rgba,
    detect_mime,
    dib_to_png,
    encode_png,
    guess_mime_from_name,
    image_size,
    load_image_bytes,
    load_image_file,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Small PNG builders used to exercise the reference decoder
# ---------------------------------------------------------------------------


def _chunk(tag: bytes, payload: bytes, *, crc_delta: int = 0) -> bytes:
    crc = (zlib.crc32(tag + payload) + crc_delta) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def _png_from_scanlines(
    width: int, height: int, color_type: int, scanlines: list[bytes]
) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_SIG
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"".join(scanlines)))
        + _chunk(b"IEND", b"")
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _filter_row(raw: bytes, previous: bytes | None, bpp: int, kind: int) -> bytes:
    out = bytearray(len(raw))
    for i in range(len(raw)):
        a = raw[i - bpp] if i >= bpp else 0
        b = previous[i] if previous is not None else 0
        c = previous[i - bpp] if previous is not None and i >= bpp else 0
        if kind == 0:
            value = raw[i]
        elif kind == 1:
            value = raw[i] - a
        elif kind == 2:
            value = raw[i] - b
        elif kind == 3:
            value = raw[i] - ((a + b) >> 1)
        elif kind == 4:
            value = raw[i] - _paeth(a, b, c)
        else:  # pragma: no cover - test bug
            raise AssertionError(f"bad filter {kind}")
        out[i] = value & 0xFF
    return bytes(out)


def _png_with_filters(
    width: int, height: int, color_type: int, raw_rows: list[bytes], filters: list[int]
) -> bytes:
    bpp = 4 if color_type == 6 else 3
    scanlines: list[bytes] = []
    previous: bytes | None = None
    for raw, kind in zip(raw_rows, filters):
        scanlines.append(bytes([kind]) + _filter_row(raw, previous, bpp, kind))
        previous = raw
    return _png_from_scanlines(width, height, color_type, scanlines)


def _sample_rgba(width: int, height: int) -> bytes:
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes(
                (
                    (x * 37 + y * 11) & 0xFF,
                    (x * 5 + y * 71 + 9) & 0xFF,
                    (x * 101 + y * 3 + 33) & 0xFF,
                    (x * 13 + y * 29 + 17) & 0xFF,
                )
            )
    return bytes(out)


def _rgba_rows(width: int, height: int) -> list[bytes]:
    data = _sample_rgba(width, height)
    stride = width * 4
    return [data[y * stride : (y + 1) * stride] for y in range(height)]


# ---------------------------------------------------------------------------
# DIB builders
# ---------------------------------------------------------------------------


def _compose(
    r: int,
    g: int,
    b: int,
    a: int,
    masks: tuple[int, int, int, int],
) -> int:
    value = 0
    for channel, mask in zip((r, g, b, a), masks):
        if not mask:
            continue
        shift = (mask & -mask).bit_length() - 1
        max_value = mask >> shift
        scaled = channel if max_value == 255 else (channel * max_value + 127) // 255
        value |= (scaled << shift) & mask
    return value


def _extract(value: int, mask: int) -> int:
    if not mask:
        return 0
    shift = (mask & -mask).bit_length() - 1
    max_value = mask >> shift
    raw = (value >> shift) & max_value
    return raw if max_value == 255 else (raw * 255 + (max_value // 2)) // max_value


def _make_dib(
    width: int,
    height: int,
    pixels: list[tuple[int, int, int, int]],
    *,
    bpp: int = 32,
    top_down: bool = False,
    header_size: int = 40,
    compression: int = 0,
    masks: tuple[int, int, int, int] | None = None,
    force_alpha_zero: bool = False,
) -> bytes:
    """Build a raw DIB (no BITMAPFILEHEADER) from top-down RGBA pixels."""
    if masks is None:
        if bpp == 32:
            masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        elif bpp == 24:
            masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
        else:  # 16 bpp BI_RGB is 5-5-5 by default
            masks = (0x7C00, 0x03E0, 0x001F, 0)
    bytes_per_pixel = bpp // 8
    stride = ((width * bpp + 31) // 32) * 4
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r, g, b, a = pixels[y * width + x]
            if force_alpha_zero:
                a = 0
            value = _compose(r, g, b, a, masks)
            row += value.to_bytes(bytes_per_pixel, "little")
        row += b"\x00" * (stride - len(row))
        rows.append(bytes(row))
    body = b"".join(rows if top_down else reversed(rows))
    height_field = -height if top_down else height
    header = struct.pack(
        "<IiiHHIIiiII",
        header_size,
        width,
        height_field,
        1,
        bpp,
        compression,
        len(body),
        2835,
        2835,
        0,
        0,
    )
    header = header.ljust(40, b"\x00")
    extras = b""
    if header_size >= 56:
        header += struct.pack("<IIII", *masks)
        header = header.ljust(header_size, b"\x00")
    elif header_size >= 52:
        header += struct.pack("<III", *masks[:3])
        header = header.ljust(header_size, b"\x00")
    elif compression == 3:
        extras = struct.pack("<III", *masks[:3])
    return header + extras + body


def _expected_rgba(
    pixels: list[tuple[int, int, int, int]],
    masks: tuple[int, int, int, int],
    *,
    force_alpha_zero: bool = False,
) -> bytes:
    out = bytearray()
    for r, g, b, a in pixels:
        value = _compose(r, g, b, a, masks)
        out.append(_extract(value, masks[0]))
        out.append(_extract(value, masks[1]))
        out.append(_extract(value, masks[2]))
        out.append(255 if not masks[3] or force_alpha_zero else _extract(value, masks[3]))
    return bytes(out)


def _pixels(width: int, height: int) -> list[tuple[int, int, int, int]]:
    return [
        (
            (x * 37 + y * 11) & 0xFF,
            (x * 5 + y * 71 + 9) & 0xFF,
            (x * 101 + y * 3 + 33) & 0xFF,
            (x * 13 + y * 29 + 17) & 0xFF,
        )
        for y in range(height)
        for x in range(width)
    ]


# ---------------------------------------------------------------------------
# Other format builders
# ---------------------------------------------------------------------------


def _jpeg_with_size(width: int, height: int, *, with_app0: bool = True) -> bytes:
    app0 = b""
    if with_app0:
        app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    sof_payload = (
        b"\x08" + struct.pack(">HH", height, width) + b"\x01" + b"\x01\x11\x00"
    )
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return b"\xff\xd8" + app0 + sof


def _gif_with_size(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _webp_with_size(width: int, height: int, variant: str) -> bytes:
    payload = b""
    if variant == "VP8X":
        canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        payload = b"\x00\x00\x00\x00" + canvas
    elif variant == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        payload = b"\x2f" + bits.to_bytes(4, "little")
    elif variant == "VP8 ":
        payload = (
            b"\x00\x00\x00"
            + b"\x9d\x01\x2a"
            + struct.pack("<HH", width & 0x3FFF, height & 0x3FFF)
        )
    else:  # pragma: no cover - test bug
        raise AssertionError(variant)
    chunk = variant.encode("ascii") + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        chunk += b"\x00"
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def _bmp_file(dib: bytes, *, pixel_offset: int = 0) -> bytes:
    if not pixel_offset:
        header_size = struct.unpack_from("<I", dib, 0)[0]
        compression = struct.unpack_from("<I", dib, 16)[0]
        extras = 12 if header_size == 40 and compression == 3 else 0
        pixel_offset = 14 + header_size + extras
    file_size = 14 + len(dib)
    return (
        b"BM"
        + struct.pack("<IHHI", file_size, 0, 0, pixel_offset)
        + dib
    )


# ---------------------------------------------------------------------------
# Sniffing / dimensions
# ---------------------------------------------------------------------------


class TestDetectMime(unittest.TestCase):
    def test_supported_formats(self) -> None:
        self.assertEqual(detect_mime(encode_png(1, 1, b"\x00\x00\x00\xff")), "image/png")
        self.assertEqual(detect_mime(_jpeg_with_size(4, 3)), "image/jpeg")
        self.assertEqual(detect_mime(b"GIF87a" + b"\x00" * 8), "image/gif")
        self.assertEqual(detect_mime(b"GIF89a" + b"\x00" * 8), "image/gif")
        self.assertEqual(
            detect_mime(_webp_with_size(4, 3, "VP8X")), "image/webp"
        )

    def test_bmp_is_not_api_supported(self) -> None:
        self.assertIsNone(detect_mime(b"BM" + b"\x00" * 60))

    def test_unknown_and_empty(self) -> None:
        self.assertIsNone(detect_mime(b""))
        self.assertIsNone(detect_mime(b"not an image at all"))
        self.assertIsNone(detect_mime(b"RIFF" + b"\x00" * 4 + b"WAVE"))


class TestImageSize(unittest.TestCase):
    def test_png(self) -> None:
        self.assertEqual(image_size(encode_png(17, 5, _sample_rgba(17, 5))), (17, 5))

    def test_jpeg_with_and_without_app0(self) -> None:
        self.assertEqual(image_size(_jpeg_with_size(640, 480)), (640, 480))
        self.assertEqual(image_size(_jpeg_with_size(7, 9, with_app0=False)), (7, 9))

    def test_gif(self) -> None:
        self.assertEqual(image_size(_gif_with_size(320, 200)), (320, 200))

    def test_webp_variants(self) -> None:
        self.assertEqual(image_size(_webp_with_size(300, 200, "VP8X")), (300, 200))
        self.assertEqual(image_size(_webp_with_size(300, 200, "VP8L")), (300, 200))
        self.assertEqual(image_size(_webp_with_size(300, 200, "VP8 ")), (300, 200))

    def test_bmp_top_down(self) -> None:
        pixels = _pixels(3, 2)
        dib = _make_dib(3, 2, pixels, bpp=24, top_down=True)
        self.assertEqual(image_size(_bmp_file(dib)), (3, 2))

    def test_unknown_or_truncated(self) -> None:
        self.assertIsNone(image_size(b""))
        self.assertIsNone(image_size(b"garbage"))
        self.assertIsNone(image_size(PNG_SIG + b"\x00" * 8))
        self.assertIsNone(image_size(b"BM" + b"\x00" * 10))
        self.assertIsNone(image_size(b"\xff\xd8" + b"\xff\xc0\x00\x02"))


# ---------------------------------------------------------------------------
# PNG codec
# ---------------------------------------------------------------------------


class TestPngCodec(unittest.TestCase):
    def test_round_trip_various_sizes(self) -> None:
        for width, height in ((1, 1), (3, 2), (17, 5), (1, 9)):
            with self.subTest(width=width, height=height):
                rgba = _sample_rgba(width, height)
                png = encode_png(width, height, rgba)
                self.assertEqual(decode_png_rgba(png), (width, height, rgba))

    def test_encoded_png_structure(self) -> None:
        png = encode_png(2, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        self.assertTrue(png.startswith(PNG_SIG))
        self.assertEqual(png[12:16], b"IHDR")
        self.assertIn(b"IDAT", png)
        self.assertTrue(png.endswith(_chunk(b"IEND", b"")))
        self.assertEqual(image_size(png), (2, 1))

    def test_encode_rejects_bad_arguments(self) -> None:
        with self.assertRaises(ValueError):
            encode_png(0, 1, b"")
        with self.assertRaises(ValueError):
            encode_png(2, 2, b"\x00" * 15)
        with self.assertRaises(TypeError):
            encode_png(1, 1, "not bytes")  # type: ignore[arg-type]

    def test_decode_rgb_color_type_2(self) -> None:
        width, height = 5, 3
        rgba = _sample_rgba(width, height)
        rgb = bytearray()
        for i in range(width * height):
            rgb += rgba[i * 4 : i * 4 + 3]
        raw_rows = [bytes(rgb[y * width * 3 : (y + 1) * width * 3]) for y in range(height)]
        png = _png_with_filters(width, height, 2, raw_rows, [0, 1, 2])
        decoded = decode_png_rgba(png)
        expected = bytearray()
        for i in range(width * height):
            expected += rgba[i * 4 : i * 4 + 3] + b"\xff"
        self.assertEqual(decoded, (width, height, bytes(expected)))

    def test_decode_all_filter_types(self) -> None:
        width, height = 4, 5
        rgba = _sample_rgba(width, height)
        rows = _rgba_rows(width, height)
        filters = [0, 1, 2, 3, 4]
        # Reuse the same filters for every row so all five get exercised.
        png = _png_with_filters(
            width, height, 6, rows, [filters[y % len(filters)] for y in range(height)]
        )
        self.assertEqual(decode_png_rgba(png), (width, height, rgba))

    def test_decode_multiple_idat_chunks(self) -> None:
        width, height = 3, 2
        rgba = _sample_rgba(width, height)
        rows = _rgba_rows(width, height)
        filtered = b"".join(b"\x00" + row for row in rows)
        compressed = zlib.compress(filtered)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        split = len(compressed) // 2
        png = (
            PNG_SIG
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", compressed[:split])
            + _chunk(b"IDAT", compressed[split:])
            + _chunk(b"IEND", b"")
        )
        self.assertEqual(decode_png_rgba(png), (width, height, rgba))

    def test_decode_rejects_corruption(self) -> None:
        good = encode_png(2, 2, _sample_rgba(2, 2))
        with self.assertRaises(ImageError):
            decode_png_rgba(b"not a png")
        with self.assertRaises(ImageError):
            decode_png_rgba(good[:20])
        with self.assertRaises(ImageError):
            decode_png_rgba(good[:-10])  # truncated IEND/IDAT

    def test_decode_rejects_bad_crc(self) -> None:
        width, height = 1, 1
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        filtered = b"\x00" + b"\x01\x02\x03\x04"
        png = (
            PNG_SIG
            + _chunk(b"IHDR", ihdr, crc_delta=1)
            + _chunk(b"IDAT", zlib.compress(filtered))
            + _chunk(b"IEND", b"")
        )
        with self.assertRaises(ImageError):
            decode_png_rgba(png)

    def test_decode_rejects_unsupported_variants(self) -> None:
        def build(
            color_type: int = 6,
            bit_depth: int = 8,
            interlace: int = 0,
            compression: int = 0,
        ) -> bytes:
            ihdr = struct.pack(
                ">IIBBBBB", 1, 1, bit_depth, color_type, compression, 0, interlace
            )
            return (
                PNG_SIG
                + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
                + _chunk(b"IEND", b"")
            )

        for kwargs in (
            {"bit_depth": 16},
            {"color_type": 0},  # grayscale
            {"color_type": 3},  # palette
            {"interlace": 1},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ImageError):
                    decode_png_rgba(build(**kwargs))
        # Grayscale compressed payload length differs, but header checks fire first.
        with self.assertRaises(ImageError):
            decode_png_rgba(build(interlace=1))


# ---------------------------------------------------------------------------
# DIB / BMP conversion
# ---------------------------------------------------------------------------


class TestDibToPng(unittest.TestCase):
    def _decode_dib(self, dib: bytes) -> tuple[int, int, bytes]:
        return decode_png_rgba(dib_to_png(dib))

    def test_24bpp_bottom_up_with_row_padding(self) -> None:
        for width, height in ((5, 3), (7, 2)):  # strides 16 and 24 (2-3 pad bytes)
            with self.subTest(width=width):
                pixels = _pixels(width, height)
                dib = _make_dib(width, height, pixels, bpp=24)
                masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
                self.assertEqual(
                    self._decode_dib(dib),
                    (width, height, _expected_rgba(pixels, masks)),
                )

    def test_24bpp_top_down(self) -> None:
        width, height = 3, 2
        pixels = _pixels(width, height)
        dib = _make_dib(width, height, pixels, bpp=24, top_down=True)
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_32bpp_bi_rgb_keeps_alpha(self) -> None:
        width, height = 4, 2
        pixels = _pixels(width, height)
        dib = _make_dib(width, height, pixels)
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_32bpp_zero_alpha_becomes_opaque(self) -> None:
        width, height = 3, 3
        pixels = _pixels(width, height)
        dib = _make_dib(width, height, pixels, force_alpha_zero=True)
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        expected = _expected_rgba(pixels, masks, force_alpha_zero=True)
        self.assertEqual(self._decode_dib(dib), (width, height, expected))
        # Every alpha byte must be 255.
        self.assertTrue(all(a == 255 for a in expected[3::4]))

    def test_32bpp_bitfields_masks_after_40_byte_header(self) -> None:
        width, height = 2, 2
        pixels = _pixels(width, height)
        masks = (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
        dib = _make_dib(width, height, pixels, compression=3, masks=masks)
        # A 40-byte header has room for only 3 masks, so alpha is treated as
        # absent (opaque) even though the pixel data carries an alpha byte.
        expected_masks = (masks[0], masks[1], masks[2], 0)
        self.assertEqual(
            self._decode_dib(dib),
            (width, height, _expected_rgba(pixels, expected_masks)),
        )

    def test_32bpp_v5_header_masks_in_header_top_down(self) -> None:
        width, height = 3, 2
        pixels = _pixels(width, height)
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        dib = _make_dib(
            width,
            height,
            pixels,
            header_size=124,
            compression=3,
            masks=masks,
            top_down=True,
        )
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_24bpp_bitfields_custom_masks(self) -> None:
        width, height = 2, 2
        pixels = _pixels(width, height)
        # RGB byte order instead of BGR, deliberately non-byte-aligned order.
        masks = (0x0000FF, 0x00FF00, 0xFF0000, 0)
        dib = _make_dib(width, height, pixels, bpp=24, compression=3, masks=masks)
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_16bpp_bi_rgb_555(self) -> None:
        width, height = 3, 2
        pixels = _pixels(width, height)
        dib = _make_dib(width, height, pixels, bpp=16)
        masks = (0x7C00, 0x03E0, 0x001F, 0)
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_16bpp_bitfields_565(self) -> None:
        width, height = 3, 2
        pixels = _pixels(width, height)
        masks = (0xF800, 0x07E0, 0x001F, 0)
        dib = _make_dib(
            width, height, pixels, bpp=16, compression=3, masks=masks
        )
        self.assertEqual(
            self._decode_dib(dib), (width, height, _expected_rgba(pixels, masks))
        )

    def test_bmp_to_png_matches_dib(self) -> None:
        width, height = 5, 2
        pixels = _pixels(width, height)
        dib = _make_dib(width, height, pixels, bpp=24)
        bmp = _bmp_file(dib)
        self.assertEqual(image_size(bmp), (width, height))
        self.assertEqual(bmp_to_png(bmp), dib_to_png(dib))

    def test_bmp_with_zero_offset_falls_back_to_header(self) -> None:
        pixels = _pixels(2, 2)
        dib = _make_dib(2, 2, pixels, bpp=24)
        bmp = _bmp_file(dib, pixel_offset=14 + 40)  # correct offset, explicit
        empty_offset = bmp[:10] + b"\x00\x00\x00\x00" + bmp[14:]
        self.assertEqual(bmp_to_png(empty_offset), dib_to_png(dib))

    def test_rejects_unsupported_dibs(self) -> None:
        pixels = _pixels(2, 2)
        valid = _make_dib(2, 2, pixels, bpp=24)

        with self.subTest("8bpp"):
            header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 8, 0, 0, 0, 0, 0, 0)
            with self.assertRaises(ImageError):
                dib_to_png(header + b"\x00" * 8)
        with self.subTest("rle compression"):
            header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 1, 0, 0, 0, 0, 0)
            with self.assertRaises(ImageError):
                dib_to_png(header + b"\x00" * 16)
        with self.subTest("old header"):
            with self.assertRaises(ImageError):
                dib_to_png(struct.pack("<I", 12) + b"\x00" * 60)
        with self.subTest("zero width"):
            header = struct.pack("<IiiHHIIiiII", 40, 0, 2, 1, 24, 0, 0, 0, 0, 0, 0)
            with self.assertRaises(ImageError):
                dib_to_png(header + b"\x00" * 8)
        with self.subTest("truncated pixels"):
            with self.assertRaises(ImageError):
                dib_to_png(valid[:-1])
        with self.subTest("empty"):
            with self.assertRaises(ImageError):
                dib_to_png(b"")

    def test_rejects_bad_bmp(self) -> None:
        with self.assertRaises(ImageError):
            bmp_to_png(b"BM" + b"\x00" * 10)
        with self.assertRaises(ImageError):
            bmp_to_png(b"XX" + b"\x00" * 60)
        pixels = _pixels(2, 2)
        dib = _make_dib(2, 2, pixels, bpp=24)
        bmp = _bmp_file(dib)
        with self.assertRaises(ImageError):
            bmp_to_png(bmp[: len(bmp) - 3])


# ---------------------------------------------------------------------------
# High-level loaders
# ---------------------------------------------------------------------------


class TestLoadImageBytes(unittest.TestCase):
    def test_png_passthrough(self) -> None:
        rgba = _sample_rgba(3, 2)
        png = encode_png(3, 2, rgba)
        payload = load_image_bytes(png, source="clipboard", name="shot.png")
        self.assertEqual(payload.data, png)
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual((payload.width, payload.height), (3, 2))
        self.assertEqual(payload.source, "clipboard")
        self.assertEqual(payload.name, "shot.png")

    def test_bmp_converted_to_png(self) -> None:
        pixels = _pixels(4, 3)
        dib = _make_dib(4, 3, pixels, bpp=24)
        bmp = _bmp_file(dib)
        payload = load_image_bytes(bmp)
        self.assertEqual(payload.mime, "image/png")
        self.assertTrue(payload.data.startswith(PNG_SIG))
        self.assertEqual((payload.width, payload.height), (4, 3))
        self.assertIsNone(payload.name)
        self.assertEqual(payload.source, "file")

    def test_jpeg_passthrough(self) -> None:
        jpeg = _jpeg_with_size(320, 240)
        payload = load_image_bytes(jpeg)
        self.assertEqual(payload.mime, "image/jpeg")
        self.assertEqual(payload.data, jpeg)
        self.assertEqual((payload.width, payload.height), (320, 240))

    def test_rejects_unsupported_and_empty(self) -> None:
        with self.assertRaises(ImageError):
            load_image_bytes(b"")
        with self.assertRaises(ImageError):
            load_image_bytes(b"plain text")
        with self.assertRaises(ImageError):
            load_image_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        with self.assertRaises(ImageError):
            load_image_bytes("not bytes")  # type: ignore[arg-type]

    def test_rejects_oversized(self) -> None:
        oversized = PNG_SIG + b"\x00" * (imageio.MAX_IMAGE_BYTES + 1)
        with self.assertRaises(ImageError):
            load_image_bytes(oversized)

    def test_accepts_bytearray_and_memoryview(self) -> None:
        png = encode_png(1, 1, b"\x10\x20\x30\x40")
        self.assertEqual(load_image_bytes(bytearray(png)).data, png)
        self.assertEqual(load_image_bytes(memoryview(png)).data, png)
        self.assertEqual(image_size(bytearray(png)), (1, 1))
        self.assertEqual(image_size(memoryview(png)), (1, 1))
        self.assertEqual(detect_mime(memoryview(png)), "image/png")


class TestLoadImageFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, data: bytes) -> str:
        path = os.path.join(self._tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_loads_png_with_basename(self) -> None:
        png = encode_png(2, 2, _sample_rgba(2, 2))
        path = self._write("formula.png", png)
        payload = load_image_file(path)
        self.assertEqual(payload.data, png)
        self.assertEqual(payload.name, "formula.png")
        self.assertEqual((payload.width, payload.height), (2, 2))

    def test_loads_bmp_and_sets_name(self) -> None:
        pixels = _pixels(2, 1)
        bmp = _bmp_file(_make_dib(2, 1, pixels, bpp=24))
        path = self._write("screenshot.bmp", bmp)
        payload = load_image_file(path, source="screenshot")
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual(payload.name, "screenshot.bmp")
        self.assertEqual(payload.source, "screenshot")

    def test_missing_file_raises_image_error(self) -> None:
        with self.assertRaises(ImageError):
            load_image_file(os.path.join(self._tmp.name, "does-not-exist.png"))


class TestMimeHelpers(unittest.TestCase):
    def test_guess_mime_from_name(self) -> None:
        self.assertEqual(guess_mime_from_name("a.png"), "image/png")
        self.assertEqual(guess_mime_from_name("A.JPEG"), "image/jpeg")
        self.assertEqual(guess_mime_from_name("b.jpg"), "image/jpeg")
        self.assertEqual(guess_mime_from_name("c.gif"), "image/gif")
        self.assertEqual(guess_mime_from_name("d.webp"), "image/webp")
        self.assertEqual(guess_mime_from_name("e.bmp"), "image/bmp")
        self.assertIsNone(guess_mime_from_name("f.txt"))
        self.assertIsNone(guess_mime_from_name("noextension"))
        self.assertIsNone(guess_mime_from_name(""))

    def test_module_constants(self) -> None:
        self.assertEqual(
            imageio.SUPPORTED_MIMES,
            ("image/png", "image/jpeg", "image/gif", "image/webp"),
        )
        self.assertEqual(imageio.MAX_IMAGE_BYTES, 32 * 1024 * 1024)
        for extension, mime in imageio.MIME_BY_EXT.items():
            self.assertTrue(extension.startswith("."))
            self.assertTrue(mime.startswith("image/"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
