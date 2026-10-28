"""Expectation-aware matching: when the robot has just asked a question with a few possible answers, a short reply is
matched against only those answers, and more loosely than the router matches anything.

    "Would you like me to guide you there?"     -> guide ("guide me", "take me there", "show me", "lead the way"), yes, no
    "Is there anything else I can help with?"   -> yes / more, another book, no / nothing else
    "Did you mean A or B?"                      -> A, B (by their labels)

"guy me", "er tqke me thre" and "yes plz" are not close enough to the phrases for the router, which has to tell hundreds of
things apart; here there are only a handful of answers, so a looser per-word threshold (`intents.expected_word_similarity`)
and, for a phrase of two or more words, a looser match of the whole text (`intents.expected_phrase_similarity`) are safe.

The expectation exists only for the next utterance after the question (the dialogue clears it when it is used, when a
button is pressed and when the visit ends), and only for short replies (`intents.expected_max_words`, fillers like "er"
and "please" not counted). Whatever does not match is left to the normal router, unchanged. The guards stay on: words that
must never match each other ("anything" and "nothing") do not, here either, and every word said has to be explained by
the answer, so "show me Dune" is not "show me".
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Optional

from robot.core.intents import NEVER_MATCH, STOP, normalise, word_similarity

DEFAULTS = {"expected_word_similarity": 0.65, "expected_phrase_similarity": 0.68, "expected_max_words": 4}

FILLERS = frozenset("er erm um uh hmm well oh ah please pls plz".split())     # not counted, and not matched
LIGHT = frozenset("me the a to you i am us we it is".split())                 # words of a phrase that need not be said

YES = ["yes", "yeah", "yep", "yup", "sure", "okay", "ok", "go ahead", "why not", "of course", "that would be great"]
NO = ["no", "nope", "nah", "no thanks", "no thank you", "not now", "not really", "that is all", "that is everything",
      "that is it", "nothing else", "nothing more", "i am done", "i am good", "done", "all set"]
GUIDE = ["guide me", "take me there", "take me", "show me", "show me the way", "lead the way", "lead me", "walk me there",
         "follow you", "take us there"]
ANOTHER = ["another book", "next book", "another one", "different book", "one more book"]


@dataclass
class Expectation:
    kind: str                                               # guide | anything_else | choose
    answers: dict = field(default_factory=dict)            # answer name -> phrases that mean it
    options: list = field(default_factory=list)             # for choose: [(choice id, label)]
    data: Optional[object] = None                           # for lend: the ISBN of the stored book on offer


@dataclass
class Match:
    answer: str                                             # a name from `answers`, or the choice id
    phrase: str
    score: float


def expect_guide() -> Expectation:
    """The robot offered to guide the visitor: yes or a guide phrase mean guide, a no means they are done."""
    return Expectation("guide", {"guide": GUIDE + YES, "done": NO})


def expect_anything_else() -> Expectation:
    return Expectation("anything_else", {"more": YES, "another": ANOTHER, "done": NO})


def expect_lend(isbn: str) -> Expectation:
    """The robot has a stored copy of the book asked about and offered it ("Would you like it?"): yes or no."""
    return Expectation("lend", {"yes": YES + ["please", "i would", "i do", "i would like it"], "no": NO}, data=isbn)


def expect_choice(options: list) -> Expectation:
    """"Did you mean A or B?": options is [(choice id, label)]."""
    return Expectation("choose", options=[(i, label) for i, label in options])


def words_of(text: str) -> list:
    return [w for w in normalise(text).split() if w not in FILLERS]


def nearer_to_another(word: str, phrase: list) -> bool:
    """The word is (or is a typo of) a word that must never be taken for one of the phrase's: "anything" for "nothing"."""
    for pair in NEVER_MATCH:
        for p in phrase:
            if p in pair:
                (other,) = pair - {p}
                if other not in phrase and difflib.SequenceMatcher(None, word, other).ratio() >= \
                        difflib.SequenceMatcher(None, word, p).ratio():
                    return True
    return False


def phrase_score(words: list, phrase: list, loose: float, whole: float) -> float:
    """0 if the words are not this phrase, else how well they are. Every word said must match a word of the phrase, in
    order, at the loose per-word similarity, and every key word of the phrase must have been said; failing that, two
    or more words that are as long as the phrase and look like it as a whole ("guy me" for "guide me")."""
    for w in words:                                                        # "anything" is never "nothing", nor a typo of it
        if w not in phrase and nearer_to_another(w, phrase):
            return 0.0
    at, sims, used = 0, [], set()
    for w in words:
        hit = next(((i, s) for i in range(at, len(phrase)) if (s := word_similarity(w, phrase[i])) >= loose), None)
        if hit is None:
            break
        sims.append(hit[1])
        used.add(hit[0])
        at = hit[0] + 1
    else:
        if all(i in used for i, p in enumerate(phrase) if p not in LIGHT):
            return sum(sims) / len(sims)
    joined, wanted = " ".join(words), " ".join(phrase)
    if len(phrase) >= 2 and len(wanted) >= 6 and len(words) == len(phrase):
        ratio = difflib.SequenceMatcher(None, joined, wanted).ratio()
        if ratio >= whole:
            return ratio * 0.9                                             # an exact wording always beats a guess
    return 0.0


def match_answers(text: str, expectation: Expectation, settings: dict) -> Optional[Match]:
    """The answer a short reply means, or None (the normal router then decides)."""
    cfg = {**DEFAULTS, **{k: v for k, v in (settings or {}).items() if k in DEFAULTS}}
    words = words_of(text)
    if not words or len(words) > int(cfg["expected_max_words"]):
        return None
    if expectation.kind == "choose":
        return match_options(words, expectation.options, float(cfg["expected_word_similarity"]))
    scored = []
    for answer, phrases in expectation.answers.items():
        best = max(((phrase_score(words, p.split(), float(cfg["expected_word_similarity"]),
                                  float(cfg["expected_phrase_similarity"])), p) for p in phrases), default=(0.0, ""))
        if best[0] > 0:
            scored.append((best[0], answer, best[1]))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None                                                        # two answers fit equally well: leave it
    return Match(scored[0][1], scored[0][2], round(scored[0][0], 3))


def match_options(words: list, options: list, loose: float) -> Optional[Match]:
    """A label said in part ("brief history", "the science one"): every word said matches a word of exactly one label."""
    found = []
    for choice, label in options:
        label_words = [w for w in normalise(label).split() if w not in STOP]
        sims = []
        for w in words:
            if w in STOP or w in LIGHT:
                continue
            best = max((word_similarity(w, lw) for lw in label_words), default=0.0)
            if best < loose:
                sims = None
                break
            sims.append(best)
        if sims:
            found.append((sum(sims) / len(sims), choice, label))
    if len(found) != 1:
        return None                                                        # nothing fits, or several do: the router asks again
    return Match(found[0][1], found[0][2], round(found[0][0], 3))
