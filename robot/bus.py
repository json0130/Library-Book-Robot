"""In-process publish/subscribe on asyncio.

    bus = Bus()
    bus.subscribe(BookScanned, on_book)        # sync or async handler
    await bus.publish(BookScanned("123"))

Handlers for an event run one after another in the order they subscribed, so ordering is
predictable. A handler subscribed to a base class (e.g. `object`) receives every event, in its
place in that order. An event published from inside a handler is delivered completely before
the outer publish continues (depth first).
A handler that raises is logged and skipped; the other handlers still run.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Union

log = logging.getLogger(__name__)

Handler = Callable[[Any], Union[None, Awaitable[None]]]


class Bus:
    def __init__(self) -> None:
        self._subs: list[tuple[type, Handler]] = []
        self.errors: list[tuple[Any, Handler, BaseException]] = []   # kept for inspection and tests

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._subs.append((event_type, handler))

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        if (event_type, handler) in self._subs:
            self._subs.remove((event_type, handler))

    def handlers_for(self, event: Any) -> list[Handler]:
        return [h for t, h in self._subs if isinstance(event, t)]

    async def publish(self, event: Any) -> int:
        """Deliver event to every matching handler; returns how many ran without error."""
        ok = 0
        for handler in self.handlers_for(event):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
                ok += 1
            except Exception as exc:              # isolate failures: keep delivering
                self.errors.append((event, handler, exc))
                log.exception("handler %r failed on %s", handler, type(event).__name__)
        return ok
