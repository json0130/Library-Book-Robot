"""docs/state_machine.md, generated from the transition table in core/dialogue.py.

    python -m robot states --mermaid     print the Mermaid diagram
    python -m robot states --markdown    print the whole document
    python -m robot states --write       write docs/state_machine.md

tests/test_state_machine.py fails if the file in docs/ is out of date, so the document cannot drift from the code.
"""

from __future__ import annotations

from pathlib import Path

from robot.core.dialogue import SPEC, TIMER_SETTINGS
from robot.core.fsm import History, MachineSpec, Previous

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "state_machine.md"

INTRO = """\
# State machine

The robot's behaviour is a hierarchical, table-driven state machine: `robot/core/fsm.py` is the generic machine
(nesting, history, guards, timers, entry and exit actions, `force()`), and `robot/core/dialogue.py` is the table on top
of it. **This file is generated from that table** (`python -m robot states --write`), and a test fails if it is out of
date, so what you read here is what the robot does.

How to read it:

- The active state is always a leaf; its path is written `ENGAGED.OFFER`.
- An event is tried in the active state first and then in each enclosing state; the first transition whose guard is
  true wins. A transition with no target is *internal*: its action runs and the state does not change.
- `Choice` is a screen button (or the visitor saying a choice in words), `Utterance` is anything else they say.
  Every event carries a source: `sensor`, `operator` (terminal or `/operator` page), `screen` (a touch button) or
  `timeout`; each transition is logged with it, the zone and the time.
- Timers are config values (`timeouts`, `returning`, `storage` in `config/robot.yaml`). Every waiting state has a
  timeout and a `PersonLeft` way out, and both end in `FAREWELL` (silent in a quiet zone: eyes and light only).
- The zone (quiet or common) and the fixed baseline mode are style settings, not states: the same state behaves
  differently through `config/styles.yaml`.
"""


def _target(t) -> str:
    if t.target is None:
        return "(stays)"
    if isinstance(t.target, History):
        return f"{t.target.state} (history)"
    if isinstance(t.target, Previous):
        return f"{t.target.state} (previous)"
    return t.target


def render_markdown(spec: MachineSpec = SPEC) -> str:
    out = [INTRO, "## Diagram\n", "```mermaid", spec.mermaid(grouped=True).rstrip(), "```\n"]
    out += ["## States\n", "| state | inside | waits | timers (config key) | what it does |", "|---|---|---|---|---|"]
    for s in spec.states.values():
        if s.name == "FAREWELL":
            timers = "`timeouts.farewell_s`, plus `returning.auto_start_delay_s` when books are waiting"
        else:
            timers = ", ".join(
                f"`{TIMER_SETTINGS[t.seconds][0]}.{TIMER_SETTINGS[t.seconds][1]}`" + ("" if t.event == "timeout" else f" → {t.event}")
                for t in s.timers if t.seconds in TIMER_SETTINGS)
        out.append(f"| {s.name} | {s.parent or ''} | {'yes' if s.waiting else ''} | {timers} | {s.doc}"
                   + (" (composite, starts in " + s.initial + ")" if s.initial else "") + " |")
    out += ["", "## Transitions\n", "| in | event | guard | goes to | action | note |", "|---|---|---|---|---|---|"]
    for t in spec.transitions:
        out.append(f"| {t.source} | {t.event} | {t.guard or ''} | {_target(t)} | {t.action or ''} | {t.note} |")
    out.append("")
    return "\n".join(out)


def write_doc(path: Path = DOC_PATH) -> Path:
    path.write_text(render_markdown(), encoding="utf-8")
    return path
