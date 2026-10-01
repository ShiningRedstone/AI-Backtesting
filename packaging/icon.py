"""Write packaging/munyun.ico: the Munyun Lab app icon (ADR-76).

A dark rounded square with the white chart-line mark of the app's top bar (the SVG path "M3 17l5-6 4 4 8-9" in a
24-unit box), drawn per size with anti-aliasing (no image library: zlib + struct only) and stored as PNG images inside
one .ico (Windows Vista and later). Deterministic: the same code writes the same bytes.

    python packaging/icon.py            # rewrites packaging/munyun.ico
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = Path(__file__).resolve().parent / "munyun.ico"
BG_TOP, BG_BOTTOM = (43, 43, 47), (18, 18, 20)          # the metallic tile of the UI (#2b2b2f -> #121214)
BORDER = (82, 82, 91)
LINE = (255, 255, 255)
PATH = [(3, 17), (8, 11), (12, 15), (20, 6)]            # the BrandMark polyline (24-unit viewBox)


def _seg_dist(px: float, py: float, a: tuple, b: tuple) -> float:
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _rounded_cover(x: float, y: float, s: int, r: float) -> float:
    """Coverage (0..1) of pixel centre (x, y) by a rounded square of side s and corner radius r."""
    cx = min(max(x, r), s - r)
    cy = min(max(y, r), s - r)
    d = math.hypot(x - cx, y - cy)
    return max(0.0, min(1.0, r - d + 0.5))


def render(size: int) -> bytes:
    s = size
    r = s * 0.22
    margin = s * 0.16                                    # mark inside the tile
    scale = (s - 2 * margin) / 18.0                      # path spans x 3..20 / y 6..17 -> fit 18 units
    pts = [(margin + (x - 2.5) * scale, margin + (y - 2.5) * scale) for x, y in PATH]
    stroke = max(1.4, s * 0.085)
    rows = []
    for j in range(s):
        row = bytearray([0])                             # PNG filter type 0
        for i in range(s):
            x, y = i + 0.5, j + 0.5
            cover = _rounded_cover(x, y, s, r)
            if cover <= 0:
                row += b"\0\0\0\0"
                continue
            t = y / s
            col = [BG_TOP[k] * (1 - t) + BG_BOTTOM[k] * t for k in range(3)]
            edge = _rounded_cover(x, y, s, r) - _rounded_cover(x - 0.0, y, s, r - max(1.0, s / 64))  # thin rim
            if s >= 32 and edge > 0:
                col = [col[k] * (1 - edge * 0.6) + BORDER[k] * edge * 0.6 for k in range(3)]
            d = min(_seg_dist(x, y, pts[k], pts[k + 1]) for k in range(len(pts) - 1))
            a = max(0.0, min(1.0, stroke / 2 - d + 0.5))
            col = [col[k] * (1 - a) + LINE[k] * a for k in range(3)]
            row += bytes([int(round(c)) for c in col] + [int(round(255 * cover))])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_ico(path: Path = OUT) -> Path:
    images = [render(s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, png in zip(SIZES, images):
        dim = 0 if s >= 256 else s                       # 0 means 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)
    return path


if __name__ == "__main__":
    print(write_ico())
