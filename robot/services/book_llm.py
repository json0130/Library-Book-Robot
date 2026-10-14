"""The LLM's part in the book flow: explain a book and recommend similar ones, grounded in the catalogue.

The model is only ever shown real catalogue records, and told to use nothing else:

    explain    the book's own record (title, author, genre, topics, summary)
    recommend  the book plus up to three similar catalogue books to choose from
    answer     the book's own record and the visitor's free question about it
    chat       no book yet: small talk, who the robot is, what it can do (never names books)

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

HISTORY_EXCHANGES = 10          # past visitor/robot pairs kept for the model (twice as many messages)
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


CHAT_RULES = (
    "\n\nNo book is on your pad right now and you have no catalogue in front of you, so do not name or "
    "describe specific books and do not say what the library holds. Chat naturally. Mention that you can "
    "return, explain and recommend books once they place one on your pad only when it fits, not in every "
    "reply; if they ask for a recommendation or an explanation, ask them to place a book on your pad."
)


def question_request(book, question: str) -> str:
    return (f"Catalogue entry:\n{record_text(book)}\n\n"
            f"The visitor said: \"{question}\"\n"
            "If this is about the book, answer in at most three short spoken sentences, using only the "
            "catalogue entry and what was said earlier in this conversation. If the entry does not say, "
            "tell them you don't have that information and offer to explain the book, suggest similar "
            "books, or point them to the help desk. If it is a general question for you (who you are, what "
            "you can do), answer that briefly and do not bring up the book unless they ask.")


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
        self.chat_system = system + CHAT_RULES
        self.history: list[dict] = []

    def reset(self) -> None:
        """Forget the conversation (the visitor left, or a new book started)."""
        self.history.clear()

    def hear(self, text: str) -> None:
        """The visitor said this. Every visitor utterance goes here, whoever ends up answering it."""
        self._remember("user", text)

    def said(self, text: str) -> None:
        """The robot said this, whether the LLM worded it or not (hello, announcements, catalogue text), so
        the model sees the conversation as it really went and doesn't greet or introduce itself again."""
        self._remember("assistant", text)

    def _remember(self, role: str, text: str) -> None:
        self.history.append({"role": role, "content": text})
        del self.history[:-2 * HISTORY_EXCHANGES]

    async def explain(self, book) -> Optional[str]:
        """A spoken explanation of a catalogue book, or None if the model is unavailable."""
        reply = await self._ask(explain_request(book))
        return reply or None

    async def recommend(self, book, candidates: list) -> Optional[str]:
        """A spoken recommendation naming at least one of the candidates, or None."""
        if not candidates:
            return None
        reply = await self._ask(recommend_request(book, candidates))
        if reply and not mentions_any(reply, candidates):
            log.warning("recommendation names none of the candidates; discarded: %r", reply)
            return None
        return reply or None

    async def answer(self, book, question: str) -> Optional[str]:
        """A spoken answer to a free question about a catalogue book, or None if the model is unavailable.
        Earlier questions and answers are part of the prompt, so "and who wrote it?" makes sense."""
        reply = await self._ask(question_request(book, question))
        return reply or None

    async def chat(self, said: str) -> Optional[str]:
        """A spoken reply to small talk or a question before any book is on the pad, or None if the model
        is unavailable. Names no books: the model has no catalogue to draw on."""
        reply = await self._ask(said, system=self.chat_system)
        return reply or None

    async def _ask(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """Send the conversation so far with `prompt` as the visitor's latest turn: for a question that is
        the words they said, for explain/recommend the catalogue request. The visitor's real words are
        already the last item of the history (hear), so the prompt replaces them instead of repeating them."""
        earlier = self.history[:-1] if self.history and self.history[-1]["role"] == "user" else self.history
        messages = ([{"role": "system", "content": system or self.system}] + earlier
                    + [{"role": "user", "content": prompt}])
        try:
            reply = await asyncio.get_running_loop().run_in_executor(None, self.client.chat, messages)
        except LLMError as exc:
            log.warning("LLM unavailable, using the catalogue text instead: %s", exc)
            return None
        reply = clean(reply)
        if not reply:
            log.warning("LLM returned an empty answer, using the catalogue text instead")
        return reply
