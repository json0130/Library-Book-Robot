"""The robot's behaviour as a table-driven hierarchical state machine (see core/fsm.py for how the machine works and
docs/state_machine.md for the diagram, generated from the table below).

    IDLE        waiting; nobody is engaging
    ENGAGED     a visitor is here
      GREET          say hello (once per person; silent where the style is silent), then OFFER
      OFFER          return a book / ask about a book / get a recommendation
      AWAIT_BOOK     the choice needs a book and the pad is empty: ask for one
      SCAN           the BookReader is reading the book (BookPlaced in any engaged state comes here)
      STORE          take the book: pick a free slot, turn and light the ring, pulse the pad, wait for BookDropped
      ANSWER         explain the book / where it lives / guide there / answer a question (existing logic)
      RECOMMEND      recommend from the book graph, by the book on the pad or by a spoken or typed interest
      ANYTHING_ELSE  "anything else?": more help, done, or a follow-up
      FAREWELL       goodbye (silent in a quiet zone: eyes and light only), then IDLE or on to RETURNING
    RETURNING   putting the stored books back on their shelves
      PLAN           group the stored books by shelf (book_graph.locate) into a queue
      NAVIGATING     drive to the next shelf (simulated by a timer; NavigationRequested goes to the base)
      AT_SHELF       unload (simulated), free the slots, then the next shelf or HOMING
      ANNOUNCE       someone is near but busy: signal ("excuse me") without starting a conversation, then carry on
      HOMING         drive home, then IDLE

The zone (quiet or common) and fixed mode are not states. Every state asks the current StyleVector what to do: a style
with the speech channel speaks and chimes, one without stays silent and uses the screen, eyes and lights; a style with
touch_buttons shows option buttons. Where the visitor gets there from a screen button, the operator's terminal or the
/operator page, the event is the same as when it comes from a sensor, with a different `source`.

Waiting states (the ones that wait for the visitor or a sensor) all have a timeout that ends in FAREWELL, and leave
for FAREWELL on PersonLeft. After a session ends with books in storage the robot waits returning.auto_start_delay_s
(in FAREWELL, so a new person engaging cancels it) and then goes RETURNING. If a visitor engages during RETURNING the
trip is paused (the base stops) and, when that session ends, resumed from the same shelf with the same time left.

The conversation itself is unchanged: the same words end it ("thanks", "that's everything"), the same questions
go to the LLM grounded in the catalogue, "where is Dune" and "guide me" work, the hello is said once per person, and
a recommendation names one reason and (in a quiet zone) one book.
"""

from __future__ import annotations

import asyncio
import difflib
from contextlib import asynccontextmanager
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from robot.config import (ENGAGEMENT_DEFAULTS, INTENT_DEFAULTS, RETURNING_DEFAULTS, SCREEN_DEFAULTS, STORAGE_DEFAULTS,
                          TIMEOUTS_DEFAULTS)
from robot.core.fsm import Event, History, Machine, MachineSpec, Previous, StateDef, Timer, Transition
from robot.core.storage import ShelfStop, Storage, StoredBook
from robot.events import (ThinkingEnded, ThinkingStarted, BookDropped, BookFocused, BookIdentified, BookPlaced, ChoicesOffered, ExpressionRequested,
                          LocationResolved, NavigationRequested, PersonEngaged, PersonLeft, PersonNearbyBusy,
                          RecommendationsReady, ReplySentence, ReturningProgress, ScanFailed, StateEntered,
                          StateTransition, StoringBook, UserChoice, UserUtterance)
from robot.core.expectations import expect_anything_else, expect_choice, expect_guide, expect_lend, match_answers
from robot.core.intents import POLITE, STOP, IntentRouter, edit_cost, normalise
from robot.services.places import FACILITIES, Place, PlaceResolver
from robot.persona import NAME
from robot.services.book_graph import Location
from robot.services.books import title_key

log = logging.getLogger("robot.dialogue")
state_log = logging.getLogger("robot.state")

# ---------------------------------------------------------------------------------------------- words
GOODBYES = ("bye", "goodbye", "thanks", "thank you", "that's all", "thats all", "that is all", "no thanks",
            "that's everything", "thats everything", "that is everything", "that's it", "thats it",
            "nothing else", "nothing more", "no more", "i'm good", "im good", "i am good", "i'm done", "im done",
            "all set", "no questions", "don't have any question", "dont have any question",
            "do not have any question")
NO_WORDS = ("no", "nope", "nah", "nothing")          # a reply starting like this to "anything else?" is "I'm done"
QUESTION_WORDS = ("what", "who", "why", "how", "when", "where", "which", "can", "could", "is", "are", "do",
                  "does", "will", "would", "should")
CHOICE_WORDS = {                    # what the visitor may say for each choice (the order is the order they are tried)
    "return": ("return", "give back", "take back", "drop off"),
    "recommend": ("recommend", "similar", "suggest", "something like", "related",
                  "other books", "more books", "read next", "what else"),
    "explain": ("explain", "about", "tell me", "summary", "describe", "synopsis", "plot", "storyline"),
}
GUIDE_WORDS = ("guide me", "guide us", "take me to it", "take me there", "take us there", "lead me", "lead us",
               "walk me", "show me the way", "follow you")
WHERE_PATTERNS = (                  # (regex, True if the visitor also asks to be taken there); the title is `q`
    (r"\bwhere (?:does|do|would|should) (?P<q>.+?) (?:go|belong|live|sit)\b", False),
    (r"\b(?:guide|take|lead|walk) (?:me|us) to (?P<q>.+)", True),
    (r"\bshow me where (?P<q>.+?) is\b", False),
    (r"\bwhere(?:'s| is| are| can i find| can i get| do i find| would i find| might i find| do you keep)\s+(?P<q>.+)", False),
    (r"\b(?:find|locate|look for|looking for|search for)\s+(?:me\s+)?(?P<q>.+)", False),
    (r"\bwhere\s+(?:the\s+|a\s+)?(?P<q>.+)", False),                # loosely typed: "where the brief history book?"
)
INTEREST_PATTERNS = (               # what a visitor is interested in, said in passing or as a request
    r"\b(?:find|get|give|show)\s+me\s+(?:a\s+|some\s+)?(?:books?|novels?|something|anything)\s+(?:about|on|like|involving|with)\s+(?P<i>.+)$",
    r"\b(?:recommend|suggest|any|some|something|anything|got|have you got)\b.*?\b(?:about|on|like|involving|featuring|regarding|with)\s+(?P<i>.+)$",
    r"\bi(?:'m| am)?\s+(?:really\s+)?(?:like|love|enjoy|into|interested in|keen on|fond of|looking for something about"
    r"|want something about|want a book about)\s+(?P<i>.+)$",
)
STORAGE_PATTERN = (r"\bstorage\b|\bslots?\b|\b(?:holding|carrying|keeping)\b.*\bbooks?\b|"
                   r"\bwhat books? (?:do you have|are you holding|have you got)\b|\bon board\b|"
                   r"\b(?:have|hold|got) any (?:returned )?books\b|\bbooks? (?:do you have|are you holding)\b|"
                   r"\bwhich books? (?:do|are|have) (?:you|u)\b.*\b(?:hold|holding|carry|carrying|got|have|storing)\b")
HOLDING_VERBS = ("have", "hold", "holding", "carry", "carrying", "keep", "keeping", "got")
PAD_PATTERN = r"\bpad\b|\bin (?:your|ur|my) hands?\b"
# "which book do you hold rn?", "do you have any book with u?": the robot's OWN books (its pad, its storage), not the library's
HOLDINGS_CUES = (r"\bwith (?:you|u|ya)\b|\bon (?:you|u|board)\b|\b(?:on|in) (?:your|ur) (?:pad|storage|slots?|ring)\b|\byour (?:pad|storage|slots?)\b|"
                 r"\bright now\b|\brn\b|\bat the moment\b|\bcurrently\b|\bon hand\b|\binside you\b|"
                 r"\b(?:you|u) (?:are |r )?(?:holding|carrying|keeping|hold|carry)\b")
PICK_ME = (r"\bjust (?:recommend|suggest|give|pick|choose|one|anything|something)\b|\bsurprise\b|\byou (?:pick|choose|decide)\b|\byour (?:choice|pick)\b|\bup to you\b|\bwhatever\b|\bnot sure\b|"
           r"\bno idea\b|\bidk\b|\bdunno\b|\bdon'?t know\b|\bany ?(?:one|book)\b|\brandom\b|\banything (?:is )?(?:fine|good|ok|okay)\b")
CHANGED_MIND = (r"\bnever ?mind\b|\bnvm\b|\bforget it\b|\bchanged my mind\b|\bdon'?t (?:need|want)\b|\bdo not (?:need|want)\b|"
                r"\bno longer (?:need|want)\b|\btake (?:it|that) back\b|\byou can (?:have|keep) (?:it|that)\b|\bgive it back\b|\bput it back\b")
HOW_TO_ASK = "Of course! Ask me about a book, ask where something is, or ask for a recommendation."
THIS_BOOK = ("it", "this", "this book", "that", "that book", "this one", "that one", "here")
ANOTHER_BOOK_WORDS = ("another book", "next book", "different book", "new book", "one more book",
                      "scan another", "another one")
YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "please", "ok", "okay")
AUTHOR_WORDS = ("who wrote", "who is the author", "who's the author", "the author", "written by", "who made")
GENRE_WORDS = ("genre", "what kind of book", "what type of book", "what sort of book")
GREETINGS = ("hi", "hello", "hey", "hiya", "yo", "good morning", "good afternoon", "good evening")
NAME_WORDS = ("your name", "who are you", "introduce yourself", "what are you", "what should i call you")

REPAIR_STATES = ("OFFER", "AWAIT_BOOK", "ANYTHING_ELSE")
HELLO = f"Hello! My name is {NAME}."
LOOSE_MIN_SCORE = 0.03            # when nothing reaches books.similarity.min_score: the nearest books above this
RETURNING_OPENING = "Hi! I'm returning some books, can I help you with anything?"
FOLLOW_UP_HELP = "I can tell you what it's about, suggest similar books, or return it."
NO_ANSWER = "Sorry, I'm not sure how to answer that. " + FOLLOW_UP_HELP
ACKNOWLEDGE = "Okay! If you'd like to look at another book, just place it on my pad."
SCAN_FAILED_MESSAGE = ("I couldn't read the barcode. Please place the book flat on the pad "
                       "with the barcode facing up.")
NAME_REPLY = f"I'm {NAME}, the library's book robot. I can take back a book, tell you about one, or recommend something."
OFFER = "Would you like to return it, hear about it, or get a recommendation?"
OFFER_NO_BOOK = ("I can take back a book, tell you about one, recommend something, or show you where a topic is. "
                 "What would you like?")
OFFER_AFTER_FAILURES = ("I still can't read that barcode, but I can tell you where a book is or recommend something. "
                        "What would you like?")
GOODBYE_SPOKEN = "Goodbye! Enjoy your reading."
THANKS_SPOKEN = "You're welcome. Enjoy your reading!"


def choice_from(text: str):
    """"return", "recommend" or "explain" if the visitor's words ask for one, else None."""
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


ASKS_FOR_MORE = re.compile(r"\?|\b(?:can|could|may|will|would) (?:you|i|we)\b|\b(?:tell|show|give|guide) me\b|\b(?:recommend|suggest)\b"
                          r"|\b(?:where|what|which|who|how|why)\b")


def is_done(text: str, state: str) -> bool:
    """The visitor is finishing: a goodbye or thanks, or "no" / "no questions" after "anything else?" (state "closing")."""
    if mentions(text, GOODBYES) or text.strip(" .!?") in ("done", "finished"):
        return not ASKS_FOR_MORE.search(text)         # "thank you, can you recommend me a book?" is thanks AND a request
    words = text.replace(",", " ").strip(" .!").split()
    if not (state == "closing" and 0 < len(words) <= 8 and "?" not in text and words[0].strip(".,!") in NO_WORDS):
        return False
    rest = " ".join(words[1:])                    # "no, can you tell me about the Martian" asks for something else
    return not (choice_from(rest) or is_question(rest) or re.search(
        r"\b(?:can|could|tell|show|where|what|which|recommend|explain|about|take|guide|find|another|other)\b", rest))


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
            query = re.sub(r"\b(?:please|for me|located|now|right now|exactly|books?|novels?|in this library|"
                           r"in the library|here)\b", " ", query)
            query = re.sub(r"^\s*(?:the|a|an|my)\s+(?=\w+\s+\w+)", "", " ".join(query.split()))
            if query.strip():
                return query.strip(), guide
    return None


def interest_from(text: str) -> str:
    """What a visitor says they are interested in ("something about space" -> "space"), or "" if they say nothing.
    "I like X" only counts as a statement, not inside a question about the book on the pad ("would I like it if I
    enjoy funny stories?")."""
    for i, pattern in enumerate(INTEREST_PATTERNS):
        if i == len(INTEREST_PATTERNS) - 1 and is_question(text):
            continue
        if match := re.search(pattern, text):
            interest = re.sub(r"[?!.,]+", " ", match.group("i"))
            interest = re.sub(r"\b(?:please|for me|to read|books?|novels?|stuff)\b", " ", interest)
            interest = " ".join(interest.split())
            if interest and interest not in ("it", "this", "that", "him", "her"):
                return interest
    return ""


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


def floor_phrase(floor) -> str:
    """"on the ground floor", "on level 2": a floor's name in a sentence."""
    return floor.name.lower() if floor.name.lower().startswith("level") else f"the {floor.name[0].lower()}{floor.name[1:]}"


def location_of(book: BookIdentified) -> Location:
    """The Location the book reader put on a BookIdentified (any part may be None)."""
    return Location(book.isbn, book.shelf, book.section, book.floor, book.zone, book.map_x, book.map_y, book.map_yaw)


# ---------------------------------------------------------------------------------------------- the table
S = StateDef
STATES = [
    S("IDLE", enter=("enter_idle",), doc="waiting; nobody is engaging"),
    S("ENGAGED", initial="GREET", exit=("exit_engaged",), doc="a visitor is here"),
    S("GREET", parent="ENGAGED", enter=("enter_greet",), doc="hello, once per person"),
    S("OFFER", parent="ENGAGED", enter=("enter_offer",), timers=(Timer("t_offer"),), waiting=True,
      doc="return a book, ask about a book, or get a recommendation"),
    S("AWAIT_BOOK", parent="ENGAGED", enter=("enter_await_book",), timers=(Timer("t_await_book"),), waiting=True,
      doc="the choice needs a book: ask for one"),
    S("SCAN", parent="ENGAGED", enter=("enter_scan",), timers=(Timer("t_scan"),), waiting=True,
      doc="the BookReader is reading the book"),
    S("STORE", parent="ENGAGED", enter=("enter_store",), exit=("exit_store",),
      timers=(Timer("t_store"), Timer("t_sim_drop", "BookDropped")), waiting=True,
      doc="slot, ring, light, pad pulse; wait for BookDropped"),
    S("ANSWER", parent="ENGAGED", enter=("enter_answer",), timers=(Timer("t_answer"),), waiting=True,
      doc="explain, where, guide, free questions"),
    S("RECOMMEND", parent="ENGAGED", enter=("enter_recommend",), doc="from the book graph"),
    S("ANYTHING_ELSE", parent="ENGAGED", enter=("enter_anything_else",), timers=(Timer("t_anything_else"),),
      waiting=True, doc="more help or done"),
    S("FAREWELL", parent="ENGAGED", enter=("enter_farewell",), timers=(Timer("t_farewell"),),
      doc="goodbye, then IDLE or RETURNING"),
    S("RETURNING", initial="PLAN", doc="putting the stored books back"),
    S("PLAN", parent="RETURNING", enter=("enter_plan",), transient=True, doc="group the stored books by shelf"),
    S("NAVIGATING", parent="RETURNING", enter=("enter_navigating",), timers=(Timer("t_drive", "arrived"),),
      doc="driving to the next shelf (simulated)"),
    S("AT_SHELF", parent="RETURNING", enter=("enter_at_shelf",), timers=(Timer("t_unload", "unloaded"),),
      doc="unloading (simulated)"),
    S("ANNOUNCE", parent="RETURNING", enter=("enter_announce",), timers=(Timer("t_announce", "announced"),),
      transient=True, doc="excuse me: signal without starting a conversation"),
    S("HOMING", parent="RETURNING", enter=("enter_homing",), timers=(Timer("t_drive", "home"),),
      doc="driving home (simulated)"),
]

T = Transition
TRANSITIONS = [
    # ---- IDLE
    T("IDLE", "PersonEngaged", "GREET", action="begin_session"),
    T("IDLE", "BookPlaced", "SCAN", action="begin_session_with_book"),
    T("IDLE", "Utterance", "GREET", action="begin_session_from_text", note="speech means a person is here"),
    # ---- ENGAGED: what applies to all its children (the children below shadow these where they need to)
    T("ENGAGED", "PersonLeft", None, guard="recently_active", action="doubt_left",
      note="the camera lost them but they just spoke or tapped: wait for leave_grace_s of quiet"),
    T("ENGAGED", "PersonLeft", "FAREWELL"),
    T("ENGAGED", "timeout", None, guard="person_present", action="keep_waiting",
      note="still there, just quiet: wait again, no goodbye"),
    T("ENGAGED", "timeout", "FAREWELL"),
    T("ENGAGED", "BookPlaced", "SCAN", action="book_placed"),
    T("ENGAGED", "Choice", "STORE", guard="return_with_book", action="choose"),
    T("ENGAGED", "Choice", "AWAIT_BOOK", guard="choice_return", action="choose"),
    T("ENGAGED", "Choice", "ANSWER", guard="ask_ready", action="choose"),
    T("ENGAGED", "Choice", "AWAIT_BOOK", guard="choice_ask", action="choose"),
    T("ENGAGED", "Choice", "RECOMMEND", guard="recommend_ready", action="choose"),
    T("ENGAGED", "Choice", "AWAIT_BOOK", guard="choice_recommend", action="choose"),
    T("ENGAGED", "Choice", "FAREWELL", guard="choice_done", action="choose"),
    T("ENGAGED", "Choice", "OFFER", guard="choice_more", action="choose"),
    T("ENGAGED", "Choice", "AWAIT_BOOK", guard="choice_another", action="another_book"),
    T("ENGAGED", "Choice", None, guard="choice_guide", action="guide_now"),
    T("ENGAGED", "Utterance", "ANSWER", guard="has_book", action="question_from_text"),
    T("ENGAGED", "Utterance", None, action="chat"),
    # ---- GREET
    T("GREET", "greeted", "OFFER"),
    T("OFFER", "Utterance", None, guard="book_statement", action="repeat_offer", note="not a question"),
    T("GREET", "Choice", None),
    T("GREET", "Utterance", None),
    # ---- AWAIT_BOOK
    T("AWAIT_BOOK", "Utterance", None, action="chat"),
    # ---- SCAN
    T("SCAN", "BookIdentified", "STORE", guard="intent_return", action="on_identified"),
    T("SCAN", "BookIdentified", "ANSWER", guard="intent_ask", action="on_identified"),
    T("SCAN", "BookIdentified", "RECOMMEND", guard="intent_recommend", action="on_identified"),
    T("SCAN", "BookIdentified", "OFFER", action="on_identified"),
    T("SCAN", "ScanFailed", "AWAIT_BOOK", guard="first_failure", action="on_scan_failed"),
    T("SCAN", "ScanFailed", "OFFER", action="on_scan_failed"),
    T("SCAN", "BookPlaced", None),
    T("SCAN", "Choice", None),
    T("SCAN", "Utterance", None, action="say_busy"),
    # ---- STORE
    T("STORE", "BookDropped", None, action="commit_store", note="the book is stored"),
    T("STORE", "stored", "ANYTHING_ELSE", note="after BookDropped"),
    T("STORE", "full", "ANYTHING_ELSE"),
    T("STORE", "rejected", "ANYTHING_ELSE"),
    T("STORE", "BookPlaced", None),
    T("STORE", "Choice", None),
    T("STORE", "Utterance", None, action="say_busy"),
    # ---- ANSWER
    T("ANSWER", "answered", "ANYTHING_ELSE"),
    T("ANSWER", "Utterance", "ANSWER", action="question_from_text"),
    # ---- RECOMMEND
    T("RECOMMEND", "recommended", "ANYTHING_ELSE"),
    T("RECOMMEND", "Choice", None),
    T("RECOMMEND", "Utterance", None),
    # ---- FAREWELL
    T("FAREWELL", "PersonEngaged", "GREET", action="begin_session"),
    T("FAREWELL", "Utterance", "GREET", action="begin_session_from_text"),
    T("FAREWELL", "PersonLeft", None),
    T("FAREWELL", "Choice", None),
    T("FAREWELL", "timeout", History("RETURNING"), guard="resume_paused", action="resume_returning",
      note="after the delay"),
    T("FAREWELL", "timeout", "RETURNING", guard="auto_return", action="start_returning", note="after the delay"),
    T("FAREWELL", "timeout", "IDLE"),
    # ---- RETURNING
    T("RETURNING", "PersonEngaged", "GREET", action="pause_returning"),
    T("RETURNING", "BookPlaced", "SCAN", action="pause_returning_for_book"),
    T("PLAN", "planned", "NAVIGATING", guard="has_stops"),
    T("PLAN", "planned", "HOMING"),
    T("NAVIGATING", "arrived", "AT_SHELF"),
    T("NAVIGATING", "PersonNearbyBusy", "ANNOUNCE"),
    T("NAVIGATING", "replan", "PLAN"),
    T("AT_SHELF", "unloaded", "NAVIGATING", guard="more_stops", action="finish_unload"),
    T("AT_SHELF", "unloaded", "HOMING", action="finish_unload"),
    T("AT_SHELF", "PersonNearbyBusy", "ANNOUNCE"),
    T("ANNOUNCE", "announced", Previous("RETURNING")),
    T("HOMING", "home", "IDLE", action="finish_returning"),
    T("HOMING", "replan", "PLAN"),
]

SPEC = MachineSpec(STATES, TRANSITIONS, initial="IDLE")

EVENT_LABELS = {"PersonEngaged": "Person engages", "PersonNearbyBusy": "Person nearby, busy", "PersonLeft": "Person leaves",
                "BookPlaced": "Book placed", "BookDropped": "Book dropped", "BookIdentified": "Book read",
                "ScanFailed": "Scan failed", "timeout": "Timeout", "Utterance": "Visitor says something",
                "Choice": "Visitor chooses"}
EVENT_WHY = {"PersonLeft": "they walked away", "timeout": "nobody answered in time", "PersonEngaged": "someone stepped up",
             "PersonNearbyBusy": "someone is near but not engaging", "BookPlaced": "a book was put on the pad",
             "BookDropped": "the pad dropped the book into its slot", "BookIdentified": "the barcode was read",
             "ScanFailed": "no barcode could be read", "greeted": "the hello is done", "stored": "the book is in its slot",
             "answered": "the answer was given", "recommended": "the recommendation was given",
             "planned": "the shelves are planned", "arrived": "the drive timer ended", "unloaded": "the unload timer ended",
             "announced": "the announcement is over", "home": "the drive home ended", "full": "no free slot",
             "rejected": "not in the catalogue"}
OPERATOR_FOR = {"PersonEngaged": ("engage", ""), "PersonNearbyBusy": ("busy", ""), "PersonLeft": ("leave", ""),
                "BookPlaced": ("book", ""), "BookDropped": ("dropped", "")}
GUARD_PICK = {"return_with_book": "return", "choice_return": "return", "ask_ready": "ask", "choice_ask": "ask",
              "recommend_ready": "recommend", "choice_recommend": "recommend", "choice_done": "done",
              "choice_more": "more", "choice_another": "another"}
GUARD_WHY = {"return_with_book": "a book is on the pad", "choice_return": "no book yet: asks for one",
             "ask_ready": "a book is on the pad, or a title was named", "choice_ask": "needs a book first",
             "recommend_ready": "a book is on the pad, or they said what they like", "choice_recommend": "needs a book first",
             "choice_done": "they are finished", "choice_more": "they want more help", "recently_active": "they spoke a moment ago: the camera just lost them", "choice_another": "another book",
             "intent_return": "they chose to return it", "intent_ask": "they chose to ask about it",
             "intent_recommend": "they chose a recommendation", "first_failure": "the first failed read: try again",
             "has_book": "a book is on the pad", "resume_paused": "a trip was paused: carry on at the same shelf",
             "auto_return": "books are in storage: start putting them back", "has_stops": "there are shelves to visit",
             "more_stops": "more shelves to visit", "book_statement": "not a question: repeat the choices"}

SCREENS = {                          # state -> (layout, title): placeholder layouts, to be designed
    "IDLE": ("full_face", ""), "GREET": ("full_face", "Hello"), "OFFER": ("split", "How can I help?"),
    "AWAIT_BOOK": ("split", "Place the book on the pad"), "SCAN": ("split", "Reading the book"),
    "STORE": ("split", "Taking your book"), "ANSWER": ("split", "About this book"),
    "RECOMMEND": ("split", "Something to read"), "ANYTHING_ELSE": ("split", "Anything else?"),
    "FAREWELL": ("full_face", "Goodbye"), "PLAN": ("full_face", "Planning"),
    "NAVIGATING": ("split", "Returning books"), "AT_SHELF": ("split", "Putting books back"),
    "ANNOUNCE": ("split", "Excuse me"), "HOMING": ("full_face", "Heading home"),
}

TIMER_SETTINGS = {                   # timer method -> (settings section, key)
    "t_offer": ("timeouts", "offer_s"), "t_await_book": ("timeouts", "await_book_s"),
    "t_scan": ("timeouts", "scan_s"), "t_store": ("timeouts", "store_s"), "t_answer": ("timeouts", "answer_s"),
    "t_anything_else": ("timeouts", "anything_else_s"), "t_drive": ("returning", "sim_drive_s"),
    "t_unload": ("returning", "sim_unload_s"), "t_announce": ("returning", "announce_s"),
    "t_sim_drop": ("storage", "sim_drop_s"),
}


# ---------------------------------------------------------------------------------------------- the handler
@dataclass
class ChoiceInfo:
    """What the visitor chose or said, for the machine's Choice and Utterance events."""
    name: str                            # return | ask | recommend | done | more | another | guide ("" for free text)
    text: str = ""
    question: str = ""                   # for ask: what they asked ("who wrote it?", "where is Dune")
    interest: str = ""                   # for recommend: what they said they like
    isbn: str = ""                       # a catalogue book they named ("tell me about Dune"): no book on the pad is needed
    explicit: bool = False               # they picked that book on the screen: it becomes the topic even over the pad's
    general: bool = False                # a recommendation not about the book in hand ("any other books?")
    place: Optional[object] = None       # services.places.Place the router found (a section, a floor, a book, or several to choose from)
    take_me: bool = False                # they asked to be taken there, not only told where
    area: bool = False                   # they asked about a section or topic area ("where are the history books")
    pick: bool = False                   # for recommend: no topic and no book, just "recommend me one": the robot chooses


@dataclass
class Session:
    """One visit's state. Nothing here goes into the book graph or the storage file."""
    person_ids: tuple = ()
    intent: Optional[str] = None         # what they chose before the book was on the pad
    book: Optional[BookIdentified] = None
    virtual: bool = False                # `book` is one they named, not one on the pad (so it can't be returned)
    question: Optional[str] = None
    interest: Optional[str] = None
    scan_failures: int = 0
    offer_said: bool = False             # the opening line already offered help
    retry_message: Optional[str] = None
    replay: Optional[str] = None         # something they said before the machine was in OFFER: handled once there
    last_action: Optional[str] = None
    repeated_offer: bool = False
    recommend_failed: bool = False       # nothing was found like the book: "any other?" means something else
    place: Optional[object] = None       # the place to tell them about, found by the router
    take_me: bool = False
    misses: int = 0                      # utterances in a row the robot could not act on in this state
    repair: bool = False                 # the choices are shown as buttons because it did not understand
    awaiting_interest: bool = False      # it asked "what kind of books do you like?" outside AWAIT_BOOK
    focus: str = ""                      # ISBN of the book it just recommended: what "that book" means next
    pick: bool = False                   # the recommendation to make is the robot's own choice (no topic, no book)
    offered: set = field(default_factory=set)     # ISBNs it has suggested this visit: "another one" is a different book
    handed_in: set = field(default_factory=set)   # ISBNs of books returned during this visit: not on offer again
    expect: Optional[object] = None      # core/expectations.Expectation: the answers to the question it just asked (next utterance only)


@dataclass
class Settings:
    timeouts: dict = field(default_factory=lambda: dict(TIMEOUTS_DEFAULTS))
    returning: dict = field(default_factory=lambda: {**RETURNING_DEFAULTS, "home": dict(RETURNING_DEFAULTS["home"])})
    storage: dict = field(default_factory=lambda: dict(STORAGE_DEFAULTS))
    engagement: dict = field(default_factory=lambda: dict(ENGAGEMENT_DEFAULTS))
    intents: dict = field(default_factory=lambda: dict(INTENT_DEFAULTS))
    screen: dict = field(default_factory=lambda: {**SCREEN_DEFAULTS, "greeting_hold_s": 0})     # no hold unless configured

    @classmethod
    def from_config(cls, cfg) -> "Settings":
        return cls(cfg.timeouts, cfg.returning, cfg.storage, cfg.engagement, cfg.intents, cfg.screen)


class Dialogue:
    def __init__(self, actuators, get_style: Callable[[], object],
                 publish: Optional[Callable[[object], Awaitable[int]]] = None, finder=None, assistant=None,
                 greet_cooldown_s: float = 0.0, clock: Callable[[], float] = time.monotonic,
                 storage: Optional[Storage] = None, settings: Optional[Settings] = None,
                 get_zone: Callable[[], str] = lambda: "common", plan=None):
        self.out = actuators
        self.get_style = get_style
        self.get_zone = get_zone
        self.publish = publish
        self.finder = finder                # services.books.BookFinder: catalogue, graph, locations
        self.plan = plan                    # services.floorplan.FloorPlan, or None
        self.places = PlaceResolver(finder, plan)
        self.router = IntentRouter(finder, plan, (settings.intents if settings else None))
        self._thinking = 0                  # nested "waiting for the LLM" sections
        self.thinking_text = "Let me check."
        self.last_choices: list = []        # the ids of the last list or options shown ("the second one")
        self.assistant = assistant          # services.book_llm.BookAssistant, or None for catalogue text only
        self.greet_cooldown_s = greet_cooldown_s
        self.clock = clock
        self.settings = settings or Settings()
        self.storage = storage if storage is not None else Storage(self._ring_slots())
        self._entering = False              # between leaving a state and the machine reporting the new one
        self.present = False                # someone has engaged and not left (a quiet visitor is not a gone one)
        self.last_activity: Optional[float] = None    # when the visitor last spoke, typed, tapped or placed a book
        self.leave_pending = False          # the camera said they left while they were active: check again later
        self.greeted = False                # said hello in this visit (reset when the session ends)
        self.session = Session()
        self.guide_target: Optional[GuideTarget] = None     # set by a return, a recommendation or "where is X"
        self._awaiting_guide = False                        # the last thing said offered to guide them
        self.stops: list[ShelfStop] = []                    # the returning trip: the shelves still to visit
        self.current_shelf: Optional[str] = None            # the shelf it is heading for or at
        self.returning_paused = False                       # a trip was interrupted and is waiting to resume
        self.interrupted_returning = False                  # the person who engaged interrupted a trip
        self.lent_slot: Optional[int] = None                # the slot lit for a visitor who took their book out
        self.lent_book = None                               # ...and which StoredBook it was: it is NOT in storage while they hold it
        self.store_slot: Optional[int] = None               # the slot reserved for the book being stored
        self._stored = False
        self._screen_text = ""
        self.last_said = ""
        self.transitions: list[tuple[str, str, str]] = []    # (from path, event, to path), for tests and debugging
        self.fsm = Machine(SPEC, self, clock=lambda: self.clock(), on_transition=self._on_transition,
                           on_entered=self._on_entered)

    def _ring_slots(self) -> int:
        configured = self.settings.storage.get("slots")
        if configured:
            return int(configured)
        from robot.actuation.storage_ring import DEFAULT_SLOTS
        return DEFAULT_SLOTS

    # ---- timers: the seconds a state waits come from config/robot.yaml ----
    def __getattr__(self, name):
        if name in TIMER_SETTINGS:
            section, key = TIMER_SETTINGS[name]

            def seconds(event, section=section, key=key):
                if section == "timeouts" and not self.settings.timeouts.get("enabled", True):
                    return None                       # waiting states wait for the visitor (or for them to leave)
                value = getattr(self.settings, section).get(key)
                return float(value) if value else None
            return seconds
        raise AttributeError(name)

    def t_farewell(self, event) -> float:
        """The goodbye lasts farewell_s; when books are waiting to go back, it also holds the auto-start delay, and
        a person engaging in that time starts a new session instead."""
        wait = float(self.settings.timeouts.get("farewell_s", 3))
        if not self.resume_paused(event) and self.auto_return(event):      # a paused trip resumes without the delay
            wait += float(self.settings.returning.get("auto_start_delay_s", 10))
        return wait

    # ---- where it is ----
    @property
    def state(self) -> str:
        """The state path, e.g. "ENGAGED.OFFER"."""
        return self.fsm.path or "IDLE"

    @property
    def leaf(self) -> str:
        return self.fsm.leaf or "IDLE"

    @property
    def book(self) -> Optional[BookIdentified]:
        """The book on the pad that was read (None if the pad is empty or the book is gone)."""
        return self.session.book

    def style_speaks(self) -> bool:
        return "speech" in self.get_style().channels

    async def start(self) -> None:
        if self.fsm.leaf is None:
            await self.fsm.start()

    async def tick(self) -> int:
        """Fire the machine's timers that are due. The runtime calls this a few times a second."""
        await self.start()
        if self.leave_pending and not self.present and self.fsm.is_in("ENGAGED") and not self.recently_active(
                Event("PersonLeft", source="sensor")):
            self.leave_pending = False                     # quiet for leave_grace_s and still no face: they did go
            await self.fsm.dispatch("PersonLeft", None, "sensor")
        return await self.fsm.tick()

    def active(self) -> None:
        self.last_activity = self.clock()

    def attach(self, bus) -> None:
        bus.subscribe(PersonEngaged, lambda e: self._feed("PersonEngaged", e, e.source))
        bus.subscribe(PersonNearbyBusy, lambda e: self._feed("PersonNearbyBusy", e, e.source))
        bus.subscribe(PersonLeft, lambda e: self._feed("PersonLeft", e, e.source))
        bus.subscribe(BookPlaced, lambda e: self._feed("BookPlaced", e, e.source))
        bus.subscribe(BookIdentified, lambda e: self._feed("BookIdentified", e, "sensor"))
        bus.subscribe(ScanFailed, lambda e: self._feed("ScanFailed", e, "sensor"))
        bus.subscribe(BookDropped, lambda e: self._feed("BookDropped", e, e.source))
        bus.subscribe(UserChoice, self.on_choice)
        bus.subscribe(UserUtterance, self.on_utterance)

    async def _feed(self, name: str, payload, source: str) -> None:
        await self.start()
        if name == "PersonEngaged":
            self.present, self.leave_pending = True, False
        elif name == "PersonLeft":
            self.present = False
        elif name == "BookPlaced":
            self.active()
        await self.fsm.dispatch(name, payload, source)

    @property
    def awaiting_guide(self) -> bool:
        return self._awaiting_guide

    @awaiting_guide.setter
    def awaiting_guide(self, value: bool) -> None:
        """Offering to guide also sets what a short reply to it can mean (core/expectations.py); taking it back clears it."""
        self._awaiting_guide = bool(value)
        if value:
            self.session.expect = expect_guide()
        elif self.session.expect is not None and self.session.expect.kind == "guide":
            self.session.expect = None

    # ---- what the visitor chose or said ----
    async def on_choice(self, event: UserChoice) -> None:
        """A button on the screen, or the operator picking for the visitor."""
        await self.start()
        self.active()
        choice = event.choice.strip()
        self.session.expect = None
        if self.leaf in ("OFFER", "ANYTHING_ELSE", "AWAIT_BOOK", "ANSWER") or choice == "done":
            self.awaiting_guide = False
        await self.fsm.dispatch("Choice", self.choice_info(choice), event.source)

    def choice_info(self, choice: str) -> ChoiceInfo:
        """A button's id as a ChoiceInfo. Besides the plain choices (return, ask, recommend, more, guide, done) the
        content views send `book:<isbn>` (open that book), `where:<isbn>` (where it is), `guide:<isbn>` (take me to
        it), `similar:<isbn>` (books like it), `section:<name>` and `floor:<n>`."""
        kind, _, value = choice.partition(":")
        kind = kind.lower()
        book = self.finder.catalog.lookup(value) if value and self.finder is not None and kind in (
            "book", "where", "guide", "similar") else None
        if kind == "book" and book is not None:
            return ChoiceInfo("ask", choice, question=f"tell me about {book.title}", isbn=book.isbn13, explicit=True)
        if kind == "where" and book is not None:
            return ChoiceInfo("ask", choice, question=f"where is {book.title}", isbn=book.isbn13, explicit=True)
        if kind == "similar" and book is not None:
            return ChoiceInfo("recommend", choice, isbn=book.isbn13, explicit=True)
        if kind == "guide" and book is not None:
            self.guide_target = self.target_for(book.isbn13, book.title)
            return ChoiceInfo("guide", choice)
        if kind == "surprise":
            return ChoiceInfo("recommend", "surprise me", pick=True)
        if kind == "topic" and value.strip():
            return ChoiceInfo("recommend", value.strip(), interest=value.strip().lower())
        if kind == "section" and value:
            return ChoiceInfo("ask", choice, question=f"where is the {value} section", area=True)
        if kind == "floor" and value:
            return ChoiceInfo("ask", choice, question=f"what is on level {value}", area=True)
        return ChoiceInfo(choice.lower(), text=choice.lower())

    async def on_utterance(self, event: UserUtterance) -> None:
        await self.start()
        self.active()
        await self.handle_text(event.text.strip(), event.source)

    async def handle_text(self, text: str, source: str, hear: bool = True) -> None:
        """Work out what the visitor means (a choice, a question, small talk) and feed the machine."""
        low = text.lower()
        if hear and self.assistant is not None:
            self.assistant.hear(text)
        expect, self.session.expect = self.session.expect, None          # an expectation lasts for one utterance
        was_awaiting, self.awaiting_guide = self.awaiting_guide, False
        if self.leaf in ("IDLE", "FAREWELL") and is_done(low, "closing"):
            return                                       # "thanks" after the goodbye is not a new visit
        info = None
        if self.leaf not in ("IDLE", "FAREWELL", "GREET"):
            if expect is not None and expect.kind == "lend" and await self.answer_lend(text, expect):
                return                                                # "Would you like it?" (a copy in its storage)
            if self.lent_book is not None and re.search(CHANGED_MIND, low):
                await self.changed_mind()                             # "nvm, I don't need it": what it did, never the LLM's guess
                return
            about_itself = self.asks_permission(low) or self.holdings_query(low) is not None   # answered from facts below
            info = None if about_itself else self.expected_info(text, expect)   # a short answer to the question it just asked
            if info is None and not about_itself:
                info = self.route_info(text, was_awaiting)            # the router: rules, fuzzy, no LLM
            if info is None and not about_itself:
                info = self.classify(low, text, was_awaiting)         # then the older rules
        if info is not None:
            self.session.misses = 0
            await self.fsm.dispatch("Choice", info, source)
            return
        if self.leaf in REPAIR_STATES and not is_question(low):       # not understood, and not a question to answer
            self.session.misses += 1
            if self.session.misses >= int(self.settings.intents.get("repair_after", 2)):
                await self.repair()
                return
        await self.fsm.dispatch("Utterance", ChoiceInfo("", text=text), source)

    async def repair(self) -> None:
        """The robot did not understand twice in a row: stop asking again. The choices are shown as buttons (always with
        "I'm done"), and in a common area said briefly."""
        s = self.session
        s.misses, s.repair = 0, True
        options = self.options_for(self.leaf, True)
        log.info("[dialogue] not understood twice in %s: showing %s", self.leaf, [o["label"] for o in options])
        labels = [o["label"].lower().rstrip(".") for o in options if o["id"] != "done"]
        brief = "Sorry, I didn't catch that. " + (f"You can say: {join_names(labels + ['I am done'])}." if labels else
                                                  "You can tap a button, or say I am done.")
        if self.style_speaks():
            await self.say(brief, "neutral")
        else:
            self.show(brief)
            await self.screen_now()

    @asynccontextmanager
    async def thinking(self):
        """Around every call that waits for the LLM: the face looks like it is thinking and the screen shows a progress
        indicator at once, the state's timeouts wait, and in a common area a short spoken "Let me check." follows if
        no sentence has arrived after intents.thinking_ack_s (once per answer)."""
        if self.assistant is None:
            yield
            return
        started = time.monotonic()
        self._thinking += 1
        first = self._thinking == 1
        ack = None
        if first:
            if self.publish is not None:
                await self.publish(ThinkingStarted())
                await self.publish(ExpressionRequested("thinking", 120.0))
            ack = asyncio.create_task(self._acknowledge())
        self.fsm.suspend_timeouts()
        try:
            yield
        finally:
            self.fsm.resume_timeouts()
            self._thinking -= 1
            if ack is not None:
                ack.cancel()
            if first and self.publish is not None:
                await self.publish(ThinkingEnded(round((time.monotonic() - started) * 1000)))
                await self.publish(ExpressionRequested("idle", 1.0))

    async def _acknowledge(self) -> None:
        await asyncio.sleep(float(self.settings.intents.get("thinking_ack_s", 1.5)))
        if self.style_speaks():
            await asyncio.get_running_loop().run_in_executor(None, self.out.deliver, self.thinking_text, self.get_style())

    def asked_for_stored(self, low: str):
        """The StoredBook when the visitor asks whether the robot has a book on it ("do you have I Robot with you?",
        "can I borrow it?") and it is holding a copy in its storage; None otherwise (the usual answer follows)."""
        words = normalise(low).split()
        you = any(w in ("you", "u", "ya") for w in words)
        holds = any(edit_cost(w, cue) <= 1 for w in words for cue in ("have", "hold", "holding", "carrying", "got") if len(w) >= 3)
        borrow = re.search(r"\b(?:can|could|may) (?:i|we) (?:have|take|borrow|get|grab|check out)\b|\b(?:borrow|check out)\b", low)
        if not ((you and (holds or "with" in words)) or borrow):
            return None
        named = self.finder.titles_in(low) if self.finder is not None else []
        isbn = named[0].isbn13 if named else (self.session.book.isbn if self.session.book is not None
                                              and any(w in ("it", "this", "that", "one") for w in words) else "")
        return self.stored_copy(isbn)

    def stored_copy(self, isbn: str):
        """The StoredBook if the robot is holding a returned copy of this book in its storage, else None. The book on
        a visitor just handed in during this visit is not on offer: it is going back on its shelf."""
        if not isbn or isbn in self.session.handed_in:
            return None
        slot = self.storage.slot_of(isbn)
        return self.storage.books[slot] if slot is not None else None

    async def offer_stored(self, stored) -> None:
        """It has the book the visitor asked about in its storage: say so and offer it, instead of sending them to the shelf."""
        if self.publish is not None and self.finder is not None and self.finder.catalog.lookup(stored.isbn) is not None:
            await self.publish(BookFocused(stored.isbn, "ask"))
        self.session.expect = expect_lend(stored.isbn)
        self.session.last_action = "choose"                           # the question is asked: no generic one after it
        await self.say(f"I have {stored.title or 'that book'} in my storage, in slot {stored.slot}. Would you like it?", "happy")

    async def changed_mind(self) -> None:
        """The visitor was handed a book from the storage ring and now says they do not want it ("nvm, you can have it"). It
        goes back into its slot, which is still lit: they are asked to put it there, and the book is in the storage again, on
        the screen and in what the robot says it holds. It never says it is "kept" somewhere it is not."""
        book, self.lent_book = self.lent_book, None
        self._screen_text = ""
        slot = self.storage.reserve(preferred=self.lent_slot)
        if slot is None:                                              # the ring filled up meanwhile: the pad is the way back
            self.session.intent = "return"
            await self.say(f"No problem. To give {book.title or 'it'} back, place it on my pad and I'll take it.", "happy")
        else:
            self.storage.commit(slot, book)
            self.out.ring.rotate_to(slot)
            self.out.ring.light_slot(slot, True)                      # lit until the visit ends: where to put it
            self.lent_slot = slot
            if self.publish is not None and self.finder is not None and self.finder.catalog.lookup(book.isbn) is not None:
                await self.publish(StoringBook(book.isbn, book.title or "", slot))       # the card, with "Returned", on the screen
            await self.say(f"No problem. Please put {book.title or 'it'} back in slot {slot}, it's lit up on my ring. "
                           "I'll keep it there.", "happy")
        self.session.expect = expect_anything_else()
        await self.say("Is there anything else I can help with?", "neutral")
        self._screen_text = ""

    async def answer_lend(self, text: str, expect) -> bool:
        """The reply to "Would you like it?": True if it was a yes or a no (and has been answered)."""
        found = match_answers(text, expect, self.settings.intents)
        if found is None:
            return False
        log.info("[expect] %r -> %s (%s, lend)", text, found.answer, found.phrase)
        slot = self.storage.slot_of(expect.data)
        stored = self.storage.books[slot] if slot is not None else None
        if found.answer == "yes" and stored is not None:
            self.storage.release(slot)                                # it leaves the ring: it is not going back to a shelf
            self.out.ring.rotate_to(slot)
            self.out.ring.light_slot(slot, True)
            self.lent_slot, self.lent_book = slot, stored
            self._screen_text = ""                                    # (nothing changes state here: start the screen text afresh)
            await self.say(f"Here you go: {stored.title or 'your book'} is in slot {stored.slot}, lit up on my ring. "
                           "Please take it from there.", "happy")
        elif found.answer == "yes":
            await self.say("Sorry, that book is not in my storage any more. The help desk can help.", "neutral")
        else:
            where = f" It goes back on shelf {stored.shelf} when I'm free." if stored is not None and stored.shelf else ""
            await self.say(f"No problem.{where}", "happy")
        self.session.expect = expect_anything_else()
        await self.say("Is there anything else I can help with?", "neutral")
        self._screen_text = ""                                        # no state change here: the next reply starts the screen text afresh
        return True

    def expected_info(self, text: str, expect) -> Optional[ChoiceInfo]:
        """A ChoiceInfo when a short reply is one of the answers the robot expected (None: the router decides)."""
        if expect is None:
            return None
        found = match_answers(text, expect, self.settings.intents)
        if found is None:
            return None
        log.info("[expect] %r -> %s (%s %.2f, %s)", text, found.answer, found.phrase, found.score, expect.kind)
        if expect.kind == "choose":
            return self.choice_info(found.answer)
        if found.answer == "guide":
            return ChoiceInfo("guide", text)
        if found.answer == "done":
            return ChoiceInfo("done", text)
        return ChoiceInfo(found.answer, text)                                 # more | another

    def route_info(self, text: str, was_awaiting: bool) -> Optional[ChoiceInfo]:
        """A ChoiceInfo from the intent router's best candidate (None: nothing confident, or an intent that needs the
        state to say what it means). Two close places become a question: the options view."""
        s = self.session
        if (stored := self.asked_for_stored(text.lower())) is not None:        # "do you have Dune with you?": it has a copy
            return ChoiceInfo("ask", text, question=text, isbn=stored.isbn, explicit=True)
        prompted = ("recommend" if (self.leaf == "AWAIT_BOOK" and s.intent == "recommend") or s.awaiting_interest else
                    "ask" if self.leaf == "AWAIT_BOOK" and s.intent == "ask" else None)
        route = self.router.route(text, self.leaf, was_awaiting, s.book is not None or bool(s.focus), prompted)
        best = route.best
        if best is None:
            return None
        log.info("[intent] %r -> %s%s", text, route.candidates[:3], " (ambiguous)" if route.ambiguous else "")
        if route.ambiguous:
            places = [c.place for c in route.ambiguous]
            return ChoiceInfo("ask", text, question=text, area=True, place=Place("ambiguous", "", choices=places))
        intent = best.intent
        if intent in ("done", "no"):
            return ChoiceInfo("done", text)
        if intent == "yes":
            if was_awaiting:
                return ChoiceInfo("guide", text)
            return ChoiceInfo("more", text) if self.leaf == "ANYTHING_ELSE" else None
        if intent == "another":
            if s.last_action == "recommend" and s.focus:                  # "another one?" right after a suggestion: a different one
                return ChoiceInfo("recommend", text, pick=True)
            return ChoiceInfo("another", text)
        if intent == "return":
            return ChoiceInfo("return", text)
        if intent == "guide":
            return ChoiceInfo("guide", text)
        if intent == "explain":
            isbn, explicit = self.referent(text.lower(), best.isbn)
            return ChoiceInfo("ask", text, question=text, isbn=isbn, explicit=explicit)
        if prompted == "recommend" and (intent == "title" or (intent == "where" and best.isbn)):
            return ChoiceInfo("recommend", text, isbn=best.isbn)     # "what do you like?" "Dune": books like it
        if intent == "title":
            book = self.finder.catalog.lookup(best.isbn) if self.finder is not None else None
            return ChoiceInfo("ask", text, question=f"tell me about {book.title}" if book else text, isbn=best.isbn)
        if intent == "recommend":
            other = re.search(r"\b(?:other|different|else|new|any books|anything|something)\b", text.lower()) is not None
            general = not best.isbn and not best.interest and s.recommend_failed and other
            pick = not best.isbn and not best.interest and self.wants_a_pick(text.lower(), prompted)
            return ChoiceInfo("recommend", text, interest=best.interest, isbn=best.isbn, general=bool(general), pick=pick)
        if intent == "where":
            if best.place is None:                                    # "where is it": the book in hand
                isbn, explicit = self.referent(text.lower())
                return ChoiceInfo("ask", text, question=text, take_me=best.take_me, isbn=isbn, explicit=explicit)
            return ChoiceInfo("ask", text, question=text, area=True, place=best.place, take_me=best.take_me)
        return None

    def classify(self, low: str, text: str, was_awaiting: bool) -> Optional[ChoiceInfo]:
        """A ChoiceInfo for words that choose something, None for free text."""
        if is_done(low, "closing" if self.leaf == "ANYTHING_ELSE" else self.leaf.lower()):
            return ChoiceInfo("done", text)
        if (picked := self.ordinal_choice(low)) is not None:   # "the second one", after a list or choices
            return self.choice_info(picked)
        asked = where_query(low)
        if asked is not None and self.find_book(asked[0]) is not None:
            return ChoiceInfo("ask", text, question=text)
        if asked is None or self.find_book(asked[0]) is None:
            if self.place_request(low) is not None:        # "where are the history books", "what's on level 2"
                return ChoiceInfo("ask", text, question=text, area=True)
        if is_guide(low) or (was_awaiting and is_yes(low)):
            return ChoiceInfo("guide", text)
        if self.leaf == "ANYTHING_ELSE":
            if any(w in low for w in ANOTHER_BOOK_WORDS):
                return ChoiceInfo("another", text)
            if is_yes(low):
                return ChoiceInfo("more", text)
        action, interest = choice_from(low), interest_from(low)
        prompted = (self.session.intent if self.leaf == "AWAIT_BOOK" else
                    "recommend" if self.session.awaiting_interest else None)
        if prompted in ("recommend", "ask") and action is None and not interest:
            reply = self.answer_to_prompt(low, text, prompted)   # answering "what do you like?" / "which book?"
            if reply is not None:
                return reply
        named = self.finder.titles_in(low) if self.finder is not None else []
        if not named and (action in ("explain", "recommend") or interest):
            loose = self.places.fuzzy_title(interest or low)    # "tell me about the martion", "like foundaton": typos
            if loose is not None and loose.kind == "book" and self.finder is not None:
                named = [self.finder.catalog.lookup(loose.isbn)]
        if named and interest and (title_key(named[0].title) in title_key(interest)
                                   or self.places.fuzzy_title(interest) is not None):
            interest = ""                                  # "something like Dune": the book, not a topic called "dune"
        isbn = named[0].isbn13 if named and not interest else ""    # a title they said: no book on the pad is needed
        if action == "return":
            return ChoiceInfo("return", text)
        if interest or action == "recommend":
            general = not isbn and not interest and (
                re.search(r"\b(?:other|different|else|new|any books|anything|something)\b", low) is not None
                and self.session.recommend_failed)          # "any other books?" after nothing was found for this one
            pick = not isbn and not interest and self.wants_a_pick(low, prompted)
            return ChoiceInfo("recommend", text, interest=interest, isbn=isbn, general=bool(general), pick=pick)
        if action == "explain":
            return ChoiceInfo("ask", text, question=text, isbn=isbn)
        return None

    def wants_a_pick(self, low: str, prompted: Optional[str]) -> bool:
        """A recommendation with no topic and no book to go on, where they want the robot to choose: after it has already asked
        "what kind of books do you like?" (asking it again is the loop), or in words that hand the choice over ("just
        recommend me one", "surprise me"). With a book in hand the usual "something like this" applies."""
        s = self.session
        nothing_to_go_on = (s.book is None or s.virtual) and (not s.focus or s.focus in s.offered)   # or only a book it picked itself
        return nothing_to_go_on and (prompted == "recommend" or re.search(PICK_ME, low) is not None)

    def answer_to_prompt(self, low: str, text: str, intent: Optional[str] = None) -> Optional[ChoiceInfo]:
        """The reply to "what kind of books do you like?" or "which book?": a title they name, or (for a
        recommendation) whatever topic or author they say."""
        named = self.finder.titles_in(low) if self.finder is not None else []
        intent = intent or self.session.intent
        if named:
            if intent == "recommend":
                return ChoiceInfo("recommend", text, isbn=named[0].isbn13)
            return ChoiceInfo("ask", text, question=f"tell me about {named[0].title}", isbn=named[0].isbn13)
        if intent == "recommend" and re.search(PICK_ME, low):
            return ChoiceInfo("recommend", text, pick=True)      # "surprise me", "not sure", "you pick": it chooses
        if intent == "recommend" and not is_question(low):
            topic = re.sub(r"\b(?:i|i'm|im|i am|really|like|love|enjoy|into|maybe|perhaps|some|something|anything|"
                           r"about|on|books?|novels?|stories|reading|please|just|hmm+|um+|well|so)\b|[?!.,]+", " ", low)
            topic = " ".join(topic.split())
            if topic:
                return ChoiceInfo("recommend", text, interest=topic)
        return None

    # ---- guards (the table names them) ----
    def has_book(self, event) -> bool:
        return self.session.book is not None

    def recently_active(self, event) -> bool:
        """A camera "they have left" while the visitor spoke, typed, tapped or placed a book within leave_grace_s:
        the face detector lost them (looking down, turned to the pad), they have not gone."""
        grace = float(self.settings.engagement.get("leave_grace_s", 60))
        return (event.source == "sensor" and self.last_activity is not None
                and self.clock() - self.last_activity < grace)

    def doubt_left(self, event) -> None:
        self.leave_pending = True
        log.info("[dialogue] the camera lost the visitor, but they were active a moment ago: not saying goodbye yet")

    def person_present(self, event) -> bool:
        """The camera (or the operator) has said someone engaged and has not said they left."""
        return self.present

    def keep_waiting(self, event) -> Awaitable[None]:
        return self.fsm.rearm(event.state or self.leaf, "timeout")

    def book_statement(self, event) -> bool:
        """A remark with a book on the pad that is neither a choice nor a question ("hmm, I don't know")."""
        return self.has_book(event) and not is_question(event.payload.text.lower())

    def _is(self, event, name: str) -> bool:
        return getattr(event.payload, "name", None) == name

    def choice_return(self, event) -> bool:
        return self._is(event, "return")

    def return_with_book(self, event) -> bool:
        """Returning needs the book on the pad; a title they only named is not one."""
        return self._is(event, "return") and self.has_book(event) and not self.session.virtual

    def choice_ask(self, event) -> bool:
        return self._is(event, "ask")

    def ask_ready(self, event) -> bool:
        """They asked something and it can be answered now: a book is on the pad, or it is "where is <title>"."""
        if not self._is(event, "ask"):
            return False
        asked = where_query(event.payload.question.lower())
        return self.has_book(event) or bool(event.payload.isbn) or event.payload.area or \
            is_guide(event.payload.question.lower()) or (asked is not None and self.find_book(asked[0]) is not None)

    def choice_recommend(self, event) -> bool:
        return self._is(event, "recommend")

    def recommend_ready(self, event) -> bool:
        info = event.payload
        return self._is(event, "recommend") and (bool(info.interest) or bool(info.isbn) or info.pick or
                                                 (self.has_book(event) and not info.general))

    def choice_done(self, event) -> bool:
        return self._is(event, "done")

    def choice_more(self, event) -> bool:
        return self._is(event, "more")

    def choice_another(self, event) -> bool:
        return self._is(event, "another")

    def choice_guide(self, event) -> bool:
        return self._is(event, "guide")

    def intent_return(self, event) -> bool:
        return self.session.intent == "return"

    def intent_ask(self, event) -> bool:
        return self.session.intent == "ask"

    def intent_recommend(self, event) -> bool:
        return self.session.intent == "recommend"

    def first_failure(self, event) -> bool:
        return self.session.scan_failures == 0

    def has_stops(self, event) -> bool:
        return bool(self.stops)

    def more_stops(self, event) -> bool:
        return len(self.stops) > 1

    def resume_paused(self, event) -> bool:
        return self.returning_paused and bool(self.storage.stops())

    def auto_return(self, event) -> bool:
        return bool(self.settings.returning.get("auto_start", True)) and bool(self.storage.stops())

    # ---- transition actions: sessions ----
    def begin_session(self, event) -> None:
        ids = tuple(getattr(event.payload, "person_ids", ()) or ())
        self.session = Session(person_ids=ids)

    def begin_session_with_book(self, event) -> None:
        self.session = Session()

    def begin_session_from_text(self, event) -> None:
        self.session = Session(replay=event.payload.text)

    def book_placed(self, event) -> None:
        """A book was placed during a session: the reader starts on it, and what comes next follows the chosen intent."""

    def choose(self, event) -> None:
        info, s = event.payload, self.session
        s.intent = info.name if info.name in ("return", "ask", "recommend") else None
        s.question = info.question or None
        s.interest = info.interest or None
        s.pick = bool(info.pick)
        s.awaiting_interest = False
        s.place, s.take_me = info.place, info.take_me
        if info.isbn and (s.book is None or s.virtual or info.explicit):   # a book they named: no pad needed
            book = self.finder.catalog.lookup(info.isbn) if self.finder is not None else None
            if book is not None:
                s.book, s.virtual = self.identified(book), True
                if self.assistant is not None:
                    self.assistant.reset()

    def another_book(self, event) -> None:
        self.session.book = None
        self.session.virtual = False
        self.session.focus = ""
        self.session.intent = None
        self.session.question = self.session.interest = None
        self.guide_target = None
        if self.assistant is not None:
            self.assistant.reset()

    def question_from_text(self, event) -> None:
        self.session.question = event.payload.text

    async def guide_now(self, event) -> None:
        await self.guide()

    def repeat_offer(self, event) -> Awaitable[None]:
        return self.say(self.offer_text(), "neutral")

    def say_busy(self, event) -> None:
        return self.say("One moment, please.", "neutral")

    def chat(self, event) -> Awaitable[None]:
        return self.talk(event.payload.text)

    # ---- transition actions: books ----
    async def on_identified(self, event) -> None:
        book: BookIdentified = event.payload
        had_book = self.session.book is not None
        self.session.book, self.session.virtual = book, False
        self.session.focus = ""
        self.session.scan_failures = 0
        self.guide_target = None
        if self.assistant is not None and had_book:
            self.assistant.reset()              # another book starts a new conversation; the chat before the first
                                                # book of a visit carries on

    def on_scan_failed(self, event) -> None:
        log.info("[dialogue] scan failed: %s", getattr(event.payload, "reason", ""))
        s = self.session
        s.scan_failures += 1
        s.retry_message = SCAN_FAILED_MESSAGE if s.scan_failures == 1 else None
        s.book = None
        self.guide_target = None
        if self.assistant is not None:
            self.assistant.reset()
        self.out.lights.spotlight(False)

    async def commit_store(self, event) -> None:
        """The pad dropped the book: it is stored now."""
        book, slot = self.session.book, self.store_slot
        if book is None or slot is None or self._stored:
            return
        place = location_of(book)
        self.storage.commit(slot, StoredBook(isbn=book.isbn, title=book.title or "", shelf=place.shelf,
                                             section=place.section, floor=place.floor, zone=place.zone,
                                             map_x=place.map_x, map_y=place.map_y, map_yaw=place.map_yaw))
        self._stored = True
        self.session.handed_in.add(book.isbn)
        title = book.title or "this book"
        parts = [f"Done: {title} is in storage slot {slot}."]     # where it belongs is the robot's business
        self.guide_target = None                         # they handed it in: no "shall I guide you to it?"
        self.session.last_action = "return"
        self.session.book = None                         # the pad is empty now
        await self.say(" ".join(parts), "happy")
        self.fsm.post("stored")

    # ---- transition actions: returning ----
    async def pause_returning(self, event) -> None:
        self.out.nav.stop()
        self.returning_paused = True
        self.interrupted_returning = True
        self.begin_session(event)
        log.info("[returning] paused at %s", self.current_shelf)

    async def pause_returning_for_book(self, event) -> None:
        self.out.nav.stop()
        self.returning_paused = True
        self.begin_session_with_book(event)

    def resume_returning(self, event) -> None:
        self.stops = self.storage.stops(first=self.current_shelf)
        log.info("[returning] resuming at shelf %s (%d stop(s) left)", self.current_shelf, len(self.stops))

    def start_returning(self, event) -> None:
        self.returning_paused = False
        self.current_shelf = None
        self.fsm.forget("RETURNING")

    async def finish_unload(self, event) -> None:
        stop = self.stops.pop(0) if self.stops else None
        if stop is None:
            return
        for slot in stop.slots:
            book = self.storage.release(slot)
            log.info("[returning] %s put back on shelf %s", book.title if book else "a book", stop.shelf)
        self.current_shelf = None

    def finish_returning(self, event) -> None:
        self.returning_paused = False
        self.current_shelf = None
        self.stops = []
        self.fsm.forget("RETURNING")
        log.info("[returning] all returned and home")

    # ---- entry and exit actions ----
    def show(self, text: str) -> None:
        """Add to the text the screen shows for the state being entered (what an action said, then what the entry said)."""
        self._screen_text = f"{self._screen_text} {text}".strip() if text else self._screen_text

    def enter_idle(self, event) -> None:
        self.show("")

    def exit_engaged(self, event) -> None:
        """Leaving ENGAGED for good (or the operator forced another state): tidy up."""
        self.end_session()

    def end_session(self) -> None:
        self.session = Session()
        self.guide_target, self.awaiting_guide = None, False
        self.interrupted_returning = False
        self.greeted = False
        self.leave_pending, self.last_activity = False, None
        if self.assistant is not None:
            self.assistant.reset()
        self.out.lights.spotlight(False)
        if self.lent_slot is not None:
            self.out.ring.light_slot(self.lent_slot, False)
            self.lent_slot, self.lent_book = None, None

    async def enter_greet(self, event) -> None:
        s = self.session
        began = time.monotonic()
        if self.interrupted_returning:                   # someone engaged while the robot was putting books back
            self.interrupted_returning = False
            s.offer_said = True
            await self.speak_or_show(RETURNING_OPENING, "happy")
        else:                                            # every visit starts with an introduction, even for the same
            self.greeted = True                          # person as before; once per visit (the camera engages once)
            await self.speak_or_show(HELLO, "happy")
        # the hello stays on the face until it has been heard or read: if the speech was quick, failed or the style is
        # silent, wait out the rest of the hold before the options replace it
        hold = float(self.settings.screen.get("greeting_hold_s", 0)) - (time.monotonic() - began)
        if hold > 0:
            await asyncio.sleep(hold)
        self.fsm.post("greeted")

    def offer_text(self) -> str:
        s, book = self.session, self.session.book
        if s.scan_failures >= 2:
            return OFFER_AFTER_FAILURES
        if s.virtual:                                    # a book they only named: the general offer
            return OFFER_NO_BOOK
        if book is not None and book.title:
            by = f" by {book.author}" if book.author else ""
            return f"You have {book.title}{by}. {OFFER}"
        if book is not None:
            return f"I read the barcode, but I don't know this book (ISBN {book.isbn}). {OFFER}"
        return OFFER_NO_BOOK

    async def enter_offer(self, event) -> None:
        s = self.session
        if s.replay is not None and is_greeting(s.replay.lower()):
            s.replay = None                              # "hi": the hello just answered it; go on to the offer
        if s.replay is not None:                         # they spoke before the offer: answer that instead
            text, s.replay = s.replay, None
            self.show(self.offer_text())
            await self.handle_text(text, event.source, hear=False)
            return
        if s.offer_said:                                  # the opening line was the offer
            s.offer_said = False
            self.show(RETURNING_OPENING)
        else:
            s.repeated_offer = s.repeated_offer or s.last_action is not None
            await self.say(self.offer_text(), "happy" if s.book is not None and s.book.title else "neutral")
        s.intent = None

    async def enter_await_book(self, event) -> None:
        s = self.session
        if s.retry_message:
            text, s.retry_message = s.retry_message, None
        else:
            what = {"return": "take it back", "ask": "tell you about it", "recommend": "find you something similar"}
            if s.intent == "return":                      # only returning needs the book itself
                text = "Place the book on my pad and I'll take it back."
            elif s.intent == "recommend":                 # no book needed: ask what they like instead
                text = ("Happy to! What kind of books do you like? Tell me a topic, a title or an author, "
                        "or place a book you like on my pad.")
            elif s.intent == "ask":
                text = "Which book? Tell me its title, or place it on my pad."
            else:
                text = "Sure! Place another book on my pad, or tell me a title or what you like to read."
        await self.say(text, "neutral")

    async def enter_scan(self, event) -> None:
        self.out.lights.spotlight(True)
        self.show("Reading the book...")

    async def enter_store(self, event) -> None:
        book = self.session.book
        title = (book.title if book and book.title else None) or "this book"
        self._stored = False
        self.store_slot = None
        self.session.intent = None
        if book is None or not book.found_in_catalog:
            await self.say(f"{title} isn't in our catalogue, so I don't know where it belongs. "
                           "Please leave it at the help desk.", "neutral")
            self.fsm.post("rejected")
            return
        slot = self.storage.reserve(preferred=book.slot)
        if slot is None:
            await self.say("My storage is full, so I can't take it. Please leave it at the help desk.", "neutral")
            self.fsm.post("full")
            return
        self.store_slot = slot
        if self.publish is not None:
            await self.publish(StoringBook(book.isbn, title, slot))
        try:
            self.out.ring.rotate_to(slot)
            self.out.ring.light_slot(slot, True)
        except ValueError:
            log.warning("slot %s is outside the storage ring", slot)
        if self.out.pad is not None:
            self.out.pad.vibrate()
        await self.say(f"Thank you for returning {title}. I'm putting it in storage slot {slot}.", "happy")

    def exit_store(self, event) -> None:
        """Leaving STORE: if the book was never dropped, give the slot back; the slot light goes off either way."""
        if self.store_slot is not None:
            if not self._stored:
                self.storage.unreserve(self.store_slot)
            try:
                self.out.ring.light_slot(self.store_slot, False)
            except ValueError:
                pass
        self.store_slot = None

    async def enter_answer(self, event) -> None:
        s = self.session
        s.intent = None
        question, s.question = s.question, None
        place, s.place = s.place, None
        if place is not None:                           # the router found a place: a section, a floor, a book, or a choice
            await self.tell_place(place, s.take_me)
            self.fsm.post("answered")
            return
        if question:
            await self.answer_text(question)
            self.fsm.post("answered")
            return
        book = s.book
        title = (book.title if book and book.title else None) or "this book"
        by = f" by {book.author}" if book and book.author else ""
        if book is not None and book.found_in_catalog and self.publish is not None:
            await self.publish(BookFocused(book.isbn, "ask"))
        await self.say(f"You have {title}{by}. What would you like to know? I can explain it, say where it belongs, "
                       "or answer a question.", "happy")

    async def enter_recommend(self, event) -> None:
        s = self.session
        s.intent = None
        interest, s.interest = s.interest, None
        pick, s.pick = s.pick, False
        if pick:
            await self.recommend_pick()
        elif interest:
            await self.recommend_by_interest(interest)
        else:
            book = s.book
            await self.act_recommend(book, (book.title if book and book.title else None) or "this book")
        s.last_action = "recommend"
        self.fsm.post("recommended")

    async def enter_anything_else(self, event) -> None:
        s = self.session
        if s.last_action is None and self.guide_target is None:
            s.last_action = "answer"
        if s.last_action in ("where", "choose"):         # "...guide you there?" / "Did you mean...?" already asked it
            self.show(self.last_said)
            return
        question = self.follow_up(s.last_action or "answer")              # may offer to guide (and then expects that)
        if s.expect is None:
            s.expect = expect_anything_else()
        await self.say(question, "neutral")

    async def enter_farewell(self, event) -> None:
        s = self.session
        spoken = THANKS_SPOKEN if getattr(event.payload, "name", None) == "done" else GOODBYE_SPOKEN
        if self.style_speaks():
            await self.say(spoken, "happy")
        else:                                            # a quiet zone: eyes and light, no words
            self.show(spoken)                            # (the line is read, not said)
            self.out.neck.look_at("forward")
            self.out.lights.pattern("pulse")
            if self.publish is not None:
                await self.publish(ExpressionRequested("happy"))
        self.end_session()

    # ---- returning ----
    def enter_plan(self, event) -> None:
        self.stops = self.storage.stops(first=self.current_shelf)
        log.info("[returning] %d stop(s) planned: %s", len(self.stops),
                 ", ".join(f"{s.shelf} ({len(s.slots)})" for s in self.stops) or "none")
        self.fsm.post("planned")

    async def _navigate(self, title: str, x: float, y: float, yaw: float, stop: Optional[ShelfStop] = None) -> None:
        if self.publish is not None:
            await self.publish(NavigationRequested("", title, x, y, yaw, shelf=stop.shelf if stop else None,
                                                   section=stop.section if stop else None,
                                                   floor=stop.floor if stop else None, zone=stop.zone if stop else None))

    async def enter_navigating(self, event) -> None:
        if not self.stops:
            self.fsm.post("replan")
            return
        stop = self.stops[0]
        self.current_shelf = stop.shelf
        self.show(f"Returning books to the {stop.section or 'library'} section.")
        await self.progress("driving", stop)
        if event.name == "announced":                    # carrying on after "excuse me": the base is still driving
            return
        await self._navigate(f"shelf {stop.shelf}", stop.x, stop.y, stop.yaw, stop)

    async def enter_at_shelf(self, event) -> None:
        stop = self.stops[0] if self.stops else None
        self.show(f"Putting {len(stop.slots)} book(s) back on shelf {stop.shelf}." if stop else "Putting books back.")
        await self.progress("unloading", stop)

    async def progress(self, phase: str, stop: Optional[ShelfStop]) -> None:
        if self.publish is not None:
            await self.publish(ReturningProgress(phase, len(self.storage), stop.section if stop else None,
                                                 stop.shelf if stop else None, stop.floor if stop else None))

    async def enter_announce(self, event) -> None:
        """Signal without starting a conversation. A style that speaks gets a soft chime and a light pattern; a quiet
        one only the light pattern and the text on the screen."""
        stop = self.stops[0] if self.stops else None
        where = f"the {stop.section} section" if stop and stop.section else "their shelves"
        self.show(f"Excuse me, I'm returning books to {where}.")
        await self.progress("announcing", stop)
        if self.style_speaks():
            self.out.voice.chime(self.get_style())
        self.out.lights.pattern("announce")

    async def enter_homing(self, event) -> None:
        if self.stops:                                    # books arrived while it was paused
            self.fsm.post("replan")
            return
        home = self.settings.returning.get("home") or {}
        station = self.plan.station if self.plan is not None else None
        if station is not None:                            # the floor plan's station is home
            home = {"x": station.x, "y": station.y, "yaw": station.yaw}
        self.show("Heading home.")
        await self.progress("homing", None)
        await self._navigate("home", float(home.get("x", 0.0)), float(home.get("y", 0.0)), float(home.get("yaw", 0.0)))

    # ---- saying things ----
    async def say(self, text: str, tag: Optional[str] = None) -> None:
        """Deliver a message in the current style and announce it for the face."""
        self.show(text)
        self.last_said = text
        await self.screen_now()                          # the screen follows the words, not the end of them
        # speaking (text-to-speech, playing it) takes seconds: do it in a thread so the camera, the display and the
        # timers keep running, and a person leaving or a tap during it is queued and handled once it is said. The state's
        # timeouts wait while the robot talks and start again when it has finished.
        self.fsm.suspend_timeouts()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.out.deliver, text, self.get_style())
        finally:
            self.fsm.resume_timeouts()
        if self.assistant is not None:
            self.assistant.said(text)               # the LLM's memory is what was really said, canned lines too
        if self.publish is not None:
            await self.publish(ReplySentence(text, tag))

    async def speak_or_show(self, text: str, tag: Optional[str] = None) -> None:
        """Say it where the style speaks; where it does not, look at the person, pulse the light and show it on the
        screen, without a word and without a printed slip."""
        if self.style_speaks():
            await self.say(text, tag)
            return
        self.show(text)
        await self.screen_now()                          # on the screen at once, not when the state's entry has finished
        self.out.neck.look_at("visitor")
        self.out.lights.pattern("pulse")

    # ---- the machine's own reports ----
    async def screen_now(self) -> None:
        """Publish the screen now, before the sentence is spoken: for a state being entered (its entry actions run before
        the machine reports it has arrived) and for a reply that does not change state (the drop confirmation, small
        talk). Whatever should be on screen is there before the first word."""
        if self.publish is not None and self.fsm.leaf:
            await self._on_entered(self.fsm.leaf, early=True)

    async def _on_transition(self, record: dict) -> None:
        self._entering = not record["internal"]
        if not record["internal"]:
            self.session.misses, self.session.repair = 0, False
        self.transitions.append((record["from"], record["event"], record["to"]))
        state_log.info("%s -> %s  (%s, %s)%s", record["from"] or "(start)", record["to"], record["event"],
                       record["source"], "  [internal]" if record["internal"] else "")
        if self.publish is not None:
            await self.publish(StateTransition(record["from"], record["to"], record["event"], record["source"],
                                               self.get_zone(), record["internal"], record["guard"], record["action"]))
        if record["internal"] and self._screen_text:        # said something without changing state: refresh the text
            await self._on_entered(self.fsm.leaf)

    async def _on_entered(self, state: str, early: bool = False) -> None:
        _, title = SCREENS.get(state, ("full_face", ""))
        if early:                                         # keep the text: the real entry report follows with it
            text = self._screen_text
        else:
            text, self._screen_text = self._screen_text, ""
            self._entering = False
        if self.publish is not None:                     # the screen composer (core/screen.py) picks the view
            await self.publish(StateEntered(self.fsm.spec.path(state), self.get_zone(), title, text,
                                            tuple(self.options_for(state)), tuple(self.options_for(state, True)),
                                            self.session.repair and state in REPAIR_STATES))

    def options_for(self, state: str, any_zone: bool = False) -> list:
        """The choices in a state: as touch buttons when the style has them (quiet zones), or for every zone with
        any_zone (the screen composer shows them as an options view everywhere). After the robot has not understood
        twice (repair) they always include "I'm done"."""
        options = self.state_options(state, any_zone)
        if self.session.repair and state in REPAIR_STATES and any_zone:
            options = options or self.state_options(state, True)
            if not any(o["id"] == "done" for o in options):
                options = options + [{"id": "done", "label": "I'm done"}]
        return options

    def state_options(self, state: str, any_zone: bool = False) -> list:
        if not (any_zone or self.get_style().touch_buttons):
            return []
        if state == "OFFER":
            options = [{"id": "return", "label": "Return a book"}, {"id": "ask", "label": "Ask about a book"},
                       {"id": "recommend", "label": "Get a recommendation"}]
            return options + [{"id": "done", "label": "Done"}] if self.session.repeated_offer else options
        if state == "ANYTHING_ELSE":                    # no buttons: the face asks "anything else?" and the visitor answers aloud
            return []
        if state == "AWAIT_BOOK":
            leave = [{"id": "more", "label": "Back"}, {"id": "done", "label": "Done"}]
            return self.recommend_options() + leave if self.session.intent == "recommend" else leave
        return []

    # ---- the conversation (kept from the flat dialogue) ----
    def holdings_query(self, low: str) -> Optional[str]:
        """"pad", "storage" or "both" when the visitor asks what the robot ITSELF is holding ("which book do you hold rn?",
        "do you have any book with you?", "what's on your pad?", "what's in your storage?"), else None. A book named that
        a copy of is in its storage is the lending question instead (asked_for_stored), and "books about space" is a
        recommendation: this needs a word that says the robot's own hands, pad or storage."""
        if self.asked_for_stored(low) is not None:
            return None
        words = normalise(low).split()
        you = any(w in ("you", "u", "ya", "your", "ur") for w in words)
        verb = any(edit_cost(w, v) <= 1 for w in words for v in HOLDING_VERBS if len(w) >= 3)
        bookish = any(w.startswith("book") or edit_cost(w, "books") <= 1 or w in ("anything", "something", "any") for w in words)
        explicit = re.search(STORAGE_PATTERN, low) or re.search(r"\byour (?:pad|storage|slots?)\b", low)
        if re.search(r"\b(?:about|involving|featuring|regarding)\b", low) and not re.search(r"\b(?:pad|storage|slots?)\b", low):
            return None                                      # "do you have any books about space?": a request for a topic
        if not (explicit or (you and verb and bookish and re.search(HOLDINGS_CUES, low))):
            return None
        pad_said = re.search(PAD_PATTERN, low) is not None
        storage_said = re.search(r"\bstorage\b|\bslots?\b|\bstored\b|\breturned\b", low) is not None
        return "pad" if pad_said and not storage_said else "storage" if storage_said and not pad_said else "both"

    def asks_permission(self, low: str) -> bool:
        """"can I ask you some questions?", "I have a question": just the permission, nothing asked yet (so not "can I ask
        you who wrote it", which has a question in it)."""
        words = normalise(low).split()
        if any(w in ("no", "not", "nothing", "none", "never", "nope") for w in words):
            return False                                     # "no, I don't have any questions": the opposite
        is_question_word = lambda w: w == "question" or (len(w) >= 6 and edit_cost(w, "questions") <= 2)
        if not (any(is_question_word(w) for w in words) or ("ask" in words and any(w in ("something", "anything", "you") for w in words))):
            return False
        left = [w for w in words if w not in STOP and w not in POLITE and not is_question_word(w)
                and w not in ("ask", "asking", "few", "couple", "quick", "something", "anything", NAME.lower())]
        return not left

    def storage_fact(self, text: str) -> Optional[str]:
        """What the robot is holding, from its own pad and storage (never the LLM): "are you holding any books?", "what's
        in your storage?", "which book do you hold?". Always says both places clearly. None if that is not what they asked."""
        kind = self.holdings_query(text)
        if kind is None:
            return None
        books = self.storage.stored()
        pad = self.session.book if self.session.book is not None and not self.session.virtual else None
        on_pad = f" {pad.title or 'A book'} is on my pad." if pad is not None else " Nothing is on my pad right now."
        if kind == "pad":
            return (f"{pad.title or 'A book'} is on my pad." if pad is not None else
                    "Nothing is on my pad right now. Place a book on it and I'll scan it, or tell me a title.")
        if not books:
            stored = f"My storage is empty right now, so I'm not holding any returned books.{on_pad}"
        else:
            names = [f"{b.title or 'a book'} in slot {b.slot}" for b in books]
            where = "; ".join(f"{b.title} goes back to shelf {b.shelf}" for b in books if b.shelf and b.title)
            many = f"{len(books)} returned books" if len(books) > 1 else "1 returned book"
            stored = (f"I'm holding {many} in my storage: {join_names(names)}. "
                      + (f"{where[0].upper()}{where[1:]}. " if where else "")
                      + f"I'll put {'them' if len(books) > 1 else 'it'} back when I'm free.{on_pad}")
        if kind == "both":                               # not clear which one they meant: say where each kind of book is
            stored += " To look up a book in the library, tell me its title or a topic."
        return stored

    async def talk(self, text: str) -> None:
        """Free text with no book to talk about: what it is holding (from its storage), small talk through the LLM
        (it names no books), who-are-you even without it, and otherwise a nudge. A plain greeting gets a greeting."""
        low = text.lower()
        if (held := self.storage_fact(low)) is not None:
            await self.say(held, "happy")
            return
        if self.asks_permission(low):                  # "can I ask you some questions?": yes, and what they can ask
            await self.say(HOW_TO_ASK, "happy")
            return
        if is_greeting(low):
            intro = "" if self.greeted else f"I'm {NAME}. "
            await self.say(f"Hi! {intro}Place a book on the pad and I'll help with it.", "happy")
            return
        if self.assistant is not None:
            async with self.thinking():
                answer = await self.assistant.chat(text)
        else:
            answer = None
        answer = answer or self.identity_fact(low)
        await self.say(answer or "Place a book on the pad and I'll help with it.", "happy" if answer else "neutral")

    @staticmethod
    def identity_fact(text: str) -> Optional[str]:
        return NAME_REPLY if any(w in text for w in NAME_WORDS) else None

    def find_book(self, query: str):
        """The catalogue book a "where is ..." means: a title (loosely spelt), an ISBN, or "this book". A section's
        name ("history", "the science section") is a place, not the title it happens to be part of."""
        if query in THIS_BOOK:
            return self.catalogue_record(self.session.book)
        if self.finder is not None and self.finder.graph is not None:
            sections = {title_key(s.section) for s in self.finder.graph.shelves.values() if s.section}
            bare = title_key(re.sub(r"\b(?:section|area|shelf|shelves|aisle)\b", " ", query))
            if bare in sections or re.search(r"\b(?:section|area|aisle)\b", query):
                return None
        return self.finder.catalog.find_title(query) if self.finder is not None else None

    def target_for(self, isbn: str, title: str) -> GuideTarget:
        return GuideTarget(isbn, title, self.finder.locate(isbn) if self.finder is not None else None)

    async def answer_text(self, text: str) -> None:
        """Answer one thing the visitor asked while a book is the topic: where it lives, to be guided, an explanation,
        or a free question (the LLM from the catalogue entry, then plain catalogue facts)."""
        low = text.lower()
        if await self.navigation_request(low):
            return
        if (held := self.storage_fact(low)) is not None:
            await self.say(held, "happy")
            self.session.last_action = "answer"
            return
        if self.asks_permission(low):
            await self.say(HOW_TO_ASK, "happy")
            self.session.last_action = "answer"
            return
        if (stored := self.asked_for_stored(low)) is not None:       # "do you have I Robot with you?", "can I borrow it?"
            await self.offer_stored(stored)
            return
        book = self.session.book
        title = (book.title if book and book.title else None) or "this book"
        if choice_from(low) == "explain" and not self.catalogue_fact(low):
            await self.act_explain(book, title)
            return
        answer = await self.answer_question(text) or self.identity_fact(low) or self.catalogue_fact(low)
        if answer:
            await self.say(answer, "happy")
        elif is_question(low):
            await self.say(NO_ANSWER, "neutral")
        else:
            await self.say(ACKNOWLEDGE, "happy")
        self.session.last_action = "answer"

    ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
                "last": -1}

    def ordinal_choice(self, text: str) -> Optional[str]:
        """The id of the item they mean by "the second one" / "number 2" / "the last one", from the last list or
        choices shown; None if there was none or they said something else."""
        if not self.last_choices or len(text.split()) > 6:
            return None
        words = re.findall(r"[a-z0-9]+", text)
        for word in words:
            index = self.ORDINALS.get(word, int(word) - 1 if word.isdigit() and 0 < int(word) <= 9 else None)
            if index is not None and -len(self.last_choices) <= index < len(self.last_choices):
                return self.last_choices[index]
        return None

    def place_request(self, text: str):
        """(Place, True if they want to be taken there) for "where is the history section", "where are the astronomy
        books", "take me to science fiction", "what's on level 2"; None if they do not ask about a place."""
        if self.finder is None:
            return None
        take_me = bool(re.search(r"\b(?:take|guide|lead|walk|bring) (?:me|us)\b|\bgo to\b|\bhead to\b", text))
        asks_where = bool(re.search(r"\bwhere\b|\b(?:section|area|aisle|floor|level)s?\b|\bbrowse\b|\bshow me\b|"
                                    r"\bfind\b|\bwhat(?:'s| is) on\b", text))
        if not (take_me or asks_where):
            return None
        if interest_from(text) and not re.search(r"\bwhere\b|\bsection\b|\barea\b", text):
            return None                                    # "find me a book about space" asks for a book, not a place
        place = self.places.resolve(text)
        return (place, take_me) if place is not None else None

    def area_request(self, text: str):
        """The old name: (Place, take_me) or None."""
        return self.place_request(text)

    async def navigation_request(self, text: str) -> bool:
        """"Where is Dune?", "guide me to Dune", "where are the history books", "guide me": handled here, True if so."""
        asked = where_query(text)
        book = self.find_book(asked[0]) if asked is not None else None
        if book is None and (found := self.place_request(text)) is not None:
            place, take_me = found
            await self.tell_place(place, take_me)
            return True
        if asked is not None:
            query, take_me = asked
            book = self.find_book(query)
            if book is None:
                return False
            self.guide_target = self.target_for(book.isbn13, book.title)
            if take_me:
                await self.guide()
            else:
                await self.tell_where(self.guide_target)
            self.session.last_action = "where"
            return True
        if is_guide(text):
            await self.guide()
            return True
        return False

    async def tell_place(self, place, take_me: bool) -> None:
        """Answer a place the resolver found: a book, a section, a floor, or a question when it was ambiguous."""
        self.session.last_action = "where"
        if place.kind == "ambiguous":
            choices = [{"id": c.choice_id, "label": c.title[0].upper() + c.title[1:]} for c in place.choices]
            self.last_choices = [c["id"] for c in choices]
            self.session.expect = expect_choice([(c["id"], c["label"]) for c in choices])
            names = join_names([c["label"] for c in choices]).replace(" and ", " or ")
            await self.say(f"Did you mean {names}?", "neutral")
            if self.publish is not None:
                await self.publish(ChoicesOffered("Which one did you mean?", tuple(choices)))
            self.session.last_action = "choose"
            return
        if place.kind == "book":
            self.guide_target = self.target_for(place.isbn, place.title)
            await (self.guide() if take_me else self.tell_where(self.guide_target))
            return
        if place.kind == "floor":
            await self.tell_floor(place.floor)
            return
        if place.kind == "facility":
            await self.tell_facility(place)
            return
        shelves = [s for s in self.finder.graph.shelves.values() if s.section == place.section] \
            if self.finder is not None and self.finder.graph is not None else []
        if not shelves:
            await self.say(f"{place.title[0].upper()}{place.title[1:]} is on level {place.floor}.", "happy")
            return
        from robot.services.book_graph import Area
        shelves.sort(key=lambda s: s.id)
        area = Area(place.topic or place.section, shelves[0], tuple(s.id for s in shelves),
                    "topic" if place.topic else "section")
        self.guide_target = self.target_for_area(area)
        await (self.guide() if take_me else self.tell_area(area, self.guide_target))

    async def tell_facility(self, place) -> None:
        """Where the toilets, the help desk, the exit or the entrance are, from the floor plan: the one nearest the robot
        (with how far, and what it is next to), and the others. Information only: it does not offer to guide."""
        name, verb, _ = FACILITIES[place.landmark]
        sentence = name[0].upper() + name[1:]
        found = self.plan.find(place.landmark) if self.plan is not None else []
        if not found:
            ask = "Please ask a member of staff." if place.landmark == "help_desk" else "The help desk can tell you."
            await self.say(f"I don't have {name} on my map yet. {ask}", "neutral")
            return
        here = self.plan.station
        found.sort(key=lambda fm: (here is not None and fm[0].level != here.level,
                                   abs(fm[0].level - (here.level if here else fm[0].level)), fm[0].level))
        lead = ""
        if place.requested:                                      # "the toilets on level 3"
            on_floor = [fm for fm in found if fm[0].level == place.floor]
            if on_floor:
                found = on_floor + [fm for fm in found if fm[0].level != place.floor]
            else:
                bare = name.removeprefix("the ")
                lead = f"There {'are' if verb == 'are' else 'is'} no {bare} on level {place.floor}. "
        floor, mark = found[0]
        words = [f"{sentence} {verb} on {floor_phrase(floor)}"]
        near = sorted((abs(m.x - mark.x) + abs(m.y - mark.y), m.name) for m in floor.landmarks if m is not mark and m.kind != mark.kind)
        if near and near[0][0] <= 4.0:
            words.append(f"{'next to' if near[0][0] <= 2.0 else 'near'} the {near[0][1].lower()}")
        if here is not None and here.level == floor.level:
            words.append(f"about {max(1, round(abs(here.x - mark.x) + abs(here.y - mark.y)))} m from here")
        text = lead + ", ".join(words) + "."
        others = [floor_phrase(f) for f, _ in found[1:] if f.level != floor.level]
        if others:
            text += f" There {'are more' if verb == 'are' else 'is another'} {'on ' + join_names(others)}."
        if self.publish is not None:
            await self.publish(LocationResolved("landmark", name, floor=floor.level, x=mark.x, y=mark.y))
        self.session.last_action = "answer"
        await self.say(text, "happy")

    async def tell_floor(self, level: int) -> None:
        names = self.plan.sections_on(level) if self.plan is not None else []
        if not names and self.finder is not None and self.finder.graph is not None:
            names = sorted({s.section for s in self.finder.graph.shelves.values() if s.floor == level and s.section})
        listed = join_names([f"the {n} section" for n in names]) if names else "no sections I know of"
        if self.publish is not None:
            await self.publish(LocationResolved("floor", f"Level {level}", floor=level))
        await self.say(f"Level {level} has {listed}.", "happy")
        if names:
            self.last_choices = [f"section:{n}" for n in names]

    async def publish_location(self, target: GuideTarget, take_me: bool = False) -> None:
        loc = target.location
        if self.publish is None or loc is None or loc.floor is None:
            return
        kind = "book" if target.isbn else "section"
        await self.publish(LocationResolved(kind, target.title, target.isbn, loc.section, loc.floor, loc.shelf,
                                            loc.map_x, loc.map_y, take_me))

    @staticmethod
    def target_for_area(area) -> GuideTarget:
        shelf = area.shelf
        label = f"the {area.name} section" if area.kind == "section" else f"the {area.name} books"
        return GuideTarget("", label, Location("", shelf.id, shelf.section, shelf.floor, shelf.zone, shelf.map_x,
                                               shelf.map_y, shelf.map_yaw))

    async def tell_area(self, area, target: GuideTarget) -> None:
        shelf = area.shelf
        shelves = " and ".join(area.shelves) if len(area.shelves) <= 2 else f"{area.shelves[0]} to {area.shelves[-1]}"
        if area.kind == "section":
            where = f"The {area.name} section is on level {shelf.floor}, on shelf {shelves}."
        else:
            where = (f"Books about {area.name} are in the {shelf.section} section on level {shelf.floor}, "
                     f"on shelf {shelf.id}.")
        offer = " Would you like me to guide you there?" if target.can_drive else ""
        self.awaiting_guide = target.can_drive
        await self.publish_location(target)              # the map first, then the words
        await self.say(where + offer, "happy")

    async def tell_where(self, target: GuideTarget) -> None:
        if (stored := self.stored_copy(target.isbn)) is not None:
            await self.offer_stored(stored)
            return
        where = target.location.describe() if target.location else ""
        if not where:
            reason = self.finder.locate_with_reason(target.isbn)[1] if self.finder is not None else ""
            log.info("[dialogue] no location for %s: %s", target.isbn, reason)
            await self.say(f"I don't know where {target.title} is shelved yet. The help desk can tell you.", "neutral")
            return
        offer = " Would you like me to guide you there?" if target.can_drive else ""
        self.awaiting_guide = target.can_drive
        await self.publish_location(target)
        await self.say(f"{target.title} is {where}.{offer}", "happy")

    async def guide(self) -> None:
        """Lead the visitor to the current target: say where, and publish NavigationRequested with the pose."""
        target, book = self.guide_target, self.session.book
        if target is None and book is not None and book.found_in_catalog:
            target = GuideTarget(book.isbn, book.title or "that book", location_of(book))
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
        await self.publish_location(target, take_me=True)
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

    async def answer_question(self, question: str) -> Optional[str]:
        """The LLM's answer to a free question about the scanned book, from its catalogue entry; None if
        there is no LLM, the book isn't in the catalogue (it would answer from memory) or the server fails."""
        record = self.catalogue_record(self.session.book)
        if record is None or self.assistant is None:
            return None
        async with self.thinking():
            return await self.assistant.answer(record, question)

    def catalogue_fact(self, text: str) -> Optional[str]:
        """Who wrote it / what genre, straight from the catalogue, for when the LLM can't answer."""
        book = self.session.book
        if book is None or not book.title:
            return None
        if book.author and any(w in text for w in AUTHOR_WORDS):
            return f"{book.title} was written by {book.author}."
        record = self.catalogue_record(book)
        if record is not None and record.genre and any(w in text for w in GENRE_WORDS):
            return f"{book.title} is {record.genre}."
        return None

    def catalogue_record(self, book):
        """The full catalogue Book for what was scanned (None if it is not in the catalogue)."""
        if book is None or not book.found_in_catalog or self.finder is None:
            return None
        return self.finder.catalog.lookup(book.isbn)

    async def act_explain(self, book, title: str) -> None:
        record = self.catalogue_record(book)
        if record is not None and self.publish is not None:
            await self.publish(BookFocused(record.isbn13, "explain"))
        if record is not None and self.assistant is not None:
            async with self.thinking():
                spoken = await self.assistant.explain(record)
            if spoken:
                await self.say(spoken, "happy")
                self.session.last_action = "explain"
                return
        summary = book.summary if book else None
        await self.say(f"{title}: {summary}" if summary else
                       f"I don't have a summary for {title}. The help desk can tell you more.",
                       "neutral" if not summary else "happy")
        self.session.last_action = "explain"

    async def act_recommend(self, book, title: str) -> None:
        if book is None or not book.found_in_catalog or self.finder is None:
            await self.say(f"I can only recommend from books in our catalogue, and {title} isn't in it.",
                           "neutral")
            return
        n = self.get_style().recommendations
        picks = self.finder.recommend(book.isbn, n)
        loose = False
        if not picks:                                       # nothing close: the nearest ones, said honestly
            picks = self.finder.recommend(book.isbn, n, min_score=LOOSE_MIN_SCORE)
            loose = bool(picks)
        if not picks:
            self.session.recommend_failed = self.session.awaiting_interest = True
            await self.say(f"I don't have anything quite like {title} yet. What kind of books do you like? Tell me a "
                           "topic, a title or an author.", "neutral")
            return
        self.session.recommend_failed = loose
        for pick in picks:                                  # the full reasons go to the log (and to the LLM below)
            log.info("[dialogue] candidate %s (%.2f): %s", pick.book.title, pick.total, "; ".join(pick.reasons))
        await self.publish_recommendations(picks, source_isbn=book.isbn)
        if self.assistant is not None:
            async with self.thinking():
                spoken = await self.assistant.recommend(self.finder.catalog.lookup(book.isbn), picks)
            if spoken:
                best = self.first_mentioned(spoken, picks)
                self.guide_target = self.target_for(best.book.isbn13, best.book.title)
                self.focus_on(best.book)
                await self.say(spoken, "happy")
                return
        best = picks[0]
        self.guide_target = self.target_for(best.book.isbn13, best.book.title)
        self.focus_on(best.book)
        text = self.recommendation_text(title, picks)
        if loose:
            text = f"I don't have anything very close to {title}, but {text[0].lower()}{text[1:]}"
        await self.say(text, "happy")

    async def recommend_by_interest(self, interest: str) -> None:
        """Recommend from what the visitor says they like, with no book on the pad."""
        picks = self.finder.recommend_interest(interest, self.get_style().recommendations) if self.finder else []
        if not picks:
            await self.say(f"I couldn't find a book about {interest} in the catalogue. You could place a book you like "
                           "on my pad, and I'll find something similar.", "neutral")
            return
        for pick in picks:
            log.info("[dialogue] candidate for %r: %s (%s)", interest, pick.book.title, pick.because)
        await self.publish_recommendations(picks, interest=interest)
        best = picks[0]
        self.guide_target = self.target_for(best.book.isbn13, best.book.title)
        self.focus_on(best.book)
        await self.say(self.recommendation_text(None, picks, interest), "happy")

    async def recommend_pick(self) -> None:
        """"Recommend me one" with nothing to go on: the robot chooses, from the catalogue, instead of asking again. It names
        the book and reads the first line of its summary, never the same book twice in a visit; asked again, it offers the next."""
        s = self.session
        picks = self.choose_picks(max(1, int(self.get_style().recommendations)))
        if not picks:
            await self.say("I've suggested all I have for now. Tell me a topic or a title and I'll look again, "
                           "or ask at the help desk.", "neutral")
            return
        s.offered.update(p.book.isbn13 for p in picks)
        for pick in picks:
            log.info("[dialogue] picked %s (%s)", pick.book.title, pick.because)
        await self.publish_recommendations(picks)
        best = picks[0].book
        self.guide_target = self.target_for(best.isbn13, best.title)
        self.focus_on(best)
        await self.say(self.pick_text(picks), "happy")

    def choose_picks(self, n: int) -> list:
        """Up to n catalogue books for a visitor who named no topic: the librarians' picks first, then the books whose topics
        the most other books share (the popular ones). Never one already suggested this visit, handed in, or in hand. The
        same visit gets the same suggestions in the same order."""
        from types import SimpleNamespace
        if self.finder is None or getattr(self.finder, "graph", None) is None:
            return []
        s = self.session
        skip = s.offered | s.handed_in | ({s.book.isbn} if s.book is not None else set()) | ({s.focus} if s.focus else set())
        graph = self.finder.graph
        counts = graph.topic_counts()
        liked = {link.to_isbn for link in graph.links if link.relation == "librarian_pick"}
        weight = lambda b: sum(counts.get(t, 0) for t in graph.topics_of(b.isbn13))
        books = sorted((b for b in self.finder.catalog.books if b.isbn13 not in skip),
                       key=lambda b: (b.isbn13 not in liked, -weight(b), b.title))
        picks = []
        for book in books[:n]:
            topics = [t for t in graph.topics_of(book.isbn13) if t in counts]
            because = ("the librarians recommend it" if book.isbn13 in liked else
                       f"a popular choice in {max(topics, key=lambda t: counts[t])}" if topics else "a good place to start")
            picks.append(SimpleNamespace(book=book, because=because))
        return picks

    @staticmethod
    def pick_text(picks: list) -> str:
        """"How about Dune by Frank Herbert? <the first line of its summary>" (several: the names, then the first one's line)."""
        names = [f"{p.book.title} by {p.book.author}" if p.book.author else p.book.title for p in picks]
        best = picks[0].book
        line = re.split(r"(?<=[.!?])\s", (best.summary or "").strip())[0]
        if len(picks) == 1:
            return f"How about {names[0]}? {line}".strip()
        return f"Here are a few to try: {join_names(names)}. I'd start with {best.title}: {line}".strip()

    def recommend_options(self) -> list:
        """The buttons under "what kind of books do you like?": the topics with the most books, and "Surprise me"."""
        graph = getattr(self.finder, "graph", None) if self.finder is not None else None
        counts = graph.topic_counts() if graph is not None else {}
        top = sorted(counts, key=lambda t: (-counts[t], t))[:3]
        return [{"id": f"topic:{t}", "label": t.title()} for t in top] + [{"id": "surprise", "label": "Surprise me"}]

    def focus_on(self, book) -> None:
        """The book just recommended is what "that book" means in the next question. With no book on the pad it becomes
        the topic (like a title they named), so "explain that", "who wrote it?" and "where is it?" are about it."""
        s = self.session
        s.focus = book.isbn13
        if s.book is None or s.virtual:
            s.book, s.virtual = self.identified(book), True

    def referent(self, low: str, isbn: str = "") -> tuple:
        """(ISBN, explicit) of the book "that book" / "that one" means: the one named, else the one just recommended.
        explicit: it replaces a book on the pad as the topic (they said "that", not "it")."""
        s = self.session
        if isbn or not s.focus:
            return isbn, False
        if s.book is not None and not s.virtual and s.book.isbn != s.focus and re.search(r"\bthat (?:one|book)\b", low):
            return s.focus, True
        return "", False

    async def publish_recommendations(self, picks: list, source_isbn: str = "", interest: str = "") -> None:
        """Tell the screen (a list of covers with one reason each); "the second one" then means the second pick."""
        self.last_choices = [f"book:{p.book.isbn13}" for p in picks]
        if self.publish is not None:
            await self.publish(RecommendationsReady(tuple({"isbn": p.book.isbn13, "because": p.because} for p in picks),
                                                    source_isbn, interest))

    @staticmethod
    def recommendation_text(title: Optional[str], picks: list, interest: Optional[str] = None) -> str:
        """The catalogue wording of a recommendation, with ONE reason (the top pick's), never a list of topics:
        "If you like Cosmos, you might enjoy A Brief History of Time by Stephen Hawking because the librarians
        suggest it after Cosmos." With several picks they are named first and the reason goes to the top one."""
        names = [f"{p.book.title} by {p.book.author}" if p.book.author else p.book.title for p in picks]
        best = picks[0]
        like = f"If you like {title}" if title else f"If you're interested in {interest}"
        because = best.because or (f"because it's similar to {title}" if title else "because it fits")
        if len(picks) == 1:
            return f"{like}, you might enjoy {names[0]} {because}."
        return f"{like}, you might enjoy {join_names(names)}. I'd start with {best.book.title} {because}."

    @staticmethod
    def first_mentioned(spoken: str, picks: list):
        """The pick whose title comes first in the spoken recommendation (the LLM has to name one of them)."""
        low = spoken.lower()
        found = [(low.index(p.book.title.lower()), i, p) for i, p in enumerate(picks) if p.book.title.lower() in low]
        return min(found)[2] if found else picks[0]

    # ---- what can happen next (for the operator page) ----
    def next_actions(self) -> list:
        """The ways out of the current state, from the table: [{"label", "to", "why", "command", "arg", "in_s"}].
        `command`/`arg` is the operator command that makes it happen (None when it happens by itself: a timer, or
        the robot's own follow-up); `in_s` is the seconds left on a timer."""
        leaf, spec, found, taken = self.leaf, self.fsm.spec, {}, set()
        for t in spec.effective_transitions(leaf):
            if t.target is None or t.event == "Utterance":
                continue
            key = ("Choice", GUARD_PICK.get(t.guard or "")) if t.event == "Choice" else (t.event, None)
            if key in taken or not self._would_pass(t):                  # the first transition that passes wins
                continue
            taken.add(key)
            anchor = t.target.state if isinstance(t.target, (History, Previous)) else t.target
            to = spec.path(spec.leaf_of(anchor)) if isinstance(t.target, str) else f"{anchor} (where it was)"
            pick = GUARD_PICK.get(t.guard or "") if t.event == "Choice" else None
            command = ("pick", pick) if pick else OPERATOR_FOR.get(t.event)
            label = (f"Visitor picks: {pick}" if pick else EVENT_LABELS.get(t.event, t.event))
            in_s = self.fsm.time_left(leaf, t.event) if t.event not in OPERATOR_FOR else None
            why = GUARD_WHY.get((t.guard or "").removeprefix("not "), t.note) or EVENT_WHY.get(t.event, "")
            if (t.guard or "").startswith("not "):
                why = f"not: {why}"
            found.setdefault((label, to), {"label": label, "to": to, "why": why or "",
                                           "command": command[0] if command else None,
                                           "arg": command[1] if command else "",
                                           "in_s": round(in_s, 1) if in_s is not None else None})
        return sorted(found.values(), key=lambda a: (a["command"] is None, a["to"]))

    def _would_pass(self, t) -> bool:
        """Whether the transition's guard holds right now, if the event it waits for came (a button press for Choice)."""
        if not t.guard:
            return True
        pick = GUARD_PICK.get(t.guard.removeprefix("not "))
        payload = ChoiceInfo(pick or "", text=pick or "") if t.event == "Choice" else None
        try:
            ok = bool(getattr(self, t.guard.removeprefix("not "))(Event(t.event, payload)))
        except Exception:                      # a guard that needs more than a button press: show it
            return True
        return (not ok) if t.guard.startswith("not ") else ok

    def state_tree(self) -> list:
        """[[top-level state, [its children...]], ...] in table order, for drawing the state map."""
        spec = self.fsm.spec
        return [[n, spec.children(n)] for n in spec.states if spec.parent(n) is None]

    # ---- operator controls ----
    def sample_book(self) -> Optional[BookIdentified]:
        """A catalogue book as the reader would report it, for the operator's goto into a state that needs one."""
        if self.finder is None or not self.finder.catalog.books:
            return None
        book = next((b for b in self.finder.catalog.books if self.finder.locate(b.isbn13)
                     and self.finder.locate(b.isbn13).has_pose), self.finder.catalog.books[0])
        return self.identified(book)

    def identified(self, book) -> BookIdentified:
        place = self.finder.locate(book.isbn13)
        return BookIdentified(isbn=book.isbn13, title=book.title, author=book.author or None, summary=book.summary,
                              shelf=(place.shelf if place else None) or book.shelf, slot=book.slot,
                              found_in_catalog=True, call_number=book.call_number or None,
                              section=place.section if place else None, floor=place.floor if place else None,
                              zone=place.zone if place else None, map_x=place.map_x if place else None,
                              map_y=place.map_y if place else None, map_yaw=place.map_yaw if place else None)

    def fill_storage(self, count: int = 2) -> None:
        """Put `count` sample books (on different shelves, with map positions) into storage if it is empty."""
        if not self.storage.empty or self.finder is None:
            return
        seen = set()
        for book in self.finder.catalog.books:
            place = self.finder.locate(book.isbn13)
            if place is None or not place.has_pose or place.shelf in seen:
                continue
            seen.add(place.shelf)
            slot = self.storage.reserve(book.slot)
            if slot is None:
                break
            self.storage.commit(slot, StoredBook(isbn=book.isbn13, title=book.title, shelf=place.shelf,
                                                 section=place.section, floor=place.floor, zone=place.zone,
                                                 map_x=place.map_x, map_y=place.map_y, map_yaw=place.map_yaw))
            if len(seen) >= count:
                break

    async def goto(self, text: str, source: str = "operator") -> str:
        """Force the machine into a state, filling in whatever context it needs (a sample book, stored books). Returns
        the path it arrived at."""
        await self.start()
        name = self.fsm.spec.resolve(text)
        leaf = self.fsm.spec.leaf_of(name)
        if "ENGAGED" in self.fsm.spec.chain(leaf) and not self.fsm.is_in("ENGAGED"):
            self.session = Session()
        if leaf in ("SCAN", "STORE", "ANSWER", "RECOMMEND", "ANYTHING_ELSE") and self.session.book is None:
            self.session.book = self.sample_book()
        if leaf == "OFFER" and self.session.book is None:
            self.session.book = None
        if "RETURNING" in self.fsm.spec.chain(leaf):
            self.fill_storage(2)
            self.stops = self.storage.stops()
            if self.stops:
                self.current_shelf = self.stops[0].shelf
        await self.fsm.force(leaf, source)
        return self.state

    async def reset(self, everything: bool = False) -> None:
        """Back to IDLE with a clean slate: the session, any paused trip and the guide target are forgotten. The stored
        books are kept unless `everything`."""
        await self.start()
        self.session = Session()
        self.guide_target, self.awaiting_guide = None, False
        self.stops, self.current_shelf = [], None
        self.returning_paused = self.interrupted_returning = False
        self.present = self.leave_pending = False
        self.last_activity = None
        self.fsm.forget("RETURNING")
        if everything:
            self.storage.clear()
        await self.fsm.force("IDLE", "operator")
