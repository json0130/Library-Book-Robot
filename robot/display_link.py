"""The way back from the display server: button taps on the touch screen and commands from the /operator page.

The display server is a separate process that only the robot talks to by HTTP. The robot POSTs what the screen should
show (actuation/face.py); this polls `GET /inbox?after=<n>` for what people did there, a few times a second:

    {"id": 7, "type": "choice",   "choice": "return"}             a button on the screen  -> UserChoice(source="screen")
    {"id": 8, "type": "operator", "command": "goto", "arg": "OFFER"}   the operator page -> Operator.run(...)

The first poll only notes where the inbox is, so taps from before the robot started are not replayed. If the
display server is not running the link tries again every few seconds, quietly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from robot.events import UserChoice

log = logging.getLogger("robot.display_link")
RETRY_S = 5.0


class DisplayLink:
    def __init__(self, bus, operator, host: str = "127.0.0.1", port: int = 8765, poll_s: float = 0.3):
        self.bus, self.operator = bus, operator
        self.host, self.port, self.poll_s = host, port, poll_s
        self.cursor: Optional[int] = None
        self.connected = False

    def _fetch(self) -> Optional[dict]:
        after = -1 if self.cursor is None else self.cursor
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/inbox?after={after}", timeout=2) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    async def step(self) -> int:
        """Poll once; returns how many inbox items were handled (None-safe when the server is away)."""
        data = await asyncio.get_running_loop().run_in_executor(None, self._fetch)
        if data is None:
            self.connected = False
            return -1
        self.connected = True
        first = self.cursor is None
        self.cursor = int(data.get("last", 0))
        if first:
            return 0
        handled = 0
        for item in data.get("items", []):
            await self.handle(item)
            handled += 1
        return handled

    async def handle(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "choice" and isinstance(item.get("choice"), str):
            await self.bus.publish(UserChoice(item["choice"], source="screen"))
        elif kind == "operator" and isinstance(item.get("command"), str):
            log.info("[operator] %s %s", item["command"], item.get("arg", ""))
            if not await self.operator.run(item["command"], str(item.get("arg", ""))):
                log.info("[operator] %r was not understood", item["command"])
        else:
            log.debug("[display] ignored inbox item %r", item)

    async def run(self) -> None:
        while True:
            try:
                result = await self.step()
            except Exception:                        # one bad item must not stop the link
                log.exception("display link failed on an inbox item")
                result = 0
            await asyncio.sleep(RETRY_S if result < 0 else self.poll_s)
