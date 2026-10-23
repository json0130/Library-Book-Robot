"""Scenario files (scenarios/*.yaml): loading and validation.

    name: return_book
    description: ...
    zones: [quiet, common]            # default both
    setup: {stored: 2}                # optional: books already in the storage ring
    steps:
      - engage
      - one_of:                       # one branch per run, chosen by the seed (recorded as a tag)
          - {name: say_first, steps: [{say: ["I'd like to return a book"]}, {place_book: random}]}
          - {name: place_first, steps: [{place_book: random}, {say: ["return it"]}]}
      - maybe: {name: unknown_isbn, p: 0.2, steps: [...]}
      - {say: {text: ["who wrote it?"], answerable: true}}
      - {tap: "Return a book"}        # a button label on the screen; {tap: {item: random}} taps a book in a list
      - {wait: 2}                     # seconds of robot time; {wait: {until_state: IDLE, timeout: 60}}
      - dropped / busy / leave / start_returning
    expect:                           # a list; each item has one kind plus optional when / unless (tags) and zones
      - {state_path: [ENGAGED.SCAN, ENGAGED.STORE, ENGAGED.FAREWELL]}
      - {views: [book_card]}

`say` steps take a list of paraphrases; `{title}` and `{author}` are filled in from the last book placed. A `say` with
`answerable: true` is a question the catalogue can answer; `unanswerable: true` one it cannot (the robot must not make
something up).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from robot.config import ROOT

SCENARIO_DIR = ROOT / "scenarios"
ZONES = ("quiet", "common")
SIMPLE_STEPS = ("engage", "busy", "leave", "dropped", "start_returning")
STEP_KINDS = SIMPLE_STEPS + ("place_book", "say", "tap", "wait", "one_of", "maybe")
EXPECT_KINDS = ("state_path", "views", "forbid_phrases", "require_phrases_any", "events", "no_events", "view_field",
                "chime", "resume_same_shelf", "silent_farewell", "book_list_valid")


class ScenarioError(ValueError):
    """A scenario file is wrong; the message names the file and the step."""


@dataclass
class Scenario:
    name: str
    description: str
    zones: tuple
    steps: list
    expect: list = field(default_factory=list)
    setup: dict = field(default_factory=dict)
    path: str = ""


def load_scenario(path) -> Scenario:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"{path.name}: cannot read it ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: not valid YAML ({exc})") from exc
    return parse_scenario(data, path.name, str(path))


def parse_scenario(data: Any, source: str = "scenario", path: str = "") -> Scenario:
    """Validate and build a Scenario; raises ScenarioError listing every problem found."""
    problems: list = []
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: a scenario is a mapping with name, steps and expect")
    unknown = sorted(set(data) - {"name", "description", "zones", "setup", "steps", "expect"})
    if unknown:
        problems.append(f"unknown key(s): {', '.join(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name or ""):
        problems.append("`name` must be lower-case letters, digits and underscores")
    zones = data.get("zones", list(ZONES))
    if not isinstance(zones, list) or not zones or any(z not in ZONES for z in zones):
        problems.append(f"`zones` must be a list of {', '.join(ZONES)}")
        zones = list(ZONES)
    setup = data.get("setup") or {}
    if not isinstance(setup, dict) or set(setup) - {"stored"} or not isinstance(setup.get("stored", 0), int):
        problems.append("`setup` may only hold `stored: <number of books>`")
        setup = {}
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("`steps` must be a non-empty list")
        steps = []
    for i, step in enumerate(steps, 1):
        _check_step(step, f"step {i}", problems)
    expect = data.get("expect") or []
    if not isinstance(expect, list):
        problems.append("`expect` must be a list")
        expect = []
    for i, item in enumerate(expect, 1):
        _check_expect(item, f"expect {i}", problems)
    if problems:
        raise ScenarioError(f"{source}: " + "; ".join(problems))
    return Scenario(name, str(data.get("description") or ""), tuple(zones), steps, expect, setup, path)


def _texts(value) -> bool:
    return (isinstance(value, str) and value.strip() != "") or (
        isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value))


def _check_step(step: Any, where: str, problems: list) -> None:
    if isinstance(step, str):
        if step not in SIMPLE_STEPS:
            problems.append(f"{where}: {step!r} is not a step (simple steps: {', '.join(SIMPLE_STEPS)}; others take a value)")
        return
    if not isinstance(step, dict) or len(step) != 1:
        problems.append(f"{where}: a step is a name or a one-key mapping, got {step!r}")
        return
    ((kind, value),) = step.items()
    if kind not in STEP_KINDS:
        problems.append(f"{where}: unknown step {kind!r}; use one of {', '.join(STEP_KINDS)}")
    elif kind in SIMPLE_STEPS:
        if value not in (None, True):
            problems.append(f"{where}: {kind} takes no value")
    elif kind == "place_book":
        isbn = value.get("isbn") if isinstance(value, dict) else value
        if not (isbn in ("random", "unknown") or re.fullmatch(r"\d{10}|\d{13}", str(isbn))):
            problems.append(f"{where}: place_book needs random, unknown or an ISBN, got {isbn!r}")
    elif kind == "say":
        text = value.get("text") if isinstance(value, dict) else value
        if not _texts(text):
            problems.append(f"{where}: say needs a text or a list of paraphrases")
        if isinstance(value, dict):
            if set(value) - {"text", "answerable", "unanswerable"}:
                problems.append(f"{where}: say takes text, answerable, unanswerable")
            if value.get("answerable") and value.get("unanswerable"):
                problems.append(f"{where}: a question is answerable or unanswerable, not both")
    elif kind == "tap":
        if not (isinstance(value, str) and value.strip()) and not (
                isinstance(value, dict) and set(value) == {"item"} and (value["item"] == "random" or isinstance(value["item"], int))):
            problems.append(f"{where}: tap needs a button label or {{item: <number>|random}}")
    elif kind == "wait":
        if isinstance(value, dict):
            if set(value) != {"until_state", "timeout"} or not isinstance(value["until_state"], str) \
                    or not isinstance(value["timeout"], (int, float)) or value["timeout"] <= 0:
                problems.append(f"{where}: wait needs seconds, or {{until_state: STATE, timeout: seconds}}")
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            problems.append(f"{where}: wait needs a number of seconds")
    elif kind == "one_of":
        if not isinstance(value, list) or len(value) < 2:
            problems.append(f"{where}: one_of needs at least two branches")
        else:
            for j, branch in enumerate(value, 1):
                _check_branch(branch, f"{where} branch {j}", problems)
    elif kind == "maybe":
        if not isinstance(value, dict) or not isinstance(value.get("p"), (int, float)) or not 0 <= value["p"] <= 1:
            problems.append(f"{where}: maybe needs p between 0 and 1")
        else:
            _check_branch(value, f"{where} maybe", problems)


def _check_branch(branch: Any, where: str, problems: list) -> None:
    if not isinstance(branch, dict) or not isinstance(branch.get("name"), str) or not isinstance(branch.get("steps"), list):
        problems.append(f"{where}: needs a name and a list of steps")
        return
    for i, step in enumerate(branch["steps"], 1):
        _check_step(step, f"{where} step {i}", problems)


def _check_expect(item: Any, where: str, problems: list) -> None:
    if not isinstance(item, dict):
        problems.append(f"{where}: must be a mapping")
        return
    kinds = [k for k in item if k in EXPECT_KINDS]
    extras = set(item) - set(EXPECT_KINDS) - {"when", "unless", "zones", "note"}
    if len(kinds) != 1:
        problems.append(f"{where}: needs exactly one of {', '.join(EXPECT_KINDS)}")
        return
    if extras:
        problems.append(f"{where}: unknown key(s) {', '.join(sorted(extras))}")
    kind, value = kinds[0], item[kinds[0]]
    if kind in ("state_path", "views", "forbid_phrases", "require_phrases_any", "events", "no_events"):
        if not _texts(value) or isinstance(value, str):
            problems.append(f"{where}: {kind} needs a list of names")
    elif kind == "view_field":
        if not isinstance(value, dict) or set(value) != {"view", "field", "matches"}:
            problems.append(f"{where}: view_field needs view, field and matches")
        else:
            try:
                re.compile(str(value["matches"]))
            except re.error as exc:
                problems.append(f"{where}: bad regular expression ({exc})")
    elif kind in ("chime", "resume_same_shelf", "silent_farewell", "book_list_valid") and not isinstance(value, bool):
        problems.append(f"{where}: {kind} is true or false")
    for key in ("when", "unless"):
        if key in item and not isinstance(item[key], str):
            problems.append(f"{where}: {key} is a tag name")
    if "zones" in item and (not isinstance(item["zones"], list) or any(z not in ZONES for z in item["zones"])):
        problems.append(f"{where}: zones is a list of {', '.join(ZONES)}")


def load_all(directory=SCENARIO_DIR) -> dict:
    """{name: Scenario} for every scenarios/*.yaml. Raises ScenarioError for the first bad file, and for a name used twice."""
    found: dict = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        scenario = load_scenario(path)
        if scenario.name in found:
            raise ScenarioError(f"{path.name}: the name {scenario.name!r} is also used by {Path(found[scenario.name].path).name}")
        found[scenario.name] = scenario
    return found
