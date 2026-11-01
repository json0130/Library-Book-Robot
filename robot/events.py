"""Events carried on the bus. Each is a plain dataclass; `timestamp` is filled in automatically.

Perception publishes ContextChanged, PersonEngaged, PersonNearbyBusy, PersonLeft, BookPlaced, BookIdentified (a
BookReader read the book), ScanFailed, BookDropped, UserUtterance, UserChoice and EmotionDetected.
Core publishes StyleChanged, StateTransition, ScreenUpdate, ReplySentence, ExpressionRequested and
NavigationRequested for actuation and the display to act on. The link to the base (drivers/turtlebot_link.py) publishes
LinkUp, LinkDown, PoseUpdated, NavProgress, NavArrived, NavFailed, NavCancelled and NoiseReading (docs/robot_link.md).

`source` says where an event came from: "sensor" (the real hardware or the camera), "operator" (the terminal or the
/operator page), "screen" (a touch button), "timeout" (a state's timer). It goes into the session log with every
state transition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _now() -> float:
    return time.time()


@dataclass
class ContextChanged:
    """Where the robot is and who is around. zone is the map zone ("quiet" or "common");
    a noise reading may override it (see core/context.py). person_ids are the numbers PersonTracker gave
    the people in view (person 1, person 2...); empty when the head count was typed or can't tell people apart."""
    zone: str
    group_size: int
    noise_db: Optional[float] = None
    person_ids: tuple = ()
    timestamp: float = field(default_factory=_now)
    zone_source: Optional[str] = None      # set by core/zones.py when the zone is already decided from the pose and the noise


@dataclass
class PersonEngaged:
    """Someone is facing the robot, close enough, for long enough: they want to talk. person_ids are the numbers
    PersonTracker gave the faces that are engaging (empty if it can't tell people apart)."""
    person_ids: tuple = ()
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class PersonNearbyBusy:
    """A person is near but not engaging (turned away, too far, busy): the robot may signal but should not start
    a conversation."""
    person_ids: tuple = ()
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class PersonLeft:
    """No face has been seen for a while: whoever was there has gone."""
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class BookPlaced:
    """A book was put on the scan pad (before it is identified)."""
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class BookDropped:
    """The pad has dropped the book into its storage slot (the pad's weight sensor, simulated by a timer or the
    operator's `dropped`)."""
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class BookIdentified:
    """A BookReader (the camera scanner today, RFID later) identified the book on the pad. found_in_catalog is False when only
    the online fallback knew it (then summary, shelf, slot and everything about where it lives are None).
    section, floor, zone and the map pose come from data/shelves.csv through the book graph; any of them
    may be None (a shelf without a pose, say)."""
    isbn: str
    title: Optional[str] = None
    author: Optional[str] = None
    summary: Optional[str] = None
    shelf: Optional[str] = None
    slot: Optional[int] = None
    found_in_catalog: bool = False
    call_number: Optional[str] = None
    section: Optional[str] = None
    floor: Optional[int] = None
    zone: Optional[str] = None                 # "quiet" or "common"
    map_x: Optional[float] = None
    map_y: Optional[float] = None
    map_yaw: Optional[float] = None
    timestamp: float = field(default_factory=_now)


BookScanned = BookIdentified               # the old name


@dataclass
class ScanFailed:
    """No ISBN could be read from the book on the pad; reason says why (for the log and the sim)."""
    reason: str
    timestamp: float = field(default_factory=_now)


@dataclass
class UserUtterance:
    """What the visitor said (speech-to-text) or typed."""
    text: str
    timestamp: float = field(default_factory=_now)
    source: str = "sensor"


@dataclass
class UserChoice:
    """The visitor tapped a button on the screen (or the operator picked for them): one of the option ids the
    last ScreenUpdate offered (return, ask, recommend, done, more, guide)."""
    choice: str
    timestamp: float = field(default_factory=_now)
    source: str = "screen"


@dataclass
class ReplySentence:
    """One sentence of the robot's reply, with an optional emotion tag from the LLM."""
    text: str
    emotion_tag: Optional[str] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class EmotionDetected:
    """The visitor's facial emotion from the camera (one of the seven), confidence 0..1."""
    emotion: str
    confidence: float
    timestamp: float = field(default_factory=_now)


@dataclass
class NavigationRequested:
    """The robot should lead the visitor to a shelf: drive to the map pose (x, y in metres, yaw in radians, on
    the map of `floor`). title and isbn say which book the visitor is going to find."""
    isbn: str
    title: str
    x: float
    y: float
    yaw: float
    shelf: Optional[str] = None
    section: Optional[str] = None
    floor: Optional[int] = None
    zone: Optional[str] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class StateTransition:
    """The state machine moved (or ran an internal transition). Paths look like "ENGAGED.OFFER"; `source` is where
    the causing event came from (sensor, operator, screen, timeout) and `zone` the zone in force."""
    from_path: str
    to_path: str
    event: str
    source: str = "sensor"
    zone: str = "common"
    internal: bool = False
    guard: Optional[str] = None
    action: Optional[str] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class StateEntered:
    """The state machine arrived in a state: what it said there (`text`) and the choices it offers (`options`, each
    {"id", "label"}). The screen composer (core/screen.py) decides what the screen shows; the machine does not."""
    state_path: str
    zone: str = "common"
    title: str = ""
    text: str = ""
    options: tuple = ()                   # touch buttons the style offers (quiet zones)
    choices: tuple = ()                   # the choices in this state, whatever the zone (for the options view)
    repair: bool = False                  # the robot did not understand twice: show the choices, always with "I'm done"
    timestamp: float = field(default_factory=_now)


@dataclass
class ScreenUpdate:
    """What the screen shows now, from the screen composer. `view` is face | book_card | book_list | directions |
    options | status and `payload` its content (a dict from one of the payload dataclasses in core/screen.py).
    `layout` is full_face (the face fills the screen) or split (content, with small eyes in a corner). `ttl_s`: how
    long the content stays without an interaction before the face comes back (None for the face). title, text and
    options are what the state machine said and offered, kept for the operator page and the log."""
    state_path: str
    zone: str = "common"
    layout: str = "full_face"
    title: str = ""
    text: str = ""
    options: tuple = ()
    view: str = "face"
    payload: dict = field(default_factory=dict)
    ttl_s: Optional[float] = None
    thinking: bool = False                # the robot is waiting for the LLM: the page shows a progress indicator
    timestamp: float = field(default_factory=_now)


@dataclass
class ThinkingStarted:
    """An utterance went to the LLM: the face looks like it is thinking and the screen shows a progress indicator."""
    timestamp: float = field(default_factory=_now)


@dataclass
class ThinkingEnded:
    """The LLM answered (or failed): `ms` is how long the robot waited."""
    ms: int = 0
    timestamp: float = field(default_factory=_now)


# ---- content for the screen (published by the dialogue, turned into views by core/screen.py) ----
@dataclass
class BookFocused:
    """The conversation is about this catalogue book now (explained, chosen from a list, named)."""
    isbn: str
    reason: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class RecommendationsReady:
    """Recommendations were given: picks are ({"isbn", "because"}, ...) in order, from a book or an interest."""
    picks: tuple
    source_isbn: str = ""
    interest: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class LocationResolved:
    """A place was found for the visitor: kind is book | section | floor. take_me: they asked to be taken there."""
    kind: str
    title: str = ""
    isbn: str = ""
    section: Optional[str] = None
    floor: Optional[int] = None
    shelf: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    take_me: bool = False
    timestamp: float = field(default_factory=_now)


@dataclass
class ChoicesOffered:
    """The visitor's words matched several things: they are asked to pick one. choices: ({"id", "label"}, ...)."""
    prompt: str
    choices: tuple
    timestamp: float = field(default_factory=_now)


@dataclass
class StoringBook:
    """A returned book is going into this storage slot."""
    isbn: str
    title: str
    slot: int
    timestamp: float = field(default_factory=_now)


@dataclass
class ReturningProgress:
    """Putting books back: phase is driving | unloading | announcing | homing; books_left are still in storage."""
    phase: str
    books_left: int
    section: Optional[str] = None
    shelf: Optional[str] = None
    floor: Optional[int] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class ExpressionRequested:
    """The face should show this emotion for hold_s seconds."""
    emotion: str
    hold_s: float = 3.0
    timestamp: float = field(default_factory=_now)


@dataclass
class StyleChanged:
    """The interaction style changed; style is a core.style.StyleVector."""
    style: Any
    context: Any = None
    timestamp: float = field(default_factory=_now)


# ---- the link to the base (the Dell on the TurtleBot, docs/robot_link.md) ----
@dataclass
class LinkUp:
    """The base answered after the link was lost (or for the first time)."""
    base_url: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class LinkDown:
    """No reply from the base for `lost_after_s`: the base stops by itself after 3 s without a heartbeat."""
    reason: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class PoseUpdated:
    """The base's pose in the RTAB-Map map frame (metres, radians). Not published for every poll: when it moved, when
    `localised` changed, and at least every few seconds."""
    x: float
    y: float
    yaw: float
    localised: bool = True
    frame: str = "map"
    timestamp: float = field(default_factory=_now)


@dataclass
class NavProgress:
    """The goal is being driven: metres left along the plan (None if the base does not know)."""
    goal_id: str
    distance_left: Optional[float] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class NavArrived:
    goal_id: str
    timestamp: float = field(default_factory=_now)


@dataclass
class NavFailed:
    """The goal did not succeed. reason: the base's (no_path, blocked, not_localised...) or ours (rejected: ..., link_down)."""
    goal_id: str
    reason: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class NavCancelled:
    goal_id: str
    reason: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class NoiseReading:
    """The base's microphones: `overall` in `unit` ("dB" calibrated, "dBFS" relative to full scale). robot_noise: taken while
    the robot itself was making noise, so it says nothing about the room."""
    overall: float
    left: Optional[float] = None
    right: Optional[float] = None
    balance: Optional[float] = None
    unit: str = "dBFS"
    calibrated: bool = False
    robot_noise: bool = False
    t: float = 0.0
    timestamp: float = field(default_factory=_now)
