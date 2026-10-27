"""A small hierarchical state machine, driven by a table.

    spec = MachineSpec(states=[StateDef("IDLE"), StateDef("ENGAGED", initial="GREET"), StateDef("GREET", parent="ENGAGED"), ...],
                       transitions=[Transition("IDLE", "PersonEngaged", "GREET", action="greet"), ...], initial="IDLE")
    machine = Machine(spec, handler)           # handler: any object with the guard and action methods the table names
    await machine.start()
    await machine.dispatch("PersonEngaged", payload, source="sensor")
    await machine.tick()                       # fires timers that are due; call it a few times a second

The table (MachineSpec) is plain data, so it can be checked, drawn and tested without a robot.

States may be nested: a composite state has an `initial` child, and entering it enters that child. The active
state is always a leaf; its path is "ENGAGED.OFFER". An event is tried on the active leaf first, then on each
ancestor, and the first matching transition (event name equal, guard true) wins, so a composite can handle an
event for all its children and a child can override it. A transition with no target is internal: its action
runs and nothing is exited or entered. A transition to a state inside its own source is local (the source
stays active); any other transition leaves the source and enters the target.

Entering runs the parents' entry actions then the child's; leaving runs the child's exit actions first. A
transition's own action runs after the exits and before the entries. Actions and guards are methods of the
handler, named in the table; they receive the Event and may be `async`. Events an action posts with
`machine.post(...)` are queued and handled after the current transition has finished (run to completion), so
no action ever runs inside another transition.

Timers: a state may have timers; each is `Timer(seconds, event)` where `seconds` is a number, or the name of a
handler method that returns the number of seconds (or None for no timer). A due timer fires its event
(default "timeout", source "timeout") starting from the state that owns it.

History: leaving a composite remembers its active leaf and the time left on its timers. A transition whose
target is History("RETURNING") goes back to that leaf with those timers, and Previous("RETURNING") goes back to
the child that was active just before the current one, again with its timers (that is how an announcement
returns to the drive it interrupted with the drive's remaining time). A `transient` state is skipped when
remembering: history resumes the state before it.

force(state) puts the machine in any state at once (the operator's goto): the exit actions of what it leaves and
the entry actions of what it enters run, and the move is reported like any other transition.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("robot.fsm")

MAX_CHAIN = 500                         # events one dispatch may set off before the machine decides it is stuck in a loop


@dataclass(frozen=True)
class Timer:
    seconds: Any                    # a number, or the name of a handler method returning a number / None
    event: str = "timeout"


@dataclass(frozen=True)
class StateDef:
    name: str
    parent: Optional[str] = None
    initial: Optional[str] = None       # the child a composite state starts in
    enter: tuple = ()                   # names of handler methods run on entry
    exit: tuple = ()                    # ...and on exit
    timers: tuple = ()                  # Timer(...)
    waiting: bool = False               # waits for the visitor or a sensor: must have a timeout and a PersonLeft way out
    transient: bool = False             # passes straight through: history skips it
    doc: str = ""


@dataclass(frozen=True)
class History:
    """A transition target: the leaf the composite state was in when it was last left, with its timers."""
    state: str


@dataclass(frozen=True)
class Previous:
    """A transition target: the child of the composite that was active just before the current one."""
    state: str


@dataclass(frozen=True)
class Transition:
    source: str                         # the state it is defined on (a composite applies to all its children)
    event: str
    target: Any = None                  # a state name, History(...), Previous(...), or None for an internal transition
    guard: Optional[str] = None         # handler method returning True when the transition may fire ("not x" inverts)
    action: Optional[str] = None        # handler method run between the exits and the entries
    note: str = ""                      # a few words for the diagram


@dataclass
class Event:
    name: str
    payload: Any = None
    source: str = "sensor"              # sensor | operator | timeout | screen | internal
    state: Optional[str] = None         # for a timer event: the state that owns the timer


class SpecError(ValueError):
    """The transition table is inconsistent (an unknown state, a composite without an initial child...)."""


def _anchor(target) -> Optional[str]:
    return target.state if isinstance(target, (History, Previous)) else target


class MachineSpec:
    def __init__(self, states: list, transitions: list, initial: str):
        self.states: dict[str, StateDef] = {}
        for s in states:
            if s.name in self.states:
                raise SpecError(f"state {s.name!r} is defined twice")
            self.states[s.name] = s
        self.transitions = list(transitions)
        self.initial = initial
        self._check()

    # ---- structure ----
    def parent(self, name: str) -> Optional[str]:
        return self.states[name].parent

    def children(self, name: str) -> list:
        return [s.name for s in self.states.values() if s.parent == name]

    def is_composite(self, name: str) -> bool:
        return bool(self.children(name))

    def ancestors(self, name: str) -> list:
        """The enclosing states, nearest first."""
        found, parent = [], self.parent(name)
        while parent:
            found.append(parent)
            parent = self.parent(parent)
        return found

    def chain(self, name: str) -> list:
        """name and its ancestors, outermost first."""
        return list(reversed([name, *self.ancestors(name)]))

    def path(self, name: str) -> str:
        return ".".join(self.chain(name))

    def leaf_of(self, name: str) -> str:
        """The leaf entering `name` ends up in: follow `initial` down."""
        while self.is_composite(name):
            name = self.states[name].initial
        return name

    def leaves(self) -> list:
        return [n for n in self.states if not self.is_composite(n)]

    def resolve(self, text: str) -> str:
        """A state name from "OFFER", "ENGAGED.OFFER", "offer" or "await book": raises SpecError if there is none."""
        wanted = text.strip().split(".")[-1].upper().replace(" ", "_").replace("-", "_")
        if wanted not in self.states:
            raise SpecError(f"no state called {text!r}; states: {', '.join(self.states)}")
        return wanted

    def effective_transitions(self, name: str) -> list:
        """What can happen in `name`, in the order they are tried: its own transitions, then its ancestors'. An
        ancestor's transition is left out when an inner state has one for the same event and guard, or an
        unguarded one for the event (which always matches first and so shadows everything outside it)."""
        found, taken, shadowed = [], set(), set()
        for state in (name, *self.ancestors(name)):
            own = [t for t in self.transitions if t.source == state]
            found += [t for t in own if t.event not in shadowed and (t.event, t.guard) not in taken]
            taken |= {(t.event, t.guard) for t in own}
            shadowed |= {t.event for t in own if t.guard is None}
        return found

    # ---- analysis ----
    def targets_of(self, t: Transition) -> list:
        """The leaves a transition can arrive in (a History or Previous target can be any leaf of the composite)."""
        if t.target is None:
            return []
        if isinstance(t.target, (History, Previous)):
            return [n for n in self.leaves() if t.target.state in self.ancestors(n)]
        return [self.leaf_of(t.target)]

    def reachable(self) -> set:
        """Every leaf the machine can get to from its initial state by following the table."""
        start = self.leaf_of(self.initial)
        seen, todo = {start}, [start]
        while todo:
            for t in self.effective_transitions(todo.pop()):
                for target in self.targets_of(t):
                    if target not in seen:
                        seen.add(target)
                        todo.append(target)
        return seen

    def _check(self) -> None:
        for s in self.states.values():
            if s.parent is not None and s.parent not in self.states:
                raise SpecError(f"{s.name}: unknown parent {s.parent!r}")
        for name in self.states:
            if self.is_composite(name):
                initial = self.states[name].initial
                if initial is None or initial not in self.children(name):
                    raise SpecError(f"{name} is composite and needs an `initial` that is one of its children")
            elif self.states[name].initial:
                raise SpecError(f"{name} has an initial child but no children")
        if self.initial not in self.states:
            raise SpecError(f"unknown initial state {self.initial!r}")
        for t in self.transitions:
            if t.source not in self.states:
                raise SpecError(f"transition from unknown state {t.source!r} on {t.event}")
            anchor = _anchor(t.target)
            if anchor is not None and anchor not in self.states:
                raise SpecError(f"transition {t.source} --{t.event}--> unknown state {anchor!r}")
            if isinstance(t.target, (History, Previous)) and not self.is_composite(anchor):
                raise SpecError(f"{t.target!r}: only a composite state has history")
            if isinstance(t.target, str) and t.target in self.ancestors(t.source):
                raise SpecError(f"transition {t.source} --{t.event}--> {t.target}: a state cannot go to its own parent")

    def method_names(self) -> set:
        """Every handler method name the table uses."""
        names = {n for s in self.states.values() for n in (*s.enter, *s.exit)}
        names |= {t.guard.removeprefix("not ") for t in self.transitions if t.guard}
        names |= {t.action for t in self.transitions if t.action}
        names |= {timer.seconds for s in self.states.values() for timer in s.timers if isinstance(timer.seconds, str)}
        return names

    # ---- diagram ----
    def mermaid(self, grouped: bool = False) -> str:
        """The machine as a Mermaid stateDiagram-v2, generated from the table.

        A transition between two children of one composite state is drawn inside it. One a composite defines for
        all its children (PersonLeft on ENGAGED) is drawn from each waiting child that does not override it, or,
        with `grouped`, left out of the arrows and listed once in a note on the composite (a diagram with the
        arrow from every child is unreadable when a composite routes a dozen events). All the others are drawn
        from where they are defined to their target."""
        lines = ["stateDiagram-v2", f"    [*] --> {self.initial}"]

        def arrow(source: str, t: Transition) -> str:
            text = t.event + (f" [{t.guard}]" if t.guard else "") + (f" / {t.action}" if t.action else "")
            suffix = " (history)" if isinstance(t.target, History) else " (previous)" if isinstance(t.target, Previous) else ""
            return f"{source} --> {_anchor(t.target)}: {text.replace(':', ' ')}{suffix}"

        def drawn(t: Transition) -> list:
            """The (source state) arrows to draw for one transition."""
            anchor = _anchor(t.target)
            if self.is_composite(t.source) and (t.source in self.ancestors(anchor) or isinstance(t.target, (History, Previous))):
                return [leaf for leaf in self.leaves() if t.source in self.ancestors(leaf)
                        and self.states[leaf].waiting and not self._overridden(leaf, t)]
            return [t.source]

        def sibling_block(parent: Optional[str], depth: int) -> None:
            pad = "    " * depth
            for name in [s for s in self.states if self.parent(s) == parent and self.is_composite(s)]:
                lines.append(f"{pad}state {name} {{")
                lines.append(f"{pad}    [*] --> {self.states[name].initial}")
                sibling_block(name, depth + 1)
                lines.append(f"{pad}}}")
            for t in self.transitions:
                anchor = _anchor(t.target)
                if t.target is not None and self.parent(t.source) == parent and self.parent(anchor) == parent \
                        and not self.is_composite(t.source) and not isinstance(t.target, (History, Previous)):
                    lines.append(pad + arrow(t.source, t))

        sibling_block(None, 0)
        wide: dict[str, list] = {}
        for t in self.transitions:
            anchor = _anchor(t.target)
            if t.target is None or (self.parent(t.source) == self.parent(anchor) and not self.is_composite(t.source)
                                    and not isinstance(t.target, (History, Previous))):
                continue
            if grouped and self.is_composite(t.source) and drawn(t) != [t.source]:
                wide.setdefault(t.source, []).append(t)
                continue
            for source in drawn(t):
                lines.append("    " + arrow(source, t))
        for composite, ts in wide.items():
            lines.append(f"    note right of {composite}")
            lines.append(f"        in any {composite} state:")
            for t in ts:
                guard = f" if {t.guard}" if t.guard else ""
                lines.append(f"        {t.event}{guard} -> {_anchor(t.target)}".replace("[", "(").replace("]", ")"))
            lines.append("    end note")
        return "\n".join(lines) + "\n"

    def _overridden(self, leaf: str, t: Transition) -> bool:
        """True if a state between `leaf` and t.source has its own transition for the same event and guard, or an
        unguarded one for the event."""
        for state in (leaf, *self.ancestors(leaf)):
            if state == t.source:
                return False
            if any(o.source == state and o.event == t.event and (o.guard == t.guard or o.guard is None)
                   for o in self.transitions):
                return True
        return False


@dataclass
class _Snapshot:
    leaf: str
    remaining: dict = field(default_factory=dict)       # state name -> [(seconds left, event), ...]


def _common_prefix(a: list, b: list) -> list:
    out = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return out


class Machine:
    def __init__(self, spec: MachineSpec, handler: object, clock: Callable[[], float] = time.monotonic,
                 on_transition: Optional[Callable[[dict], Any]] = None,
                 on_entered: Optional[Callable[[str], Any]] = None):
        """on_transition(record) is called for every transition after the exits and before the entries;
        on_entered(leaf) after the entry actions of the state the machine has arrived in."""
        self.spec, self.handler, self.clock, self.on_transition = spec, handler, clock, on_transition
        self.on_entered = on_entered
        missing = sorted(n for n in spec.method_names() if not callable(getattr(handler, n, None)))
        if missing:
            raise SpecError(f"the handler has no method(s): {', '.join(missing)}")
        self.leaf: Optional[str] = None
        self._deadlines: dict[str, list] = {}               # state -> [(due, event)] for the states now active
        self._history: dict[str, _Snapshot] = {}            # composite -> where it was left
        self._previous: dict[str, _Snapshot] = {}           # composite -> the child active before the current one
        self._queue: deque = deque()
        self._running = False
        self._suspended = 0                                 # >0: the "timeout" timers wait (the robot is speaking or thinking)
        self._armed: dict[str, list] = {}                   # state -> [(seconds, event)] as armed, to start them again
        self._current: Optional[Event] = None               # the event being handled

    # ---- where we are ----
    @property
    def state(self) -> Optional[str]:
        return self.leaf

    @property
    def path(self) -> str:
        return self.spec.path(self.leaf) if self.leaf else ""

    def is_in(self, name: str) -> bool:
        """True if `name` is the active leaf or one of its ancestors."""
        return self.leaf is not None and (name == self.leaf or name in self.spec.ancestors(self.leaf))

    def time_left(self, state: Optional[str] = None, event: str = "timeout") -> Optional[float]:
        """Seconds until the state's timer for `event` fires, or None if it has none."""
        for due, ev in self._deadlines.get(state or self.leaf, []):
            if ev == event:
                return max(0.0, due - self.clock())
        return None

    def remembered(self, composite: str) -> Optional[str]:
        """The leaf a composite state was left in (its history), if any."""
        snap = self._history.get(composite)
        return snap.leaf if snap else None

    async def rearm(self, state: str, event_name: str = "timeout") -> None:
        """Start `state`'s timer for `event_name` again (a timer that fired and was not wanted: keep waiting)."""
        for timer in self.spec.states[state].timers:
            if timer.event != event_name:
                continue
            seconds = await self._call(timer.seconds, Event(event_name)) if isinstance(timer.seconds, str) else timer.seconds
            if seconds is not None and float(seconds) > 0:
                self._deadlines.setdefault(state, []).append((self.clock() + float(seconds), event_name))

    def suspend_timeouts(self) -> None:
        """Hold the "timeout" timers (not the drive, unload or announce ones) while the robot speaks or waits for the LLM."""
        self._suspended += 1

    def resume_timeouts(self) -> None:
        """When the last hold is released the "timeout" timers of the active states start again from now."""
        self._suspended = max(0, self._suspended - 1)
        if self._suspended or self.leaf is None:
            return
        now = self.clock()
        for name in self.spec.chain(self.leaf):
            fresh = [(now + seconds, ev) for seconds, ev in self._armed.get(name, []) if ev == "timeout"]
            if fresh:
                self._deadlines[name] = [d for d in self._deadlines.get(name, []) if d[1] != "timeout"] + fresh

    def forget(self, composite: str) -> None:
        self._history.pop(composite, None)
        self._previous.pop(composite, None)

    # ---- running ----
    async def start(self, source: str = "internal") -> None:
        """Enter the initial state."""
        self._queue.append(Event("__start__", source=source))
        await self._drain()

    def post(self, name: str, payload: Any = None, source: Optional[str] = None) -> None:
        """Queue an event; it is handled once the current transition is finished. Unless told otherwise it has the
        source of the event being handled (a follow-up of an operator's event is still the operator's), else "internal"."""
        self._queue.append(Event(name, payload, source or (self._current.source if self._current else "internal")))

    async def dispatch(self, name: str, payload: Any = None, source: str = "sensor") -> None:
        """Handle an event, and everything it sets off, to completion."""
        self._queue.append(Event(name, payload, source))
        await self._drain()

    async def force(self, state: str, source: str = "operator", payload: Any = None) -> None:
        """Go to `state` (a name or path) whatever the table says."""
        self._queue.append(Event("__force__", (self.spec.resolve(state), payload), source))
        await self._drain()

    async def tick(self, now: Optional[float] = None) -> int:
        """Fire the timers that are due; returns how many fired."""
        now = self.clock() if now is None else now
        fired = 0
        for state, timers in list(self._deadlines.items()):
            for due, ev in sorted(timers):
                if ev == "timeout" and self._suspended:
                    continue
                if due <= now:
                    self._deadlines[state].remove((due, ev))
                    self._queue.append(Event(ev, source="timeout", state=state))
                    fired += 1
        if fired:
            await self._drain()
        return fired

    async def _drain(self) -> None:
        if self._running:                                   # called from inside an action: the loop below will get to it
            return
        self._running = True
        try:
            handled = 0
            while self._queue:
                handled += 1
                if handled > MAX_CHAIN:
                    self._queue.clear()
                    raise RuntimeError("the state machine kept posting events to itself; giving up")
                event = self._queue.popleft()
                self._current = event
                if event.name == "__start__":
                    await self._go(self.spec.leaf_of(self.spec.initial), [], Event("start", source=event.source), False)
                elif event.name == "__force__":
                    await self._force(event)
                else:
                    await self._handle(event)
        finally:
            self._running = False
            self._current = None

    # ---- one event ----
    async def _call(self, name: Optional[str], event: Event) -> Any:
        if not name:
            return None
        invert = name.startswith("not ")
        result = getattr(self.handler, name.removeprefix("not "))(event)
        if inspect.isawaitable(result):
            result = await result
        return (not result) if invert else result

    async def _handle(self, event: Event) -> None:
        if self.leaf is None:
            return
        if event.source == "timeout" and event.state is not None and not self.is_in(event.state):
            return                                          # the state that owned this timer has been left already
        start = event.state if (event.source == "timeout" and event.state) else self.leaf
        for state in (start, *self.spec.ancestors(start)):
            for t in self.spec.transitions:
                if t.source != state or t.event != event.name:
                    continue
                if t.guard and not await self._call(t.guard, event):
                    continue
                await self._fire(t, event)
                return
        log.debug("[fsm] %s ignored in %s", event.name, self.path)

    async def _fire(self, t: Transition, event: Event) -> None:
        if t.target is None:                                 # internal: just the action
            await self._call(t.action, event)
            await self._announce(self.leaf, self.leaf, event, True, t)
            return
        spec, source = self.spec, t.source
        restore: dict = {}
        if isinstance(t.target, (History, Previous)):
            snaps = self._history if isinstance(t.target, History) else self._previous
            snap = snaps.get(t.target.state)
            leaf = snap.leaf if snap else spec.leaf_of(t.target.state)
            restore = dict(snap.remaining) if snap else {}
            inside = t.target.state in (source, *spec.ancestors(source))     # coming back within the composite
            keep = spec.chain(t.target.state) if inside else \
                _common_prefix(spec.chain(source)[:-1], spec.chain(t.target.state)[:-1])
        else:
            leaf = spec.leaf_of(t.target)
            if source == t.target:
                keep = spec.chain(source)[:-1]
            elif source in spec.ancestors(t.target):         # local: the source stays active
                keep = spec.chain(source)
            else:
                keep = _common_prefix(spec.chain(source)[:-1], spec.chain(t.target)[:-1])
        await self._go(leaf, keep, event, False, t, restore)

    async def _force(self, event: Event) -> None:
        target, payload = event.payload
        event = Event("force", payload, event.source)
        leaf = self.spec.leaf_of(target)
        old = self.spec.chain(self.leaf) if self.leaf else []
        keep = _common_prefix(old[:-1], self.spec.chain(leaf)[:-1])
        await self._go(leaf, keep, event, False)

    async def _go(self, leaf: str, keep: list, event: Event, internal: bool, t: Optional[Transition] = None,
                  restore: Optional[dict] = None) -> None:
        """Leave the active branch below `keep`, run the transition's action, report it, enter `leaf`."""
        old_leaf = self.leaf
        old_chain = self.spec.chain(old_leaf) if old_leaf else []
        exiting = old_chain[len(keep):]
        await self._exit(exiting, old_leaf, event)
        await self._call(t.action if t else None, event)
        await self._announce(old_leaf, leaf, event, internal, t)
        await self._enter(self.spec.chain(leaf)[len(keep):], event, restore or {})

    # ---- leaving and entering ----
    async def _exit(self, exiting: list, old_leaf: Optional[str], event: Event) -> None:
        """Leave `exiting` (outermost first), innermost exit actions first, remembering history and timers."""
        if not exiting:
            return
        now = self.clock()
        timers = {n: [(max(0.0, due - now), ev) for due, ev in self._deadlines[n]]
                  for n in self.spec.chain(old_leaf) if n in self._deadlines}
        for name in reversed(exiting):
            for action in self.spec.states[name].exit:
                await self._call(action, event)
        for name in exiting:                                 # remember where each composite we leave was left
            if self.spec.is_composite(name):
                self._history[name] = self._snapshot(name, old_leaf, timers)
        parent = self.spec.parent(exiting[0])
        if parent is not None:                               # and which child of the parent we are leaving
            self._previous[parent] = _Snapshot(old_leaf, {n: list(v) for n, v in timers.items()})
        for name in exiting:
            self._deadlines.pop(name, None)
        self.leaf = self.spec.parent(exiting[0])

    def _snapshot(self, composite: str, leaf: str, timers: dict) -> _Snapshot:
        """History for `composite` left in `leaf`: a transient leaf is replaced by the state that came before it."""
        if self.spec.states[leaf].transient:
            earlier = self._previous.get(composite)
            if earlier is not None and earlier.leaf != leaf:
                return _Snapshot(earlier.leaf, {n: list(v) for n, v in earlier.remaining.items()})
        return _Snapshot(leaf, {n: list(v) for n, v in timers.items()})

    async def _enter(self, entering: list, event: Event, restore: dict) -> None:
        """Enter `entering` (outermost first): start each state's timers, then run its entry actions."""
        for name in entering:
            self.leaf = name                                  # so actions see where we are
            state = self.spec.states[name]
            if name in restore:
                self._deadlines[name] = [(self.clock() + sec, ev) for sec, ev in restore[name]]
                self._armed[name] = [(sec, ev) for sec, ev in restore[name]]
            else:
                due, armed = [], []
                for timer in state.timers:
                    seconds = await self._call(timer.seconds, event) if isinstance(timer.seconds, str) else timer.seconds
                    if seconds is not None and float(seconds) > 0:
                        due.append((self.clock() + float(seconds), timer.event))
                        armed.append((float(seconds), timer.event))
                if due:
                    self._deadlines[name] = due
                    self._armed[name] = armed
            for action in state.enter:
                await self._call(action, event)
        if self.on_entered is not None and entering:
            result = self.on_entered(entering[-1])
            if inspect.isawaitable(result):
                await result

    async def _announce(self, old_leaf: Optional[str], new_leaf: str, event: Event, internal: bool,
                        t: Optional[Transition] = None) -> None:
        if self.on_transition is None:
            return
        record = {"from": self.spec.path(old_leaf) if old_leaf else "", "to": self.spec.path(new_leaf),
                  "event": event.name, "source": event.source, "internal": internal,
                  "guard": t.guard if t else None, "action": t.action if t else None, "t": self.clock()}
        result = self.on_transition(record)
        if inspect.isawaitable(result):
            await result
