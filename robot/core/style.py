"""Interaction style: how the robot behaves in a given Context.

The rules live in config/styles.yaml, one entry per <zone>_<alone|group>, plus `fixed` (the
study's baseline: same behaviour everywhere) and `default` (the safe fallback for anything not
listed, and the source of any field an entry leaves out).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from robot.core.context import Context

VOICES = ("normal", "whisper")
NECK_POSES = ("lowered", "upright")
LAYOUTS = ("full_face", "split")
CHANNELS = ("speech", "screen", "gaze", "spotlight", "print")


@dataclass(frozen=True)
class StyleVector:
    voice: str = "whisper"
    volume: float = 0.3
    channels: tuple = ("screen",)
    light_color: str = "#3a6fd8"
    brightness: float = 0.15
    eye_expression: str = "neutral"
    neck_pose: str = "lowered"
    screen_layout: str = "split"
    proactive: bool = False
    speed_limit: float = 0.3
    name: str = field(default="default", compare=False)   # which table entry produced it

    def as_dict(self) -> dict:
        d = asdict(self)
        d["channels"] = list(self.channels)
        return d

    def summary(self) -> str:
        return (f"{self.name}: voice={self.voice} vol={self.volume:g} channels={','.join(self.channels)} "
                f"light={self.light_color}@{self.brightness:g} eyes={self.eye_expression} "
                f"neck={self.neck_pose} screen={self.screen_layout} "
                f"proactive={'yes' if self.proactive else 'no'} speed<={self.speed_limit:g}m/s")


SAFE_DEFAULT = StyleVector()
_FIELDS = {f.name for f in fields(StyleVector)} - {"name"}


def _build(name: str, entry: dict, base: StyleVector) -> StyleVector:
    values = base.as_dict()
    values.update({k: v for k, v in (entry or {}).items() if k in _FIELDS})
    values["channels"] = tuple(c for c in values["channels"] if c in CHANNELS)
    values["name"] = name
    style = StyleVector(**values)
    if style.voice not in VOICES or style.neck_pose not in NECK_POSES or style.screen_layout not in LAYOUTS:
        raise ValueError(f"styles.yaml entry {name!r} has an invalid voice, neck_pose or screen_layout")
    return style


class StylePolicy:
    """Maps a Context to a StyleVector using the table from styles.yaml."""

    def __init__(self, table: dict):
        table = dict(table or {})
        self.default = _build("default", table.pop("default", {}), SAFE_DEFAULT)
        self.styles = {name: _build(name, entry, self.default) for name, entry in table.items()}

    @classmethod
    def from_file(cls, path: Path | str) -> "StylePolicy":
        import yaml
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})

    def style_for(self, context: Optional[Context], fixed: bool = False) -> StyleVector:
        """The style for this context; `fixed` ignores the context (baseline condition)."""
        if fixed:
            return self.styles.get("fixed", self.default)
        if context is None:
            return self.default
        return self.styles.get(context.key, self.default)
