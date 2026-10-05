"""Which of the seven emotions the robot's face shows.

The robot blends two signals:
  - the visitor's detected facial emotion (EmotionDetected, with a confidence 0..1), and
  - an optional emotion tag on the reply sentence (ReplySentence.emotion_tag, from the LLM).

blend() scores each candidate and picks the highest:
  - the reply tag scores TAG_WEIGHT (0.6): what the robot is saying sets its expression;
  - the visitor's emotion scores USER_WEIGHT * confidence (0.5 * c), after mapping it through
    EMPATHY: the robot mirrors happy/sad/surprise/neutral but answers anger and fear with
    concern (sad) and disgust with neutral, rather than copying them;
  - neutral always scores NEUTRAL_BASE (0.25), so weak or missing signals give a calm face.
Scores for the same emotion add up. Ties go to the tag, then the visitor's emotion, then neutral.
So a tag always wins over a differing user emotion (0.6 > 0.5), a confident user emotion
(confidence above 0.5) wins when there is no tag, and anything weaker stays neutral.
"""

from __future__ import annotations

from typing import Optional

EMOTIONS = ("happy", "sad", "angry", "surprise", "fear", "disgust", "neutral")
IDLE = "idle"

ALIASES = {
    "happy": "happy", "joy": "happy", "happiness": "happy", "smile": "happy",
    "sad": "sad", "sadness": "sad", "unhappy": "sad",
    "angry": "angry", "anger": "angry", "mad": "angry",
    "surprise": "surprise", "surprised": "surprise", "shock": "surprise",
    "fear": "fear", "scared": "fear", "afraid": "fear", "fearful": "fear",
    "disgust": "disgust", "disgusted": "disgust",
    "neutral": "neutral", "calm": "neutral", "normal": "neutral",
    "idle": IDLE, "rest": IDLE,
}

TAG_WEIGHT = 0.6
USER_WEIGHT = 0.5
NEUTRAL_BASE = 0.25
EMPATHY = {"happy": "happy", "sad": "sad", "surprise": "surprise", "neutral": "neutral",
           "angry": "sad", "fear": "sad", "disgust": "neutral"}


def normalize(name) -> Optional[str]:
    """Canonical emotion name (or "idle") for a name or alias, None if unknown."""
    return ALIASES.get(str(name).strip().lower()) if name is not None else None


def valid_names() -> list[str]:
    return list(EMOTIONS) + [IDLE]


def blend(user_emotion: Optional[str] = None, user_confidence: float = 0.0,
          reply_tag: Optional[str] = None) -> str:
    """One of the seven emotions for the face; see the module docstring for the weighting."""
    scores = {"neutral": NEUTRAL_BASE}
    order = []                                   # tie-break order: tag, user, neutral
    tag = normalize(reply_tag)
    if tag in EMOTIONS:
        scores[tag] = scores.get(tag, 0.0) + TAG_WEIGHT
        order.append(tag)
    user = normalize(user_emotion)
    if user in EMOTIONS:
        mapped = EMPATHY[user]
        conf = min(1.0, max(0.0, float(user_confidence)))
        scores[mapped] = scores.get(mapped, 0.0) + USER_WEIGHT * conf
        order.append(mapped)
    order.append("neutral")
    best = max(scores.values())
    return next(e for e in order if abs(scores.get(e, 0.0) - best) < 1e-9)
