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


def choose_words(options, rng: random.Random, typo_probability: float, **names) -> tuple:
    """(text the visitor types, the paraphrase it came from, whether typos were added). `{title}`-style placeholders in
    the paraphrase are filled from `names` first."""
    base = pick(options, rng)
    text = base.format(**names) if names else base
    typed = rng.random() < typo_probability
    return (add_typos(text, rng) if typed else text), base, typed
