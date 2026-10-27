"""Call numbers as a hierarchy, so "how close are these two shelves?" has an answer.

    parse_call_number("520.1 SAG").levels      # ("500", "520", "520.1")            Dewey Decimal
    parse_call_number("QB981 .S24").levels     # ("Q", "QB", "QB981")                Library of Congress
    parse_call_number("F HER").levels          # ("F",)                              fiction-style: prefix only
    closeness(a, b)                            # 0..1: same class, and how far apart the numbers are
    shared_area(a, b)                          # "astronomy": what part of the library two numbers share

Dewey numbers are cut into hundreds, tens, units and then each decimal digit (500 -> 520 -> 523 -> 523.8;
a level that repeats, as the tens of 520, is dropped). LC numbers are cut into the class letter, the
subclass letters, the number and its decimal (Q -> QB -> QB981 -> QB981.5); the cutter (".S24") is ignored.
Codes that are neither, such as "F HER" or "FIC-FIT" on a fiction shelf, only share their prefix, and they
are only loosely close. Anything unreadable gives an empty CallNumber instead of an error.

Closeness has two parts. Sharing a class earns a little (the same Dewey hundred, the same LC class letter,
then the same Dewey ten or LC subclass). Inside one numbering scope (a Dewey hundred, an LC subclass) the
rest comes from how far apart the numbers are, fading fast: QB981 and QB982 are next to each other, QB44.2
and QB981 are in the same subclass but nowhere near each other, so they are not "close by".

shared_area() names the part of the library two numbers share ("astronomy", "science", "fiction") from small
tables of Dewey hundreds and tens and LC classes and subclasses, so a reason can say "both shelved in
astronomy" instead of quoting a call number. Numbers the tables don't cover fall back to a general phrase.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

DEWEY = re.compile(r"^(\d{3})(?:\s*\.\s*(\d+))?(?!\d)")
LC = re.compile(r"^([A-Z]{1,3})\s*(\d{1,4})(?:\s*\.\s*(\d+))?")
PREFIX = re.compile(r"^([A-Z]{1,5})(?=$|[\s\-./])")

PREFIX_ONLY_CLOSENESS = 0.3     # "F HER" and "F ADA" share the fiction prefix and nothing more
MAIN_CREDIT = 0.1               # the same Dewey hundred / LC class letter
DIVISION_CREDIT = 0.1           # ...and the same Dewey ten / LC subclass
NUMBER_CREDIT = 0.8             # the rest comes from how near the numbers are in their scope
NEAR = 25.0                     # numbers this far apart are about a third as close (the decay is exp(-distance / NEAR))
CLOSE_BY = 0.5                  # closeness from here counts as "shelved close by" in a reason
GENERIC_AREA = "the same part of the library"


@dataclass(frozen=True)
class CallNumber:
    raw: str = ""
    system: str = ""                    # "dewey", "lc", "other" (prefix only) or "" (empty or unreadable)
    levels: tuple = ()                  # broadest first
    main: str = ""                      # Dewey hundred ("500"), LC class letter ("Q") or the shelf-code prefix ("F")
    division: str = ""                  # Dewey ten ("520") or LC subclass ("QB"); "" for shelf codes
    number: Optional[float] = None      # the class number as a number: 523.8, 981.0; None for shelf codes

    def __bool__(self) -> bool:
        return bool(self.levels)

    @property
    def deepest(self) -> str:
        return self.levels[-1] if self.levels else ""


def _dewey_levels(whole: str, decimals: str | None) -> tuple:
    levels = [whole[0] + "00", whole[:2] + "0", whole]
    levels += [f"{whole}.{decimals[:k]}" for k in range(1, len(decimals or "") + 1)]
    return tuple(level for i, level in enumerate(levels) if i == 0 or level != levels[i - 1])


def parse_call_number(text) -> CallNumber:
    """The CallNumber for a shelf mark. Never raises: nonsense gives an empty CallNumber."""
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return CallNumber()
    upper = raw.upper()
    if match := DEWEY.match(upper):
        whole, decimals = match.group(1), match.group(2)
        return CallNumber(raw, "dewey", _dewey_levels(whole, decimals), whole[0] + "00", whole[:2] + "0",
                          float(f"{whole}.{decimals or '0'}"))
    if match := LC.match(upper):
        letters, number, decimal = match.groups()
        levels = [letters[0], letters, letters + number]
        if decimal:
            levels.append(f"{letters}{number}.{decimal}")
        return CallNumber(raw, "lc", tuple(level for i, level in enumerate(levels) if i == 0 or level != levels[i - 1]),
                          letters[0], letters, float(f"{number}.{decimal or '0'}"))
    if match := PREFIX.match(upper):
        return CallNumber(raw, "other", (match.group(1),), match.group(1))
    return CallNumber(raw)


def closeness(a: CallNumber, b: CallNumber) -> float:
    """0 (nothing in common) to 1 (the same place) for two call numbers of the same kind.

    The same class earns a little; inside one numbering scope (a Dewey hundred, an LC subclass) the numbers'
    distance earns the rest, fading quickly: QB981 and QB982 are ~0.97, QB44.2 and QB981 only 0.2.
    """
    if not a or not b or a.system != b.system:
        return 0.0
    if a.system == "other":
        return PREFIX_ONLY_CLOSENESS if a.main == b.main else 0.0
    if a.levels == b.levels:
        return 1.0
    if a.main != b.main:
        return 0.0
    score = MAIN_CREDIT
    same_division = a.division == b.division
    if same_division:
        score += DIVISION_CREDIT
    if a.system == "dewey" or same_division:                  # a Dewey hundred or an LC subclass is one numbering
        score += NUMBER_CREDIT * math.exp(-abs(a.number - b.number) / NEAR)
    return min(1.0, score)


# ---- naming the part of the library: small tables, deliberately incomplete
DEWEY_HUNDREDS = {"000": "general works and computing", "100": "philosophy and psychology", "200": "religion",
                  "300": "the social sciences", "400": "language", "500": "science", "600": "technology",
                  "700": "the arts", "800": "literature", "900": "history and geography"}
DEWEY_TENS = {"000": "computing and general works", "020": "library science", "070": "journalism and publishing",
              "100": "philosophy", "150": "psychology", "170": "ethics", "320": "political science", "330": "economics",
              "340": "law", "370": "education", "390": "customs and folklore", "410": "linguistics", "500": "science",
              "510": "mathematics", "520": "astronomy", "530": "physics", "540": "chemistry", "550": "earth sciences",
              "560": "fossils", "570": "biology", "580": "plants", "590": "animals", "610": "medicine",
              "620": "engineering", "630": "agriculture", "640": "home and family", "650": "business",
              "690": "building", "710": "landscape design", "720": "architecture", "730": "sculpture",
              "740": "drawing and design", "750": "painting", "770": "photography", "780": "music",
              "790": "sports and entertainment", "810": "American literature", "820": "English literature",
              "830": "German literature", "840": "French literature", "900": "history", "910": "geography and travel",
              "920": "biography", "930": "ancient history", "940": "European history", "950": "Asian history",
              "960": "African history", "970": "North American history", "980": "South American history"}
LC_CLASSES = {"A": "general works", "B": "philosophy, psychology and religion", "C": "history and archaeology",
              "D": "world history", "E": "American history", "F": "American local history",
              "G": "geography and anthropology", "H": "the social sciences", "J": "political science", "K": "law",
              "L": "education", "M": "music", "N": "the fine arts", "P": "language and literature", "Q": "science",
              "R": "medicine", "S": "agriculture", "T": "technology", "U": "military science",
              "V": "naval science", "Z": "library science"}
LC_SUBCLASSES = {"QA": "mathematics and computing", "QB": "astronomy", "QC": "physics", "QD": "chemistry",
                 "QE": "geology", "QH": "biology", "QK": "botany", "QL": "zoology", "QM": "anatomy",
                 "QP": "physiology", "QR": "microbiology", "BD": "philosophy", "BF": "psychology", "BL": "religion",
                 "DA": "British history", "DS": "Asian history", "GN": "anthropology", "GV": "sports and recreation",
                 "HB": "economics", "HC": "economic history", "HD": "industry and labour", "HF": "commerce",
                 "HM": "sociology", "HQ": "family and gender studies", "JC": "political theory",
                 "ML": "music literature", "PN": "literature", "PQ": "Romance literature", "PR": "English literature",
                 "PS": "American literature", "PZ": "fiction", "TA": "engineering", "TK": "electronics and computing",
                 "TL": "vehicles and aviation", "TX": "home economics"}
SHELF_CODES = {"F": "fiction", "FIC": "fiction", "SF": "science fiction", "MYS": "mystery", "MYST": "mystery",
               "BIO": "biography", "J": "children's books", "JUV": "children's books", "YA": "young adult books",
               "REF": "reference books"}


def shared_area(a: CallNumber, b: CallNumber) -> str:
    """The part of the library two call numbers share, in words: "astronomy" for QB44.2 and QB981 (or 520 and
    523), "science" for 520 and 590, "fiction" for "F HER" and "F ADA". GENERIC_AREA if the tables don't say,
    and for numbers with nothing in common."""
    if not a or not b or a.system != b.system or a.main != b.main:
        return GENERIC_AREA
    if a.system == "other":
        return SHELF_CODES.get(a.main, GENERIC_AREA)
    if a.system == "dewey":
        return (DEWEY_TENS.get(a.division) if a.division == b.division else None) or DEWEY_HUNDREDS.get(a.main, GENERIC_AREA)
    return (LC_SUBCLASSES.get(a.division) if a.division == b.division else None) or LC_CLASSES.get(a.main, GENERIC_AREA)
