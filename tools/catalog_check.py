"""Check the book data files: catalogue, topics, shelves and curated links.

    python tools/catalog_check.py                  # the files named in config/robot.yaml (books:)
    python tools/catalog_check.py --strict         # warnings count as failures too
    python tools/catalog_check.py --catalog my.csv --topics my.yaml --shelves s.csv --links l.csv

ERRORS (the exit code is 1): a bad ISBN checksum, the same ISBN twice, a book on a shelf that isn't in
shelves.csv, a storage slot outside the ring, a broken topics.yaml (a broader topic that doesn't exist, an
alias used twice, a loop), bad rows in shelves.csv or curated_links.csv, a link to a book that isn't in
the catalogue.
WARNINGS: subjects the vocabulary doesn't know (add an alias in topics.yaml), books with fewer than 2
topics, books with no neighbour scoring above the threshold (they never get recommended from or to),
call numbers that can't be read, a shelf, section or floor that disagrees with shelves.csv, series gaps.
Then some statistics: how often each topic is used, books per section and floor.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

import yaml  # noqa: E402

from robot.actuation.storage_ring import DEFAULT_SLOTS  # noqa: E402
from robot.config import ROOT, load_config  # noqa: E402
from robot.services.book_graph import BookGraph, SHELF_COLUMNS, LINK_COLUMNS, parse_links, parse_shelves  # noqa: E402
from robot.services.books import COLUMNS, Book, Catalog, CatalogError, read_rows  # noqa: E402
from robot.services.call_numbers import parse_call_number  # noqa: E402
from robot.services.topics import TopicVocabulary, norm_key  # noqa: E402

MIN_TOPICS = 2


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    summary: list = field(default_factory=list)          # what was read
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def short(path) -> str:
    """A path relative to the repo when it is inside it."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _read(report: Report, path, required, aliases=None):
    """The rows of a file, or None after recording why not."""
    if path is None:
        return None
    try:
        return read_rows(path, required, aliases)[2]
    except CatalogError as exc:
        report.errors.append(str(exc))
        return None


def check(catalog_path, topics_path=None, shelves_path=None, links_path=None, weights: Optional[dict] = None,
          slots: int = DEFAULT_SLOTS, min_score: Optional[float] = None) -> Report:
    """Check the files and return a Report. Never raises for a problem in the data."""
    report = Report()
    name = lambda p: Path(p).name

    # ---- topics
    vocab = TopicVocabulary()
    if topics_path is not None and Path(topics_path).is_file():
        try:
            data = yaml.safe_load(Path(topics_path).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            report.errors.append(f"{topics_path}: not valid YAML: {exc}")
            data = {}
        vocab = TopicVocabulary(data or {}, source=name(topics_path))
        report.errors.extend(vocab.problems)
        report.summary.append(f"{short(topics_path)} ({len(vocab)} topics)")
    elif topics_path is not None:
        report.warnings.append(f"{topics_path}: topics file not found, so every subject counts as unknown")

    # ---- shelves
    shelves, shelves_loaded = {}, False
    if shelves_path is not None and Path(shelves_path).is_file():
        rows = _read(report, shelves_path, SHELF_COLUMNS[:4])
        if rows is not None:
            shelves_loaded = True
            shelves, problems = parse_shelves(rows)
            report.errors.extend(f"{shelves_path}:{line}: {text}" for line, text in problems)
            report.summary.append(f"{short(shelves_path)} ({len(shelves)} shelves)")
            for shelf in shelves.values():
                if not shelf.has_pose:
                    report.warnings.append(f"{name(shelves_path)}: shelf {shelf.id} has no map position (x, y, yaw), "
                                           "so the robot can describe it but not drive to it")
    elif shelves_path is not None:
        report.warnings.append(f"{shelves_path}: shelves file not found, so no book has a location")

    # ---- catalogue
    books, seen = [], {}
    rows = _read(report, catalog_path, COLUMNS, {"tags": "subjects"})
    for line_no, row in rows or []:
        try:
            book = Catalog._book_from_row(row)
        except ValueError as exc:
            report.errors.append(f"{catalog_path}:{line_no}: {exc}")
            continue
        if book.isbn13 in seen:
            report.errors.append(f"{catalog_path}:{line_no}: duplicate ISBN {book.isbn13} "
                                 f"(also on line {seen[book.isbn13]})")
            continue
        seen[book.isbn13] = line_no
        books.append((line_no, book))
    if rows is not None:
        report.summary.insert(0, f"{short(catalog_path)} ({len(books)} books)")
    for line_no, book in books:
        _check_book(report, catalog_path, line_no, book, shelves, shelves_loaded, slots)

    # ---- curated links
    links = []
    if links_path is not None and Path(links_path).is_file():
        rows = _read(report, links_path, LINK_COLUMNS[:3])
        if rows is not None:
            links, problems = parse_links(rows)
            report.errors.extend(f"{links_path}:{line}: {text}" for line, text in problems)
            for link in links:
                for isbn in (link.from_isbn, link.to_isbn):
                    if isbn not in seen:
                        report.errors.append(f"{links_path}: link {link.from_isbn} -> {link.to_isbn} names {isbn}, "
                                             "which is not in the catalogue")
                        break
            report.summary.append(f"{short(links_path)} ({len(links)} links)")

    # ---- the graph: topics and neighbours
    catalog = Catalog([b for _, b in books])
    graph = BookGraph(catalog, vocab, shelves, [l for l in links if l.from_isbn in seen and l.to_isbn in seen],
                      weights=weights, min_score=min_score)
    threshold = graph.min_score
    for subject, isbns in sorted(graph.unknown_topics().items()):
        words = vocab.words
        guess = difflib.get_close_matches(subject, list(words), n=1, cutoff=0.75)
        hint = f" (did you mean {words[guess[0]]!r}? add {subject!r} as an alias)" if guess else \
            " (add it to topics.yaml, or as an alias of an existing topic)"
        titles = ", ".join(catalog.lookup(i).title for i in isbns[:3]) + ("..." if len(isbns) > 3 else "")
        report.warnings.append(f"unknown subject {subject!r}, used by {len(isbns)} book(s): {titles}{hint}")
    for book in catalog.books:
        topics = graph.topics_of(book.isbn13)
        if len(topics) < MIN_TOPICS:
            report.warnings.append(f"{book.title}: only {len(topics)} topic(s) ({', '.join(topics) or 'none'}); "
                                   f"give it at least {MIN_TOPICS} so it can be matched")
        best = max((graph.score(book.isbn13, o.isbn13).total for o in catalog.books if o.isbn13 != book.isbn13),
                   default=0.0)
        if len(catalog) > 1 and best < threshold:
            report.warnings.append(f"{book.title}: no neighbour scores {threshold:g} or more (best {best:.2f}), "
                                   "so it is never recommended or used to recommend; check its topics")

    # ---- statistics
    report.stats = {
        "books": len(catalog),
        "topics": dict(sorted(graph.topic_counts().items(), key=lambda kv: (-kv[1], kv[0]))),
        "sections": _count(b.section or "(none)" for b in catalog.books),
        "floors": _count(("level " + str(b.floor)) if b.floor is not None else "(none)" for b in catalog.books),
        "shelves": _count(b.shelf or "(none)" for b in catalog.books),
    }
    return report


def _count(items) -> dict:
    counts: dict = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _check_book(report: Report, path, line_no: int, book: Book, shelves: dict, have_shelves: bool, slots: int) -> None:
    where = f"{path}:{line_no}"
    if book.slot is not None and not 0 <= book.slot < slots:
        report.errors.append(f"{where}: {book.title}: slot {book.slot} is outside the storage ring (0..{slots - 1})")
    shelf = {k.lower(): v for k, v in shelves.items()}.get(book.shelf.lower()) if book.shelf else None
    if book.shelf and have_shelves and shelf is None:
        report.errors.append(f"{where}: {book.title}: shelf {book.shelf!r} is not defined in shelves.csv")
    if not book.shelf:
        report.warnings.append(f"{where}: {book.title}: no shelf, so the robot can't say where it belongs")
    if shelf is not None:
        if book.section and shelf.section and book.section != shelf.section:
            report.warnings.append(f"{where}: {book.title}: section {book.section!r} but shelf {shelf.id} is in "
                                   f"{shelf.section!r} (the shelf wins)")
        if book.floor is not None and shelf.floor is not None and book.floor != shelf.floor:
            report.warnings.append(f"{where}: {book.title}: floor {book.floor} but shelf {shelf.id} is on floor "
                                   f"{shelf.floor} (the shelf wins)")
    if book.call_number and not parse_call_number(book.call_number):
        report.warnings.append(f"{where}: {book.title}: call number {book.call_number!r} is not Dewey, "
                               "Library of Congress or a shelf code like 'F HER'; it is ignored when comparing")
    if book.series_index is not None and not book.series:
        report.warnings.append(f"{where}: {book.title}: series_index {book.series_index} but no series")


def format_report(report: Report, top_topics: int = 12) -> str:
    lines = ["Checked " + ", ".join(report.summary) if report.summary else "Nothing was read"]
    for title, items in (("ERRORS", report.errors), ("WARNINGS", report.warnings)):
        if items:
            lines += ["", f"{title} ({len(items)})"] + [f"  - {i}" for i in items]
    stats = report.stats
    if stats:
        lines += ["", "STATS", f"  {stats['books']} books"]
        topics = list(stats["topics"].items())
        lines.append("  topics: " + ", ".join(f"{t} {n}" for t, n in topics[:top_topics])
                     + (f" (+{len(topics) - top_topics} more)" if len(topics) > top_topics else ""))
        for key, label in (("sections", "section"), ("floors", "floor"), ("shelves", "shelf")):
            lines.append(f"  books per {label}: " + ", ".join(f"{k} {n}" for k, n in stats[key].items()))
    lines += ["", f"Result: {'OK' if report.ok else 'FAILED'} - {len(report.errors)} error(s), {len(report.warnings)} warning(s)"]
    return "\n".join(lines)


def main(argv=None) -> int:
    cfg = load_config()
    books = cfg.books
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--catalog", type=Path, default=cfg.path(books["catalog_path"]))
    p.add_argument("--topics", type=Path, default=cfg.path(books["topics_path"]))
    p.add_argument("--shelves", type=Path, default=cfg.path(books["shelves_path"]))
    p.add_argument("--links", type=Path, default=cfg.path(books["links_path"]))
    p.add_argument("--slots", type=int, default=DEFAULT_SLOTS, help="storage ring slots (default %(default)s)")
    p.add_argument("--min-score", type=float, default=None,
                   help="a book with no neighbour above this is reported (default: books.similarity.min_score)")
    p.add_argument("--strict", action="store_true", help="warnings make the exit code 1 as well")
    args = p.parse_args(argv)
    weights = {**books["similarity"], **({"min_score": args.min_score} if args.min_score is not None else {})}
    report = check(args.catalog, args.topics, args.shelves, args.links, weights, args.slots)
    print(format_report(report))
    return 0 if report.ok and not (args.strict and report.warnings) else 1


if __name__ == "__main__":
    sys.exit(main())
