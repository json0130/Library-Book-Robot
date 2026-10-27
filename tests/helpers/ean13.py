"""A minimal EAN-13 barcode encoder and PNG writer for tests and tools. Standard library only.

    modules = encode("9780345391803")                   # 95 characters of "0"/"1" (1 = bar)
    width, height, gray = bitmap("9780345391803")        # 8-bit grayscale, row-major, black bars on white
    png = png_bytes(width, height, gray)                 # PNG file contents

EAN-13 layout: start guard 101, six digits (parity chosen by the first digit), centre guard
01010, six digits, end guard 101. The first digit is encoded only through the parity pattern.
"""

from __future__ import annotations

import struct
import zlib

L_CODES = ("0001101", "0011001", "0010011", "0111101", "0100011",
           "0110001", "0101111", "0111011", "0110111", "0001011")
G_CODES = tuple(code[::-1].translate(str.maketrans("01", "10")) for code in L_CODES)   # reversed complement of L
R_CODES = tuple(code.translate(str.maketrans("01", "10")) for code in L_CODES)
PARITY = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
          "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")


def check_digit(first12: str) -> str:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(first12))
    return str((10 - total % 10) % 10)


def encode(digits: str) -> str:
    """The 95 modules of an EAN-13 as "0"/"1" characters. Raises ValueError for a bad code."""
    if len(digits) != 13 or not digits.isdigit():
        raise ValueError("EAN-13 needs exactly 13 digits")
    if check_digit(digits[:12]) != digits[12]:
        raise ValueError(f"bad EAN-13 check digit in {digits}")
    parity = PARITY[int(digits[0])]
    left = "".join((L_CODES if p == "L" else G_CODES)[int(d)] for p, d in zip(parity, digits[1:7]))
    right = "".join(R_CODES[int(d)] for d in digits[7:])
    return "101" + left + "01010" + right + "101"


def bitmap(digits: str, module_px: int = 3, height: int = 120, quiet_modules: int = 10):
    """(width, height, gray bytes) with quiet zones on both sides; 255 = white, 0 = black."""
    modules = encode(digits)
    row = bytearray([255] * (quiet_modules * module_px))
    for m in modules:
        row += bytes([0 if m == "1" else 255]) * module_px
    row += bytes([255] * (quiet_modules * module_px))
    pad = bytes([255]) * (len(row) * (height // 4))            # white margin above and below
    return len(row), height + 2 * (height // 4), pad + bytes(row) * height + pad


def png_bytes(width: int, height: int, gray: bytes) -> bytes:
    """A grayscale 8-bit PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + gray[y * width:(y + 1) * width] for y in range(height))   # filter 0 per row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
