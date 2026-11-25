"""Held-out phrasings: `scenarios run --variants FILE` replaces what the pretend visitor says.

The scenarios' own paraphrases were written while the intent router was being tuned, so a batch with them measures how
well the router fits those sentences. Phrasings written by people who have not seen the router measure how well it
understands new ones. The file is YAML, scenario -> step -> phrasings (see scenarios/heldout_template.yaml):

    ask_about_book:
      1: ["so what's this one about", "give me the gist of it"]
      2: ["who's the writer"]

A step is the number of a `say` step: the first `say` in the scenario file is 1, the next 2, and so on, going through
the branches of a one_of in the order they are written. `{title}` and `{author}` work as in the scenarios. Only `text`
is replaced: the checks, the answerable flags and everything else of the scenario stay as they are.

A scenario with no phrasings at all is left out of the batch (so the template can be filled in a scenario at a time);
one with some but not all of its steps filled in is an error, because a batch that mixed tuned and held-out phrasings
could not be told apart from either.
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from pathlib import Path

import yaml

from robot.scenarios.spec import Scenario, ScenarioError

PLACEHOLDERS = {"title", "author"}


def say_steps(steps: list) -> list:
    """Every `say` step of a scenario, in the order they are written (branches of a one_of included): [(n, step dict)]."""
    found: list = []

    def walk(items):
        for step in items:
            if not isinstance(step, dict):
                continue
            ((kind, value),) = step.items()
            if kind == "say":
                found.append(step)
            elif kind == "one_of":
                for branch in value:
                    walk(branch["steps"])
            elif kind == "maybe":
                walk(value["steps"])
    walk(steps)
    return list(enumerate(found, 1))


def load_variants(path, scenarios: dict) -> dict:
    """{scenario name: {step number: [phrasings]}} from the file, checked against the scenarios. Raises ScenarioError
    naming every problem. Scenarios and steps left empty are not in the result."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"{path.name}: cannot read it ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: not valid YAML ({exc})") from exc
    data = data or {}
    if not isinstance(data, dict):
        raise ScenarioError(f"{path.name}: expected scenario names at the top level, each with step numbers under it")
    problems: list = []
    found: dict = {}
    for name, steps in data.items():
        if name not in scenarios:
            problems.append(f"{name!r} is not a scenario (scenarios: {', '.join(scenarios)})")
            continue
        if steps is None:
            continue
        if not isinstance(steps, dict):
            problems.append(f"{name}: expected step numbers with a list of phrasings each")
            continue
        count = len(say_steps(scenarios[name].steps))
        got: dict = {}
        for key, phrasings in steps.items():
            where = f"{name} step {key}"
            if not re.fullmatch(r"\d+", str(key)) or not 1 <= int(key) <= count:
                problems.append(f"{where}: the steps of {name} are 1 to {count}")
                continue
            if phrasings is None or phrasings == []:
                continue
            if isinstance(phrasings, str):
                phrasings = [phrasings]
            if not isinstance(phrasings, list) or not all(isinstance(p, str) and p.strip() for p in phrasings):
                problems.append(f"{where}: expected a list of phrasings (text)")
                continue
            bad = sorted({m for p in phrasings for m in re.findall(r"\{(.*?)\}", p) if m not in PLACEHOLDERS})
            if bad or any(p.count("{") != p.count("}") for p in phrasings):
                problems.append(f"{where}: only {{title}} and {{author}} may appear in braces")
                continue
            got[int(key)] = [p.strip() for p in phrasings]
        if got:
            missing = [n for n in range(1, count + 1) if n not in got]
            if missing and not any(p.startswith(f"{name} step") for p in problems):
                problems.append(f"{name}: step(s) {', '.join(map(str, missing))} have no phrasings (fill in all {count} or none)")
            found[name] = got
    if problems:
        raise ScenarioError(f"{path.name}: " + "; ".join(problems))
    if not found:
        raise ScenarioError(f"{path.name}: no phrasings in it yet (fill in scenarios/heldout_template.yaml)")
    return found


def apply_variants(scenario: Scenario, phrasings: dict, source: str) -> Scenario:
    """A copy of the scenario whose `say` steps use the given phrasings, marked as held-out (`heldout` = the file name)."""
    steps = copy.deepcopy(scenario.steps)
    for n, step in say_steps(steps):
        value = step["say"]
        if isinstance(value, dict):
            value["text"] = list(phrasings[n])
        else:
            step["say"] = list(phrasings[n])
    return replace(scenario, steps=steps, heldout=source)


def heldout_scenarios(scenarios: dict, variants: dict, source: str) -> dict:
    """The scenarios that have held-out phrasings, rewritten to use them (the others are left out)."""
    return {name: apply_variants(scenarios[name], phrasings, source) for name, phrasings in variants.items()}
