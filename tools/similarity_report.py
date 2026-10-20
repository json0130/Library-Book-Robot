"""See how the book graph's similarity compares with text similarity alone, and measure it against your labels.

    python tools/similarity_report.py                 # per book: top-3 by graph vs by text, and where they disagree
    python tools/similarity_report.py --k 5
    python tools/similarity_report.py --make-pairs    # write data/eval/similarity_pairs.csv (about 20 pairs) to label
    python tools/similarity_report.py --eval          # read your labels and score each method

The report: for every book its top-k neighbours by the graph's total score (topics, broader topics, author,
series, call number, curated links and text together) and by text similarity alone, the overlap@k between the
two lists (how many of the k are in both), and the books where they disagree most. A disagreement usually means
a missing or wrong topic: text says two books are alike but they share no topic, or the reverse.

--make-pairs picks about 20 pairs from across the score range (high, middle and low graph scores, shuffled so the
order doesn't hint at the answer) and leaves `label` empty. Fill it in: 1 if a reader of one book would want the
other, 0 if not; leave a row blank to skip it. --eval then reports, for the graph, text alone and the old
tag-overlap (Jaccard) baseline: the mean score of the similar pairs vs the not-similar pairs, and the pairwise
ranking accuracy (how often a similar pair scores above a not-similar one; 0.5 is a coin flip, 1.0 perfect).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.app import build_finder  # noqa: E402
from robot.config import load_config  # noqa: E402
from robot.services.books import CatalogError, read_rows  # noqa: E402

PAIR_COLUMNS = ("isbn_a", "title_a", "isbn_b", "title_b", "label")
TRUE, FALSE = ("1", "y", "yes", "true", "similar"), ("0", "n", "no", "false", "different")


# ---- the scoring methods being compared: each takes (graph, isbn_a, isbn_b) and returns a number ----
def graph_score(graph, a: str, b: str) -> float:
    return graph.score(a, b).total


def text_score(graph, a: str, b: str) -> float:
    return graph.embedder.similarity(a, b)


def jaccard_score(graph, a: str, b: str) -> float:
    """The old method: Jaccard overlap of genre and tags, as written in the catalogue."""
    def topics(isbn):
        book = graph.catalog.lookup(isbn)
        return {t.lower() for t in (book.genre, *book.subjects) if t}
    ta, tb = topics(a), topics(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


METHODS = {"graph": graph_score, "text only": text_score, "jaccard (old)": jaccard_score}


def neighbours(graph, isbn: str, score, k: int) -> list:
    """The k other books scoring highest by `score`, best first (ties by title)."""
    others = [b for b in graph.catalog.books if b.isbn13 != isbn]
    others.sort(key=lambda b: (-round(score(graph, isbn, b.isbn13), 9), b.title.lower()))
    return others[:k]


def report(graph, k: int = 3) -> str:
    books = sorted(graph.catalog.books, key=lambda b: b.title.lower())
    k = max(1, min(k, len(books) - 1))
    lines, rows = [f"Top {k} neighbours of each book: graph vs text similarity alone", ""], []
    for book in books:
        by_graph = neighbours(graph, book.isbn13, graph_score, k)
        by_text = neighbours(graph, book.isbn13, text_score, k)
        common = {b.isbn13 for b in by_graph} & {b.isbn13 for b in by_text}
        rows.append((len(common) / k, book, by_graph, by_text))
        fmt = lambda items, score: ", ".join(f"{b.title} ({score(graph, book.isbn13, b.isbn13):.2f})" for b in items)
        lines += [f"{book.title}  overlap@{k} = {len(common)}/{k}",
                  f"    graph: {fmt(by_graph, graph_score)}", f"    text:  {fmt(by_text, text_score)}"]
    mean = sum(r[0] for r in rows) / len(rows) if rows else 0.0
    lines += ["", f"Mean overlap@{k}: {mean:.2f}", "", "Biggest disagreements (lowest overlap first):"]
    for overlap, book, by_graph, by_text in sorted(rows, key=lambda r: (r[0], r[1].title.lower()))[:5]:
        mine = set(graph.topics_of(book.isbn13))
        only_text = [b for b in by_text if b.isbn13 not in {g.isbn13 for g in by_graph}]
        only_graph = [b for b in by_graph if b.isbn13 not in {t.isbn13 for t in by_text}]
        if not only_text and not only_graph:
            continue
        lines.append(f"  {book.title}: overlap {overlap:.2f}")
        for b in only_text:
            shared = mine & set(graph.topics_of(b.isbn13))
            lines.append(f"    text says {b.title} is alike; "
                         + (f"they share the topic(s) {', '.join(sorted(shared))}" if shared
                            else "but they share NO topic: a missing or wrong topic?"))
        for b in only_graph:
            lines.append(f"    graph says {b.title} is alike; their descriptions share little text")
    return "\n".join(lines)


# ---- pairs to label, and scoring against the labels ----
def make_pairs(graph, count: int = 20, seed: int = 1) -> list:
    """About `count` pairs from the high, middle and low thirds of the graph score, shuffled, as CSV rows."""
    pairs = sorted(itertools.combinations(graph.catalog.books, 2),
                   key=lambda p: (-round(graph_score(graph, p[0].isbn13, p[1].isbn13), 9), p[0].title, p[1].title))
    if len(pairs) <= count:
        chosen = pairs
    else:
        third, chosen = len(pairs) // 3, []
        for part, share in ((pairs[:third], count // 3), (pairs[third:2 * third], count - 2 * (count // 3)),
                            (pairs[2 * third:], count // 3)):
            step = len(part) / share
            chosen += [part[int(i * step + step / 2)] for i in range(share)]
    random.Random(seed).shuffle(chosen)
    return [[a.isbn13, a.title, b.isbn13, b.title, ""] for a, b in chosen]


def write_pairs(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("# Label each pair in the last column: 1 if someone who likes one of these books would want the other,\n"
                "# 0 if not. Leave a row blank to skip it. Then run: python tools/similarity_report.py --eval\n")
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(PAIR_COLUMNS)
        writer.writerows(rows)


def read_labels(path: Path) -> tuple:
    """([(isbn_a, isbn_b, 1 or 0)], [problems]) from a labelled pairs file; unlabelled rows are skipped."""
    _, _, rows = read_rows(path, ("isbn_a", "isbn_b", "label"))
    labelled, problems = [], []
    for line_no, row in rows:
        label = row["label"].strip().lower()
        if not label:
            continue
        if label in TRUE:
            labelled.append((row["isbn_a"], row["isbn_b"], 1))
        elif label in FALSE:
            labelled.append((row["isbn_a"], row["isbn_b"], 0))
        else:
            problems.append(f"{path.name}:{line_no}: label {row['label']!r} is not 1 or 0")
    return labelled, problems


def evaluate(graph, labelled: list) -> tuple:
    """({method: (mean score of similar pairs, mean of not-similar, ranking accuracy or None)}, number skipped, (similar, not similar))."""
    usable, skipped = [], 0
    for a, b, label in labelled:
        if graph.catalog.lookup(a) and graph.catalog.lookup(b):
            usable.append((graph.catalog.lookup(a).isbn13, graph.catalog.lookup(b).isbn13, label))
        else:
            skipped += 1
    results = {}
    for name, method in METHODS.items():
        scores = [(method(graph, a, b), label) for a, b, label in usable]
        yes, no = [s for s, label in scores if label], [s for s, label in scores if not label]
        accuracy = None
        if yes and no:
            wins = sum(1.0 if y > n else 0.5 if y == n else 0.0 for y in yes for n in no)
            accuracy = wins / (len(yes) * len(no))
        results[name] = (sum(yes) / len(yes) if yes else None, sum(no) / len(no) if no else None, accuracy)
    return results, skipped, (sum(1 for *_, l in usable if l), sum(1 for *_, l in usable if not l))


def format_eval(results: dict, counts: tuple, skipped: int) -> str:
    num = lambda v: "   -  " if v is None else f"{v:6.3f}"
    lines = [f"Labelled pairs: {sum(counts)} ({counts[0]} similar, {counts[1]} not similar)"
             + (f"; {skipped} skipped (a book is not in the catalogue)" if skipped else ""), "",
             f"{'method':16s} {'mean similar':>13s} {'mean not':>9s} {'gap':>7s} {'ranking accuracy':>17s}"]
    for name, (yes, no, accuracy) in results.items():
        gap = None if yes is None or no is None else yes - no
        lines.append(f"{name:16s} {num(yes):>13s} {num(no):>9s} {num(gap):>7s} {num(accuracy):>17s}")
    if not (counts[0] and counts[1]):
        lines += ["", "Ranking accuracy needs at least one similar and one not-similar pair."]
    return "\n".join(lines)


def main(argv=None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--k", type=int, default=3, help="neighbours per book (default 3)")
    p.add_argument("--make-pairs", action="store_true", help="write the pairs file to label")
    p.add_argument("--eval", action="store_true", help="score the methods against your labels")
    p.add_argument("--pairs", type=Path, default=cfg.path("data/eval/similarity_pairs.csv"),
                   help="the pairs file (default data/eval/similarity_pairs.csv)")
    p.add_argument("--count", type=int, default=20, help="pairs to write with --make-pairs (default 20)")
    args = p.parse_args(argv)
    try:
        graph = build_finder(cfg).graph
    except CatalogError as exc:
        print(f"Cannot load the book data: {exc}", file=sys.stderr)
        return 1
    if len(graph.catalog) < 2:
        print("Need at least two books in the catalogue.", file=sys.stderr)
        return 1
    if args.make_pairs:
        if args.pairs.exists():
            print(f"{args.pairs} already exists; move it away first so your labels aren't overwritten.", file=sys.stderr)
            return 1
        rows = make_pairs(graph, args.count)
        write_pairs(args.pairs, rows)
        print(f"Wrote {len(rows)} pairs to {args.pairs}. Fill in the label column (1 = similar, 0 = not), "
              "then run with --eval.")
        return 0
    if args.eval:
        try:
            labelled, problems = read_labels(args.pairs)
        except CatalogError as exc:
            print(f"{exc}\nCreate it with --make-pairs first.", file=sys.stderr)
            return 1
        for problem in problems:
            print(f"  skipped: {problem}", file=sys.stderr)
        if not labelled:
            print(f"No labels in {args.pairs} yet: fill in the label column (1 or 0).", file=sys.stderr)
            return 1
        results, skipped, counts = evaluate(graph, labelled)
        print(format_eval(results, counts, skipped))
        return 0
    print(report(graph, args.k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
