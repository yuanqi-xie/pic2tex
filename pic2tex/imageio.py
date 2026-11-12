"""Image sniffing, dimension parsing, PNG codecs and Windows DIB/BMP conversion.

The module uses only the Python standard library (``struct`` and ``zlib``) so
Pic2TeX runs on a stock CPython installation with no third-party packages.
Everything here is byte-level: no filesystem side effects beyond the explicit
``load_image_file`` helper and no Win32 API calls (see
:mod:`pic2tex.clipboard` for the clipboard plumbing).

Data flows the app relies on:

* :func:`detect_mime` / :func:`image_size` sniff raw bytes (PNG, JPEG, GIF,
  WebP and BMP headers).
* :func:`encode_png` / :func:`decode_png_rgba` are a minimal, self-contained
  8-bit RGBA PNG codec.  ``decode_png_rgba`` exists mainly so tests can verify
  the encoder independently.
* :func:`dib_to_png` converts the raw ``CF_DIB`` / ``CF_DIBV5`` clipboard bytes
  produced by Windows screenshot tools into a PNG the vision API accepts.
* :func:`load_image_bytes` / :func:`load_image_file` normalise arbitrary input
  into :class:`pic2tex.models.ImagePayload`.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, replace
from typing import Final

from .models import ImagePayload

__all__ = [
    "SUPPORTED_MIMES",
    "MIME_BY_EXT",
    "MAX_IMAGE_BYTES",
    "ImageError",
    "detect_mime",
    "image_size",
    "encode_png",
    "decode_png_rgba",
    "dib_to_png",
    "bmp_to_png",
    "load_image_bytes",
    "load_image_file",
    "guess_mime_from_name",
]

#: MIME types the vision API is allowed to receive.
SUPPORTED_MIMES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

#: MIME type lookup by (lower-case) file extension.
MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".dib": "image/bmp",
}

#: Hard upper bound for image bytes accepted by :func:`load_image_bytes`.
MAX_IMAGE_BYTES: Final[int] = 32 * 1024 * 1024

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI: Final[bytes] = b"\xff\xd8"
_GIF_SIGNATURES: Final[tuple[bytes, ...]] = (b"GIF87a", b"GIF89a")

#: Default 24/32-bit BI_RGB channel masks (BGR byte order little-endian).
_RGB_MASKS: Final[tuple[int, int, int]] = (0x00FF0000, 0x0000FF00, 0x000000FF)
_RGBA_ALPHA_MASK: Final[int] = 0xFF000000
#: Default BI_RGB 16-bit layout: 5 bits per channel (xRRRRRGG GGGBBBBB).
_RGB555_MASKS: Final[tuple[int, int, int]] = (0x7C00, 0x03E0, 0x001F)

_BI_RGB: Final[int] = 0
_BI_BITFIELDS: Final[int] = 3

#: DIB header sizes Windows may hand us (40/52/56 = v1/v2/v3, 108/124 = v4/v5).
_SUPPORTED_DIB_HEADER_SIZES: Final[tuple[int, ...]] = (40, 52, 56, 108, 124)


class ImageError(Exception):
    """Raised when image data cannot be recognised, decoded or converted."""


# ---------------------------------------------------------------------------
# Small struct helpers
# ---------------------------------------------------------------------------


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _as_bytes(data: object, *, what: str = "image data") -> bytes:
    """Normalise a bytes-like object to ``bytes`` with a clear error message."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise ImageError(f"{what} must be bytes-like, got {type(data).__name__}")


# ---------------------------------------------------------------------------
# Format sniffing and dimension parsing
# ---------------------------------------------------------------------------


def detect_mime(data: bytes) -> str | None:
    """Return the MIME type sniffed from magic bytes, or ``None``.

    Only the API-supported formats are reported.  Windows BMP data is
    recognised by :func:`image_size`/:func:`bmp_to_png` but deliberately
    returns ``None`` here because the API cannot take ``image/bmp``.
    """
    if not data:
        return None
    if isinstance(data, memoryview):
        data = data.tobytes()
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(_JPEG_SOI):
        return "image/jpeg"
    if data.startswith(_GIF_SIGNATURES):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # BMP ("BM") is intentionally None: recognised, but not API-supported.
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Parse ``(width, height)`` from the header of PNG/JPEG/GIF/WebP/BMP data.

    Returns ``None`` when the format is unknown or the header is truncated.
    The pixels are not decoded, so this is cheap even for large images.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    if not data:
        return None
    if isinstance(data, memoryview):
        data = data.tobytes()
    if data.startswith(_PNG_SIGNATURE):
        return _png_size(data)
    if data.startswith(_JPEG_SOI):
        return _jpeg_size(data)
    if data.startswith(_GIF_SIGNATURES):
        return _gif_size(data)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    if data.startswith(b"BM"):
        return _bmp_size(data)
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    # signature(8) + length(4) + "IHDR"(4) + width(4) + height(4)
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return (width, height) if width and height else None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Scan JPEG marker segments for the first SOFn frame header."""
    length = len(data)
    pos = 2
    while pos + 1 < length:
        if data[pos] != 0xFF:
            pos += 1
            continue
        # Skip fill bytes / repeated 0xFF.
        while pos < length and data[pos] == 0xFF:
            pos += 1
        if pos >= length:
            return None
        marker = data[pos]
        pos += 1
        if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD8:
            # Stuffed byte, TEM, RSTn or SOI: no length field.
            continue
        if marker == 0xD9:  # EOI before any SOF
            return None
        if pos + 2 > length:
            return None
        segment_length = struct.unpack_from(">H", data, pos)[0]
        if segment_length < 2 or pos + segment_length > length:
            return None
        is_sof = 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
        if is_sof:
            if segment_length < 7 or pos + 7 > length:
                return None
            height = struct.unpack_from(">H", data, pos + 3)[0]
            width = struct.unpack_from(">H", data, pos + 5)[0]
            return (width, height) if width and height else None
        pos += segment_length
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    width, height = struct.unpack_from("<HH", data, 6)
    return (width, height) if width and height else None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """Inspect the first relevant RIFF chunk (VP8X, VP8L or VP8)."""
    length = len(data)
    pos = 12
    while pos + 8 <= length:
        fourcc = data[pos : pos + 4]
        chunk_size = _u32(data, pos + 4)
        payload = pos + 8
        if payload + chunk_size > length:
            return None
        if fourcc == b"VP8X":
            # flags(1) + reserved(3) + canvas width-1 (3) + height-1 (3)
            if chunk_size < 10:
                return None
            width = int.from_bytes(data[payload + 4 : payload + 7], "little") + 1
            height = int.from_bytes(data[payload + 7 : payload + 10], "little") + 1
            return (width, height) if width and height else None
        if fourcc == b"VP8L":
            # signature byte 0x2F then 14-bit width-1 / height-1, LSB-first.
            if chunk_size < 5:
                return None
            bits = int.from_bytes(data[payload + 1 : payload + 5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return (width, height)
        if fourcc == b"VP8 ":
            # 3-byte frame tag, 3-byte keyframe start code, then dimensions.
            if chunk_size < 10:
                return None
            if data[payload + 3 : payload + 6] != b"\x9d\x01\x2a":
                return None
            width = struct.unpack_from("<H", data, payload + 6)[0] & 0x3FFF
            height = struct.unpack_from("<H", data, payload + 8)[0] & 0x3FFF
            return (width, height) if width and height else None
        pos = payload + chunk_size + (chunk_size & 1)  # chunks are 2-byte aligned
    return None


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 14 + 40 or data[:2] != b"BM":
        return None
    try:
        info = _parse_dib_info(data, base=14)
    except (ImageError, struct.error):
        return None
    return info.width, info.height


# ---------------------------------------------------------------------------
# Minimal PNG codec (8-bit RGB / RGBA)
# ---------------------------------------------------------------------------

_PNG_COLOR_RGB: Final[int] = 2
_PNG_COLOR_RGBA: Final[int] = 6


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    """Build one PNG chunk: length, type, payload and CRC32 over type+payload."""
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode top-down 8-bit RGBA pixels into a valid PNG byte string.

    ``rgba`` must contain exactly ``width * height * 4`` bytes.  Every scanline
    uses filter type 0 (None); the data is zlib-compressed from the standard
    library.
    """
    if not isinstance(width, int) or not isinstance(height, int):
        raise TypeError("width and height must be integers")
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid image dimensions {width}x{height}")
    if not isinstance(rgba, (bytes, bytearray, memoryview)):
        raise TypeError(f"rgba must be bytes-like, got {type(rgba).__name__}")
    pixels = bytes(rgba)
    expected = width * height * 4
    if len(pixels) != expected:
        raise ValueError(
            f"expected {expected} RGBA bytes for {width}x{height}, got {len(pixels)}"
        )
    stride = width * 4
    # Filter type 0 (None) for every scanline, assembled with C-level joins.
    filtered = b"".join(
        b"\x00" + pixels[y * stride : (y + 1) * stride] for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, _PNG_COLOR_RGBA, 0, 0, 0)
    return b"".join(
        (
            _PNG_SIGNATURE,
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(filtered, 6)),
            _png_chunk(b"IEND", b""),
        )
    )


def _paeth_predictor(a: int, b: int, c: int) -> int:
    """PNG Paeth predictor (a=left, b=above, c=upper-left)."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(
    filtered: bytes, width: int, height: int, bytes_per_pixel: int
) -> bytearray:
    """Reverse PNG scanline filtering into raw top-down pixel data."""
    stride = width * bytes_per_pixel
    expected = height * (stride + 1)
    if len(filtered) != expected:
        raise ImageError(
            f"PNG image data has {len(filtered)} bytes, expected {expected}"
        )
    out = bytearray(stride * height)
    previous = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = filtered[pos]
        pos += 1
        line = bytearray(filtered[pos : pos + stride])
        pos += stride
        if filter_type == 0:  # None
            pass
        elif filter_type == 1:  # Sub
            for i in range(bytes_per_pixel, stride):
                line[i] = (line[i] + line[i - bytes_per_pixel]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                a = line[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                b = previous[i]
                c = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                line[i] = (line[i] + _paeth_predictor(a, b, c)) & 0xFF
        else:
            raise ImageError(f"unsupported PNG filter type {filter_type} on row {y}")
        out[y * stride : (y + 1) * stride] = line
        previous = line
    return out


def decode_png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG to ``(w, h, rgba_bytes)``.

    This is a deliberately small reference decoder used by tests and utility
    code; indexed, grayscale, 16-bit and interlaced PNGs raise
    :class:`ImageError` rather than being half-supported.
    """
    raw = _as_bytes(data, what="PNG data")
    if not raw.startswith(_PNG_SIGNATURE):
        raise ImageError("not a PNG file (bad signature)")
    pos = len(_PNG_SIGNATURE)
    width = height = 0
    bit_depth = color_type = 0
    interlace = 0
    idat = bytearray()
    saw_ihdr = False
    saw_iend = False
    while pos < len(raw):
        if pos + 8 > len(raw):
            raise ImageError("truncated PNG chunk header")
        chunk_length = struct.unpack_from(">I", raw, pos)[0]
        chunk_type = raw[pos + 4 : pos + 8]
        payload_start = pos + 8
        payload_end = payload_start + chunk_length
        if payload_end + 4 > len(raw):
            raise ImageError(f"truncated PNG chunk {chunk_type!r}")
        payload = raw[payload_start:payload_end]
        stored_crc = struct.unpack_from(">I", raw, payload_end)[0]
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            raise ImageError(f"PNG CRC mismatch in chunk {chunk_type!r}")
        if chunk_type == b"IHDR":
            if saw_ihdr or chunk_length != 13:
                raise ImageError("invalid PNG IHDR chunk")
            saw_ihdr = True
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            if width == 0 or height == 0:
                raise ImageError("PNG has zero width or height")
            if bit_depth != 8:
                raise ImageError(f"unsupported PNG bit depth {bit_depth} (only 8)")
            if color_type not in (_PNG_COLOR_RGB, _PNG_COLOR_RGBA):
                raise ImageError(
                    f"unsupported PNG color type {color_type} (only RGB/RGBA)"
                )
            if compression != 0 or filter_method != 0:
                raise ImageError("unsupported PNG compression/filter method")
            if interlace != 0:
                raise ImageError("interlaced PNG is not supported")
        elif chunk_type == b"IDAT":
            idat += payload
        elif chunk_type == b"IEND":
            saw_iend = True
            break
        pos = payload_end + 4
    if not saw_ihdr:
        raise ImageError("PNG is missing the IHDR chunk")
    if not idat:
        raise ImageError("PNG has no image data")
    if not saw_iend:
        raise ImageError("PNG is missing the IEND chunk")
    try:
        filtered = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError(f"corrupt PNG image data: {exc}") from exc
    channels = 4 if color_type == _PNG_COLOR_RGBA else 3
    pixels = _unfilter(filtered, width, height, channels)
    if color_type == _PNG_COLOR_RGBA:
        return width, height, bytes(pixels)
    count = width * height
    rgba = bytearray(count * 4)
    rgba[0::4] = pixels[0::3]
    rgba[1::4] = pixels[1::3]
    rgba[2::4] = pixels[2::3]
    rgba[3::4] = b"\xff" * count
    return width, height, bytes(rgba)


# ---------------------------------------------------------------------------
# Windows DIB (CF_DIB / CF_DIBV5) conversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DibInfo:
    """Parsed BITMAPINFOHEADER family header plus resolved channel masks."""

    width: int
    height: int
    bit_count: int
    compression: int
    top_down: bool
    red_mask: int
    green_mask: int
    blue_mask: int
    alpha_mask: int
    header_size: int
    pixel_offset: int  # absolute offset into the buffer passed to the parser


def _parse_dib_info(data: bytes, base: int = 0) -> _DibInfo:
    """Parse a BITMAPINFOHEADER (or V4/V5) at ``data[base:]``.

    Palette entries are irrelevant for the 16/24/32-bit formats we accept, so
    the header size plus optional colour masks determine the pixel offset.
    """
    available = len(data) - base
    if available < 40:
        raise ImageError("truncated DIB: header needs at least 40 bytes")
    header_size = _u32(data, base)
    if header_size < 40:
        raise ImageError(f"unsupported DIB header size {header_size} (need >= 40)")
    if header_size > available:
        raise ImageError(
            f"truncated DIB header: declared {header_size} bytes, only {available}"
        )
    if header_size not in _SUPPORTED_DIB_HEADER_SIZES:
        # Larger/newer headers are structurally compatible (palette/masks are
        # before pixel data); accept them as long as the mandatory fields parse.
        if header_size < 124:
            raise ImageError(f"unsupported DIB header size {header_size}")
    width = _i32(data, base + 4)
    raw_height = _i32(data, base + 8)
    planes = _u16(data, base + 12)
    bit_count = _u16(data, base + 14)
    compression = _u32(data, base + 16)
    if width <= 0:
        raise ImageError(f"invalid DIB width {width}")
    if raw_height == 0:
        raise ImageError("invalid DIB height 0")
    if planes != 1:
        raise ImageError(f"unsupported DIB plane count {planes}")
    if bit_count not in (16, 24, 32):
        raise ImageError(
            f"unsupported DIB bit depth {bit_count} (only 16, 24 and 32 bpp)"
        )
    if compression not in (_BI_RGB, _BI_BITFIELDS):
        raise ImageError(
            f"unsupported DIB compression {compression} (only BI_RGB and BI_BITFIELDS)"
        )

    top_down = raw_height < 0
    height = abs(raw_height)

    if compression == _BI_BITFIELDS:
        masks_offset: int
        if header_size >= 52:
            # V4/V5 store the masks inside the header.
            masks_offset = base + 40
        else:
            # 40-byte BITMAPINFOHEADER: 3 RGB masks follow the header.
            masks_offset = base + header_size
        if masks_offset + 12 > len(data):
            raise ImageError("truncated DIB: missing colour masks")
        red_mask = _u32(data, masks_offset)
        green_mask = _u32(data, masks_offset + 4)
        blue_mask = _u32(data, masks_offset + 8)
        alpha_mask = 0
        if header_size >= 56 and masks_offset + 16 <= len(data):
            alpha_mask = _u32(data, masks_offset + 12)
        masks_extra = 12 if header_size < 52 else 0
        for name, mask in (("red", red_mask), ("green", green_mask), ("blue", blue_mask)):
            if mask == 0:
                raise ImageError(f"DIB colour masks are invalid: {name} mask is zero")
        if red_mask & green_mask or red_mask & blue_mask or green_mask & blue_mask:
            raise ImageError("DIB colour masks overlap")
    else:  # BI_RGB defaults
        if bit_count == 16:
            red_mask, green_mask, blue_mask = _RGB555_MASKS
        else:
            red_mask, green_mask, blue_mask = _RGB_MASKS
        alpha_mask = _RGBA_ALPHA_MASK if bit_count == 32 else 0
        masks_extra = 0

    max_value = (1 << bit_count) - 1
    for name, mask in (
        ("red", red_mask),
        ("green", green_mask),
        ("blue", blue_mask),
        ("alpha", alpha_mask),
    ):
        if mask & ~max_value:
            raise ImageError(
                f"DIB {name} mask 0x{mask:08X} does not fit in {bit_count} bpp"
            )
    pixel_offset = base + header_size + masks_extra
    return _DibInfo(
        width=width,
        height=height,
        bit_count=bit_count,
        compression=compression,
        top_down=top_down,
        red_mask=red_mask,
        green_mask=green_mask,
        blue_mask=blue_mask,
        alpha_mask=alpha_mask,
        header_size=header_size,
        pixel_offset=pixel_offset,
    )


def _mask_shift_and_max(mask: int) -> tuple[int, int]:
    """Return ``(right_shift, max_value)`` for a contiguous channel mask."""
    if mask == 0:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    return shift, mask >> shift


def _scale_channel(value: int, shift: int, max_value: int) -> int:
    """Extract a masked channel value and scale it to 0..255."""
    if max_value == 0:
        return 0
    raw = (value >> shift) & max_value
    if max_value == 255:
        return raw
    return (raw * 255 + (max_value // 2)) // max_value


def _rasterize_dib(data: bytes, info: _DibInfo) -> bytes:
    """Convert DIB pixel data into top-down 8-bit RGBA bytes."""
    width = info.width
    height = info.height
    bit_count = info.bit_count
    stride = ((width * bit_count + 31) // 32) * 4
    pixel_end = info.pixel_offset + stride * height
    if info.pixel_offset < 0 or pixel_end > len(data):
        raise ImageError(
            "truncated DIB: pixel data needs "
            f"{pixel_end - info.pixel_offset} bytes at offset {info.pixel_offset}, "
            f"only {max(0, len(data) - info.pixel_offset)} available"
        )
    rgba = bytearray(width * height * 4)
    total = width * height
    # Fast paths for the layouts Windows actually produces (BGRA/BGR).
    standard_masks = (info.red_mask, info.green_mask, info.blue_mask) == _RGB_MASKS
    if bit_count == 32 and standard_masks and info.alpha_mask in (0, _RGBA_ALPHA_MASK):
        for y in range(height):
            row_index = y if info.top_down else height - 1 - y
            src = info.pixel_offset + row_index * stride
            row = data[src : src + stride]
            dst = y * width * 4
            rgba[dst : dst + width * 4 : 4] = row[2 : width * 4 : 4]
            rgba[dst + 1 : dst + width * 4 : 4] = row[1 : width * 4 : 4]
            rgba[dst + 2 : dst + width * 4 : 4] = row[0 : width * 4 : 4]
            if info.alpha_mask:
                rgba[dst + 3 : dst + width * 4 : 4] = row[3 : width * 4 : 4]
            else:
                rgba[dst + 3 : dst + width * 4 : 4] = b"\xff" * width
    elif bit_count == 24 and standard_masks and info.alpha_mask == 0:
        for y in range(height):
            row_index = y if info.top_down else height - 1 - y
            src = info.pixel_offset + row_index * stride
            row = data[src : src + stride]
            dst = y * width * 4
            rgba[dst : dst + width * 4 : 4] = row[2 : width * 3 : 3]
            rgba[dst + 1 : dst + width * 4 : 4] = row[1 : width * 3 : 3]
            rgba[dst + 2 : dst + width * 4 : 4] = row[0 : width * 3 : 3]
            rgba[dst + 3 : dst + width * 4 : 4] = b"\xff" * width
    else:
        bytes_per_pixel = bit_count // 8
        r_shift, r_max = _mask_shift_and_max(info.red_mask)
        g_shift, g_max = _mask_shift_and_max(info.green_mask)
        b_shift, b_max = _mask_shift_and_max(info.blue_mask)
        a_shift, a_max = _mask_shift_and_max(info.alpha_mask)
        for y in range(height):
            row_index = y if info.top_down else height - 1 - y
            src = info.pixel_offset + row_index * stride
            row = data[src : src + stride]
            dst = y * width * 4
            for x in range(width):
                offset = x * bytes_per_pixel
                value = int.from_bytes(row[offset : offset + bytes_per_pixel], "little")
                out = dst + x * 4
                rgba[out] = _scale_channel(value, r_shift, r_max)
                rgba[out + 1] = _scale_channel(value, g_shift, g_max)
                rgba[out + 2] = _scale_channel(value, b_shift, b_max)
                rgba[out + 3] = (
                    _scale_channel(value, a_shift, a_max) if a_max else 0xFF
                )
    # Screenshot convention: a fully transparent 32-bit alpha channel means
    # "no alpha information" (Windows often leaves the byte zeroed).
    if info.alpha_mask and total:
        alphas = rgba[3::4]
        if alphas.count(0) == total:
            rgba[3::4] = b"\xff" * total
    return bytes(rgba)


def _convert_dib(data: bytes, info: _DibInfo) -> bytes:
    stride = ((info.width * info.bit_count + 31) // 32) * 4
    needed = stride * info.height
    if info.pixel_offset < 0 or info.pixel_offset + needed > len(data):
        raise ImageError(
            "truncated DIB: pixel data out of range "
            f"(need {needed} bytes at offset {info.pixel_offset}, "
            f"buffer is {len(data)} bytes)"
        )
    rgba = _rasterize_dib(data, info)
    return encode_png(info.width, info.height, rgba)


def dib_to_png(dib: bytes) -> bytes:
    """Convert raw ``CF_DIB`` / ``CF_DIBV5`` bytes into PNG bytes.

    Supports 16/24/32 bpp, ``BI_RGB`` and ``BI_BITFIELDS``, bottom-up
    (positive height) and top-down (negative height) data, and 4-byte row
    padding.  A fully zero 32-bit alpha channel is treated as opaque.
    """
    data = _as_bytes(dib, what="DIB data")
    info = _parse_dib_info(data, base=0)
    return _convert_dib(data, info)


def bmp_to_png(bmp: bytes) -> bytes:
    """Convert a complete ``.bmp`` file (BITMAPFILEHEADER + DIB) into PNG."""
    data = _as_bytes(bmp, what="BMP data")
    if len(data) < 14 + 40:
        raise ImageError("truncated BMP: file header or DIB header is missing")
    if data[:2] != b"BM":
        raise ImageError("not a BMP file (missing 'BM' signature)")
    pixel_offset = _u32(data, 10)  # bfOffBits
    info = _parse_dib_info(data, base=14)
    if pixel_offset:
        if pixel_offset < 14 + info.header_size:
            raise ImageError(f"invalid BMP pixel data offset {pixel_offset}")
        if pixel_offset > len(data):
            raise ImageError("truncated BMP: pixel data offset beyond end of file")
        info = replace(info, pixel_offset=pixel_offset)
    return _convert_dib(data, info)


# ---------------------------------------------------------------------------
# High-level loading
# ---------------------------------------------------------------------------


def load_image_bytes(
    data: bytes, *, source: str = "file", name: str | None = None
) -> ImagePayload:
    """Normalise arbitrary image bytes into an :class:`ImagePayload`.

    PNG/JPEG/GIF/WebP data is passed through unchanged; BMP is converted to
    PNG.  Unsupported formats, empty input, data over 32 MiB and malformed BMP
    payloads raise :class:`ImageError`.
    """
    raw = _as_bytes(data)
    if not raw:
        raise ImageError("empty image data")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageError(
            f"image is too large ({len(raw)} bytes); maximum is "
            f"{MAX_IMAGE_BYTES} bytes (32 MiB)"
        )
    if raw[:2] == b"BM":
        png = bmp_to_png(raw)
        width, height = image_size(png) or (0, 0)
        return ImagePayload(
            data=png,
            mime="image/png",
            width=width,
            height=height,
            source=source,
            name=name,
        )
    mime = detect_mime(raw)
    if mime is None:
        raise ImageError(
            "unsupported image format (expected PNG, JPEG, GIF, WebP or BMP)"
        )
    width, height = image_size(raw) or (0, 0)
    return ImagePayload(
        data=raw,
        mime=mime,
        width=width,
        height=height,
        source=source,
        name=name,
    )


def load_image_file(
    path: str | os.PathLike[str], *, source: str = "file"
) -> ImagePayload:
    """Read ``path`` and return it as an :class:`ImagePayload`.

    All I/O and format errors are raised as :class:`ImageError` so the GUI only
    has to catch one exception type.  ``name`` is set to the file's basename.
    """
    file_path = os.fspath(path)
    try:
        file_size = os.path.getsize(file_path)
    except OSError as exc:
        raise ImageError(f"cannot read image file {file_path!r}: {exc}") from exc
    if file_size > MAX_IMAGE_BYTES:
        raise ImageError(
            f"image file is too large ({file_size} bytes); maximum is "
            f"{MAX_IMAGE_BYTES} bytes (32 MiB)"
        )
    try:
        with open(file_path, "rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise ImageError(f"cannot read image file {file_path!r}: {exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(
            f"image file is too large; maximum is {MAX_IMAGE_BYTES} bytes (32 MiB)"
        )
    return load_image_bytes(
        data, source=source, name=os.path.basename(file_path)
    )


def guess_mime_from_name(name: str) -> str | None:
    """Return the MIME type implied by a file extension, or ``None``."""
    if not name:
        return None
    try:
        extension = os.path.splitext(os.fspath(name))[1].lower()
    except (TypeError, ValueError):
        return None
    return MIME_BY_EXT.get(extension)
