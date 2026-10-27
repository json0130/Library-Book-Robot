"""Keeps the AI server's language model warm, so a visitor's first question is not the slow one.

The first request after the model has been idle can take many seconds (the model is loaded or its cache is cold).
Two tiny requests deal with that, both in the background and both silent (nothing is spoken, shown or published
on the bus; the only trace is the log):

    warm-up     once, as the robot starts, without delaying the start-up
    keep-alive  every `ai_server.keepalive_s` seconds while the dialogue is IDLE (0 turns it off)

They use their own client, so they never share `last_timing` with a visitor's request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from robot.services.llm import LIBRARY_SYSTEM_PROMPT, LLMClient, LLMError

log = logging.getLogger("robot.warmup")

KEEPALIVE_DEFAULT_S = 240
RECHECK_S = 5.0                  # how often to look whether the robot has become idle, when a keep-alive is due
MESSAGES = [{"role": "system", "content": LIBRARY_SYSTEM_PROMPT},
            {"role": "user", "content": "Reply with the single word: ready."}]


class Warmup:
    def __init__(self, client, is_idle: Callable[[], bool], keepalive_s: float = KEEPALIVE_DEFAULT_S,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.is_idle, self.keepalive_s, self.clock = client, is_idle, float(keepalive_s), clock
        self.latencies: list = []                  # [(kind, ms or None)], newest last; for the tests and the logs

    @classmethod
    def from_app(cls, app) -> "Warmup":
        server = app.cfg.robot.get("ai_server") or {}
        client = LLMClient(app.cfg.ai_host, app.cfg.ai_port, float(server.get("timeout_s", 300)))      # it is in the background: let a cold model take its time
        keepalive = server.get("keepalive_s")
        return cls(client, lambda: app.dialogue.leaf == "IDLE",
                   KEEPALIVE_DEFAULT_S if keepalive is None else float(keepalive))

    async def ping(self, kind: str) -> Optional[int]:
        """One tiny request, off the event loop. Returns its latency in ms, or None if it failed (logged, never raised)."""
        started = self.clock()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.client.chat, list(MESSAGES))
        except LLMError as exc:
            log.warning("[llm] %s failed after %d ms: %s", kind, (self.clock() - started) * 1000, exc)
            self.latencies.append((kind, None))
            return None
        except Exception:                                      # a background nicety must never take the robot down
            log.exception("[llm] %s failed", kind)
            self.latencies.append((kind, None))
            return None
        ms = round((self.clock() - started) * 1000)
        log.info("[llm] %s answered in %d ms", kind, ms)
        self.latencies.append((kind, ms))
        return ms

    async def run(self) -> None:
        """The warm-up, then (if keepalive_s > 0) a keep-alive each time that long has passed since the last request
        and the robot is idle. Runs until cancelled."""
        await self.ping("warm-up")
        if self.keepalive_s <= 0:
            return
        last = self.clock()
        while True:
            await asyncio.sleep(max(0.0, last + self.keepalive_s - self.clock()))
            if not self.is_idle():                            # somebody is being helped: they keep the model warm
                await asyncio.sleep(min(RECHECK_S, self.keepalive_s))
                continue
            await self.ping("keep-alive")
            last = self.clock()
