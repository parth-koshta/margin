"""Margin's icon: three bars on a near-black tile, read top to bottom like a statement. Money in (white), money out
(dimmer), and what's left (green), which starts where money out ends: the margin. It stays clear at 16 pixels. One set
of shapes makes the SVG (browsers that take it), the PNGs (Safari, bookmarks) and the Margin app's icon, so they always
match. Standard library only: the PNGs are drawn here with 4x4 anti-aliasing and cached in memory.
"""
from __future__ import annotations

import functools
import os
import struct
import subprocess
import tempfile
import zlib

# 32 x 32 grid. Each bar is a stroke (width STROKE, round ends) from x0 to x1 at height y.
TILE, GLYPH, LEFT = "#111113", "#ffffff", "#3fd07a"
OUT_OPACITY = .45                            # money out: the glyph colour, see-through
APP_TILE = ("#2a2a2f", "#0c0c0e")            # the Mac app icon: a soft top-to-bottom shade
APP_GLYPH = ("#ffffff", "#d9e6fb")
APP_LEFT = ("#5fe394", "#25b466")
TILE_RADIUS = 8
STROKE = 4.4
BARS = [("in", 9.5, 9.0, 23.0), ("out", 16.0, 9.0, 16.0), ("left", 22.5, 16.0, 23.0)]   # kind, y, x0, x1


def _n(v: float) -> str:
    return f"{v:g}"


PATHS = {kind: f"M{_n(x0)} {_n(y)}H{_n(x1)}" for kind, y, x0, x1 in BARS}


def bars() -> str:
    """The three bars as SVG paths (no tile). Each path has the class b-<kind>, so a page can animate them."""
    paint = {"in": f'stroke="{GLYPH}"', "out": f'stroke="{GLYPH}" stroke-opacity="{OUT_OPACITY:g}"',
             "left": f'stroke="{LEFT}"'}
    return (f'<g fill="none" stroke-width="{_n(STROKE)}" stroke-linecap="round">'
            + "".join(f'<path class="b-{k}" d="{PATHS[k]}" {paint[k]}/>' for k, *_ in BARS) + "</g>")


def svg() -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            f'<rect width="32" height="32" rx="{TILE_RADIUS}" fill="{TILE}"/>'
            f'<rect x=".4" y=".4" width="31.2" height="31.2" rx="{TILE_RADIUS - .4}" fill="none" stroke="#fff" '
            f'stroke-opacity=".14" stroke-width=".8"/>'          # a faint edge, so the tile shows on dark backgrounds
            f'{bars()}</svg>').encode()


def _in_tile(x: float, y: float, x0: float, x1: float, r: float) -> bool:
    if not (x0 <= x < x1 and x0 <= y < x1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, x0 + r), x1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _bar_at(x: float, y: float) -> str | None:
    """Which bar the point (on the 32 grid) is on, if any."""
    half = STROKE / 2
    for kind, by, x0, x1 in BARS:
        cx = min(max(x, x0), x1)
        if (x - cx) ** 2 + (y - by) ** 2 <= half * half:
            return kind
    return None


def _rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


# Where the tile sits on the 32-unit canvas: (inset, corner radius). The bars scale with the tile.
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
    pair = (lambda c: (_rgb(c[0]), _rgb(c[1]))) if shade else (lambda c: (_rgb(c),) * 2)
    tile = pair(APP_TILE if shade else TILE)
    glyph = pair(APP_GLYPH if shade else GLYPH)
    left = pair(APP_LEFT if shade else LEFT)
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
                    t = gy / 32
                    col = _mix(*tile, t)
                    kind = _bar_at(gx, gy)
                    if kind == "in":
                        col = _mix(*glyph, t)
                    elif kind == "out":
                        col = _mix(col, _mix(*glyph, t), OUT_OPACITY)
                    elif kind == "left":
                        col = _mix(*left, t)
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
