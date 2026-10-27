"""A small knowledge graph of the books: what they are about, who wrote them, where they live, how they relate.

    graph = BookGraph.from_files("data/catalog.csv", "data/topics.yaml", "data/shelves.csv", "data/curated_links.csv")
    graph.recommend(isbn, 3)              # [Recommendation(book, score, reasons), ...]
    graph.score(isbn_a, isbn_b)           # Score: a total and its components, each with a reason
    graph.locate(isbn)                    # Location(section, floor, shelf, zone, map pose) or None
    graph.reload()                        # after editing the CSV/YAML files

Nodes (plain dataclasses and dicts, no graph library; `kind` is one of NODE_KINDS):
    book, author, topic (a topic some book has), broader (a topic that only appears as the parent of one),
    class (a call-number class such as dewey:520 or lc:QB), shelf, section, series
Edges (kind, with `relation` and `note` where they apply):
    written_by  book -> author          about      book -> topic          broader  topic -> broader topic,
    in_class    book -> deepest class                                      and class -> broader class
    on_shelf    book -> shelf           in_section book -> section, shelf -> section
    in_series   book -> series          curated    book -> book (relation: series_next, librarian_pick, related)
Only books and the library's own layout are in the graph. Nothing about visitors, people or sessions ever is.

How two books are compared (score): seven components, each 0..1 with a short reason, weighted by
books.similarity in config/robot.yaml and divided by the weights' sum, so the total is 0..1:
    topics       shared topics, weighted by IDF (a rare topic counts for more than "fiction"): the IDF-weighted
                 Jaccard overlap, the shared topics' weight over everything either book is about. A general topic
                 (one with a narrower child in topics.yaml, like science or fiction) counts for only
                 books.similarity.general_topics (0.4) of its IDF
    broader      topics related through a broader topic ("astronomy" and "physics" both fall under "science"),
                 never counting a topic the books already share
    author       the same author
    series       the next book in the series 1.0, an earlier or other book of the same series 0.6
    call_number  same class, and how near the numbers are within it (QB981/QB982 yes, QB44.2/QB981 no); its reason
                 names the area ("both shelved in astronomy"), never a call number (services/call_numbers.py)
    curated      a librarian_pick or related row in curated_links.csv (series_next rows feed `series`)
    text         TF-IDF cosine of summary + subjects (services/text_similarity.py; swap in a better Embedder)
recommend() puts the next book in a series first, then ranks by total, breaking ties by title then ISBN, and
leaves out the book itself and anything below books.similarity.min_score.

Two kinds of reason come out. Score.reasons is the full list, strongest first, for the language model and the
logs. Recommendation.because is ONE natural sentence for speech, the most specific thing the two books have
in common, chosen in this order: series, same author, curated link, the most specific shared topic ("because,
like Cosmos, it's about astronomy"; a general topic only if no specific one is shared and no shelf area
applies), the shelf area, a broader topic, and last a generic line. It never lists several topics.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from robot.services.books import Book, Catalog, CatalogError, read_rows, whole_number
from robot.services.call_numbers import CLOSE_BY, CallNumber, closeness, parse_call_number, shared_area
from robot.services.isbn import to_isbn13
from robot.services.text_similarity import Embedder, TfidfEmbedder
from robot.services.topics import TopicVocabulary, norm_key

log = logging.getLogger("robot.graph")

NODE_KINDS = ("book", "author", "topic", "broader", "class", "shelf", "section", "series")
EDGE_KINDS = ("written_by", "about", "broader", "in_class", "on_shelf", "in_section", "in_series", "curated")
RELATIONS = ("series_next", "librarian_pick", "related")
ZONES = ("quiet", "common")
SHELF_COLUMNS = ("shelf", "section", "floor", "zone", "map_x", "map_y", "map_yaw", "description")
LINK_COLUMNS = ("from_isbn", "to_isbn", "relation", "note")

COMPONENTS = ("topics", "broader", "author", "series", "call_number", "curated", "text")
DEFAULT_WEIGHTS = {"topics": 0.30, "broader": 0.10, "author": 0.12, "series": 0.15,
                   "call_number": 0.08, "curated": 0.10, "text": 0.15}
DEFAULT_MIN_SCORE = 0.08
DEFAULT_GENERAL_TOPICS = 0.4            # a general topic is worth this fraction of its IDF
SERIES_NEXT, SERIES_OTHER = 1.0, 0.6
CURATED_VALUE = {"librarian_pick": 1.0, "related": 0.7}
TEXT_REASON_MIN = 0.05                  # below this the texts are not alike enough to say so


# ---------------------------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Shelf:
    id: str
    section: str = ""
    floor: Optional[int] = None
    zone: str = ""                       # "quiet" or "common"
    map_x: Optional[float] = None
    map_y: Optional[float] = None
    map_yaw: Optional[float] = None
    description: str = ""

    @property
    def has_pose(self) -> bool:
        return None not in (self.map_x, self.map_y, self.map_yaw)


@dataclass(frozen=True)
class Location:
    """Where a book lives. Some fields may be missing: `has_pose` says whether the robot can drive there."""
    isbn: str
    shelf: Optional[str] = None
    section: Optional[str] = None
    floor: Optional[int] = None
    zone: Optional[str] = None
    map_x: Optional[float] = None
    map_y: Optional[float] = None
    map_yaw: Optional[float] = None
    description: str = ""

    @property
    def has_pose(self) -> bool:
        return None not in (self.map_x, self.map_y, self.map_yaw)

    def describe(self) -> str:
        """Words for a visitor, e.g. "on shelf 3-SCI-A, in the Science section on level 3"; "" if nothing is known."""
        where = []
        if self.section:
            where.append(f"in the {self.section} section")
        if self.floor is not None:
            where.append(f"on level {self.floor}")
        place = " ".join(where)
        if self.shelf:
            return f"on shelf {self.shelf}" + (f", {place}" if place else "")
        return place


@dataclass(frozen=True)
class CuratedLink:
    from_isbn: str
    to_isbn: str
    relation: str
    note: str = ""


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    label: str


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str
    relation: str = ""
    note: str = ""


@dataclass(frozen=True)
class Component:
    name: str
    value: float                         # 0..1
    weight: float
    reason: str = ""                     # empty when value is 0

    @property
    def contribution(self) -> float:
        return self.value * self.weight


@dataclass(frozen=True)
class Score:
    a: str
    b: str
    total: float                         # 0..1
    components: tuple
    next_in_series: bool = False         # b follows a in a series

    @property
    def reasons(self) -> list:
        """The reasons that count, the biggest contribution first."""
        live = [c for c in self.components if c.value > 0 and c.reason]
        return [c.reason for c in sorted(live, key=lambda c: (-c.contribution, c.name))]

    def component(self, name: str) -> Component:
        return next(c for c in self.components if c.name == name)


@dataclass(frozen=True)
class Recommendation:
    book: Book
    score: Score
    because: str = ""                    # one natural reason for speech: "because, like Cosmos, it's about astronomy"

    @property
    def total(self) -> float:
        return self.score.total

    @property
    def components(self) -> tuple:
        return self.score.components

    @property
    def reasons(self) -> list:
        return self.score.reasons


# ---------------------------------------------------------------------------------------------- loading


def parse_shelves(rows: Iterable) -> tuple:
    """({shelf id: Shelf}, [(line number, problem)]) from the rows of shelves.csv."""
    shelves, problems = {}, []
    for line_no, row in rows:
        shelf_id = row.get("shelf", "")
        if not shelf_id:
            problems.append((line_no, "empty shelf id"))
            continue
        if shelf_id.lower() in {s.lower() for s in shelves}:
            problems.append((line_no, f"duplicate shelf {shelf_id!r}"))
            continue
        try:
            zone = row.get("zone", "").lower()
            if zone and zone not in ZONES:
                raise ValueError(f"zone must be quiet or common, got {row['zone']!r}")
            shelves[shelf_id] = Shelf(shelf_id, row.get("section", ""), whole_number(row, "floor"), zone,
                                      *(_number(row, c) for c in ("map_x", "map_y", "map_yaw")),
                                      row.get("description", ""))
        except ValueError as exc:
            problems.append((line_no, str(exc)))
    return shelves, problems


def parse_links(rows: Iterable) -> tuple:
    """([CuratedLink], [(line number, problem)]) from the rows of curated_links.csv."""
    links, problems = [], []
    for line_no, row in rows:
        a, b = to_isbn13(row.get("from_isbn", "")), to_isbn13(row.get("to_isbn", ""))
        if a is None or b is None:
            problems.append((line_no, f"bad ISBN {row.get('from_isbn') if a is None else row.get('to_isbn')!r}"))
        elif row.get("relation", "") not in RELATIONS:
            problems.append((line_no, f"relation must be one of {', '.join(RELATIONS)}, got {row.get('relation')!r}"))
        elif a == b:
            problems.append((line_no, "a book can't be linked to itself"))
        else:
            links.append(CuratedLink(a, b, row["relation"], row.get("note", "")))
    return links, problems


def load_shelves(path: Path | str) -> dict:
    path, _, rows = read_rows(path, SHELF_COLUMNS[:4])
    shelves, problems = parse_shelves(rows)
    if problems:
        raise CatalogError(f"{path}:{problems[0][0]}: {problems[0][1]}")
    return shelves


def load_links(path: Path | str) -> list:
    path, _, rows = read_rows(path, LINK_COLUMNS[:3])
    links, problems = parse_links(rows)
    if problems:
        raise CatalogError(f"{path}:{problems[0][0]}: {problems[0][1]}")
    return links


def _number(row: dict, column: str) -> Optional[float]:
    text = (row.get(column) or "").strip()
    try:
        return float(text) if text else None
    except ValueError:
        raise ValueError(f"{column} must be a number, got {text!r}") from None


def natural_join(items: list) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


# ---------------------------------------------------------------------------------------------- the graph


class BookGraph:
    def __init__(self, catalog: Catalog, vocabulary: Optional[TopicVocabulary] = None,
                 shelves: Optional[Mapping] = None, links: Iterable = (), weights: Optional[Mapping] = None,
                 embedder: Optional[Embedder] = None, min_score: Optional[float] = None):
        self._sources: Optional[tuple] = None
        self.weights = {name: float(DEFAULT_WEIGHTS[name]) for name in COMPONENTS}
        self.general_topics = DEFAULT_GENERAL_TOPICS
        for name, value in (weights or {}).items():
            if name == "min_score":
                min_score = value
            elif name == "general_topics":
                if not 0 <= float(value) <= 1:
                    raise ValueError("similarity weight general_topics must be between 0 and 1")
                self.general_topics = float(value)
            elif name not in DEFAULT_WEIGHTS:
                raise ValueError(f"unknown similarity weight {name!r}; use {', '.join(COMPONENTS)}, min_score "
                                 "or general_topics")
            elif float(value) < 0:
                raise ValueError(f"similarity weight {name} must not be negative")
            else:
                self.weights[name] = float(value)
        if sum(self.weights.values()) <= 0:
            raise ValueError("the similarity weights add up to zero")
        self.min_score = DEFAULT_MIN_SCORE if min_score is None else float(min_score)
        self.embedder = embedder if embedder is not None else TfidfEmbedder()
        self._build(catalog, vocabulary or TopicVocabulary(), dict(shelves or {}), list(links))

    @classmethod
    def from_files(cls, catalog_path, topics_path=None, shelves_path=None, links_path=None,
                   weights: Optional[Mapping] = None, embedder: Optional[Embedder] = None) -> "BookGraph":
        """Build from the data files. The topics, shelves and links files are optional: a missing one is the
        same as an empty one (with a log message). A file that exists but is broken raises CatalogError."""
        def optional(path, load, empty):
            if path is None or not Path(path).is_file():
                if path is not None:
                    log.info("%s not found; continuing without it", path)
                return empty
            return load(path)
        graph = cls(Catalog.load(catalog_path), optional(topics_path, TopicVocabulary.load, TopicVocabulary()),
                    optional(shelves_path, load_shelves, {}), optional(links_path, load_links, []),
                    weights=weights, embedder=embedder)
        graph._sources = (catalog_path, topics_path, shelves_path, links_path)
        return graph

    def reload(self) -> None:
        """Read the files again and rebuild. If a file is now broken, CatalogError is raised and the graph
        keeps what it had. Only possible for a graph made by from_files()."""
        if self._sources is None:
            raise RuntimeError("this graph was not built from files, so there is nothing to reload")
        fresh = type(self).from_files(*self._sources, embedder=self.embedder, weights={
            **self.weights, "min_score": self.min_score, "general_topics": self.general_topics})
        self.__dict__.update(fresh.__dict__)

    # ---- building ----
    def _build(self, catalog: Catalog, vocabulary: TopicVocabulary, shelves: dict, links: list) -> None:
        self.catalog, self.vocabulary, self.shelves = catalog, vocabulary, shelves
        self._shelf_by_key = {s.lower(): shelf for s, shelf in shelves.items()}
        books = catalog.books
        self._topics, self._unknown, self._ext = {}, {}, {}
        for book in books:
            topics, unknown = vocabulary.normalise([book.genre, *book.subjects])
            self._topics[book.isbn13], self._unknown[book.isbn13] = topics, unknown
            self._ext[book.isbn13] = frozenset(topics) | {p for t in topics for p in vocabulary.ancestors(t)}
        self._n = len(books)
        self._df = _document_frequency(self._topics.values())
        self._df_ext = _document_frequency(self._ext.values())
        self._authors = {b.isbn13: tuple(_authors_of(b)) for b in books}
        self._calls = {b.isbn13: parse_call_number(b.call_number) for b in books}
        self._series: dict[str, list] = {}
        for book in books:
            if book.series:
                self._series.setdefault(norm_key(book.series), []).append(book)
        self.links = []
        self._links: dict[frozenset, list] = {}
        for link in links:
            if link.from_isbn in catalog._by_isbn and link.to_isbn in catalog._by_isbn:
                self.links.append(link)
                self._links.setdefault(frozenset((link.from_isbn, link.to_isbn)), []).append(link)
            else:
                log.warning("curated link %s -> %s names a book that is not in the catalogue; ignored",
                            link.from_isbn, link.to_isbn)
        self.embedder.fit({b.isbn13: " ".join([b.summary or "", b.genre, *b.subjects]) for b in books})
        self._build_nodes()

    def _build_nodes(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._adjacent: dict[str, list] = {}

        def node(node_id: str, kind: str, label: str) -> str:
            self.nodes.setdefault(node_id, Node(node_id, kind, label))
            return node_id

        def edge(src: str, dst: str, kind: str, relation: str = "", note: str = "") -> None:
            e = Edge(src, dst, kind, relation, note)
            self.edges.append(e)
            self._adjacent.setdefault(src, []).append(e)
            self._adjacent.setdefault(dst, []).append(e)

        used_topics = set()
        for book in self.catalog.books:
            b = node(f"book:{book.isbn13}", "book", book.title)
            for key, name in self._authors[book.isbn13]:
                edge(b, node(f"author:{key}", "author", name), "written_by")
            for topic in self._topics[book.isbn13]:
                used_topics.add(topic)
                edge(b, node(f"topic:{topic}", "topic", topic), "about")
            call = self._calls[book.isbn13]
            if call:
                parent = None
                for level in call.levels:
                    cid = node(f"class:{call.system}:{level}", "class", f"{_SYSTEMS[call.system]} {level}")
                    if parent and not any(e.kind == "broader" and e.src == cid for e in self._adjacent.get(cid, [])):
                        edge(cid, parent, "broader")
                    parent = cid
                edge(b, parent, "in_class")
            location = self.locate(book.isbn13)
            if book.shelf:
                shelf = node(f"shelf:{book.shelf.lower()}", "shelf", self._shelf_by_key.get(book.shelf.lower(), Shelf(book.shelf)).id)
                edge(b, shelf, "on_shelf")
            section = (location.section if location else "") or book.section
            if section:
                sec = node(f"section:{norm_key(section)}", "section", section)
                edge(b, sec, "in_section")
            if book.series:
                note = f"book {book.series_index}" if book.series_index is not None else ""
                edge(b, node(f"series:{norm_key(book.series)}", "series", book.series), "in_series", note=note)
        for shelf in self.shelves.values():
            if f"shelf:{shelf.id.lower()}" in self.nodes and shelf.section:
                sec = node(f"section:{norm_key(shelf.section)}", "section", shelf.section)
                edge(f"shelf:{shelf.id.lower()}", sec, "in_section")
        for topic in sorted(used_topics | {p for t in used_topics for p in self.vocabulary.ancestors(t)}):
            kind = "topic" if topic in used_topics else "broader"
            self.nodes[f"topic:{topic}"] = Node(f"topic:{topic}", kind, topic)
            for parent in self.vocabulary.broader.get(topic, ()):
                edge(f"topic:{topic}", node(f"topic:{parent}", "broader", parent), "broader")
        for link in self.links:
            edge(f"book:{link.from_isbn}", f"book:{link.to_isbn}", "curated", link.relation, link.note)

    # ---- looking things up ----
    def neighbours(self, node_id: str) -> list:
        """Every edge touching a node, in either direction."""
        return list(self._adjacent.get(node_id, ()))

    def topics_of(self, isbn: str) -> tuple:
        """The book's topics: its genre and subjects folded through topics.yaml (unknown ones kept as typed)."""
        return self._topics.get(isbn, ())

    def unknown_topics(self) -> dict:
        """{subject the vocabulary doesn't know: [ISBNs that use it]}, for tools/catalog_check.py."""
        unknown: dict[str, list] = {}
        for isbn, subjects in self._unknown.items():
            for subject in subjects:
                unknown.setdefault(subject, []).append(isbn)
        return unknown

    def topic_counts(self) -> dict:
        """{topic: how many books are about it}."""
        return dict(self._df)

    def call_number(self, isbn: str) -> CallNumber:
        return self._calls.get(isbn, CallNumber())

    def idf(self, topic: str) -> float:
        """How much a shared topic is worth: more the fewer books have it (always above 0)."""
        return math.log((self._n + 1) / (self._df.get(topic, 0) + 1)) + 1

    def topic_weight(self, topic: str) -> float:
        """What sharing a topic is worth: its IDF, reduced for a general topic (one with a narrower child)."""
        weight = self.idf(topic)
        return weight * self.general_topics if self.vocabulary.is_general(topic) else weight

    def _idf_ext(self, topic: str) -> float:
        return math.log((self._n + 1) / (self._df_ext.get(topic, 0) + 1)) + 1

    def locate(self, isbn: str) -> Optional[Location]:
        return self.locate_with_reason(isbn)[0]

    def locate_with_reason(self, isbn: str) -> tuple:
        """(Location or None, why not or why incomplete or None). A shelf that isn't in shelves.csv still gives
        the shelf name and whatever section and floor the catalogue has, but no map pose."""
        book = self.catalog.lookup(isbn)
        if book is None:
            return None, "the book is not in the catalogue"
        shelf = self._shelf_by_key.get(book.shelf.lower()) if book.shelf else None
        if shelf is not None:
            location = Location(book.isbn13, shelf.id, shelf.section or book.section or None,
                                shelf.floor if shelf.floor is not None else book.floor, shelf.zone or None,
                                shelf.map_x, shelf.map_y, shelf.map_yaw, shelf.description)
            return location, None if shelf.has_pose else f"shelf {shelf.id} has no map position yet"
        if book.shelf:
            return (Location(book.isbn13, book.shelf, book.section or None, book.floor),
                    f"shelf {book.shelf!r} is not in shelves.csv, so there is no map position")
        if book.section or book.floor is not None:
            return (Location(book.isbn13, None, book.section or None, book.floor),
                    "the catalogue gives no shelf for this book, so there is no map position")
        return None, "the catalogue gives no shelf, section or floor for this book"

    # ---- scoring ----
    def _linked(self, a: str, b: str) -> list:
        return self._links.get(frozenset((a, b)), [])

    def score(self, a: str, b: str) -> Score:
        """How alike two catalogue books are: a 0..1 total and the components behind it, each with its reason."""
        if a not in self._topics or b not in self._topics:
            raise KeyError(f"not in the catalogue: {a if a not in self._topics else b}")
        w = self.weights
        components = []
        for name in COMPONENTS:
            value, reason = getattr(self, f"_c_{name}")(a, b)
            components.append(Component(name, value, w[name], reason if value > 0 else ""))
        total = sum(c.contribution for c in components) / sum(w.values())
        return Score(a, b, min(1.0, total), tuple(components), next_in_series=self._is_next(a, b))

    def recommend(self, isbn: str, n: int = 3) -> list:
        """Up to n other books for someone who has this one, best first, each with its score, components and
        reasons. The next book in a series comes first; the rest are ranked by total, ties by title then ISBN.
        The book itself and anything below books.similarity.min_score are left out. [] for an unknown book."""
        book = self.catalog.lookup(isbn)
        if book is None or n <= 0:
            return []
        found = []
        for other in self.catalog.books:
            if other.isbn13 != book.isbn13:
                score = self.score(book.isbn13, other.isbn13)
                if score.total >= self.min_score:
                    found.append(Recommendation(other, score, self.spoken_reason(book.isbn13, other.isbn13, score)))
        found.sort(key=lambda r: (not r.score.next_in_series, -round(r.total, 9), r.book.title.lower(), r.book.isbn13))
        return found[:n]

    def spoken_reason(self, a: str, b: str, score: Optional[Score] = None) -> str:
        """ONE natural reason for recommending b to someone holding a, starting "because": the most specific
        thing they share, in the order series, same author, curated link, most specific shared topic, shelf
        area, general topic, broader topic, then a generic line. Never several topics."""
        book_a, book_b = self.catalog.lookup(a), self.catalog.lookup(b)
        score = score or self.score(a, b)
        title = book_a.title
        series = score.component("series")
        same_series = bool(book_a.series and norm_key(book_a.series) == norm_key(book_b.series))
        name = book_b.series if (score.next_in_series and not same_series) else book_a.series
        name = name or book_b.series
        if score.next_in_series:
            return f"because it's the next book in {name}" if name else "because it's the next book in the series"
        if series.value > 0:
            earlier = series.reason.startswith("an earlier")
            where = name or "the series"
            return f"because it comes earlier in {where}" if earlier else f"because it's also part of {where}"
        author = score.component("author")
        if author.value > 0:
            return "because it's also by " + author.reason.removeprefix("same author: ")
        curated = score.component("curated")
        if curated.value > 0:
            # a librarian_pick is directional: "after Cosmos, read Hawking". Read the other way it is only a pairing.
            forward = any(l.relation == "librarian_pick" and (l.from_isbn, l.to_isbn) == (a, b) for l in self._linked(a, b))
            return (f"because the librarians suggest it after {title}" if forward
                    else f"because the librarians link it with {title}")
        shared = set(self._topics[a]) & set(self._topics[b])
        specific = [t for t in shared if not self.vocabulary.is_general(t)]
        if specific:                                                # the most specific: deepest, then rarest
            topic = min(specific, key=lambda t: (-self.vocabulary.depth(t), -self.idf(t), t))
            return f"because, like {title}, it's about {topic}"
        area = score.component("call_number")
        if area.reason:
            return f"because, like {title}, it's shelved with {area.reason.split(' ', 3)[3]}"
        if shared:
            return f"because, like {title}, it's about {min(shared, key=lambda t: (-self.idf(t), t))}"
        broader = score.component("broader")
        if broader.reason:
            return f"because, like {title}, it falls under {broader.reason.removeprefix('both fall under ')}"
        return f"because it's similar to {title}"

    def _is_next(self, a: str, b: str) -> bool:
        """b follows a: the next series_index of the same series, or a series_next row from a to b."""
        if any(l.relation == "series_next" and (l.from_isbn, l.to_isbn) == (a, b) for l in self._linked(a, b)):
            return True
        book_a, book_b = self.catalog.lookup(a), self.catalog.lookup(b)
        return bool(book_a.series and norm_key(book_a.series) == norm_key(book_b.series)
                    and None not in (book_a.series_index, book_b.series_index)
                    and book_b.series_index == book_a.series_index + 1)

    def _c_topics(self, a: str, b: str) -> tuple:
        A, B = set(self._topics[a]), set(self._topics[b])
        shared = A & B
        if not shared:
            return 0.0, ""
        value = sum(self.topic_weight(t) for t in shared) / sum(self.topic_weight(t) for t in A | B)
        best = sorted(shared, key=lambda t: (-self.topic_weight(t), t))[:3]      # specific and rare first
        return value, "both about " + natural_join(best)

    def _c_broader(self, a: str, b: str) -> tuple:
        direct = set(self._topics[a]) & set(self._topics[b])                 # already credited as shared topics
        ea, eb = self._ext[a] - direct, self._ext[b] - direct
        matches = ea & eb
        if not matches:
            return 0.0, ""
        value = sum(self._idf_ext(t) for t in matches) / sum(self._idf_ext(t) for t in ea | eb)
        names = sorted(matches, key=lambda t: (-self._idf_ext(t), t))[:2]
        return value, "both fall under " + natural_join(names)

    def _c_author(self, a: str, b: str) -> tuple:
        mine = {key for key, _ in self._authors[a]}
        shared = [name for key, name in self._authors[b] if key in mine]
        return (1.0, "same author: " + natural_join(shared)) if shared else (0.0, "")

    def _c_series(self, a: str, b: str) -> tuple:
        book_a, book_b = self.catalog.lookup(a), self.catalog.lookup(b)
        same = bool(book_a.series and norm_key(book_a.series) == norm_key(book_b.series))
        name = book_a.series if same else (book_b.series or book_a.series)
        if self._is_next(a, b):
            return SERIES_NEXT, f"next in the series {name}" if name else "the next book in the series"
        earlier = (same and None not in (book_a.series_index, book_b.series_index)
                   and book_b.series_index < book_a.series_index)
        if any(l.relation == "series_next" and (l.from_isbn, l.to_isbn) == (b, a) for l in self._linked(a, b)):
            earlier = True
        if not (same or earlier):
            return 0.0, ""
        if not name:
            return SERIES_OTHER, "an earlier book in the series"
        return SERIES_OTHER, f"an earlier book in the series {name}" if earlier else f"same series: {name}"

    def _c_call_number(self, a: str, b: str) -> tuple:
        ca, cb = self._calls[a], self._calls[b]
        value = closeness(ca, cb)
        if value < CLOSE_BY:                                        # a little credit, but not "close by": say nothing
            return value, ""
        if ca.system == "other":                                    # "F HER", "F ADA": only the prefix is shared
            return value, f"both shelved with {shared_area(ca, cb)}"
        return value, f"both shelved in {shared_area(ca, cb)}"      # the area, never the call number

    def _c_curated(self, a: str, b: str) -> tuple:
        best = (0.0, "")
        for link in self._linked(a, b):
            value = CURATED_VALUE.get(link.relation, 0.0)          # series_next rows feed the series component
            if value > best[0]:
                label = "librarian's pick" if link.relation == "librarian_pick" else "linked by the librarians"
                best = (value, label + (f": {link.note}" if link.note else ""))
        return best

    def _c_text(self, a: str, b: str) -> tuple:
        value = self.embedder.similarity(a, b)
        if value <= 0:
            return 0.0, ""
        if value < TEXT_REASON_MIN:
            return value, ""
        terms = self.embedder.shared_terms(a, b, 3)
        return value, "similar descriptions" + (f" (both mention {natural_join(terms)})" if terms else "")


def _document_frequency(sets: Iterable) -> dict:
    counts: dict[str, int] = {}
    for items in sets:
        for item in items:
            counts[item] = counts.get(item, 0) + 1
    return counts


def _authors_of(book: Book):
    for name in book.author.split(";"):
        name = name.strip()
        if name:
            yield norm_key(name), name


_SYSTEMS = {"dewey": "Dewey", "lc": "LC", "other": "Shelf code"}
