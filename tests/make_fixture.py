#!/usr/bin/env python3
"""Writes tests/fixtures/target.png: white, phone-sized, one red rectangle."""
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT = 1170, 2532
TARGET = (400, 1200, 770, 1360)  # x1, y1, x2, y2


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png():
    x1, y1, x2, y2 = TARGET
    white = b"\x00" + b"\xff\xff\xff" * WIDTH
    red = b"\x00" + b"\xff\xff\xff" * x1 + b"\xff\x00\x00" * (x2 - x1) + b"\xff\xff\xff" * (WIDTH - x2)
    rows = b"".join(red if y1 <= y < y2 else white for y in range(HEIGHT))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).parent / "fixtures" / "target.png"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(png())
    print(out)
