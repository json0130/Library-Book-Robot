"""Events carried on the bus. Each is a plain dataclass; `timestamp` is filled in automatically.

Perception publishes ContextChanged, BookPlaced, BookScanned, ScanFailed, UserUtterance and EmotionDetected.
Core publishes StyleChanged, ReplySentence and ExpressionRequested for actuation to act on.
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
    a noise reading may override it (see core/context.py)."""
    zone: str
    group_size: int
    noise_db: Optional[float] = None
    timestamp: float = field(default_factory=_now)


@dataclass
class BookPlaced:
    """A book was put on the scan pad (before it is identified)."""
    timestamp: float = field(default_factory=_now)


@dataclass
class BookScanned:
    """The book on the pad was identified by its ISBN barcode. found_in_catalog is False when only
    the online fallback knew it (then summary, shelf and slot are None)."""
    isbn: str
    title: Optional[str] = None
    author: Optional[str] = None
    summary: Optional[str] = None
    shelf: Optional[str] = None
    slot: Optional[int] = None
    found_in_catalog: bool = False
    timestamp: float = field(default_factory=_now)


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
