"""The intent router: what does the visitor mean? Decided by rules before the LLM is ever asked.

    router = IntentRouter(finder, plan, settings)
    route = router.route("whre is the sicence books?", state="ANYTHING_ELSE")
    route.best          # Candidate(intent="where", confidence=0.93, place=Place(section Science), ...)
    route.ambiguous     # [Candidate, Candidate] when two places fit equally well ("history": the section or the book?)

Steps: normalise (lower case, contractions expanded, punctuation gone, a few speech-recognition slips repaired), then
match, word by word and fuzzily, against

    * a small phrase list per intent (done, yes, no, return, explain, recommend, guide, where),
    * the catalogue titles, the floor plan's sections, floors ("level 2") and the topic aliases of topics.yaml.

Word matching is fuzzy with a per-word similarity threshold (`intents.word_similarity`, stricter for short words:
`intents.short_word_similarity`; words of one or two letters must be exact), built on difflib plus an edit distance in which a
swap of two neighbouring letters is cheap, so "dnoe" is "done" but "anything" is never "nothing" (an explicit guard
too). Every candidate carries a confidence: the words' similarity times how much of what was said it explains.

The route lists candidates best first. One clear winner is acted on; two close place candidates are shown as options (the
dialogue asks "Did you mean...?"); nothing confident returns an empty route and the dialogue goes on to its other rules and,
if those do not know either, to the LLM. Nothing here talks to the network.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

from robot.services.books import title_key

CONTRACTIONS = {"i'm": "i am", "im": "i am", "that's": "that is", "thats": "that is", "it's": "it is", "what's": "what is",
                "whats": "what is", "where's": "where is", "wheres": "where is", "who's": "who is", "let's": "let us",
                "don't": "do not", "dont": "do not", "doesn't": "does not", "can't": "can not", "cant": "can not",
                "won't": "will not", "i'd": "i would", "i'll": "i will", "i've": "i have", "you're": "you are",
                "isn't": "is not", "aren't": "are not", "couldn't": "could not", "wouldn't": "would not",
                "gonna": "going to", "wanna": "want to", "gotta": "got to", "thx": "thanks", "pls": "please",
                "plz": "please", "ty": "thanks"}
HEARD_AS = {"wear": "where", "ware": "where", "were": "where", "know": "no", "dun": "done", "dune": "dune"}
HEARD_BEFORE = {"wear": ("is", "are", "can", "do", "does", "would"), "were": ("is", "are", "does", "do")}
STOP = frozenset("""i am the a an to is it me you can could would will please my we us our of on in at for with and or but so
that this these those there here be do does did have has had um uh er erm hmm like just maybe then well oh ah ok okay yes yeah
no not any some what who how which when why are was were your yours mine want need wish going get got let us oh see now
actually right so also really very much thing things""".split())
EXPLAINED_EXTRA = frozenset("book books novel novels stuff section sections area areas aisle floor floors level levels shelf "
                            "shelves story stories read reading something anything about".split())
NEVER_MATCH = {frozenset(p) for p in (("anything", "nothing"), ("another", "other"), ("another", "mother"), ("no", "now"),
                                      ("done", "dune"), ("dune", "done"), ("floor", "flour"), ("tell", "tall"),
                                      ("where", "there"), ("where", "here"), ("this", "that"))}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "ground": 1, "one": 1, "two": 2, "to": 2, "too": 2,
            "three": 3, "four": 4, "for": 4, "five": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}

PHRASES = {
    "done": ["done", "finished", "that is all", "that is everything", "that is it", "thanks", "thank you", "bye", "goodbye",
             "no thanks", "nothing else", "nothing more", "no more", "all set", "i am good", "good for now", "no questions",
             "not now", "that will do", "i am finished"],
    "return": ["return", "give back", "take back", "drop off", "hand in", "returning"],
    "explain": ["tell me about", "explain", "what is it about", "what is about", "summary", "summarise", "summarize", "describe",
                "synopsis", "plot", "storyline"],
    "recommend": ["recommend", "recommendation", "suggest", "similar", "something like", "anything like", "related",
                  "read next", "other books", "more books", "what else", "recommendations", "books like this", "like this one",
                  "more like this", "something similar"],
    "guide": ["guide me", "guide us", "take me there", "take me to it", "lead me", "walk me", "show me the way", "show me way", "show us the way", "lead the way",
              "follow you", "take us there"],
    "yes": ["yes", "yeah", "yep", "yup", "sure", "please", "okay", "go ahead", "why not"],
    "no": ["no", "nope", "nah"],
    "another": ["another book", "next book", "different book", "new book", "one more book", "scan another", "another one"],
}
WHERE_CUES = ["where", "find", "locate", "looking for", "look for", "show me", "take me to", "guide me to", "lead me to",
              "go to", "head to", "browse", "what is on", "which floor", "bring me to", "walk me to", "take us to"]
GUIDE_CUES = ["take me", "guide me", "lead me", "walk me", "bring me", "take us", "go to", "head to"]
INTEREST_PATTERNS = (
    r"\b(?:find|get|give|show)\s+me\s+(?:a\s+|some\s+)?(?:books?|novels?|something|anything)\s+(?:about|on|like|involving|with)\s+(?P<i>.+)$",
    r"\b(?:recommend|suggest|any|some|something|anything|got)\b.*?\b(?:about|on|like|involving|featuring|regarding|with)\s+(?P<i>.+)$",
    r"\b(?:a|an|some)\s+(?:good|great|nice|fun|interesting|favorite|favourite|exciting|best)\s+(?P<i>.+)$",
    r"\bi\s+(?:really\s+)?(?:like|love|enjoy|am into|am interested in|am keen on)\s+(?P<i>.+)$",
    r"\bbooks?\s+(?:about|on)\s+(?P<i>.+)$",
)
INTENT_WORDS = frozenset(w for group in (PHRASES.values(), [WHERE_CUES, GUIDE_CUES]) for ph in group for p in ph
                         for w in p.split()) - {"about", "like", "similar", "related", "find", "show", "take", "back"}
FILLER_WORDS = ("book", "books", "novel", "novels", "read", "reads", "story", "stories", "stuff", "please", "titles")

DEFAULTS = {"word_similarity": 0.8, "short_word_similarity": 0.85, "min_confidence": 0.7, "ambiguity_margin": 0.15,
            "repair_after": 2}


# ---------------------------------------------------------------------------------------------- words
def normalise(text: str) -> str:
    """lower case, contractions expanded, punctuation gone (hyphens become spaces), a few speech slips repaired."""
    text = (text or "").lower().replace("’", "'")
    words = []
    for raw in text.replace("-", " ").split():
        word = raw.strip(".,!?;:\"()[]")
        words.extend(CONTRACTIONS.get(word, re.sub(r"[^a-z0-9' ]", "", word)).split())
    words = [w.replace("'", "") for w in words if w]
    fixed = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in HEARD_BEFORE and nxt in HEARD_BEFORE[w]:
            w = HEARD_AS[w]
        fixed.append(w)
    return " ".join(fixed)


def edit_cost(a: str, b: str) -> float:
    """Edit distance where inserting, deleting or replacing a letter costs 1 and swapping two neighbours 0.5."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], prev2[j - 2] + 0.5)
        prev2, prev = prev, row
    return prev[-1]


def word_similarity(a: str, b: str) -> float:
    """0..1 how alike two words are. Words of one or two letters must be identical."""
    if a == b:
        return 1.0
    if frozenset((a, b)) in NEVER_MATCH or min(len(a), len(b)) <= 2 or a[0] != b[0] and len(a) <= 4:
        return 0.0
    size = max(len(a), len(b))
    cost = 1 - edit_cost(a, b) / size
    if a[0] == b[0]:                                                  # difflib is generous: only for the same first letter
        cost = max(cost, difflib.SequenceMatcher(None, a, b).ratio() if edit_cost(a, b) <= 2 else 0)
    return cost


def needed(word: str, settings: dict) -> float:
    return settings["short_word_similarity"] if len(word) <= 4 else settings["word_similarity"]


def similar(a: str, b: str, settings: dict) -> float:
    """The similarity if it clears the threshold for these words, else 0."""
    s = word_similarity(a, b)
    return s if s >= needed(b, settings) else 0.0


@dataclass
class Candidate:
    intent: str                              # done yes no return explain recommend guide where another title
    confidence: float
    place: Optional[object] = None           # services.places.Place for where
    isbn: str = ""                           # a catalogue book named (explain / recommend / where)
    interest: str = ""                       # for recommend: what they like
    take_me: bool = False                    # for where: they asked to be taken there
    label: str = ""                          # what it matched, for the log and the options

    def __repr__(self) -> str:
        return f"<{self.intent} {self.confidence:.2f} {self.label}>"


@dataclass
class Route:
    text: str
    norm: str
    candidates: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)       # close place candidates: ask which

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def content_words(self) -> list:
        return [w for w in self.norm.split() if w not in STOP]


PRIORITY = ("done", "return", "guide", "another", "where", "recommend", "explain", "yes", "no", "title")


class IntentRouter:
    def __init__(self, finder=None, plan=None, settings: Optional[dict] = None):
        from robot.services.places import PlaceResolver
        self.finder, self.plan = finder, plan
        self.settings = {**DEFAULTS, **(settings or {})}
        self.resolver = PlaceResolver(finder, plan)
        self.reload()

    def reload(self) -> None:
        """Rebuild the vocabularies from the catalogue, the floor plan and topics.yaml (after `reload` in the sim)."""
        books = self.finder.catalog.books if self.finder is not None else []
        self.titles = {b.isbn13: [w for w in normalise(title_key(b.title)).split() if w not in STOP] for b in books}
        names = {s.section for s in self.finder.graph.shelves.values() if s.section} if self.finder is not None else set()
        if self.plan is not None:
            names |= {s.name for f in self.plan.floors.values() for s in f.sections}
        self.sections = {n: normalise(n).split() for n in names}
        words = self.finder.graph.vocabulary.words if self.finder is not None else {}
        self.topics = {tuple(normalise(alias).split()): canonical for alias, canonical in words.items()}
        self._known = self._all = None

    # ---- phrase matching ----
    def match_phrase(self, words: list, phrase: list) -> Optional[tuple]:
        """(mean similarity, indexes of the words used) if the phrase's words all appear in order, else None."""
        used, sims, at = [], [], 0
        for p in phrase:
            found = next(((i, s) for i in range(at, len(words)) if (s := similar(words[i], p, self.settings))), None)
            if found is None:
                return None
            used.append(found[0])
            sims.append(found[1])
            at = found[0] + 1
        if len(used) > 1 and used[-1] - used[0] > len(phrase) + 1:
            return None                                              # the words are far apart: a coincidence
        return sum(sims) / len(sims), used

    def best_phrase(self, words: list, phrases: list, content: set, also: frozenset = frozenset()) -> tuple:
        """(confidence, matched word indexes) of the best phrase of an intent. `also`: indexes of words explained some
        other way (a title or a topic that was named)."""
        best = (0.0, [])
        for phrase in phrases:
            m = self.match_phrase(words, phrase.split())
            if m is None:
                continue
            sim, used = m
            coverage = len({words[i] for i in set(used) | set(also)} & content) / len(content) if content else 1.0
            conf = sim * (0.55 + 0.45 * min(1.0, coverage))
            if conf > best[0]:
                best = (conf, used)
        return best

    # ---- entities ----
    def fuzzy_phrase(self, words: list, phrase: tuple) -> Optional[tuple]:
        m = self.match_phrase(words, list(phrase))
        return m

    def floor_candidate(self, words: list) -> Optional[tuple]:
        for i, w in enumerate(words):
            if similar(w, "level", self.settings) or similar(w, "floor", self.settings):
                spots = [j for j in list(range(max(0, i - 2), i)) + list(range(i + 1, min(len(words), i + 3))) if words[j] in ORDINALS]
                number = ORDINALS[words[spots[0]]] if spots else None
                if number is not None and number in self.resolver.floors():
                    from robot.services.places import Place
                    return Place("floor", f"Level {number}", floor=number), {i, spots[0]}
        return None

    def section_candidates(self, words: list) -> list:
        found = []
        for name, name_words in self.sections.items():
            m = self.match_phrase(words, name_words)
            if m:
                found.append((name, m[0], set(m[1]), len(name_words)))
        if not found:
            return []
        longest = max(f[3] for f in found)                            # "science fiction" beats "science"
        return [f for f in found if f[3] == longest]

    def topic_candidates(self, words: list) -> list:
        found = []
        for alias, canonical in self.topics.items():
            m = self.match_phrase(words, list(alias))
            if m and len(alias[0]) > 2:
                found.append((canonical, m[0], set(m[1]), len(alias)))
        found.sort(key=lambda f: (-f[3], -f[1]))
        return found

    def title_candidates(self, words: list, content: set) -> list:
        """[(isbn, confidence, indexes)] for catalogue titles the words match (some or all of the title's words)."""
        out = []
        for isbn, title_words in self.titles.items():
            if not title_words:
                continue
            used, sims = set(), []
            for tw in title_words:
                hit = next(((i, s) for i, w in enumerate(words) if i not in used and w not in STOP
                            and (s := similar(w, tw, self.settings)) and (s == 1.0 or min(len(w), len(tw)) > 4)), None)   # a short word must be exact: "dun" is not "Dune"
                if hit:
                    used.add(hit[0])
                    sims.append(hit[1])
            if not sims:
                continue
            title_cov = len(sims) / len(title_words)
            utt_cov = len({words[i] for i in used} & content) / len(content) if content else 0.0
            out.append((isbn, sum(sims) / len(sims) * min(1.0, utt_cov) * (0.85 + 0.15 * title_cov), used, title_cov))
        out.sort(key=lambda t: -t[1])
        return out

    def extract_interest(self, norm: str, question: bool = False) -> str:
        """What the visitor says they like ("a good sci fi book" -> "sci fi"), or "". "I like X" inside a question about a
        book ("would I like it if I enjoy funny stories?") is not a request."""
        for index, pattern in enumerate(INTEREST_PATTERNS):
            if question and index == 3:
                continue
            if match := re.search(pattern, norm):
                words = [w for w in match.group("i").split()
                         if not any(w == f or (len(w) >= 3 and w[0] == f[0] and edit_cost(w, f) <= 1) for f in FILLER_WORDS)]
                text = " ".join(words)
                if text and text not in ("it", "this", "that", "him", "her", "one", "this one", "that one", "it one"):
                    return text
        return ""

    def known_words(self) -> set:
        """Single words worth rejoining a split word to: section, title and topic words and the phrase words (5+ letters)."""
        if getattr(self, "_known", None) is None:
            pool = set(INTENT_WORDS)
            for group in (self.titles.values(), self.sections.values(), self.topics):
                for item in group:
                    pool.update(item)
            self._known = {w for w in pool if len(w) >= 5 and w not in STOP}
        return self._known

    def all_words(self) -> set:
        """Every word of every title, section and topic: a word that is one of these is never a misspelling."""
        if getattr(self, "_all", None) is None:
            self._all = {w for group in (self.titles.values(), self.sections.values(), self.topics) for item in group for w in item}
        return self._all

    def rejoin(self, words: list) -> list:
        """Speech recognition sometimes splits one word in two ("his story", "sci ence"): when two neighbouring words
        that do not both mean something alone are, run together, a known word, they become that word."""
        known, out, i = self.known_words(), [], 0
        while i < len(words):
            if i + 1 < len(words) and not (words[i] in known and words[i + 1] in known):
                joined = words[i] + words[i + 1]
                match = next((k for k in known if abs(len(k) - len(joined)) <= 1 and similar(joined, k, self.settings)), None)
                if match and (words[i] in STOP or words[i + 1] in STOP or len(words[i]) > 2) and len(joined) >= 6:
                    out.append(match)
                    i += 2
                    continue
            out.append(words[i])
            i += 1
        return out

    # ---- the route ----
    def route(self, text: str, state: str = "", awaiting_guide: bool = False, has_book: bool = False,
              prompted: Optional[str] = None) -> Route:
        """Rank what the words could mean. state is the dialogue's leaf state; awaiting_guide: the robot just offered a
        guide; has_book: a book is on the pad (so "where is it" means that book); prompted: the robot asked "what do you
        like?" (recommend) or "which book?" (ask), so a bare place or topic is the answer, not a question about places."""
        from robot.services.places import Place
        words = self.rejoin(normalise(text).split())
        norm = " ".join(words)
        route = Route(text, norm)
        if not words:
            return route
        content_list = [w for w in words if w not in STOP and w not in EXPLAINED_EXTRA]
        content = set(content_list)
        entity_content = {w for w in content if w not in INTENT_WORDS}      # what is left to be a title, a place or a topic
        cues = {i for i in range(len(words)) if any(similar(words[i], c.split()[0], self.settings) for c in WHERE_CUES)}
        cand: list = []
        placed: dict = {}                        # id(where candidate) -> the words its place matched

        def add(intent, conf, **kw):
            if conf >= self.settings["min_confidence"]:
                cand.append(Candidate(intent, round(conf, 3), **kw))

        # the entities named: a book title, what they like
        question = "?" in text or words[0] in ("would", "will", "do", "does", "is", "are", "can", "could", "who", "what", "why", "how")
        interest = self.extract_interest(norm, question)
        titles = self.title_candidates(words, entity_content)
        best_title = titles[0] if titles and titles[0][1] >= self.settings["min_confidence"] else None
        isbn = best_title[0] if best_title else ""
        entity_idx: set = set(best_title[2]) if best_title else set()
        if interest:
            interest_idx = {i for i, w in enumerate(words) if w in interest.split()}
            if best_title and set(interest.split()) <= {words[i] for i in best_title[2]}:
                interest = ""                                        # "something like Dune": the book, not a topic
            else:
                entity_idx |= interest_idx
        # intent phrases
        strong_idx: set = set()
        for intent, phrases in PHRASES.items():
            conf, used = self.best_phrase(words, phrases, content, frozenset(entity_idx) if intent in ("explain", "recommend") else frozenset())
            if not used:
                continue
            if intent == "no" and (state != "ANYTHING_ELSE" or len(content_list) > 1):
                continue
            if intent == "yes" and len(content_list) > 1:
                continue
            if intent == "explain" and [words[i] for i in used] == ["about"]:
                conf *= 0.8                                           # a lone "about" is weak
            add(intent, conf, label=" ".join(words[i] for i in used))
            if conf >= 0.85 and intent in ("done", "guide", "yes", "no", "return", "another"):
                strong_idx |= set(used)
        if not any(c.intent == "done" for c in cand) and len(content_list) == 1 and state in ("OFFER", "ANYTHING_ELSE", "AWAIT_BOOK"):
            word = content_list[0]
            if len(word) == 4 and (word[0] == "d" or word in ("none", "gone")) and edit_cost(word, "done") <= 1 and word not in self.all_words() and set(words) <= STOP | {word}:
                add("done", 0.75, label=f"{word!r} for done")          # "I'm dine": a slip of the finger on a four-letter word
        if strong_idx:                                                 # "guide me" is not about "The Hitchhiker's Guide"
            entity_content = {w for i, w in enumerate(words) if i not in strong_idx and w not in STOP
                              and w not in EXPLAINED_EXTRA and w not in INTENT_WORDS}
            titles = self.title_candidates(words, entity_content) if entity_content else []
            best_title = titles[0] if titles and titles[0][1] >= self.settings["min_confidence"] else None
            isbn = best_title[0] if best_title else ""
        place_cue = bool(self.best_phrase(words, WHERE_CUES, content)[1]) or any(
            words[i] in ("section", "area", "aisle", "floor", "level", "shelf") for i in range(len(words)))
        take_me = bool(self.best_phrase(words, GUIDE_CUES, content)[1]) and "way" not in words and "there" not in words
        # attach what was named to explain / recommend
        for c in cand:
            if c.intent in ("explain", "recommend"):
                c.isbn = isbn
        if interest:
            topics_named = bool(self.topic_candidates(interest.split()))
            add("recommend", 0.9 if topics_named else 0.75, interest=interest, label=f"interest {interest!r}")
        # places: floors, sections, topics and titles, with a where cue or when that is all they said
        explained_extra = {i for i, w in enumerate(words) if w in EXPLAINED_EXTRA or i in cues or w in ("where", "is", "are")}
        place_only = len(content - EXPLAINED_EXTRA) <= 4
        if (place_cue or place_only) and not (prompted and not place_cue):
            places: list = []
            floor = self.floor_candidate(words)
            if floor:
                places.append((floor[0], 1.0, floor[1], "floor"))
            for name, sim, used, _ in self.section_candidates(words):
                places.append((self.resolver.section_place(name), sim, used, f"section {name}"))
            names_a_place = any(w in ("section", "area", "aisle", "floor", "level") for w in words)
            title_wins = bool(best_title) and best_title[1] >= 0.9 and not places
            if not places and not title_wins:
                for topic, sim, used, _ in self.topic_candidates(words)[:1]:
                    place = self.resolver.topic_in(topic)
                    if place is not None and place.kind == "section":
                        places.append((place, sim * 0.95, used, f"topic {topic}"))
            if best_title and not names_a_place and (place_cue or places) and \
                    not (interest or any(c.intent in ("recommend", "explain") for c in cand)):
                book = next(b for b in self.finder.catalog.books if b.isbn13 == isbn)
                places.append((self.resolver.book_place(book), best_title[1], best_title[2], f"title {book.title}"))
            for place, sim, used, label in places:
                covered = (used | explained_extra) & set(range(len(words)))
                coverage = len({words[i] for i in covered} & content) / len(content) if content else 1.0
                conf = sim * (0.55 + 0.45 * min(1.0, coverage)) if coverage < 1.0 else sim
                if place.kind != "floor" and not place_cue:
                    conf *= 0.95
                add("where", conf, place=place, isbn=place.isbn if place.kind == "book" else "", take_me=take_me, label=label)
                if cand and cand[-1].label == label:
                    placed[id(cand[-1])] = {words[i] for i in used}
        if has_book and place_cue and any(w in ("it", "this", "that") for w in words) and not any(c.intent == "where" for c in cand):
            add("where", 0.9, label="where is it (the book on the pad)", take_me=take_me)
        if best_title and not cand:
            add("title", best_title[1] * 0.9, isbn=isbn, label="title")
        cand.sort(key=lambda c: (-c.confidence, PRIORITY.index(c.intent)))
        # a "where" that is only the topic/interest of a recommendation is not a place question
        if any(c.intent == "recommend" and c.interest for c in cand):
            strong = any(similar(w, "where", self.settings) or w in ("section", "area", "aisle", "floor", "level", "shelf") for w in words)
            cand = [c for c in cand if not (c.intent == "where" and not strong)]
        recs = [c for c in cand if c.intent == "recommend"]
        for extra in recs[1:]:                                     # "recommend something about space": one recommendation
            recs[0].interest = recs[0].interest or extra.interest
            recs[0].isbn = recs[0].isbn or extra.isbn
            cand.remove(extra)
        route.candidates = cand
        wheres = [c for c in cand if c.intent == "where"]
        if len(wheres) > 1 and cand[0].intent == "where":
            top = wheres[0]
            top_words = placed.get(id(top), set())
            close = [c for c in wheres if top.confidence - c.confidence < self.settings["ambiguity_margin"]
                     and c.place.choice_id != top.place.choice_id
                     and not top_words > placed.get(id(c), set())]         # the top one names more: "the brief history"
            if close:
                route.ambiguous = [top] + close[:3]
        return route
