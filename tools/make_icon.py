"""Generate Pic2TeX application icons (assets/icon.ico + assets/icon.png).

Pure standard library: renders a supersampled icon in memory and writes a
multi-size Windows ICO with uncompressed 32-bit DIB entries (no Pillow needed).

Usage:  py -3 tools/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pic2tex.imageio import encode_png  # noqa: E402

SS = 4  # supersampling factor
BLUE_TOP = (37, 99, 235)
BLUE_BOTTOM = (29, 58, 138)
WHITE = (255, 255, 255)
AMBER = (245, 158, 11)


def _inside_rounded_rect(x: float, y: float, w: float, h: float, r: float) -> bool:
    if x < r:
        cx, cy = r, r if y < r else (h - r if y > h - r else y)
    elif x > w - r:
        cx, cy = w - r, r if y < r else (h - r if y > h - r else y)
    else:
        return True
    if y < r:
        cy = r
    elif y > h - r:
        cy = h - r
    else:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render_rgba(size: int) -> bytes:
    """Return a *size* x *size* RGBA image (top-down rows)."""
    big = size * SS
    radius = big * 0.19
    buffer = bytearray(big * big * 4)
    for y in range(big):
        for x in range(big):
            if not _inside_rounded_rect(x + 0.5, y + 0.5, big, big, radius):
                continue
            t = (x + y) / (2.0 * (big - 1))
            r = int(BLUE_TOP[0] + (BLUE_BOTTOM[0] - BLUE_TOP[0]) * t)
            g = int(BLUE_TOP[1] + (BLUE_BOTTOM[1] - BLUE_TOP[1]) * t)
            b = int(BLUE_TOP[2] + (BLUE_BOTTOM[2] - BLUE_TOP[2]) * t)
            idx = (y * big + x) * 4
            buffer[idx : idx + 4] = bytes((r, g, b, 255))

    def fill_rect(x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int]) -> None:
        px0, py0 = int(x0 * big), int(y0 * big)
        px1, py1 = int(x1 * big), int(y1 * big)
        for py in range(max(0, py0), min(big, py1)):
            row = py * big * 4
            for px in range(max(0, px0), min(big, px1)):
                idx = row + px * 4
                buffer[idx : idx + 3] = bytes(color)

    # Stylised "T" (for TeX) with an amber baseline.
    fill_rect(0.24, 0.28, 0.76, 0.37, WHITE)
    fill_rect(0.44, 0.28, 0.56, 0.72, WHITE)
    fill_rect(0.27, 0.79, 0.73, 0.86, AMBER)

    # Box-filter downsample SS x SS.
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                base = ((y * SS + dy) * big + x * SS) * 4
                for dx in range(SS):
                    idx = base + dx * 4
                    r += buffer[idx]
                    g += buffer[idx + 1]
                    b += buffer[idx + 2]
                    a += buffer[idx + 3]
            n = SS * SS
            out_idx = (y * size + x) * 4
            out[out_idx : out_idx + 4] = bytes((r // n, g // n, b // n, a // n))
    return bytes(out)


def _dib_entry(size: int, rgba: bytes) -> bytes:
    """32-bit BGRA bottom-up DIB with an all-zero AND mask (valid ICO content)."""
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        row = y * size * 4
        for x in range(size):
            idx = row + x * 4
            r, g, b, a = rgba[idx], rgba[idx + 1], rgba[idx + 2], rgba[idx + 3]
            pixels += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,        # BITMAPINFOHEADER size
        size,      # width
        size * 2,  # height (XOR + AND)
        1,         # planes
        32,        # bit count
        0,         # BI_RGB
        size * size * 4,
        0, 0, 0, 0,
    )
    return header + bytes(pixels) + mask


def make_ico(sizes: tuple[int, ...]) -> bytes:
    images = {size: render_rgba(size) for size in sizes}
    count = len(images)
    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * count
    for size in sorted(images):
        dib = _dib_entry(size, images[size])
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(dib), offset + len(blobs),
        )
        blobs += dib
    return struct.pack("<HHH", 0, 1, count) + bytes(entries) + bytes(blobs)


def main() -> int:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    ico = make_ico(sizes)
    (assets / "icon.ico").write_bytes(ico)
    rgba256 = render_rgba(256)
    (assets / "icon.png").write_bytes(encode_png(256, 256, rgba256))
    print(f"icon.ico: {len(ico)} bytes ({', '.join(map(str, sizes))})")
    print(f"icon.png: 256x256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
