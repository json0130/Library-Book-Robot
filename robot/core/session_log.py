"""Study log: one JSON line per event with the context and style in force at the time.

Files go to data/logs/session-<start time>.jsonl (gitignored). Each line:
    {"t": 1730000000.12, "event": "BookScanned", "data": {...},
     "context": {"zone": "quiet", ...}, "style": {"name": "quiet_alone", ...}, "fixed": false}
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Optional


def _plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


class SessionLog:
    def __init__(self, directory: Path | str, session_id: Optional[str] = None):
        self.directory = Path(directory)
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.path = self.directory / f"session-{self.session_id}.jsonl"

    def write(self, event, context=None, style=None, **extra) -> dict:
        record = {"t": round(time.time(), 3), "event": type(event).__name__, "data": _plain(event),
                  "context": _plain(context), "style": _plain(style), **extra}
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record
