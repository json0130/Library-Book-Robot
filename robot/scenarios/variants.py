"""Paraphrases and typos for the visitor's words. Everything here takes the run's random.Random, so a seed
reproduces the same wording."""

from __future__ import annotations

import random
import re

KEYBOARD_NEIGHBOURS = {"a": "sq", "e": "wr", "i": "uo", "o": "ip", "u": "yi", "t": "ry", "n": "bm", "s": "ad", "r": "et"}


def pick(options, rng: random.Random):
    """One of the paraphrases (a string is its own only option)."""
    options = [options] if isinstance(options, str) else list(options)
    if not options:
        raise ValueError("no paraphrases to pick from")
    return options[rng.randrange(len(options))]


def add_typos(text: str, rng: random.Random, max_typos: int = 2) -> str:
    """Realistic typing mistakes in 1..max_typos longer words: two letters swapped, a letter dropped, a letter doubled, or
    a neighbouring key hit. The first letter and short words are left alone, so it stays readable."""
    words = text.split(" ")
    candidates = [i for i, w in enumerate(words) if len(re.sub(r"\W", "", w)) >= 4 and not w.startswith("{")]
    if not candidates:
        return text
    for i in rng.sample(candidates, min(len(candidates), rng.randint(1, max(1, max_typos)))):
        w = words[i]
        letters = [j for j in range(1, len(w) - 1) if w[j].isalpha()]
        if len(letters) < 2:
            continue
        j = letters[rng.randrange(len(letters) - 1)]
        kind = rng.choice(("swap", "drop", "double", "near"))
        if kind == "swap" and w[j + 1].isalpha():
            w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
        elif kind == "drop":
            w = w[:j] + w[j + 1:]
        elif kind == "double":
            w = w[:j] + w[j] + w[j:]
        else:
            near = KEYBOARD_NEIGHBOURS.get(w[j].lower())
            w = w[:j] + (near[rng.randrange(len(near))] if near else w[j]) + w[j + 1:]
        words[i] = w
    return " ".join(words)


def load_speech_errors(path=None) -> dict:
    """config/speech_errors.yaml: homophones, drop_words, merge_pairs, fillers (empty lists if the file is missing)."""
    import yaml
    from robot.config import ROOT
    path = path or ROOT / "config" / "speech_errors.yaml"
    try:
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except OSError:
        data = {}
    return {"homophones": data.get("homophones") or {}, "drop_words": data.get("drop_words") or [],
            "merge_pairs": data.get("merge_pairs") or [], "fillers": data.get("fillers") or []}


def speechify(text: str, rng: random.Random, errors: dict) -> str:
    """The words the way speech recognition might hear them: a homophone, a lost short word, two words run together,
    and a filler ("um") at the start or middle. One or two kinds of change, whatever there is to change."""
    words = text.split(" ")

    def homophones():
        return [i for i, w in enumerate(words) if w.lower().strip("?.,!") in errors["homophones"]]

    def droppable():
        return [i for i, w in enumerate(words) if i > 0 and w.lower() in errors["drop_words"]] if len(words) > 3 else []

    def merges():
        return [i for i in range(len(words) - 1) if [words[i].lower(), words[i + 1].lower()] in errors["merge_pairs"]]

    kinds = [k for k, found in (("homophone", homophones()), ("drop", droppable()), ("merge", merges()),
                                ("filler", errors["fillers"])) if found]
    for kind in rng.sample(kinds, min(len(kinds), rng.randint(1, 2))):
        if kind == "homophone" and homophones():
            i = rng.choice(homophones())
            word = words[i].strip("?.,!")
            words[i] = words[i].replace(word, pick(errors["homophones"][word.lower()], rng))
        elif kind == "drop" and droppable():
            words.pop(rng.choice(droppable()))
        elif kind == "merge" and merges():
            i = rng.choice(merges())
            words[i:i + 2] = [words[i] + words[i + 1]]
        elif kind == "filler":
            words.insert(0 if rng.random() < 0.6 else rng.randrange(len(words)), pick(errors["fillers"], rng))
    return " ".join(words)


_ERRORS: dict = {}


def choose_words(options, rng: random.Random, typo_probability: float, speech_probability: float = 0.0, **names) -> tuple:
    """(text the visitor types or says, the paraphrase it came from, a description of the variation: "" / "typos" /
    "speech" / "typos+speech"). `{title}`-style placeholders in the paraphrase are filled from `names` first."""
    base = pick(options, rng)
    text = base.format(**names) if names else base
    kinds = []
    if rng.random() < typo_probability:
        text = add_typos(text, rng)
        kinds.append("typos")
    if speech_probability and rng.random() < speech_probability:
        if not _ERRORS:
            _ERRORS.update(load_speech_errors())
        text = speechify(text, rng, _ERRORS)
        kinds.append("speech")
    return text, base, "+".join(kinds)
