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


SIMILARITY_DEFAULTS = {             # see services/book_graph.py for what each component measures
    "topics": 0.30,                 # shared topics, IDF-weighted (a rare topic counts for more)
    "broader": 0.10,                # topics related through a broader topic
    "author": 0.12,
    "series": 0.15,                 # same series (the next book is always ranked first)
    "call_number": 0.08,            # shared prefix of the Dewey / LC call numbers
    "curated": 0.10,                # librarian_pick and related rows in curated_links.csv
    "text": 0.15,                   # TF-IDF similarity of summary and subjects
    "min_score": 0.08,              # books scoring less than this (0..1) are not recommended
    "general_topics": 0.4,          # a general topic (one with a narrower child in topics.yaml) counts for this fraction
}

BOOKS_DEFAULTS = {
    "catalog_path": "data/catalog.csv",
    "topics_path": "data/topics.yaml",
    "shelves_path": "data/shelves.csv",
    "links_path": "data/curated_links.csv",
    "online_fallback": True,
    "cache_dir": "data/cache",
    "scan_timeout_s": 5.0,
    "scanner_camera_index": 0,
    "use_llm": True,
    "reader": "camera",                      # how the book on the pad is read: camera (barcode) or rfid (not implemented yet)
    "floorplan_path": "data/floorplan.yaml",  # floors, sections and landmarks for the directions view
    "covers_dir": "data/covers",              # downloaded covers (tools/import_books.py --covers), gitignored
}


RETURNING_DEFAULTS = {
    "auto_start": True,             # after a session ends with books in storage, go and put them back
    "auto_start_delay_s": 10,       # ...once nobody has engaged for this long
    "sim_drive_s": 6,               # how long a simulated drive to a shelf (or home) takes
    "sim_unload_s": 3,              # how long a simulated unload at a shelf takes
    "announce_s": 3,                # how long the "excuse me" signal lasts
    "home": {"x": 0.0, "y": 0.0, "yaw": 0.0},   # the map pose of the robot's home spot
}

STORAGE_DEFAULTS = {
    "slots": None,                  # None: the storage ring's own size (actuation/storage_ring.py DEFAULT_SLOTS)
    "path": "data/state/storage.json",   # what is stored survives a restart; gitignored
    "sim_drop_s": 4,                # the simulated BookDropped after a slot is lit; 0 = wait for the operator's `dropped`
}

TIMEOUTS_DEFAULTS = {               # how long each waiting state waits before the robot says goodbye (seconds)
    "enabled": True,                # False: a waiting state waits for the visitor for ever (a person leaving still ends the visit)
    "offer_s": 30, "await_book_s": 30, "scan_s": 20, "store_s": 30, "answer_s": 30, "anything_else_s": 20,
    "farewell_s": 3,                # how long the goodbye lasts before the session is over
}

ENGAGEMENT_DEFAULTS = {
    "require_close": False,         # False: any face engages (greets) after dwell_s. True: it must also be min_face_px tall (not in use yet)
    "min_face_px": 60,              # a face this tall (pixels) or taller counts as close enough, about 1.5 m on a 640x480 webcam
    "dwell_s": 0.5,                 # ...and must stay that way this long to count as engaging
    "leave_grace_s": 60,            # the camera losing a visitor who spoke, typed or tapped within this long is not them leaving
    "left_after_s": 12.0,           # no face for this long: the person has left (long: looking down at a book loses the face)
}

SCREEN_DEFAULTS = {
    "content_ttl_s": {"quiet": 30, "common": 15},   # a content view goes back to the face this long after the last
                                                    # interaction (quiet zones read, common areas listen)
    "summary_chars": {"quiet": 180, "common": 110},  # how much of a summary a book card shows
    "greeting_hold_s": 2.5,         # the hello stays on the full face at least this long (spoken or not) before the options appear
}

INTENT_DEFAULTS = {                 # the intent router (core/intents.py) and what the robot does when it does not understand
    "word_similarity": 0.8,         # how alike two words must be to count as the same (typos, speech errors)
    "short_word_similarity": 0.85,  # ...stricter for words of up to four letters (one or two letters must be exact)
    "min_confidence": 0.7,          # below this a match is not acted on: the LLM is asked instead
    "ambiguity_margin": 0.15,       # two places closer than this: ask which one
    "repair_after": 2,              # this many utterances in a row not understood in a state: show the choices as buttons
    "thinking_ack_s": 1.5,          # common zone only: a short spoken "Let me check." if no sentence has arrived after this
    "expected_word_similarity": 0.65,    # right after a question with few answers (guide offer, "anything else?", "did you mean"):
    "expected_phrase_similarity": 0.68,  # looser matching of a short reply against only those answers (core/expectations.py)
    "expected_max_words": 4,             # ...for replies of up to this many words (fillers like "er" and "please" not counted)
}

SCENARIO_DEFAULTS = {               # the scenario test harness (robot/scenarios): it only observes, it does not change behaviour
    "typo_probability": 0.05,       # chance that a `say` step gets realistic typos
    "speech_probability": 0.3,      # chance that a `say` step gets speech-recognition style changes (config/speech_errors.yaml)
    "think_s": 0.8,                 # robot-seconds the pretend visitor waits after the robot has finished, before the next step
    "run_timeout_s": 240,           # real seconds before one run is stopped and marked as stuck
    "latency": {"first_feedback_ms": 500,        # a screen update or the thinking indication
                "first_sentence_ms": 1000,       # the first content sentence of a deterministic / catalogue answer
                "first_sentence_llm_ms": 6000},  # ...of an answer that waited for the LLM
    "robot_overrides": {            # applied to the robot's settings for the runs (config/robot.yaml has them off for manual tests)
        "returning": {"auto_start": True}, "timeouts": {"enabled": True},
        "screen": {"greeting_hold_s": 0}},
}

DISPLAY_DEFAULTS = {
    "host": "127.0.0.1",            # where the robot reaches the display server
    "port": 8765,
    "bind_host": "127.0.0.1",       # what the display server listens on; 0.0.0.0 lets a laptop open /operator
    "camera_fps": 15,               # pictures per second sent to the operator page while it is open (one a second when nobody watches)
    "camera_width": 480,            # ...shrunk to this many pixels wide
    "poll_s": 0.3,                  # how often the robot asks the display server for button taps and operator input
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
        merged["similarity"] = {**SIMILARITY_DEFAULTS, **((self.robot.get("books") or {}).get("similarity") or {})}
        return merged

    def section(self, name: str, defaults: dict) -> dict:
        """A section of robot.yaml with defaults filled in (one level deep, so `returning.home` can be partial)."""
        merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in defaults.items()}
        for key, value in (self.robot.get(name) or {}).items():
            merged[key] = {**merged[key], **value} if isinstance(merged.get(key), dict) and isinstance(value, dict) else value
        return merged

    @property
    def returning(self) -> dict:
        return self.section("returning", RETURNING_DEFAULTS)

    @property
    def storage(self) -> dict:
        return self.section("storage", STORAGE_DEFAULTS)

    @property
    def timeouts(self) -> dict:
        return self.section("timeouts", TIMEOUTS_DEFAULTS)

    @property
    def engagement(self) -> dict:
        return self.section("engagement", ENGAGEMENT_DEFAULTS)

    @property
    def intents(self) -> dict:
        return self.section("intents", INTENT_DEFAULTS)

    @property
    def scenarios(self) -> dict:
        return self.section("scenarios", SCENARIO_DEFAULTS)

    @property
    def screen(self) -> dict:
        return self.section("screen", SCREEN_DEFAULTS)

    @property
    def display(self) -> dict:
        return self.section("display", DISPLAY_DEFAULTS)

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
