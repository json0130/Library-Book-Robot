"""Book covers for the screen: downloaded ones in data/covers/<isbn>.jpg, else a generated placeholder.

    cover_file(covers_dir, isbn)          # the downloaded JPEG's Path, or None
    placeholder_svg(isbn, title, author)  # an SVG: the title on a colour derived from the ISBN (same ISBN, same colour)
    download_cover(isbn, covers_dir)      # one cover from Open Library's covers service (tools/import_books.py --covers)

Open Library: https://covers.openlibrary.org/b/isbn/<isbn>-L.jpg returns a blank image when it has no cover;
`?default=false` makes it answer 404 instead, so a missing cover is never saved as a blank picture. ISBN look-ups
are limited to 100 requests per IP every 5 minutes (more answers 403), hence the polite delay in the tool.
"""

from __future__ import annotations

import hashlib
import html
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

COVER_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
USER_AGENT = "Library-Book-Robot import_books (university research project)"
MIN_JPEG_BYTES = 1000                   # anything smaller is not a real cover


def cover_file(covers_dir, isbn: str) -> Optional[Path]:
    if not covers_dir or not isbn or not isbn.isdigit():
        return None
    path = Path(covers_dir) / f"{isbn}.jpg"
    return path if path.is_file() else None


def cover_colour(isbn: str) -> str:
    """A dark, saturated background colour from the ISBN, so each placeholder looks different but stable."""
    digest = hashlib.sha1(isbn.encode("ascii", "replace")).digest()
    hue = digest[0] * 360 // 256
    return f"hsl({hue}, 55%, 28%)"


def placeholder_svg(isbn: str, title: str = "", author: str = "") -> str:
    """A 2:3 cover with the title (wrapped, at most 5 lines) and the author on a colour derived from the ISBN."""
    lines = textwrap.wrap(title or isbn or "Book", 14)[:5] or ["Book"]
    if len(textwrap.wrap(title or "", 14)) > 5:
        lines[-1] = lines[-1][:12] + "…"
    size = 26 if max(len(l) for l in lines) <= 10 else 22
    top = 150 - (len(lines) - 1) * size * 0.6
    text = "".join(f'<tspan x="100" y="{top + i * size * 1.2:.0f}">{html.escape(l)}</tspan>' for i, l in enumerate(lines))
    by = html.escape(textwrap.shorten(author or "", 24, placeholder="…"))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" width="200" height="300">'
            f'<rect width="200" height="300" rx="8" fill="{cover_colour(isbn)}"/>'
            f'<rect x="10" y="10" width="180" height="280" rx="4" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="2"/>'
            f'<text font-family="sans-serif" font-weight="700" font-size="{size}" fill="#fff" text-anchor="middle">{text}</text>'
            f'<text x="100" y="270" font-family="sans-serif" font-size="15" fill="rgba(255,255,255,.8)" '
            f'text-anchor="middle">{by}</text></svg>')


def download_cover(isbn: str, covers_dir, timeout: float = 10.0, opener=urllib.request.urlopen) -> str:
    """Fetch one cover into covers_dir/<isbn>.jpg. Returns "saved", "exists", "missing" (Open Library has none)
    or "failed: <why>". Never raises for a network problem."""
    target = Path(covers_dir) / f"{isbn}.jpg"
    if target.is_file():
        return "exists"
    request = urllib.request.Request(COVER_URL.format(isbn=isbn), headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing"
        return f"failed: HTTP {exc.code}" + (" (rate limit: wait a few minutes)" if exc.code == 403 else "")
    except (urllib.error.URLError, OSError) as exc:
        return f"failed: {exc}"
    if len(data) < MIN_JPEG_BYTES or not data.startswith(b"\xff\xd8"):
        return "missing"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return "saved"
