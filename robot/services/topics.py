"""The controlled vocabulary of topics (data/topics.yaml): synonyms folded into one name, plus "broader" topics.

    vocab = TopicVocabulary.load("data/topics.yaml")
    vocab.canonical("Cosmos")                  # "astronomy"
    vocab.ancestors("astronomy")               # ("science",)
    vocab.normalise(["space", "Comedy", "zeitgeist"])
    # -> (("astronomy", "humour", "zeitgeist"), ("zeitgeist",))      known topics, then the unknown ones kept as typed

A topic that some other topic names as its `broader` is *general* (science, fiction: they have narrower
children); one with no children is specific. The graph weighs general topics less and never picks one as the
spoken reason when a specific topic is shared. depth() says how many broader topics sit above a topic.

A subject the vocabulary doesn't know is kept (as its normalised spelling) and returned in the second list, so
nothing is lost and tools/catalog_check.py can ask you to add an alias for it. Matching ignores case, hyphens,
underscores and extra spaces: "Popular-Science" is "popular science".

The file is a mapping from topic name to {aliases: [...], broader: <topic> or [topics], description: "..."}.
Mistakes (a broader topic that doesn't exist, an alias claimed by two topics, a loop) are collected in
`problems` instead of raising, so the checker can list them all; load() turns them into a CatalogError.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

from robot.services.books import CatalogError


def norm_key(text) -> str:
    """Lower case, hyphens/underscores/slashes as spaces, runs of spaces collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", str(text or "").lower())).strip(" .,;:")


class TopicVocabulary:
    def __init__(self, entries: Optional[Mapping] = None, source: str = "topics"):
        self.source = source
        self.broader: dict[str, tuple] = {}            # topic -> its broader topics
        self.aliases: dict[str, tuple] = {}            # topic -> its aliases
        self.descriptions: dict[str, str] = {}
        self._lookup: dict[str, str] = {}              # normalised name or alias -> topic
        self.problems: list[str] = []
        self.general: frozenset = frozenset()          # topics that have a narrower child
        entries = entries or {}
        if not isinstance(entries, Mapping):
            self.problems.append(f"{source}: the file must be a mapping from topic name to its details")
            return
        for name, details in entries.items():
            key = norm_key(name)
            if not key:
                self.problems.append(f"{source}: an empty topic name")
                continue
            if key in self.aliases:
                self.problems.append(f"{source}: topic {key!r} is listed twice")
                continue
            details = {} if details is None else details
            if not isinstance(details, Mapping):
                self.problems.append(f"{source}: {key!r} must have aliases/broader/description (or nothing), "
                                     f"not {type(details).__name__}")
                details = {}
            self.aliases[key] = tuple(norm_key(a) for a in _as_list(details.get("aliases")) if norm_key(a))
            self.descriptions[key] = str(details.get("description") or "")
            self.broader[key] = tuple(norm_key(b) for b in _as_list(details.get("broader")) if norm_key(b))
        for key in self.aliases:
            self._claim(key, key)
        for key, aliases in self.aliases.items():
            for alias in aliases:
                self._claim(alias, key)
        for key, parents in list(self.broader.items()):
            kept = []
            for parent in parents:
                if parent not in self.aliases:
                    self.problems.append(f"{source}: {key!r} has broader topic {parent!r}, which is not defined")
                elif parent == key:
                    self.problems.append(f"{source}: {key!r} is its own broader topic")
                else:
                    kept.append(parent)
            self.broader[key] = tuple(kept)
        for key in self.aliases:
            loop = self._loop_from(key)
            if loop:
                self.problems.append(f"{source}: broader topics loop: {' -> '.join(loop)}")
                self.broader[loop[-2]] = tuple(p for p in self.broader[loop[-2]] if p != loop[-1])
        self.general = frozenset(parent for parents in self.broader.values() for parent in parents)

    def _claim(self, text: str, topic: str) -> None:
        owner = self._lookup.setdefault(text, topic)
        if owner != topic:
            self.problems.append(f"{self.source}: {text!r} is used for both {owner!r} and {topic!r}")

    def _loop_from(self, start: str) -> Optional[list]:
        """A path start -> ... -> a repeated topic, if following `broader` from start loops."""
        seen = {start}
        frontier = [(start, [start])]
        while frontier:
            node, trail = frontier.pop()
            for parent in self.broader.get(node, ()):
                if parent in trail:
                    return trail + [parent]
                if parent not in seen:
                    seen.add(parent)
                    frontier.append((parent, trail + [parent]))
        return None

    @classmethod
    def load(cls, path: Path | str) -> "TopicVocabulary":
        path = Path(path)
        if not path.is_file():
            raise CatalogError(f"topics file not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            raise CatalogError(f"{path}: cannot read the topics file: {exc}") from None
        vocab = cls(data or {}, source=path.name)
        if vocab.problems:
            raise CatalogError(vocab.problems[0] + (f" (and {len(vocab.problems) - 1} more)" if len(vocab.problems) > 1 else ""))
        return vocab

    def __contains__(self, topic: str) -> bool:
        return norm_key(topic) in self.aliases

    def __len__(self) -> int:
        return len(self.aliases)

    @property
    def topics(self) -> list:
        return sorted(self.aliases)

    @property
    def words(self) -> dict:
        """Every name and alias (normalised) and the topic it stands for, e.g. {"cosmos": "astronomy"}."""
        return dict(self._lookup)

    def canonical(self, subject) -> Optional[str]:
        """The topic a subject or alias stands for, or None if the vocabulary doesn't know it."""
        return self._lookup.get(norm_key(subject))

    def ancestors(self, topic: str) -> tuple:
        """Every broader topic, nearest first, without repeats (safe even if a loop slipped through)."""
        found, queue = [], list(self.broader.get(topic, ()))
        while queue:
            parent = queue.pop(0)
            if parent not in found and parent != topic:
                found.append(parent)
                queue.extend(self.broader.get(parent, ()))
        return tuple(found)

    def is_general(self, topic: str) -> bool:
        """True if some other topic is narrower than this one (science has astronomy under it)."""
        return topic in self.general

    def depth(self, topic: str) -> int:
        """How many broader topics are above it: 0 for science, 1 for astronomy, 2 for a child of a child."""
        return len(self.ancestors(topic))

    def normalise(self, subjects: Iterable) -> tuple:
        """(topics, unknown): the subjects as canonical topics, each once and in order, with the ones the
        vocabulary doesn't know kept as their normalised spelling and listed again as `unknown`."""
        topics, unknown = [], []
        for subject in subjects:
            key = self.canonical(subject)
            if key is None:
                key = norm_key(subject)
                if key and key not in unknown:
                    unknown.append(key)
            if key and key not in topics:
                topics.append(key)
        return tuple(topics), tuple(unknown)


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]
