"""Book catalogue: ISBN lookup, similar books, and an optional Open Library fallback.

    catalog = Catalog.load("data/catalog.csv")
    book = catalog.lookup("978-0-345-39180-3")        # Book or None
    catalog.similar(book, n=3)                         # up to 3 related books, never `book` itself
    finder = BookFinder(catalog, online_fallback=True, cache_dir="data/cache")
    finder.find("9780141439518")                       # catalogue first, then Open Library

data/catalog.csv is SAMPLE data until the library's real catalogue replaces it. Columns:
isbn13, title, author, genre, tags (semicolon separated), summary, shelf, slot.
"""

from __future__ import annotations

import csv
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

from robot.services.isbn import to_isbn13

log = logging.getLogger("robot.books")

COLUMNS = ("isbn13", "title", "author", "genre", "tags", "summary", "shelf", "slot")
OPEN_LIBRARY_URL = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
ONLINE_TIMEOUT_S = 3.0


class CatalogError(ValueError):
    """The catalogue file is missing or malformed; the message names the file and line."""


@dataclass(frozen=True)
class Book:
    isbn13: str
    title: str
    author: str = ""
    genre: str = ""
    tags: tuple = ()
    summary: Optional[str] = None
    shelf: Optional[str] = None
    slot: Optional[int] = None
    found_in_catalog: bool = True          # False for books known only from the online fallback

    @property
    def topics(self) -> frozenset:
        """Genre and tags, lower-cased: the set that similar() compares."""
        return frozenset(t.lower() for t in (*self.tags, self.genre) if t)


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


class Catalog:
    def __init__(self, books: Iterable[Book] = ()):
        self.books: list[Book] = []
        self._by_isbn: dict[str, Book] = {}
        for book in books:
            if book.isbn13 in self._by_isbn:
                raise CatalogError(f"duplicate ISBN {book.isbn13} in the catalogue")
            self.books.append(book)
            self._by_isbn[book.isbn13] = book

    def __len__(self) -> int:
        return len(self.books)

    @classmethod
    def load(cls, path: Path | str) -> "Catalog":
        path = Path(path)
        if not path.is_file():
            raise CatalogError(f"catalogue file not found: {path} (expected columns: {', '.join(COLUMNS)})")
        with open(path, newline="", encoding="utf-8") as f:
            lines = [(n, line) for n, line in enumerate(f, 1) if line.strip() and not line.lstrip().startswith("#")]
        if not lines:
            raise CatalogError(f"{path}: the catalogue is empty (no header line)")
        header_no, header_line = lines[0]
        header = [h.strip() for h in next(csv.reader([header_line]))]
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            raise CatalogError(f"{path}:{header_no}: missing column(s): {', '.join(missing)}")
        books = []
        for line_no, line in lines[1:]:
            row = dict(zip(header, next(csv.reader([line]))))
            try:
                books.append(cls._book_from_row(row))
            except ValueError as exc:
                raise CatalogError(f"{path}:{line_no}: {exc}") from None
        try:
            return cls(books)
        except CatalogError as exc:
            raise CatalogError(f"{path}: {exc}") from None

    @staticmethod
    def _book_from_row(row: dict) -> Book:
        raw = (row.get("isbn13") or "").strip()
        isbn = to_isbn13(raw)
        if isbn is None:
            raise ValueError(f"bad ISBN {raw!r} (not a valid ISBN-10 or ISBN-13)")
        title = (row.get("title") or "").strip()
        if not title:
            raise ValueError(f"empty title for ISBN {isbn}")
        slot_text = (row.get("slot") or "").strip()
        try:
            slot = int(slot_text) if slot_text else None
        except ValueError:
            raise ValueError(f"slot must be a whole number, got {slot_text!r}") from None
        tags = tuple(t.strip() for t in (row.get("tags") or "").split(";") if t.strip())
        return Book(isbn13=isbn, title=title, author=(row.get("author") or "").strip(),
                    genre=(row.get("genre") or "").strip(), tags=tags,
                    summary=(row.get("summary") or "").strip() or None,
                    shelf=(row.get("shelf") or "").strip() or None, slot=slot)

    def lookup(self, isbn: str) -> Optional[Book]:
        """The catalogue book for an ISBN-10 or ISBN-13 (any formatting), or None."""
        code = to_isbn13(isbn)
        return self._by_isbn.get(code) if code else None

    def similar(self, book: Book, n: int = 3) -> list[Book]:
        """Up to n other books ranked by Jaccard overlap of genre and tags (best first).

        Ties break on title, then ISBN, so the order is always the same. Books sharing nothing
        with `book` are not similar and are left out.
        """
        scored = []
        for other in self.books:
            if other.isbn13 == book.isbn13:
                continue
            score = jaccard(book.topics, other.topics)
            if score > 0:
                scored.append((-score, other.title.lower(), other.isbn13, other))
        scored.sort(key=lambda s: s[:3])
        return [s[3] for s in scored[:max(0, n)]]


class OpenLibrary:
    """Title and author for an ISBN from openlibrary.org, cached on disk. Never raises."""

    def __init__(self, cache_dir: Path | str | None = None, timeout_s: float = ONLINE_TIMEOUT_S):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout_s = timeout_s

    def _cache_path(self, isbn: str) -> Optional[Path]:
        return self.cache_dir / f"{isbn}.json" if self.cache_dir else None

    def lookup(self, isbn: str) -> Optional[Book]:
        cache = self._cache_path(isbn)
        if cache is not None and cache.is_file():
            try:
                return self._to_book(isbn, json.loads(cache.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass                                   # unreadable cache entry: ask again
        url = OPEN_LIBRARY_URL.format(isbn=urllib.parse.quote(isbn))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("Open Library lookup for %s failed: %s", isbn, exc)
            return None
        entry = data.get(f"ISBN:{isbn}") if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not entry.get("title"):
            return None                                # not found online; not cached, it may be added later
        info = {"title": entry["title"],
                "authors": [a.get("name", "") for a in entry.get("authors", []) if isinstance(a, dict)]}
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(info), encoding="utf-8")
            except OSError as exc:
                log.debug("could not write cache %s: %s", cache, exc)
        return self._to_book(isbn, info)

    @staticmethod
    def _to_book(isbn: str, info: dict) -> Optional[Book]:
        title = info.get("title")
        if not title:
            return None
        return Book(isbn13=isbn, title=str(title), author=", ".join(a for a in info.get("authors", []) if a),
                    found_in_catalog=False)


class BookFinder:
    """Catalogue first; with online_fallback, Open Library for ISBNs the catalogue lacks."""

    def __init__(self, catalog: Catalog, online_fallback: bool = True,
                 cache_dir: Path | str | None = None, timeout_s: float = ONLINE_TIMEOUT_S):
        self.catalog = catalog
        self.online = OpenLibrary(cache_dir, timeout_s) if online_fallback else None

    def find(self, isbn: str) -> Optional[Book]:
        code = to_isbn13(isbn)
        if code is None:
            return None
        book = self.catalog.lookup(code)
        if book is None and self.online is not None:
            book = self.online.lookup(code)
        return book

    def similar_to(self, isbn: str, n: int = 3) -> list[Book]:
        """Similar catalogue books for an ISBN; empty if the book is not in the catalogue."""
        book = self.catalog.lookup(isbn)
        return self.catalog.similar(book, n) if book else []
