"""Generate the app icon set (PNG + ICO) with no external dependencies."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 512
OUT = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "icons"


def rounded_rect(x: float, y: float, w: float, h: float, r: float) -> bool:
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def circle_ring(x: float, y: float, cx: float, cy: float, radius: float, width: float) -> bool:
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    return radius - width <= distance <= radius


def sample(x: float, y: float) -> tuple[int, int, int, int]:
    # transparent outside the rounded square
    if not rounded_rect(x, y, SIZE, SIZE, 96):
        return (0, 0, 0, 0)

    # diagonal gradient: cyan -> indigo -> violet
    t = (x / SIZE) * 0.45 + (y / SIZE) * 0.55
    r = int(94 + (139 - 94) * t)
    g = int(234 * (1 - t) + 92 * t)
    b = int(212 * (1 - t) + 246 * t)

    # subtle inner highlight
    glow = max(0.0, 1.0 - (((x - 150) ** 2 + (y - 110) ** 2) ** 0.5) / 320.0)
    r = min(255, int(r + glow * 30))
    g = min(255, int(g + glow * 30))
    b = min(255, int(b + glow * 20))

    # two interlocking rings, the collaboration mark
    ring = circle_ring(x, y, 205, 256, 92, 26) or circle_ring(x, y, 307, 256, 92, 26)
    dot = ((x - 256) ** 2 + (y - 256) ** 2) ** 0.5 <= 20
    if ring or dot:
        return (248, 252, 255, 255)
    return (r, g, b, 255)


def render(size: int) -> bytes:
    scale = SIZE / size
    rows = []
    for py in range(size):
        row = bytearray()
        row.append(0)
        for px in range(size):
            # 2x2 supersampling for softer edges
            r = g = b = a = 0
            for sy in (0.25, 0.75):
                for sx in (0.25, 0.75):
                    sr, sg, sb, sa = sample((px + sx) * scale, (py + sy) * scale)
                    r += sr * sa
                    g += sg * sa
                    b += sb * sa
                    a += sa
            if a == 0:
                row.extend((0, 0, 0, 0))
            else:
                row.extend((r // a, g // a, b // a, a // 4))
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int) -> bytes:
    raw = render(size)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(png_bytes: bytes, size: int) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png_bytes), 22)
    return header + entry + png_bytes


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
    }.items():
        (OUT / name).write_bytes(png(size))
        print("wrote", name)
    large = png(256)
    (OUT / "icon.ico").write_bytes(ico(large, 256))
    print("wrote icon.ico")


if __name__ == "__main__":
    main()
