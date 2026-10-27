"""What a run records: one timeline of everything that happened, in order, with robot time and real time.

Every entry is a dict {"n": line number, "t": robot seconds since the run began, "rt": real seconds, "kind": ..., ...}.
The kinds: step (what the pretend visitor did), state (a transition), said (the robot's sentence, with its voice),
screen (a ScreenUpdate), cmd (an ESP32 command), chime, nav (NavigationRequested), llm (a request, reply or what the
robot did with it), log (a warning or error from the robot) and error. Checks and the transcript read this timeline;
nothing here changes how the robot behaves: it subscribes to the bus and wraps the actuators to observe them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Callable, Optional

from robot.events import (NavigationRequested, ScreenUpdate, StateTransition, ThinkingEnded, ThinkingStarted)
from robot.services.book_llm import BookAssistant

SKIP_EVENTS = {"StateEntered", "ThinkingStarted", "ThinkingEnded", "ReplySentence", "ExpressionRequested", "StyleChanged", "ContextChanged"}
TEXT_LIMIT = 400


def key_fields(view: str, p: dict) -> dict:
    """The few payload fields of a screen worth reading in a transcript and checking."""
    if view == "book_card":
        return {"title": p.get("title"), "badge": p.get("badge"), "level": p.get("level"), "section": p.get("section"),
                "shelf": p.get("shelf"), "buttons": [b.get("label") for b in p.get("buttons", [])]}
    if view == "book_list":
        return {"heading": p.get("heading"), "items": [{"isbn": i.get("isbn"), "title": i.get("title"),
                                                         "reason": i.get("reason")} for i in p.get("items", [])]}
    if view == "directions":
        return {"kind": p.get("kind"), "title": p.get("title"), "level": p.get("level"), "section": p.get("section"),
                "shelf": p.get("shelf"), "steps": p.get("steps"), "distance_m": p.get("distance_m"),
                "other_floor": p.get("other_floor"), "buttons": [b.get("label") for b in p.get("buttons", [])]}
    if view == "options":
        return {"prompt": p.get("prompt"), "options": [o.get("label") for o in p.get("options", [])]}
    if view == "status":
        return {"heading": p.get("heading"), "destination": p.get("destination"), "books_left": p.get("books_left"),
                "detail": p.get("detail")}
    return {"caption": p.get("caption")} if p.get("caption") else {}


class Recorder:
    def __init__(self, clock: Callable[[], float], real=time.monotonic):
        self.clock, self.real = clock, real
        self._t0, self._r0 = clock(), real()
        self.entries: list = []
        self.event_names: list = []                    # [(line number, event class name)]
        self.last_screen: Optional[dict] = None
        self.lock = threading.Lock()

    def add(self, kind: str, **fields) -> dict:
        with self.lock:
            entry = {"n": len(self.entries) + 1, "t": round(self.clock() - self._t0, 3),
                     "rt": round(self.real() - self._r0, 4), "kind": kind, **fields}
            self.entries.append(entry)
            return entry

    # ---- the bus ----
    def attach(self, bus) -> None:
        bus.subscribe(object, self.on_event)

    def on_event(self, event) -> None:
        name = type(event).__name__
        if isinstance(event, StateTransition):
            if not event.internal or event.event != "greeted":
                self.add("state", **{"from": event.from_path, "to": event.to_path, "event": event.event,
                                     "source": event.source, "zone": event.zone, "internal": event.internal})
        elif isinstance(event, ScreenUpdate):
            fields = key_fields(event.view, event.payload)
            entry = self.add("screen", view=event.view, layout=event.layout, state_path=event.state_path,
                             zone=event.zone, key=fields, thinking=event.thinking)
            self.last_screen = {"view": event.view, "payload": event.payload, "options": list(event.options),
                                "n": entry["n"]}
        elif isinstance(event, (ThinkingStarted, ThinkingEnded)):
            self.add("thinking", phase="start" if isinstance(event, ThinkingStarted) else "end",
                     ms=getattr(event, "ms", None))
        elif isinstance(event, NavigationRequested):
            self.add("nav", title=event.title, isbn=event.isbn, shelf=event.shelf, section=event.section,
                     floor=event.floor, x=event.x, y=event.y)
        if name not in SKIP_EVENTS:
            with self.lock:
                self.event_names.append((len(self.entries), name))

    # ---- the actuators ----
    def wrap_actuators(self, actuators, esp32) -> None:
        deliver = actuators.deliver

        def recording_deliver(text, style):
            spoken = "speech" in style.channels
            self.add("said", text=text, voice=style.voice if spoken else "none", spoken=spoken,
                     channels=list(style.channels))
            return deliver(text, style)
        actuators.deliver = recording_deliver
        chime = actuators.voice.chime

        def recording_chime(style):
            self.add("chime", volume=round(style.volume * 0.6, 2))
            return chime(style)
        actuators.voice.chime = recording_chime
        send = esp32.send

        def recording_send(cmd, **params):
            self.add("cmd", cmd=cmd, params=params)
            return send(cmd, **params)
        esp32.send = recording_send

    # ---- logging ----
    def log_handler(self) -> logging.Handler:
        recorder = self

        class Handler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING and record.name.startswith("robot"):
                    recorder.add("log", level=record.levelname, logger=record.name, message=record.getMessage()[:TEXT_LIMIT],
                                 exception=bool(record.exc_info))
        handler = Handler(level=logging.WARNING)
        return handler


class RecordingClient:
    """Wraps an LLM client (chat(messages) -> str) and records every request and reply with its real latency."""

    def __init__(self, client, recorder: Recorder):
        self.client, self.recorder = client, recorder

    def chat(self, messages):
        started = time.monotonic()
        prompt = messages[-1]["content"] if messages else ""
        self.recorder.add("llm", phase="request", prompt=prompt[:TEXT_LIMIT])
        try:
            reply = self.client.chat(messages)
        except Exception as exc:                          # LLMError normally: the dialogue falls back to the catalogue
            self.recorder.add("llm", phase="error", error=str(exc)[:TEXT_LIMIT], ms=round((time.monotonic() - started) * 1000))
            raise
        timing = getattr(self.client, "last_timing", None) or {}
        self.recorder.add("llm", phase="reply", reply=str(reply)[:TEXT_LIMIT], ms=round((time.monotonic() - started) * 1000),
                          first_byte_ms=timing.get("first_byte_ms"), total_ms=timing.get("total_ms"))
        return reply

    def __getattr__(self, name):
        return getattr(self.client, name)


class RecordingAssistant(BookAssistant):
    """A BookAssistant that records whether each reply was used, or discarded for the catalogue fallback."""

    def __init__(self, client, recorder: Recorder):
        super().__init__(RecordingClient(client, recorder))
        self.recorder = recorder

    async def _outcome(self, method: str, coroutine):
        reply = await coroutine
        self.recorder.add("llm", phase="used" if reply else "fallback", method=method,
                          reply=(reply or "")[:TEXT_LIMIT])
        return reply

    async def explain(self, book):
        return await self._outcome("explain", super().explain(book))

    async def recommend(self, book, candidates):
        return await self._outcome("recommend", super().recommend(book, candidates))

    async def answer(self, book, question):
        return await self._outcome("answer", super().answer(book, question))

    async def chat(self, said):
        return await self._outcome("chat", super().chat(said))
