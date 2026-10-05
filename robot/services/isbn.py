"""ISBN handling: pure functions, no I/O.

    to_isbn13("0-345-39180-2")   -> "9780345391803"    (ISBN-10 converted)
    to_isbn13("978-0-345-39180-3") -> "9780345391803"
    to_isbn13("5901234123457")   -> None                (EAN-13 that is not an ISBN: no 978/979 prefix)
    to_isbn13("12345")           -> None                (5-digit price add-on, or anything else)
"""

from __future__ import annotations

import re
from typing import Optional

_SEPARATORS = re.compile(r"[\s-]+")


def normalize(text: str) -> str:
    """Strip hyphens and spaces and upper-case a trailing x (ISBN-10 check digit)."""
    return _SEPARATORS.sub("", str(text)).upper()


def is_valid_isbn10(code: str) -> bool:
    code = normalize(code)
    if not re.fullmatch(r"\d{9}[\dX]", code):
        return False
    total = sum((10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(code))
    return total % 11 == 0


def is_valid_isbn13(code: str) -> bool:
    code = normalize(code)
    if not re.fullmatch(r"97[89]\d{10}", code):
        return False
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


def isbn13_check_digit(first12: str) -> str:
    """The EAN-13 / ISBN-13 check digit for 12 digits."""
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(first12))
    return str((10 - total % 10) % 10)


def isbn10_to_isbn13(code: str) -> str:
    """Convert a valid ISBN-10 to ISBN-13. Raises ValueError if the ISBN-10 is not valid."""
    code = normalize(code)
    if not is_valid_isbn10(code):
        raise ValueError(f"not a valid ISBN-10: {code!r}")
    body = "978" + code[:9]
    return body + isbn13_check_digit(body)


def to_isbn13(text: str) -> Optional[str]:
    """The ISBN-13 for a scanned or typed code, or None if it is not a valid ISBN.

    Accepts ISBN-13 and ISBN-10 (converted), with or without hyphens and spaces. Rejects bad
    checksums, EAN-13 codes outside the 978/979 book prefixes, and 5-digit add-on codes. An
    18-digit EAN-13 + 5-digit add-on run together is read as its EAN-13 part.
    """
    code = normalize(text)
    if len(code) == 18 and code.isdigit():
        code = code[:13]
    if len(code) == 13:
        return code if is_valid_isbn13(code) else None
    if len(code) == 10:
        return isbn10_to_isbn13(code) if is_valid_isbn10(code) else None
    return None
