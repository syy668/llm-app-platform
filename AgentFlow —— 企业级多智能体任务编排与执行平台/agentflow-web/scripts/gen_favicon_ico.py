# -*- coding: utf-8 -*-
"""一次性脚本：生成 public/favicon.ico（32x32，PNG-in-ICO）。

用法: python scripts/gen_favicon_ico.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 32
R = 7  # 圆角半径
BLUE = (22, 93, 255)      # #165DFF
WHITE = (255, 255, 255)
LIGHT = (148, 191, 255)   # #94BFFF
TRANSPARENT = (0, 0, 0, 0)


def _in_rounded_rect(x: float, y: float) -> bool:
    if x < R and y < R:
        return (x - R) ** 2 + (y - R) ** 2 <= R * R
    if x > SIZE - 1 - R and y < R:
        return (x - (SIZE - 1 - R)) ** 2 + (y - R) ** 2 <= R * R
    if x < R and y > SIZE - 1 - R:
        return (x - R) ** 2 + (y - (SIZE - 1 - R)) ** 2 <= R * R
    if x > SIZE - 1 - R and y > SIZE - 1 - R:
        return (x - (SIZE - 1 - R)) ** 2 + (y - (SIZE - 1 - R)) ** 2 <= R * R
    return True


def _on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float, width: float) -> bool:
    """点到线段距离 <= width/2。"""
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return dx * dx + dy * dy <= (width / 2) ** 2


def pixel(x: int, y: int) -> tuple[int, int, int, int]:
    if not _in_rounded_rect(x + 0.5, y + 0.5):
        return TRANSPARENT
    cx, cy = x + 0.5, y + 0.5
    # 连线 (9,11)->(16,11)->(16,21)->(23,21)
    segs = [(9, 11, 16, 11), (16, 11, 16, 21), (16, 21, 23, 21)]
    for ax, ay, bx, by in segs:
        if _on_segment(cx, cy, ax, ay, bx, by, 2.2):
            return (*WHITE, 217)  # opacity 0.85
    # 节点圆
    for nx, ny, color in ((9, 11, WHITE), (16, 21, WHITE), (23, 21, LIGHT)):
        if (cx - nx) ** 2 + (cy - ny) ** 2 <= 9.0:  # r=3
            return (*color, 255)
    return (*BLUE, 255)


def build_png() -> bytes:
    raw = b""
    for y in range(SIZE):
        raw += b"\x00"  # filter none
        for x in range(SIZE):
            raw += bytes(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def wrap_ico(png: bytes) -> bytes:
    # ICONDIR + 1 个 ICONDIRENTRY（PNG 数据，宽高 32）
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE % 256, SIZE % 256, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "public" / "favicon.ico"
    out.write_bytes(wrap_ico(build_png()))
    print(f"written: {out} ({out.stat().st_size} bytes)")
