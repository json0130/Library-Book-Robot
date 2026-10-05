"""The robot's situation: which zone it is in and whether the visitor is alone or in a group.

derive_context() combines the map zone with an optional noise reading: a loud reading turns a
quiet area into a common one (and the reverse), using the thresholds in config/zones.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ZONES = ("quiet", "common")


@dataclass(frozen=True)
class Context:
    zone: str                       # effective zone: "quiet" or "common"
    noise_db: Optional[float] = None
    group_size: int = 1             # people with the visitor, counting them

    @property
    def social(self) -> str:
        """"alone" for one person (or nobody yet), "group" for two or more."""
        return "group" if self.group_size >= 2 else "alone"

    @property
    def key(self) -> str:
        """Style table key, e.g. "quiet_alone"."""
        return f"{self.zone}_{self.social}"


@dataclass(frozen=True)
class NoiseThresholds:
    quiet_below_db: float = 45.0
    common_above_db: float = 60.0

    @classmethod
    def from_zones(cls, zones_cfg: dict) -> "NoiseThresholds":
        noise = (zones_cfg or {}).get("noise", {}) or {}
        return cls(float(noise.get("quiet_below_db", cls.quiet_below_db)),
                   float(noise.get("common_above_db", cls.common_above_db)))


def zone_from_noise(map_zone: str, noise_db: Optional[float], t: NoiseThresholds) -> str:
    """The map zone, unless the noise reading is clearly on the other side of the band."""
    if noise_db is None:
        return map_zone
    if noise_db >= t.common_above_db:
        return "common"
    if noise_db <= t.quiet_below_db:
        return "quiet"
    return map_zone


def derive_context(map_zone: str, group_size: int, noise_db: Optional[float] = None,
                   thresholds: NoiseThresholds = NoiseThresholds(),
                   default_zone: str = "common") -> Context:
    """Effective Context from the map zone, head count and an optional noise reading.

    An unknown map zone falls back to default_zone; a negative head count counts as nobody.
    """
    zone = map_zone if map_zone in ZONES else default_zone
    return Context(zone=zone_from_noise(zone, noise_db, thresholds),
                   noise_db=noise_db, group_size=max(0, int(group_size)))


def resolve_area(area: str, zones_cfg: dict) -> str:
    """Map a named area (config/zones.yaml `map`) or a zone name to a zone."""
    if area in ZONES:
        return area
    zones_cfg = zones_cfg or {}
    return (zones_cfg.get("map") or {}).get(area, zones_cfg.get("default_zone", "common"))
