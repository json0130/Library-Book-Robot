"""Load config/robot.yaml, styles.yaml and zones.yaml.

    cfg = load_config()                 # from config/ next to this repo
    cfg.robot["ai_server"]["host"]      # AI_HOST / AI_PORT environment variables override it
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


BOOKS_DEFAULTS = {
    "catalog_path": "data/catalog.csv",
    "online_fallback": True,
    "cache_dir": "data/cache",
    "scan_timeout_s": 5.0,
    "scanner_camera_index": 0,
}


@dataclass
class Config:
    robot: dict = field(default_factory=dict)
    styles: dict = field(default_factory=dict)
    zones: dict = field(default_factory=dict)

    @property
    def books(self) -> dict:
        """The `books:` section with defaults filled in."""
        merged = dict(BOOKS_DEFAULTS)
        merged.update(self.robot.get("books") or {})
        return merged

    @property
    def ai_host(self) -> str:
        return self.robot["ai_server"]["host"]

    @property
    def ai_port(self) -> int:
        return int(self.robot["ai_server"]["port"])

    def path(self, relative: str) -> Path:
        """A path from the config, resolved against the repo root."""
        p = Path(relative)
        return p if p.is_absolute() else ROOT / p


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


def load_config(config_dir: Path | str = CONFIG_DIR, env: dict | None = None) -> Config:
    config_dir = Path(config_dir)
    env = os.environ if env is None else env
    cfg = Config(robot=load_yaml(config_dir / "robot.yaml"),
                 styles=load_yaml(config_dir / "styles.yaml"),
                 zones=load_yaml(config_dir / "zones.yaml"))
    server = cfg.robot.setdefault("ai_server", {})
    server.setdefault("host", "127.0.0.1")
    server.setdefault("port", 7898)
    if env.get("AI_HOST"):
        server["host"] = env["AI_HOST"]
    if env.get("AI_PORT"):
        server["port"] = int(env["AI_PORT"])
    return cfg
