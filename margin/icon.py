"""Margin's icon: a lowercase m drawn as two bars with rounded tops, the second one higher, white on a near-black
tile. It reads as the name and as a chart that rises, and stays clear at 16 pixels. One set of shapes makes the SVG
(browsers that take it), the PNGs (Safari, bookmarks) and the Margin app's icon, so they always match. Standard
library only: the PNGs are drawn here with 4x4 anti-aliasing and cached in memory.
"""
from __future__ import annotations

import functools
import math
import os
import struct
import subprocess
import tempfile
import zlib

# 32 x 32 grid. The m is one stroke (width STROKE, round ends) along three legs and two half-circle tops.
TILE, GLYPH = "#111113", "#ffffff"
APP_TILE = ("#2a2a2f", "#0c0c0e")           # the Mac app icon: a soft top-to-bottom shade
APP_GLYPH = ("#ffffff", "#d9e6fb")
TILE_RADIUS = 8
STROKE = 3.3
LEGS = [(9.5, 15.5, 23.0), (16.0, 12.5, 23.0), (22.5, 12.5, 23.0)]      # x, top, bottom
ARCS = [(12.75, 15.5, 3.25), (19.25, 12.5, 3.25)]                       # centre x, centre y, radius (top half)
PATH = "M9.5 23V15.5a3.25 3.25 0 0 1 6.5 0V23M16 15.5v-3a3.25 3.25 0 0 1 6.5 0V23"


def svg() -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            f'<rect width="32" height="32" rx="{TILE_RADIUS}" fill="{TILE}"/>'
            f'<rect x=".4" y=".4" width="31.2" height="31.2" rx="{TILE_RADIUS - .4}" fill="none" stroke="#fff" '
            f'stroke-opacity=".14" stroke-width=".8"/>'          # a faint edge, so the tile shows on dark backgrounds
            f'<path d="{PATH}" fill="none" stroke="{GLYPH}" stroke-width="{STROKE}" stroke-linecap="round" '
            f'stroke-linejoin="round"/></svg>').encode()


def _in_tile(x: float, y: float, x0: float, x1: float, r: float) -> bool:
    if not (x0 <= x < x1 and x0 <= y < x1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, x0 + r), x1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_m(x: float, y: float) -> bool:
    """Is the point (on the 32 grid) on the m's stroke?"""
    half = STROKE / 2
    for lx, top, bottom in LEGS:
        cy = min(max(y, top), bottom)
        if (x - lx) ** 2 + (y - cy) ** 2 <= half * half:
            return True
    return any(y <= cy and abs(math.hypot(x - cx, y - cy) - r) <= half for cx, cy, r in ARCS)


def _rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


# Where the tile sits on the 32-unit canvas: (inset, corner radius). The m scales with the tile.
STYLES = {
    "tab": (0, TILE_RADIUS),
    "square": (0, 0),                  # Apple rounds touch icons itself
    "mac": (100 / 32, 185.4 / 32),     # macOS app-icon grid: an 824 tile on a 1024 canvas, 185 corners
}


@functools.lru_cache(maxsize=None)
def png(size: int, style: str = "tab") -> bytes:
    """The icon as a size x size PNG in one of STYLES."""
    inset, radius = STYLES[style]
    k = (32 - 2 * inset) / 32
    shade = style == "mac"
    tile = (_rgb(APP_TILE[0]), _rgb(APP_TILE[1])) if shade else (_rgb(TILE),) * 2
    glyph = (_rgb(APP_GLYPH[0]), _rgb(APP_GLYPH[1])) if shade else (_rgb(GLYPH),) * 2
    n, scale = (4 if size <= 128 else 2), 32 / size   # 2x2 is plenty once pixels are this small
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        for px in range(size):
            r = g = b = a = 0
            for sy in range(n):
                y = (py + (sy + 0.5) / n) * scale
                for sx in range(n):
                    x = (px + (sx + 0.5) / n) * scale
                    if not _in_tile(x, y, inset, 32 - inset, radius):
                        continue
                    gx, gy = (x - inset) / k, (y - inset) / k          # back on the 32 grid
                    col = _mix(*(glyph if _in_m(gx, gy) else tile), gy / 32)
                    r += col[0]; g += col[1]; b += col[2]; a += 1
            if a:
                rows += bytes((round(r / a), round(g / a), round(b / a), round(255 * a / (n * n))))
            else:
                rows += b"\0\0\0\0"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def write_icns(path: str) -> None:
    """The Mac app icon (.icns) at every size macOS asks for. iconutil comes with macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "margin.iconset")
        os.mkdir(iconset)
        for pt in (16, 32, 128, 256, 512):
            for x in (1, 2):
                with open(os.path.join(iconset, f"icon_{pt}x{pt}{'@2x' if x == 2 else ''}.png"), "wb") as f:
                    f.write(png(pt * x, "mac"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", path], check=True, capture_output=True)


# Served without the sign-in key (there's nothing private in an icon), so the sign-in page shows it too.
ROUTES = {
    "/favicon.svg": (svg, "image/svg+xml"),
    "/favicon.png": (lambda: png(32), "image/png"),
    "/favicon.ico": (lambda: png(32), "image/png"),          # browsers that ask for the old name get the PNG
    "/apple-touch-icon.png": (lambda: png(180, "square"), "image/png"),
}

HEAD_TAGS = ('<link rel="icon" href="/favicon.png" sizes="32x32">'
             '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
             '<link rel="apple-touch-icon" href="/apple-touch-icon.png">')
