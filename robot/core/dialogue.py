"""The interaction as a small state machine.

States and the events that move between them:

    idle        --person arrives, style proactive-->  invited      (robot invites them over)
    idle        --visitor speaks-->                   invited      (visitor started it)
    idle/invited/offering/closing --BookPlaced-->     book_placed  --> scanning (scan starts at once)
    scanning    --BookScanned-->                      offering     (offer: return, explain, recommend)
    scanning    --ScanFailed-->                       idle         (ask them to place the book flat)
    offering    --visitor says return/explain/recommend--> acting  --> closing (do it, then wrap up)
    offering    --anything else-->                    offering     (offer the three choices again)
    closing     --visitor speaks-->                   invited      (they want something else)
    offering/closing --anything else about the book-->  stays (answered by the LLM from the catalogue entry)
    closing     --visitor asks for another book-->    invited
    idle/invited/offering/closing --"where is <title>"-->  stays (says level, section and shelf; idle -> invited)
    idle/invited/offering/closing --"guide me"-->           stays (publishes NavigationRequested to the shelf)
    any         --visitor says bye/thanks-->          closing
    any         --everyone left (group_size 0)-->     idle

The hello is said once per person. When the camera tells people apart (ContextChanged.person_ids: person 1,
person 2...) a person who has been greeted is never greeted again, even after leaving the view and coming
back, and a new person is. When it can't (typed context, head count only), the hello is said once per visit
(until everyone has left) and at most once per `greet_cooldown_s`.

Every action reads the current StyleVector, so the same step is whispered and printed in a
quiet zone but spoken aloud in a common area (Actuators.deliver picks the channels).

The three choices are grounded in the catalogue: return gives the storage slot and where the book belongs
(level, section and shelf: facts, never the model's words), explain and recommend ask the LLM
(services/book_llm.py) to put the catalogue record, or up to three books the book graph found similar
(with its reasons why), into speech. Once a book is scanned, it stays the topic until the visitor leaves or
scans another: a follow-up (return/explain/recommend again, or a free question such as "who wrote it?") is
about that book and goes to the LLM with the conversation so far. If there is no LLM, the server is down or
the answer is unusable, they use the plain catalogue text instead. A book that is not in the catalogue never
goes to the model.

After a return or a recommendation the robot offers to guide the visitor to the shelf. "Guide me" (or yes)
publishes NavigationRequested with the shelf's map pose; "where is <title>" says where a book lives and
offers the same. A shelf without a map pose is described but not driven to.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from robot.persona import NAME
from robot.events import (BookPlaced, BookScanned, ContextChanged, NavigationRequested, ReplySentence, ScanFailed,
                          UserUtterance)
from robot.services.book_graph import Location

log = logging.getLogger("robot.dialogue")

STATES = ("idle", "invited", "book_placed", "scanning", "offering", "acting", "closing")
FREE_STATES = ("idle", "invited", "offering", "closing")       # the visitor can ask where a book is, or to be guided
GOODBYES = ("bye", "goodbye", "thanks", "thank you", "that's all", "thats all", "that is all", "no thanks",
            "that's everything", "thats everything", "that is everything", "that's it", "thats it",
            "nothing else", "nothing more", "no more", "i'm good", "im good", "i am good", "i'm done", "im done",
            "all set", "no questions", "don't have any question", "dont have any question",
            "do not have any question")
NO_WORDS = ("no", "nope", "nah", "nothing")          # a reply starting like this to "anything else?" is "I'm done"
QUESTION_WORDS = ("what", "who", "why", "how", "when", "where", "which", "can", "could", "is", "are", "do",
                  "does", "will", "would", "should")
ACTIONS = ("return", "explain", "recommend")
CHOICE_WORDS = {                    # what the visitor may say for each choice
    "return": ("return", "give back", "take back", "drop off"),
    "explain": ("explain", "about", "tell me", "summary", "describe", "synopsis", "plot", "storyline"),
    "recommend": ("recommend", "similar", "suggest", "something like", "related",
                  "other books", "more books", "read next", "what else"),
}
GUIDE_WORDS = ("guide me", "guide us", "take me to it", "take me there", "take us there", "lead me", "lead us",
               "walk me", "show me the way", "follow you")
WHERE_PATTERNS = (                  # (regex, True if the visitor also asks to be taken there); the title is `q`
    (r"\bwhere (?:does|do|would|should) (?P<q>.+?) (?:go|belong|live|sit)\b", False),
    (r"\b(?:guide|take|lead|walk) (?:me|us) to (?P<q>.+)", True),
    (r"\bshow me where (?P<q>.+?) is\b", False),
    (r"\bwhere(?:'s| is| are| can i find| can i get| do i find| would i find| might i find| do you keep)\s+(?P<q>.+)", False),
    (r"\b(?:find|locate|look for|looking for|search for)\s+(?:me\s+)?(?P<q>.+)", False),
)
THIS_BOOK = ("it", "this", "this book", "that", "that book", "this one", "that one", "here")
ANOTHER_BOOK_WORDS = ("another book", "next book", "different book", "new book", "one more book",
                      "scan another", "another one")
YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "please", "ok", "okay")
AUTHOR_WORDS = ("who wrote", "who is the author", "who's the author", "the author", "written by", "who made")
GENRE_WORDS = ("genre", "what kind of book", "what type of book", "what sort of book")
FOLLOW_UP_HELP = "I can tell you what it's about, suggest similar books, or return it."
NO_ANSWER = "Sorry, I'm not sure how to answer that. " + FOLLOW_UP_HELP
ACKNOWLEDGE = "Okay! If you'd like to look at another book, just place it on my pad."
SCAN_FAILED_MESSAGE = ("I couldn't read the barcode. Please place the book flat on the pad "
                       "with the barcode facing up.")
HELLO = f"Hello! I'm {NAME}. Put a book on my pad and I can return it, tell you about it, or suggest something similar."
GREETINGS = ("hi", "hello", "hey", "hiya", "yo", "good morning", "good afternoon", "good evening")
NAME_WORDS = ("your name", "who are you", "introduce yourself", "what are you", "what should i call you")
NAME_REPLY = f"I'm {NAME}, the library's book robot. Put a book on my pad and I can return it, tell you about it, or suggest something similar."
OFFER = "Would you like to return it, hear about it, or get a recommendation?"


def choice_from(text: str):
    """"return", "explain" or "recommend" if the visitor's words ask for one, else None."""
    text = text.lower()
    return next((c for c, words in CHOICE_WORDS.items() if any(w in text for w in words)), None)


def mentions(text: str, phrases) -> bool:
    """True if the text contains one of the phrases. Longer phrases also match with a typo or two ("thats
    everyhing"): the visitor is typing or speaking quickly and the meaning is plain."""
    if any(p in text for p in phrases):
        return True
    if is_question(text):                  # "is there anything else?" is not a typo of "nothing else"
        return False
    words = text.replace(",", " ").split()
    for p in phrases:
        n = len(p.split())
        if len(p) < 10:
            continue
        for i in range(len(words) - n + 1):
            if difflib.SequenceMatcher(None, " ".join(words[i:i + n]), p).ratio() >= 0.9:
                return True
    return False


def is_done(text: str, state: str) -> bool:
    """The visitor is finishing: a goodbye or thanks, or "no" / "no questions" after "anything else?"."""
    if mentions(text, GOODBYES):
        return True
    words = text.replace(",", " ").strip(" .!").split()
    return (state == "closing" and 0 < len(words) <= 8 and "?" not in text
            and words[0].strip(".,!") in NO_WORDS)


def is_question(text: str) -> bool:
    words = text.replace(",", " ").split()
    return "?" in text or (bool(words) and words[0] in QUESTION_WORDS)


def is_greeting(text: str) -> bool:
    """A bare hello ("hi", "hey Stuart", "good morning"): not "hi, what is your name?"."""
    words = text.replace(",", " ").strip(" .!?").split()
    return 0 < len(words) <= 3 and (words[0] in GREETINGS or " ".join(words[:2]) in GREETINGS)


def where_query(text: str) -> Optional[tuple]:
    """(what the visitor is asking about, True if they also want to be taken there) for "where is Dune?",
    "find me the hitchhiker's guide", "guide me to cosmos"; None if it isn't that kind of question."""
    for pattern, guide in WHERE_PATTERNS:
        if match := re.search(pattern, text):
            query = re.sub(r"[?!.,]+", " ", match.group("q"))
            query = re.sub(r"\b(?:please|for me|located|now|right now|exactly|book|novel)\b", " ", query)
            query = re.sub(r"^\s*(?:the|a|an|my)\s+(?=\w+\s+\w+)", "", " ".join(query.split()))
            if query.strip():
                return query.strip(), guide
    return None


def is_guide(text: str) -> bool:
    return any(w in text for w in GUIDE_WORDS)


def is_yes(text: str) -> bool:
    """A short yes ("yes", "yeah please", "ok sure"): not "sure, who wrote it?"."""
    words = text.replace(",", " ").strip(" .!?").split()
    return 0 < len(words) <= 3 and words[0].strip(".,!?") in YES_WORDS


def join_names(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass(frozen=True)
class GuideTarget:
    """Where "guide me" goes: a book and the shelf it lives on."""
    isbn: str
    title: str
    location: Optional[Location]

    @property
    def can_drive(self) -> bool:
        return self.location is not None and self.location.has_pose


def location_of(book: BookScanned) -> Location:
    """The Location the scanner put on a BookScanned (any part may be None)."""
    return Location(book.isbn, book.shelf, book.section, book.floor, book.zone, book.map_x, book.map_y, book.map_yaw)


class Dialogue:
    def __init__(self, actuators, get_style: Callable[[], object],
                 publish: Optional[Callable[[object], Awaitable[int]]] = None, finder=None, assistant=None,
                 greet_cooldown_s: float = 0.0, clock: Callable[[], float] = time.monotonic):
        self.out = actuators
        self.get_style = get_style
        self.publish = publish
        self.finder = finder                # services.books.BookFinder, for recommendations
        self.assistant = assistant          # services.book_llm.BookAssistant, or None for catalogue text only
        self.greet_cooldown_s = greet_cooldown_s
        self.clock = clock
        self.greeted = False                # said hello in this visit (reset when everyone has left)
        self.greeted_ids: set = set()       # people (person numbers) who have been greeted this run
        self.last_greeting: Optional[float] = None
        self.state = "idle"
        self.book = None
        self.guide_target: Optional[GuideTarget] = None     # set by a return, a recommendation or "where is X"
        self.awaiting_guide = False                         # the last thing said offered to guide them
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
            self.greeted = False
            self.guide_target, self.awaiting_guide = None, False
            if self.state != "idle":
                self.book = None
                if self.assistant is not None:
                    self.assistant.reset()
                self._go("idle", "everyone left")
            return
        if self.state == "idle" and self.get_style().proactive and self._may_greet(event.person_ids):
            self.greeted, self.last_greeting = True, self.clock()
            self.greeted_ids.update(event.person_ids)
            self._go("invited", "person arrived")
            await self.say(HELLO, "happy")

    def _may_greet(self, person_ids: tuple = ()) -> bool:
        if person_ids:                                  # people are told apart: greet whoever is new
            return any(p not in self.greeted_ids for p in person_ids)
        if self.greeted:
            return False
        return self.last_greeting is None or self.clock() - self.last_greeting >= self.greet_cooldown_s

    async def on_utterance(self, event: UserUtterance) -> None:
        text = event.text.strip().lower()
        if self.assistant is not None:
            self.assistant.hear(event.text.strip())
        was_awaiting, self.awaiting_guide = self.awaiting_guide, False
        if self.state != "idle" and is_done(text, self.state):
            self._go("closing", "visitor said goodbye")
            await self.say("You're welcome. Enjoy your reading!", "happy")
            return
        if self.state in FREE_STATES and await self.navigation_request(text, was_awaiting):
            return
        if self.state == "idle":
            self._go("invited", "visitor spoke")
            await self.talk_without_book(text, event.text.strip(), "Hi! Place a book on the pad and I'll help with it.")
        elif self.state in ("offering", "closing") and self.book is not None:
            await self.talk_about_book(text, event.text.strip())
        elif self.state == "closing":
            self._go("invited", "visitor wants more")
            await self.say("Sure. Place another book on the pad.", "happy")
        elif self.state == "invited":
            await self.talk_without_book(text, event.text.strip(), "Place a book on the pad and I'll help with it.")
        else:                                    # book_placed / scanning / acting: busy
            await self.say("One moment, please.", "neutral")

    # ---- where is it / guide me ----
    async def navigation_request(self, text: str, was_awaiting: bool) -> bool:
        """"Where is Dune?", "guide me", or yes to the offer to guide: handled here, True if so. Anything else
        (including "where is the bathroom", which names no book) is left to the rest of the dialogue."""
        asked = where_query(text)
        if asked is not None:
            query, take_me = asked
            book = self.find_book(query)
            if book is None:
                return False
            if self.state == "idle":
                self._go("invited", "visitor asked where")
            self.guide_target = self.target_for(book.isbn13, book.title)
            if take_me:
                await self.guide()
            else:
                await self.tell_where(self.guide_target)
            return True
        if is_guide(text) or (was_awaiting and is_yes(text)):
            if self.state == "idle":
                self._go("invited", "visitor asked to be guided")
            await self.guide()
            return True
        return False

    def find_book(self, query: str):
        """The catalogue book a "where is ..." means: a title (loosely spelt), an ISBN, or "this book"."""
        if query in THIS_BOOK:
            return self.catalogue_record(self.book)
        return self.finder.catalog.find_title(query) if self.finder is not None else None

    def target_for(self, isbn: str, title: str) -> GuideTarget:
        return GuideTarget(isbn, title, self.finder.locate(isbn) if self.finder is not None else None)

    async def tell_where(self, target: GuideTarget) -> None:
        where = target.location.describe() if target.location else ""
        if not where:
            reason = self.finder.locate_with_reason(target.isbn)[1] if self.finder is not None else ""
            log.info("[dialogue] no location for %s: %s", target.isbn, reason)
            await self.say(f"I don't know where {target.title} is shelved yet. The help desk can tell you.", "neutral")
            return
        offer = " Would you like me to guide you there?" if target.can_drive else ""
        self.awaiting_guide = target.can_drive
        await self.say(f"{target.title} is {where}.{offer}", "happy")

    async def guide(self) -> None:
        """Lead the visitor to the current target: say where, and publish NavigationRequested with the pose."""
        target = self.guide_target
        if target is None and self.book is not None and self.book.found_in_catalog:
            target = GuideTarget(self.book.isbn, self.book.title or "that book", location_of(self.book))
        if target is None:
            await self.say("I'm not sure where to take you yet. Ask me where a book is, or return a book or ask "
                           "for a recommendation first.", "neutral")
            return
        where = target.location.describe() if target.location else ""
        if not target.can_drive:
            tail = f" It's {where}." if where else " The help desk can tell you where it is."
            await self.say(f"I don't have a map position for {target.title} yet, so I can't take you there.{tail}", "neutral")
            return
        loc = target.location
        await self.say(f"Okay, follow me to {target.title}. It's {where}.", "happy")
        if self.publish is not None:
            await self.publish(NavigationRequested(target.isbn, target.title, loc.map_x, loc.map_y, loc.map_yaw,
                                                   shelf=loc.shelf, section=loc.section, floor=loc.floor, zone=loc.zone))

    def follow_up(self, action: str) -> str:
        """What the robot says after an action: after a return or a recommendation, an offer to guide."""
        target = self.guide_target
        if action in ("return", "recommend") and target is not None and target.can_drive:
            self.awaiting_guide = True
            return f"Would you like me to guide you to {target.title}, or is there anything else I can help with?"
        return "Is there anything else I can help with?"

    async def talk_without_book(self, text: str, said: str, nudge: str) -> None:
        """The visitor spoke with no book on the pad. A plain greeting gets the nudge; anything else goes to
        the LLM as small talk (it names no books), with who-are-you answered even if the LLM is down."""
        if is_greeting(text):
            intro = "" if self.greeted else f"I'm {NAME}. "      # not again if the hello already said it
            await self.say(f"Hi! {intro}Place a book on the pad and I'll help with it.", "happy")
            return
        answer = (await self.assistant.chat(said) if self.assistant is not None else None) or self.identity_fact(text)
        await self.say(answer or nudge, "happy" if answer else "neutral")

    @staticmethod
    def identity_fact(text: str) -> Optional[str]:
        return NAME_REPLY if any(w in text for w in NAME_WORDS) else None

    async def talk_about_book(self, text: str, said: str) -> None:
        """The visitor spoke while a book is the topic (offering, or closing after an answer)."""
        action = choice_from(text)
        if action is not None:
            self._go("acting", f"chose {action}")
            await self.act(action)
            self._go("closing", f"{action} done")
            await self.say(self.follow_up(action), "neutral")
            return
        if self.state == "closing" and (any(w in text for w in ANOTHER_BOOK_WORDS) or is_yes(text)):
            self._go("invited", "visitor wants more")
            await self.say("Sure. Place another book on the pad.", "happy")
            return
        answer = await self.answer_question(said) or self.identity_fact(text) or self.catalogue_fact(text)
        if answer:
            await self.say(answer, "happy")
        elif self.state == "offering":
            await self.say(OFFER, "neutral")
        elif is_question(text):
            await self.say(NO_ANSWER, "neutral")
        else:                                        # not a question and nothing to look up: just an acknowledgement
            await self.say(ACKNOWLEDGE, "happy")

    async def answer_question(self, question: str) -> Optional[str]:
        """The LLM's answer to a free question about the scanned book, from its catalogue entry; None if
        there is no LLM, the book isn't in the catalogue (it would answer from memory) or the server fails."""
        record = self.catalogue_record(self.book)
        if record is None or self.assistant is None:
            return None
        return await self.assistant.answer(record, question)

    def catalogue_fact(self, text: str) -> Optional[str]:
        """Who wrote it / what genre, straight from the catalogue, for when the LLM can't answer."""
        book = self.book
        if book is None or not book.title:
            return None
        if book.author and any(w in text for w in AUTHOR_WORDS):
            return f"{book.title} was written by {book.author}."
        record = self.catalogue_record(book)
        if record is not None and record.genre and any(w in text for w in GENRE_WORDS):
            return f"{book.title} is {record.genre}."
        return None

    async def on_book_placed(self, event: BookPlaced) -> None:
        if self.state in ("idle", "invited", "offering", "closing"):      # offering: they swapped the book
            self._go("book_placed", "book placed")
            self.out.lights.spotlight(True)
            self._go("scanning", "scan started")

    async def on_book_scanned(self, event: BookScanned) -> None:
        if self.state != "scanning":
            return
        had_book, self.book = self.book is not None, event
        self.guide_target = None
        if self.assistant is not None and had_book:
            self.assistant.reset()                  # another book starts a new conversation; the chat before
                                                    # the first book of a visit carries on
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
        self.guide_target = None
        if self.assistant is not None:
            self.assistant.reset()
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
            await self.act_explain(book, title)

    def catalogue_record(self, book):
        """The full catalogue Book for what was scanned (None if it is not in the catalogue)."""
        if book is None or not book.found_in_catalog or self.finder is None:
            return None
        return self.finder.catalog.lookup(book.isbn)

    async def act_explain(self, book, title: str) -> None:
        record = self.catalogue_record(book)
        if record is not None and self.assistant is not None:
            spoken = await self.assistant.explain(record)
            if spoken:
                await self.say(spoken, "happy")
                return
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
        place = location_of(book)
        if place.describe():
            parts.append(f"It belongs {place.describe()}.")
        self.guide_target = GuideTarget(book.isbn, title, place)
        await self.say(" ".join(parts), "happy")

    async def act_recommend(self, book, title: str) -> None:
        if book is None or not book.found_in_catalog or self.finder is None:
            await self.say(f"I can only recommend from books in our catalogue, and {title} isn't in it.",
                           "neutral")
            return
        picks = self.finder.recommend(book.isbn, self.get_style().recommendations)
        if not picks:
            await self.say(f"I couldn't find books like {title} in the catalogue.", "neutral")
            return
        for pick in picks:                                  # the full reasons go to the log (and to the LLM below)
            log.info("[dialogue] candidate %s (%.2f): %s", pick.book.title, pick.total, "; ".join(pick.reasons))
        if self.assistant is not None:
            spoken = await self.assistant.recommend(self.finder.catalog.lookup(book.isbn), picks)
            if spoken:
                best = self.first_mentioned(spoken, picks)
                self.guide_target = self.target_for(best.book.isbn13, best.book.title)
                await self.say(spoken, "happy")
                return
        best = picks[0]
        self.guide_target = self.target_for(best.book.isbn13, best.book.title)
        await self.say(self.recommendation_text(title, picks), "happy")

    @staticmethod
    def recommendation_text(title: str, picks: list) -> str:
        """The catalogue wording of a recommendation, with ONE reason (the top pick's), never a list of topics:
        "If you like Cosmos, you might enjoy A Brief History of Time by Stephen Hawking because the librarians
        suggest it after Cosmos." With several picks they are named first and the reason goes to the top one."""
        names = [f"{p.book.title} by {p.book.author}" if p.book.author else p.book.title for p in picks]
        best = picks[0]
        because = best.because or f"because it's similar to {title}"
        if len(picks) == 1:
            return f"If you like {title}, you might enjoy {names[0]} {because}."
        return f"If you like {title}, you might enjoy {join_names(names)}. I'd start with {best.book.title} {because}."

    @staticmethod
    def first_mentioned(spoken: str, picks: list):
        """The pick whose title comes first in the spoken recommendation (the LLM has to name one of them)."""
        low = spoken.lower()
        found = [(low.index(p.book.title.lower()), i, p) for i, p in enumerate(picks) if p.book.title.lower() in low]
        return min(found)[2] if found else picks[0]

    async def say(self, text: str, tag: Optional[str] = None) -> None:
        """Deliver a message in the current style and announce it for the face."""
        self.out.deliver(text, self.get_style())
        if self.assistant is not None:
            self.assistant.said(text)               # the LLM's memory is what was really said, canned lines too
        if self.publish is not None:
            await self.publish(ReplySentence(text, tag))
