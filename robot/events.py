"""Events carried on the bus. Each is a plain dataclass; `timestamp` is filled in automatically.

Perception publishes ContextChanged, PersonEngaged, PersonNearbyBusy, PersonLeft, BookPlaced, BookIdentified (a
BookReader read the book), ScanFailed, BookDropped, UserUtterance, UserChoice and EmotionDetected.
Core publishes StyleChanged, StateTransition, ScreenUpdate, ReplySentence, ExpressionRequested and
NavigationRequested for actuation and the display to act on.

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
class ScreenUpdate:
    """What the touch screen should show for the state just entered (placeholder layouts for now): the state path,
    the zone, a layout name, a title, some text and the option buttons (each {"id", "label"}) when there are any."""
    state_path: str
    zone: str = "common"
    layout: str = "full_face"
    title: str = ""
    text: str = ""
    options: tuple = ()
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
