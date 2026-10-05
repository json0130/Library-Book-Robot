"""The interaction as a small state machine.

States and the events that move between them:

    idle        --person arrives, style proactive-->  invited      (robot invites them over)
    idle        --visitor speaks-->                   invited      (visitor started it)
    idle/invited/closing --BookPlaced-->              book_placed  --> scanning (scan starts at once)
    scanning    --BookScanned-->                      offering     (offer: return, explain, recommend)
    scanning    --ScanFailed-->                       idle         (ask them to place the book flat)
    offering    --visitor says return/explain/recommend--> acting  --> closing (do it, then wrap up)
    offering    --anything else-->                    offering     (offer the three choices again)
    closing     --visitor speaks-->                   invited      (they want something else)
    any         --visitor says bye/thanks-->          closing
    any         --everyone left (group_size 0)-->     idle

Every action reads the current StyleVector, so the same step is whispered and printed in a
quiet zone but spoken aloud in a common area (Actuators.deliver picks the channels).

The three choices use the catalogue only (the LLM comes later): return gives the shelf and
storage slot, explain gives the catalogue summary, recommend names up to three similar books.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from robot.events import (BookPlaced, BookScanned, ContextChanged, ReplySentence, ScanFailed,
                          UserUtterance)

log = logging.getLogger("robot.dialogue")

STATES = ("idle", "invited", "book_placed", "scanning", "offering", "acting", "closing")
GOODBYES = ("bye", "goodbye", "thanks", "thank you", "that's all", "no thanks")
ACTIONS = ("return", "explain", "recommend")
CHOICE_WORDS = {                    # what the visitor may say for each choice
    "return": ("return", "give back", "take back", "drop off"),
    "explain": ("explain", "about", "tell me", "summary", "describe"),
    "recommend": ("recommend", "similar", "suggest", "something like"),
}
SCAN_FAILED_MESSAGE = ("I couldn't read the barcode. Please place the book flat on the pad "
                       "with the barcode facing up.")
OFFER = "Would you like to return it, hear about it, or get a recommendation?"


def choice_from(text: str):
    """"return", "explain" or "recommend" if the visitor's words ask for one, else None."""
    text = text.lower()
    return next((c for c, words in CHOICE_WORDS.items() if any(w in text for w in words)), None)


def join_names(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class Dialogue:
    def __init__(self, actuators, get_style: Callable[[], object],
                 publish: Optional[Callable[[object], Awaitable[int]]] = None, finder=None):
        self.out = actuators
        self.get_style = get_style
        self.publish = publish
        self.finder = finder                # services.books.BookFinder, for recommendations
        self.state = "idle"
        self.book = None
        self.transitions: list[tuple[str, str, str]] = []    # (from, trigger, to)

    def attach(self, bus) -> None:
        bus.subscribe(ContextChanged, self.on_context)
        bus.subscribe(UserUtterance, self.on_utterance)
        bus.subscribe(BookPlaced, self.on_book_placed)
        bus.subscribe(BookScanned, self.on_book_scanned)
        bus.subscribe(ScanFailed, self.on_scan_failed)

    # ---- transitions ----
    def _go(self, new: str, trigger: str) -> None:
        if new not in STATES:
            raise ValueError(new)
        self.transitions.append((self.state, trigger, new))
        log.info("[dialogue] %s -> %s (%s)", self.state, new, trigger)
        self.state = new

    async def on_context(self, event: ContextChanged) -> None:
        if event.group_size <= 0:
            if self.state != "idle":
                self.book = None
                self._go("idle", "everyone left")
            return
        if self.state == "idle" and self.get_style().proactive:
            self._go("invited", "person arrived")
            await self.say("Hello! Put a book on my pad and I can return it, tell you about it, "
                           "or suggest something similar.", "happy")

    async def on_utterance(self, event: UserUtterance) -> None:
        text = event.text.strip().lower()
        if self.state != "idle" and any(g in text for g in GOODBYES):
            self._go("closing", "visitor said goodbye")
            await self.say("You're welcome. Enjoy your reading!", "happy")
            return
        if self.state == "idle":
            self._go("invited", "visitor spoke")
            await self.say("Hi! Place a book on the pad and I'll help with it.", "happy")
        elif self.state == "offering":
            action = choice_from(text)
            if action is None:
                await self.say(OFFER, "neutral")
                return
            self._go("acting", f"chose {action}")
            await self.act(action)
            self._go("closing", f"{action} done")
            await self.say("Is there anything else I can help with?", "neutral")
        elif self.state == "closing":
            self._go("invited", "visitor wants more")
            await self.say("Sure. Place another book on the pad.", "happy")
        elif self.state == "invited":
            await self.say("Place a book on the pad and I'll help with it.", "neutral")
        else:                                    # book_placed / scanning / acting: busy
            await self.say("One moment, please.", "neutral")

    async def on_book_placed(self, event: BookPlaced) -> None:
        if self.state in ("idle", "invited", "closing"):
            self._go("book_placed", "book placed")
            self.out.lights.spotlight(True)
            self._go("scanning", "scan started")

    async def on_book_scanned(self, event: BookScanned) -> None:
        if self.state != "scanning":
            return
        self.book = event
        self._go("offering", "book identified")
        if event.title:
            by = f" by {event.author}" if event.author else ""
            await self.say(f"You have {event.title}{by}. {OFFER}", "happy")
        else:
            await self.say(f"I read the barcode, but I don't know this book (ISBN {event.isbn}). "
                           f"{OFFER}", "neutral")

    async def on_scan_failed(self, event: ScanFailed) -> None:
        if self.state != "scanning":
            return
        log.info("[dialogue] scan failed: %s", event.reason)
        self.book = None
        self.out.lights.spotlight(False)
        self._go("idle", "scan failed")
        await self.say(SCAN_FAILED_MESSAGE, "sad")

    # ---- the three choices, from the catalogue ----
    async def act(self, action: str) -> None:
        book = self.book
        title = (book.title if book and book.title else None) or "this book"
        if action == "return":
            await self.act_return(book, title)
        elif action == "recommend":
            await self.act_recommend(book, title)
        else:
            summary = book.summary if book else None
            await self.say(f"{title}: {summary}" if summary else
                           f"I don't have a summary for {title}. The help desk can tell you more.",
                           "neutral" if not summary else "happy")

    async def act_return(self, book, title: str) -> None:
        if book is None or not book.found_in_catalog:
            await self.say(f"{title} isn't in our catalogue, so I don't know where it belongs. "
                           "Please leave it at the help desk.", "neutral")
            return
        parts = [f"Thank you for returning {title}."]
        if book.slot is not None:
            try:
                self.out.ring.rotate_to(book.slot)
                parts.append(f"I've put it in storage slot {book.slot}.")
            except ValueError:
                log.warning("catalogue slot %s is outside the storage ring", book.slot)
        if book.shelf:
            parts.append(f"It belongs on shelf {book.shelf}.")
        await self.say(" ".join(parts), "happy")

    async def act_recommend(self, book, title: str) -> None:
        if book is None or not book.found_in_catalog or self.finder is None:
            await self.say(f"I can only recommend from books in our catalogue, and {title} isn't in it.",
                           "neutral")
            return
        similar = self.finder.similar_to(book.isbn, 3)
        if not similar:
            await self.say(f"I couldn't find books like {title} in the catalogue.", "neutral")
            return
        names = [f"{b.title} by {b.author}" if b.author else b.title for b in similar]
        await self.say(f"If you like {title}, you might enjoy {join_names(names)}.", "happy")

    async def say(self, text: str, tag: Optional[str] = None) -> None:
        """Deliver a message in the current style and announce it for the face."""
        self.out.deliver(text, self.get_style())
        if self.publish is not None:
            await self.publish(ReplySentence(text, tag))
