"""Start a catalogue from a list of ISBNs: look each up on Open Library and write a draft to review.

    python tools/import_books.py isbns.txt                   # one ISBN per line; # starts a comment
    python tools/import_books.py isbns.txt --out my_draft.csv --delay 2

For every ISBN it fetches the title, authors, subjects, description and publish year
(openlibrary.org/api/books?jscmd=details), maps the subjects through data/topics.yaml, and writes
data/catalog_draft.csv: one row per book in the catalog.csv column layout, for you to review and then paste
into catalog.csv. It never touches catalog.csv. Fields Open Library can't know are left blank:
genre, shelf, slot, call_number, section, floor, series and series_index. Authors are joined with "; ".

Subjects that match nothing in topics.yaml are left out of the draft and listed at the end, most common first,
with a suggestion when one looks like an existing topic: add them as aliases (or new topics) in topics.yaml and
run the import again. Answers are cached in data/cache/<isbn>.details.json (gitignored), so a re-run only asks
Open Library about ISBNs it hasn't seen, and waits --delay seconds (default 1) between those requests.
ISBNs already in catalog.csv are skipped unless you pass --include-existing.

    python tools/import_books.py isbns.txt --covers          # also download each book's cover
    python tools/import_books.py --covers-only               # just the covers, for every book in catalog.csv

--covers saves Open Library covers (covers.openlibrary.org/b/isbn/<isbn>-L.jpg?default=false, which answers 404
when there is no cover instead of a blank image) to data/covers/<isbn>.jpg (gitignored), skipping covers already
there. Open Library allows 100 cover look-ups by ISBN per 5 minutes, so it waits at least 3 s between them. Books
without a cover get a generated placeholder on the screen.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.config import load_config  # noqa: E402
from robot.services.books import Catalog, CatalogError  # noqa: E402
from robot.services.covers import download_cover  # noqa: E402
from robot.services.isbn import to_isbn13  # noqa: E402
from robot.services.topics import TopicVocabulary, norm_key  # noqa: E402

DETAILS_URL = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=details"
USER_AGENT = "Library-Book-Robot import_books (university research project)"
TIMEOUT_S = 10.0
DRAFT_COLUMNS = ("isbn13", "title", "author", "genre", "subjects", "summary", "shelf", "slot",
                 "call_number", "section", "floor", "series", "series_index", "year")
MAX_SUMMARY = 700


def read_isbns(path: Path) -> tuple:
    """([ISBN-13, ...] without repeats, [problem lines]) from a text file with one ISBN per line."""
    isbns, problems = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        isbn = to_isbn13(text)
        if isbn is None:
            problems.append(f"{path.name}:{number}: {text!r} is not a valid ISBN")
        elif isbn not in isbns:
            isbns.append(isbn)
    return isbns, problems


def fetch_details(isbn: str, cache_dir: Optional[Path], delay: float, last_request: list) -> Optional[dict]:
    """Open Library's `details` for an ISBN (cached), or None if it isn't there or the request fails.
    `last_request` is a one-item list holding when the last network request was made, for the polite delay."""
    cache = cache_dir / f"{isbn}.details.json" if cache_dir else None
    if cache is not None and cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                                              # unreadable: ask again
    wait = delay - (time.monotonic() - last_request[0])
    if last_request[0] and wait > 0:
        time.sleep(wait)
    request = urllib.request.Request(DETAILS_URL.format(isbn=urllib.parse.quote(isbn)),
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  {isbn}: request failed ({exc})", file=sys.stderr)
        return None
    finally:
        last_request[0] = time.monotonic()
    entry = data.get(f"ISBN:{isbn}") if isinstance(data, dict) else None
    details = entry.get("details") if isinstance(entry, dict) else None
    if not isinstance(details, dict) or not details.get("title"):
        return None                                           # not on Open Library: not cached, it may be added later
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(details), encoding="utf-8")
        except OSError as exc:
            print(f"  could not cache {isbn}: {exc}", file=sys.stderr)
    return details


def subject_names(details: dict) -> list:
    """The subjects as plain strings, whether Open Library gave strings or {"name": ...} objects."""
    names = []
    for subject in details.get("subjects") or []:
        name = subject.get("name") if isinstance(subject, dict) else subject
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def author_names(details: dict) -> list:
    return [a["name"].strip() for a in details.get("authors") or [] if isinstance(a, dict) and a.get("name")]


def description_text(details: dict) -> str:
    """The description on one line (the catalogue loader reads one row per line), cut to a reasonable length."""
    value = details.get("description")
    if isinstance(value, dict):
        value = value.get("value")
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_SUMMARY else text[:MAX_SUMMARY].rsplit(" ", 1)[0] + "..."


def publish_year(details: dict) -> str:
    match = re.search(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)", str(details.get("publish_date") or ""))     # also "c1985"
    return match.group(1) if match else ""


def map_subjects(subjects: list, vocab: TopicVocabulary) -> tuple:
    """(topics, unmatched): each subject through topics.yaml. Open Library subjects are often compound
    ("Space flight -- Fiction", "Mars (Planet)"), so the whole string is tried first, then its parts."""
    topics, unmatched = [], []
    for subject in subjects:
        parts = [subject] + [p for p in re.split(r"\s*--\s*", subject) if p != subject]
        parts += [re.sub(r"\s*\(.*?\)", "", p).strip() for p in parts]
        topic = next((vocab.canonical(p) for p in parts if p and vocab.canonical(p)), None)
        if topic is None:
            unmatched.append(subject)
        elif topic not in topics:
            topics.append(topic)
    return topics, unmatched


def draft_row(isbn: str, details: dict, vocab: TopicVocabulary) -> tuple:
    """(CSV row as a list in DRAFT_COLUMNS order, unmatched subjects)."""
    topics, unmatched = map_subjects(subject_names(details), vocab)
    unmatched = list(dict.fromkeys(unmatched))                    # a subject listed twice is still one subject
    row = {"isbn13": isbn, "title": str(details["title"]).strip(), "author": "; ".join(author_names(details)),
           "subjects": ";".join(topics), "summary": description_text(details), "year": publish_year(details)}
    return [row.get(c, "") for c in DRAFT_COLUMNS], unmatched


def suggestion(subject: str, vocab: TopicVocabulary) -> str:
    words = vocab.words
    close = difflib.get_close_matches(norm_key(subject), list(words), n=1, cutoff=0.7)
    return f" (looks like {words[close[0]]!r})" if close else ""


def import_books(isbns: list, vocab: TopicVocabulary, cache_dir: Optional[Path], delay: float, skip: set = frozenset()):
    """(rows, unmatched Counter, not_found list, skipped list) for the ISBNs, asking Open Library for the ones
    that aren't cached."""
    rows, unmatched, not_found, skipped = [], Counter(), [], []
    last_request = [0.0]
    for isbn in isbns:
        if isbn in skip:
            skipped.append(isbn)
            continue
        details = fetch_details(isbn, cache_dir, delay, last_request)
        if details is None:
            not_found.append(isbn)
            continue
        row, missed = draft_row(isbn, details, vocab)
        rows.append(row)
        unmatched.update(missed)
        print(f"  {isbn}  {row[1]}", flush=True)
    return rows, unmatched, not_found, skipped


COVER_DELAY_S = 3.0                       # 100 ISBN cover look-ups per 5 minutes is Open Library's limit


def fetch_covers(isbns: list, covers_dir: Path, delay: float, opener=None, sleep=time.sleep) -> dict:
    """Download the covers that are not there yet, waiting `delay` seconds between requests. Returns
    {"saved": [...], "exists": [...], "missing": [...], "failed": [...]}."""
    import urllib.request as request_module
    opener = opener or request_module.urlopen
    result = {"saved": [], "exists": [], "missing": [], "failed": []}
    asked = False
    for isbn in isbns:
        if (covers_dir / f"{isbn}.jpg").is_file():
            result["exists"].append(isbn)
            continue
        if asked:
            sleep(delay)
        asked = True
        outcome = download_cover(isbn, covers_dir, TIMEOUT_S, opener)
        key = outcome.split(":", 1)[0]
        result[key].append(isbn if key != "failed" else f"{isbn} ({outcome[8:]})")
        print(f"  cover {isbn}: {outcome}", flush=True)
    return result


def print_covers(result: dict, covers_dir: Path) -> None:
    print(f"\nCovers in {covers_dir}: {len(result['saved'])} saved, {len(result['exists'])} already there, "
          f"{len(result['missing'])} not on Open Library (a placeholder is shown)")
    for failure in result["failed"]:
        print(f"  failed: {failure}", file=sys.stderr)


def write_draft(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("# DRAFT from tools/import_books.py. Review it, fill in genre, shelf, slot, call_number, section,\n"
                "# floor, series and series_index, then paste the rows into catalog.csv (this file is never read).\n")
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(DRAFT_COLUMNS)
        writer.writerows(rows)


def main(argv=None) -> int:
    cfg = load_config()
    books = cfg.books
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("isbns", type=Path, nargs="?", help="text file with one ISBN per line")
    p.add_argument("--out", type=Path, default=cfg.path("data/catalog_draft.csv"), help="the draft to write")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to Open Library (default 1)")
    p.add_argument("--no-cache", action="store_true", help="ask Open Library again even for cached ISBNs")
    p.add_argument("--include-existing", action="store_true", help="also draft ISBNs already in catalog.csv")
    p.add_argument("--covers", action="store_true", help="also download each book's cover to data/covers/")
    p.add_argument("--covers-only", action="store_true", help="only download covers, for every book in catalog.csv "
                                                              "(or the ISBN file if given)")
    p.add_argument("--covers-dir", type=Path, default=cfg.path(books.get("covers_dir", "data/covers")))
    args = p.parse_args(argv)
    cover_delay = max(args.delay, COVER_DELAY_S)

    catalog_path = cfg.path(books["catalog_path"])
    if args.covers_only:
        if args.isbns is not None:
            if not args.isbns.is_file():
                print(f"ISBN file not found: {args.isbns}", file=sys.stderr)
                return 2
            isbns = read_isbns(args.isbns)[0]
        else:
            try:
                isbns = [b.isbn13 for b in Catalog.load(catalog_path).books]
            except CatalogError as exc:
                print(f"Cannot read the catalogue: {exc}", file=sys.stderr)
                return 2
        print(f"Covers for {len(isbns)} book(s), waiting {cover_delay:g} s between requests")
        print_covers(fetch_covers(isbns, args.covers_dir, cover_delay), args.covers_dir)
        return 0
    if args.isbns is None:
        p.error("give an ISBN file (or --covers-only)")
    if args.out.resolve() == catalog_path.resolve():
        print(f"Refusing to write over the catalogue ({catalog_path}); the draft goes to another file.", file=sys.stderr)
        return 2
    if not args.isbns.is_file():
        print(f"ISBN file not found: {args.isbns}", file=sys.stderr)
        return 2
    isbns, problems = read_isbns(args.isbns)
    for problem in problems:
        print(f"  skipped: {problem}", file=sys.stderr)
    try:
        vocab = TopicVocabulary.load(cfg.path(books["topics_path"]))
        existing = {b.isbn13 for b in Catalog.load(catalog_path).books} if catalog_path.is_file() else set()
    except CatalogError as exc:
        print(f"Cannot read the data files: {exc}", file=sys.stderr)
        return 2
    cache_dir = None if args.no_cache else cfg.path(books["cache_dir"])
    print(f"{len(isbns)} ISBN(s) from {args.isbns.name}; asking Open Library for the ones not cached "
          f"(waiting {args.delay:g} s between requests)")
    rows, unmatched, not_found, skipped = import_books(isbns, vocab, cache_dir, args.delay,
                                                       set() if args.include_existing else existing)
    write_draft(args.out, rows)
    print(f"\nWrote {len(rows)} book(s) to {args.out}")
    if args.covers:
        print_covers(fetch_covers([r[0] for r in rows], args.covers_dir, cover_delay), args.covers_dir)
    if skipped:
        print(f"Skipped {len(skipped)} already in the catalogue (--include-existing to draft them anyway)")
    if not_found:
        print("Not found on Open Library, so not in the draft: " + ", ".join(not_found))
    if unmatched:
        print("\nSubjects that match nothing in topics.yaml (left out of the draft). Add them as aliases of a topic, "
              "or as new topics, then run again:")
        for subject, count in unmatched.most_common():
            print(f"  {count:3d}  {subject}{suggestion(subject, vocab)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
