"""The LLM's part in the book flow: explain a book and recommend similar ones, grounded in the catalogue.

The model is only ever shown real catalogue records, and told to use nothing else:

    explain    the book's own record (title, author, genre, topics, summary)
    recommend  the book plus up to three similar catalogue books to choose from

and its answer is checked: a recommendation must name at least one of the candidates, or it is
thrown away. Anything that goes wrong (server down, timeout, odd answer) returns None, and the
dialogue falls back to the plain catalogue text. Returning a book (shelf and storage slot) never
goes through the model, because those are facts, not language.

Books that are not in the catalogue are never sent to the model: it would answer from memory,
and that is where invented titles and plots come from.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from robot.services.llm import LIBRARY_SYSTEM_PROMPT, LLMError, split_sentences

log = logging.getLogger("robot.book_llm")

HISTORY_EXCHANGES = 10          # past visitor/robot pairs kept for the model
MAX_SENTENCES = 3               # replies are spoken, so they are short

GROUNDING = (
    "\n\nRight now you are talking about books from the library catalogue. Use only the facts given "
    "in the catalogue entries below. Do not add plot details, awards, dates or other facts that are not "
    "listed there, and do not mention any book that is not listed."
)


def record_text(book) -> str:
    lines = [f"Title: {book.title}", f"Author: {book.author or 'unknown'}"]
    if book.genre:
        lines.append(f"Genre: {book.genre}")
    if book.tags:
        lines.append("Topics: " + ", ".join(book.tags))
    if book.summary:
        lines.append(f"Summary: {book.summary}")
    return "\n".join(lines)


def explain_request(book) -> str:
    return (f"Catalogue entry:\n{record_text(book)}\n\n"
            "The visitor asked what this book is about. Tell them in two or three short spoken sentences.")


def recommend_request(book, candidates: list) -> str:
    listed = "\n\n".join(f"Candidate {i}:\n{record_text(c)}" for i, c in enumerate(candidates, 1))
    return (f"The visitor has this book:\n{record_text(book)}\n\n"
            f"Books you may recommend (choose only from these, and say each title exactly as written):\n\n{listed}\n\n"
            "The visitor asked for something similar. In two or three short spoken sentences, "
            "recommend one or more of the candidates and say briefly why it suits them.")


def clean(reply: str) -> str:
    """One plain spoken paragraph of at most MAX_SENTENCES sentences: no markdown, labels or newlines."""
    text = re.sub(r"^\s*(assistant|robot)\s*:\s*", "", str(reply), flags=re.I)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(split_sentences(text)[:MAX_SENTENCES])


def mentions_any(text: str, books: list) -> bool:
    low = text.lower()
    return any(b.title.lower() in low for b in books)


class BookAssistant:
    """Asks the AI server's LLM about catalogue books. client: services.llm.LLMClient (or any object
    with chat(messages) -> str that raises LLMError)."""

    def __init__(self, client, system: str = LIBRARY_SYSTEM_PROMPT):
        self.client = client
        self.system = system + GROUNDING
        self.history: list[dict] = []

    def reset(self) -> None:
        """Forget the conversation (the visitor left)."""
        self.history.clear()

    async def explain(self, book) -> Optional[str]:
        """A spoken explanation of a catalogue book, or None if the model is unavailable."""
        reply = await self._ask("Explain this book.", explain_request(book))
        return reply or None

    async def recommend(self, book, candidates: list) -> Optional[str]:
        """A spoken recommendation naming at least one of the candidates, or None."""
        if not candidates:
            return None
        reply = await self._ask("Recommend something similar.", recommend_request(book, candidates))
        if reply and not mentions_any(reply, candidates):
            log.warning("recommendation names none of the candidates; discarded: %r", reply)
            return None
        return reply or None

    async def _ask(self, visitor_said: str, prompt: str) -> Optional[str]:
        messages = ([{"role": "system", "content": self.system}] + self.history
                    + [{"role": "user", "content": prompt}])
        try:
            reply = await asyncio.get_running_loop().run_in_executor(None, self.client.chat, messages)
        except LLMError as exc:
            log.warning("LLM unavailable, using the catalogue text instead: %s", exc)
            return None
        reply = clean(reply)
        if not reply:
            log.warning("LLM returned an empty answer, using the catalogue text instead")
        if reply:                  # history holds the short request, not the whole catalogue prompt
            self.history += [{"role": "user", "content": visitor_said}, {"role": "assistant", "content": reply}]
            del self.history[:-2 * HISTORY_EXCHANGES]
        return reply
