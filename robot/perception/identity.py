"""Tells visitors apart: the first face gets person 1, the next new face person 2, and a face that comes
back after being out of view is recognised as the same person.

Face boxes come from the AI server's detection. Each box is cut out of the camera frame and turned into a
128-number embedding by SFace (OpenCV's FaceRecognizerSF, model in models/face/, see tools/get_face_models.py).
Two embeddings of the same person point the same way; PersonTracker compares them by cosine similarity.

Privacy: the embeddings live only in this object's memory. Nothing is sent to the AI server, written to
disk or kept after the program ends, so "person 1" means one person for one run. (The server's own
enrollment, tools/face_enroll.py, keeps photos for good and is for named people.)

Matching is greedy and per frame: each known person is given to at most one face. A face whose box sits
where a person was a moment ago is accepted with a lower similarity, so turning your head doesn't make a
new person. A new person is only created from a face of a usable size.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("robot.identity")

FACE_SIZE = 112                 # SFace's input size
MIN_FACE_PX = 40                # smaller faces give unreliable embeddings
MARGIN = 0.12                   # context around the server's tight box
MAX_EMBEDDINGS = 6              # views kept per person (front, left, right, ...)
DIVERSE_BELOW = 0.92            # a new view is only kept if it differs from every stored one by this much
CONTINUITY_IOU = 0.3            # a box overlapping the person's last box this much counts as the same person...
CONTINUITY_RECENT_S = 3.0       # ...if it was seen this recently...
CONTINUITY_FACTOR = 0.6         # ...and its similarity reaches this fraction of the threshold


class EmbedderUnavailable(RuntimeError):
    """OpenCV, numpy or the SFace model file is missing."""


class FaceEmbedder:
    def __init__(self, model_path: Path | str):
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise EmbedderUnavailable(f"{exc}. Install with: sudo apt install python3-opencv python3-numpy") from exc
        path = Path(model_path)
        if not path.is_file():
            raise EmbedderUnavailable(f"face model not found: {path}. Run: python tools/get_face_models.py")
        if not hasattr(cv2, "FaceRecognizerSF"):
            raise EmbedderUnavailable("this OpenCV has no FaceRecognizerSF (needs 4.5.4 or newer)")
        self.cv2, self.np = cv2, np
        self.recognizer = cv2.FaceRecognizerSF.create(str(path), "")

    def embed(self, frame, box: Sequence[int]):
        """A unit-length 128-number vector for the face in `box` ([top, right, bottom, left]) or None if it
        is too small or outside the frame."""
        top, right, bottom, left = (int(v) for v in box)
        h, w = frame.shape[:2]
        mt, ml = int((bottom - top) * MARGIN), int((right - left) * MARGIN)
        top, bottom, left, right = max(0, top - mt), min(h, bottom + mt), max(0, left - ml), min(w, right + ml)
        if min(bottom - top, right - left) < MIN_FACE_PX:
            return None
        face = self.cv2.resize(frame[top:bottom, left:right], (FACE_SIZE, FACE_SIZE))
        vec = self.recognizer.feature(face).flatten().astype("float32")
        norm = float(self.np.linalg.norm(vec))
        return vec / norm if norm else None


def iou(a: Sequence[int], b: Sequence[int]) -> float:
    """Overlap of two [top, right, bottom, left] boxes: 0 apart, 1 identical."""
    top, right, bottom, left = max(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])
    inter = max(0, bottom - top) * max(0, right - left)
    union = (a[2] - a[0]) * (a[1] - a[3]) + (b[2] - b[0]) * (b[1] - b[3]) - inter
    return inter / union if union > 0 else 0.0


class Person:
    def __init__(self, number: int):
        self.number = number
        self.views: list = []                   # unit vectors
        self.box: Optional[Sequence[int]] = None
        self.seen_at = float("-inf")

    def similarity(self, vec) -> float:
        return max(float(v @ vec) for v in self.views) if self.views else -1.0

    def remember(self, vec) -> None:
        if not self.views or self.similarity(vec) < DIVERSE_BELOW:
            self.views.append(vec)
            del self.views[:-MAX_EMBEDDINGS]


class PersonTracker:
    """identify(frame, detections) -> a person number (1, 2, ...) per detection, in order; None for a
    face too small to tell apart."""

    def __init__(self, embedder, threshold: float = 0.40, clock=None):
        import time
        self.embedder, self.threshold = embedder, threshold
        self.clock = clock or time.monotonic
        self.people: list[Person] = []
        self.last_scores: list[dict] = []           # per detection of the latest call: {person number: similarity}

    def __call__(self, frame, detections: list[dict]) -> list[Optional[int]]:
        now = self.clock()
        boxes = [d["box"] for d in detections]
        vecs = [self.embedder.embed(frame, b) for b in boxes]
        pairs = []                                  # (score, face index, person) for every usable pairing
        self.last_scores = [{} for _ in vecs]
        for i, vec in enumerate(vecs):
            if vec is None:
                continue
            for person in self.people:
                score = person.similarity(vec)
                self.last_scores[i][person.number] = score
                near = (person.box is not None and now - person.seen_at <= CONTINUITY_RECENT_S
                        and iou(boxes[i], person.box) >= CONTINUITY_IOU)
                need = self.threshold * (CONTINUITY_FACTOR if near else 1.0)
                if score >= need:
                    pairs.append((score + (1.0 if near else 0.0), i, person))
        result: list[Optional[int]] = [None] * len(detections)
        taken: set[int] = set()
        for _, i, person in sorted(pairs, key=lambda p: -p[0]):
            if result[i] is None and person.number not in taken:
                result[i] = person.number
                taken.add(person.number)
                self._seen(person, vecs[i], boxes[i], now)
        for i, vec in enumerate(vecs):
            if vec is not None and result[i] is None:               # nobody known: a new person
                best = max(self.people, key=lambda p: p.similarity(vec), default=None)
                person = Person(len(self.people) + 1)
                self.people.append(person)
                result[i] = person.number
                self._seen(person, vec, boxes[i], now)
                log.info("[camera] new person: person %d%s", person.number,
                         f" (closest was person {best.number}, similarity {best.similarity(vec):.2f})" if best else "")
        return result

    @staticmethod
    def _seen(person: Person, vec, box, now: float) -> None:
        person.remember(vec)
        person.box, person.seen_at = box, now
