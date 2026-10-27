"""What place a visitor means: a book, a section or a floor ("where is Dune", "take me to history", "where are the
science books", "what's on level 2"), from the book graph and the floor plan.

    resolver = PlaceResolver(finder, plan)
    place = resolver.resolve("where are the science books")   # Place(kind="section", section="Science", ...)

Tried in this order: a floor ("level 2", "the ground floor"); a title said in full; a section name (the longest one
the words name, so "science fiction" is not "science"); a topic or one of its aliases from topics.yaml (the section
holding most of its books); a loosely typed title. When the words fit several things equally well the Place has
kind "ambiguous" and the candidates in `choices`, so the visitor can be asked which one.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

from robot.services.books import title_key
from robot.services.text_similarity import stem, words_of

FLOOR_WORDS = {"ground": 1, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
FILLER = r"\b(?:where|is|are|the|a|an|can|could|i|you|me|us|find|show|take|guide|lead|walk|bring|to|go|get|tell|" \
         r"please|book|books|novel|novels|located|in|this|library|on|of|what|whats|what's|which|shelf|section|" \
         r"area|it|there|do|does|keep|have|any|some|for|looking|no|yes|ok|okay|then|so|um|hmm|well|explain|about|" \
         r"summary|describe|recommend|similar|like|something|anything|more)\b"
# What a visitor may ask for besides books: landmark kind -> (how to say it, its verb, the ways people name it). The kinds
# are those of data/floorplan.yaml; the answer comes from the plan (the nearest one to the robot, and the others).
FACILITIES = {
    "toilets": ("the toilets", "are", ["toilet", "toilets", "restroom", "restrooms", "rest room", "bathroom", "bathrooms",
                                       "washroom", "washrooms", "wash room", "loo", "lavatory", "lavatories", "wc"]),
    "help_desk": ("the help desk", "is", ["help desk", "helpdesk", "information desk", "info desk", "service desk",
                                          "enquiry desk", "enquiries", "front desk", "reception", "circulation desk"]),
    "exit": ("the exit", "is", ["exit", "way out", "get out", "emergency exit", "fire exit"]),
    "entrance": ("the entrance", "is", ["entrance", "main door", "way in"]),
}
NEEDS_CUE = ("exit", "entrance")       # a bare "exit" may mean "I am leaving": only with "where" or "the"
FUZZY_MIN = 0.75           # how alike a loosely typed title must be
TIE = 0.05                 # candidates closer than this are ambiguous


@dataclass
class Place:
    kind: str                          # book | section | floor | facility | ambiguous
    title: str                         # what to call it ("Dune", "the History section", "Level 2")
    isbn: str = ""
    section: str = ""
    floor: Optional[int] = None
    shelf: str = ""
    topic: str = ""                    # the topic the section was found for ("astronomy")
    landmark: str = ""                 # for a facility: the kind in the floor plan (toilets, help_desk, exit, entrance)
    requested: bool = False            # for a facility: the visitor named the floor ("the toilets on level 3"): `floor` is it
    choices: list = field(default_factory=list)       # Places, for an ambiguous one

    @property
    def choice_id(self) -> str:
        """The UserChoice id that picks this place (see Dialogue.on_choice)."""
        return {"book": f"where:{self.isbn}", "section": f"section:{self.section}",
                "floor": f"floor:{self.floor}"}.get(self.kind, "")


class PlaceResolver:
    def __init__(self, finder, plan=None):
        self.finder, self.plan = finder, plan

    @property
    def graph(self):
        return self.finder.graph if self.finder is not None else None

    def floors(self) -> set:
        found = set(self.plan.floors) if self.plan is not None else set()
        if self.graph is not None:
            found |= {s.floor for s in self.graph.shelves.values() if s.floor is not None}
        return found

    def sections(self) -> dict:
        """section name -> (floor, [shelves]) from shelves.csv."""
        out: dict = {}
        for shelf in (self.graph.shelves.values() if self.graph is not None else []):
            if shelf.section:
                out.setdefault(shelf.section, (shelf.floor, []))[1].append(shelf)
        return out

    # ---- the steps ----
    def resolve(self, text: str) -> Optional[Place]:
        text = text.lower()
        for step in (self.title_in, self.facility_in, self.floor_in, self.section_in, self.topic_in, self.fuzzy_title):
            place = step(text)
            if place is None:
                continue
            if place.kind == "section" and not place.topic:          # "the brief history": a section, or a title?
                book = self.fuzzy_title(text)
                extra = set(self.query_words(text)) - {stem(w) for w in words_of(place.section)}
                if book is not None and book.kind == "book" and extra:
                    return self.ambiguous([book, place])
            return place
        return None

    @staticmethod
    def query_words(text: str) -> list:
        return [stem(w) for w in words_of(re.sub(FILLER, " ", text))]

    def floor_in(self, text: str) -> Optional[Place]:
        match = (re.search(r"\b(?:level|floor)\s*(\d+)\b", text) or re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:floor|level)\b", text))
        level = int(match.group(1)) if match else None
        if level is None and (word := re.search(r"\b(ground|first|second|third|fourth|fifth)\s+(?:floor|level)\b", text)):
            level = FLOOR_WORDS[word.group(1)]
        if level is None or level not in self.floors():
            return None
        return Place("floor", f"Level {level}", floor=level)

    def facility_in(self, text: str) -> Optional[Place]:
        """The toilets, the help desk, the exit or the entrance, when the text names one (spelt as in FACILITIES)."""
        for kind, (_, _, names) in FACILITIES.items():
            said = next((n for n in names if re.search(rf"\b{re.escape(n)}\b", text)), None)
            if said:
                if kind in NEEDS_CUE and " " not in said and not re.search(r"\bwhere\b|\bthe\b|\bwhich\b|\bhow do i\b", text):
                    continue
                named = self.floor_in(text)                 # "the toilets on level 3"
                return self.facility_place(kind, named.floor if named else None)
        return None

    def facility_place(self, kind: str, floor: Optional[int] = None) -> Place:
        """The facility on the floor the visitor named, else the nearest one to the robot (its floor first), or a Place
        with no floor when the plan has none."""
        name = FACILITIES[kind][0]
        found = self.plan.find(kind) if self.plan is not None else []
        if floor is not None:
            return Place("facility", name, floor=floor, landmark=kind, requested=True)
        if not found:
            return Place("facility", name, landmark=kind)
        station = self.plan.station
        here = station.level if station else None
        found.sort(key=lambda fm: (fm[0].level != here, abs(fm[0].level - (here or fm[0].level)), fm[0].level))
        floor, mark = found[0]
        return Place("facility", name, floor=floor.level, landmark=kind)

    def book_place(self, book) -> Place:
        place = self.finder.locate(book.isbn13)
        return Place("book", book.title, isbn=book.isbn13, section=(place.section if place else "") or "",
                     floor=place.floor if place else None, shelf=(place.shelf if place else "") or "")

    def title_in(self, text: str) -> Optional[Place]:
        named = self.finder.titles_in(text) if self.finder is not None else []
        if not named:
            return None
        longest = [b for b in named if len(title_key(b.title)) == len(title_key(named[0].title))]
        if len(longest) > 1:
            return self.ambiguous([self.book_place(b) for b in longest])
        return self.book_place(named[0])

    def section_place(self, name: str, topic: str = "") -> Place:
        floor, shelves = self.sections().get(name, (None, []))
        if floor is None and self.plan is not None and (found := self.plan.where_section(name)):
            floor = found[0].level
        return Place("section", f"the {name} section", section=name, floor=floor,
                     shelf=sorted(s.id for s in shelves)[0] if shelves else "", topic=topic)

    def section_in(self, text: str) -> Optional[Place]:
        stems = {stem(w) for w in words_of(text)}
        names = set(self.sections())
        if self.plan is not None:
            names |= {s.name for f in self.plan.floors.values() for s in f.sections}
        matched = [n for n in names if {stem(w) for w in words_of(n)} <= stems]
        if not matched:
            return None
        size = max(len(words_of(n)) for n in matched)
        best = sorted(n for n in matched if len(words_of(n)) == size)
        if len(best) > 1:
            return self.ambiguous([self.section_place(n) for n in best])
        return self.section_place(best[0])

    def topic_in(self, text: str) -> Optional[Place]:
        if self.graph is None:
            return None
        wanted, scores = self.graph.topic_shelf_scores(text)
        if not scores:
            return None
        by_section: dict = {}
        for shelf_id, score in scores.items():
            section = self.graph.shelves[shelf_id].section
            if section:
                by_section[section] = by_section.get(section, 0) + score
        if not by_section:
            return None
        ranked = sorted(by_section.items(), key=lambda kv: (-kv[1], kv[0]))
        top = [name for name, score in ranked if score == ranked[0][1]]
        if len(top) > 1:
            return self.ambiguous([self.section_place(n, wanted[0]) for n in top])
        return self.section_place(top[0], wanted[0])

    def fuzzy_title(self, text: str) -> Optional[Place]:
        if self.finder is None:
            return None
        query = " ".join(re.sub(FILLER, " ", re.sub(r"[^a-z0-9' ]+", " ", text)).split())
        query = title_key(query)
        if len(query) < 3:
            return None
        scored = []
        for book in self.finder.catalog.books:
            key = title_key(book.title)
            ratio = difflib.SequenceMatcher(None, query, key).ratio()
            if len(query) >= 4 and re.search(rf"\b{re.escape(query)}\b", key):
                ratio = max(ratio, 0.9)                 # a word or two of the title, said exactly
            if ratio >= FUZZY_MIN:
                scored.append((ratio, book))
        if not scored:
            return None
        scored.sort(key=lambda s: (-s[0], s[1].title))
        close = [b for r, b in scored if scored[0][0] - r < TIE]
        if len(close) > 1:
            return self.ambiguous([self.book_place(b) for b in close])
        return self.book_place(scored[0][1])

    @staticmethod
    def ambiguous(places: list) -> Place:
        return Place("ambiguous", "", choices=places)
