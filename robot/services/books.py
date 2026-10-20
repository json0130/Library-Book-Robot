"""Book catalogue: ISBN lookup, the book graph (recommendations and shelf locations), and an optional Open
Library fallback.

    catalog = Catalog.load("data/catalog.csv")
    book = catalog.lookup("978-0-345-39180-3")        # Book or None
    catalog.find_title("hitchhiker")                   # fuzzy: Book or None
    finder = BookFinder(catalog, online_fallback=True, cache_dir="data/cache")
    finder.find("9780141439518")                       # catalogue first, then Open Library
    finder.recommend("9780345539434", 3)               # Recommendations with scores and reasons (book_graph.py)
    finder.locate("9780345539434")                     # Location: section, floor, shelf, zone, map pose

data/catalog.csv is SAMPLE data until the library's real catalogue replaces it. Required columns: isbn13,
title, author, genre, summary, shelf, slot, and subjects (semicolon separated; an older file may call it
tags). Optional: call_number, section, floor, series, series_index, year. docs/catalog.md describes each.
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

from robot.services.isbn import to_isbn13

log = logging.getLogger("robot.books")

COLUMNS = ("isbn13", "title", "author", "genre", "subjects", "summary", "shelf", "slot")
OPTIONAL_COLUMNS = ("call_number", "section", "floor", "series", "series_index", "year")
OPEN_LIBRARY_URL = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
ONLINE_TIMEOUT_S = 3.0


class CatalogError(ValueError):
    """A data file (catalogue, topics, shelves, links) is missing or malformed; the message names the file
    and line."""


def whole_number(row: dict, column: str) -> Optional[int]:
    """An optional whole-number cell of a CSV row: None if blank, ValueError naming the column if not a number."""
    text = (row.get(column) or "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        raise ValueError(f"{column} must be a whole number, got {text!r}") from None


def title_key(text: str) -> str:
    """A title for comparing: lower case, no punctuation, no leading "the"/"a"/"an"."""
    words = re.sub(r"[^a-z0-9 ]+", "", str(text or "").lower().replace("-", " ")).split()
    return " ".join(words[1:] if len(words) > 1 and words[0] in ("the", "a", "an") else words)


@dataclass(frozen=True)
class Book:
    isbn13: str
    title: str
    author: str = ""                       # several authors are separated by semicolons
    genre: str = ""
    subjects: tuple = ()                   # as written in the catalogue; the graph folds them into topics
    summary: Optional[str] = None
    shelf: Optional[str] = None            # a shelf id from data/shelves.csv
    slot: Optional[int] = None
    call_number: str = ""
    section: str = ""
    floor: Optional[int] = None
    series: str = ""
    series_index: Optional[int] = None
    year: Optional[int] = None
    found_in_catalog: bool = True          # False for books known only from the online fallback

    @property
    def tags(self) -> tuple:
        """The old name of `subjects`."""
        return self.subjects


def read_rows(path: Path | str, required: Iterable[str], aliases: Optional[dict] = None) -> tuple:
    """The data rows of a CSV file with a header: (path, header, [(line number, {column: text})]).

    Blank lines and lines starting with # are skipped. `aliases` maps an old column name to its new one
    ({"tags": "subjects"}). Raises CatalogError naming the file (and the line) if it is missing, empty or
    lacks a required column. Cells are stripped; a short row leaves its last columns empty.
    """
    path = Path(path)
    if not path.is_file():
        raise CatalogError(f"file not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        lines = [(n, line) for n, line in enumerate(f, 1) if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise CatalogError(f"{path}: the file is empty (no header line)")
    header_no, header_line = lines[0]
    aliases = aliases or {}
    header = [aliases.get(h.strip(), h.strip()) for h in next(csv.reader([header_line]))]
    missing = [c for c in required if c not in header]
    if missing:
        raise CatalogError(f"{path}:{header_no}: missing column(s): {', '.join(missing)}")
    rows = []
    for line_no, line in lines[1:]:
        cells = next(csv.reader([line]))
        rows.append((line_no, {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header)}))
    return path, header, rows


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
        path, _, rows = read_rows(path, COLUMNS, aliases={"tags": "subjects"})
        books = []
        for line_no, row in rows:
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
        subjects = tuple(t.strip() for t in (row.get("subjects") or "").split(";") if t.strip())
        return Book(isbn13=isbn, title=title, author=(row.get("author") or "").strip(),
                    genre=(row.get("genre") or "").strip(), subjects=subjects,
                    summary=(row.get("summary") or "").strip() or None,
                    shelf=(row.get("shelf") or "").strip() or None, slot=whole_number(row, "slot"),
                    call_number=(row.get("call_number") or "").strip(), section=(row.get("section") or "").strip(),
                    floor=whole_number(row, "floor"), series=(row.get("series") or "").strip(),
                    series_index=whole_number(row, "series_index"), year=whole_number(row, "year"))

    def lookup(self, isbn: str) -> Optional[Book]:
        """The catalogue book for an ISBN-10 or ISBN-13 (any formatting), or None."""
        code = to_isbn13(isbn)
        return self._by_isbn.get(code) if code else None

    def find_title(self, query: str, cutoff: float = 0.6) -> Optional[Book]:
        """The book a visitor means by a title typed or said loosely, or None. An ISBN works too. Tries the
        exact title, then a title that contains the words (or that the words contain), then the closest
        spelling; case, punctuation and a leading "the" don't matter, so "hitchhikers guide" finds
        "The Hitchhiker's Guide to the Galaxy"."""
        if (code := to_isbn13(query)) and (book := self._by_isbn.get(code)):
            return book
        wanted = title_key(query)
        if not wanted:
            return None
        titles = {title_key(b.title): b for b in self.books}
        if wanted in titles:
            return titles[wanted]
        if len(wanted) < 3:                       # too little to go on: "i" is in half the titles
            return None
        containing = sorted((len(t), t) for t in titles if wanted in t or (len(t) >= 4 and t in wanted))
        if containing:
            return titles[containing[0][1]]
        close = difflib.get_close_matches(wanted, list(titles), n=1, cutoff=cutoff)
        return titles[close[0]] if close else None


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
    """Catalogue first; with online_fallback, Open Library for ISBNs the catalogue lacks. Also the way to the
    book graph: recommendations and shelf locations (only for catalogue books)."""

    def __init__(self, catalog: Catalog, online_fallback: bool = True,
                 cache_dir: Path | str | None = None, timeout_s: float = ONLINE_TIMEOUT_S, graph=None):
        from robot.services.book_graph import BookGraph          # book_graph imports this module
        self.graph = graph if graph is not None else BookGraph(catalog)
        self.online = OpenLibrary(cache_dir, timeout_s) if online_fallback else None

    @property
    def catalog(self) -> Catalog:
        """The graph's catalogue, so a reload() is seen everywhere."""
        return self.graph.catalog

    def reload(self) -> None:
        """Read the data files again after editing them. Keeps the old data if the new files are bad."""
        self.graph.reload()

    def find(self, isbn: str) -> Optional[Book]:
        code = to_isbn13(isbn)
        if code is None:
            return None
        book = self.catalog.lookup(code)
        if book is None and self.online is not None:
            book = self.online.lookup(code)
        return book

    def recommend(self, isbn: str, n: int = 3) -> list:
        """book_graph.Recommendation objects (book, score, reasons) for an ISBN, best first; empty if the
        book is not in the catalogue."""
        book = self.catalog.lookup(isbn)
        return self.graph.recommend(book.isbn13, n) if book else []

    def locate(self, isbn: str):
        """Where a catalogue book lives (book_graph.Location), or None."""
        return self.locate_with_reason(isbn)[0]

    def locate_with_reason(self, isbn: str) -> tuple:
        """(Location or None, why it is missing or incomplete or None)."""
        book = self.catalog.lookup(isbn)
        if book is None:
            return None, "the book is not in the catalogue"
        return self.graph.locate_with_reason(book.isbn13)
