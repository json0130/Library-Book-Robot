import asyncio
import unittest

from robot.core.fsm import (Event, History, Machine, MachineSpec, Previous, SpecError, StateDef, Timer, Transition)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Handler:
    """Records every action and guard call in order, so tests can check the order things happen in."""

    def __init__(self):
        self.log = []
        self.flags = {"ok": True, "other": False}
        self.drive_s = 6.0
        self.machine = None

    def __getattr__(self, name):
        if name.startswith("enter_") or name.startswith("exit_") or name.startswith("do_"):
            def method(event, name=name):
                self.log.append(name)
            return method
        raise AttributeError(name)

    def ok(self, event):
        return self.flags["ok"]

    def other(self, event):
        return self.flags["other"]

    def long_drive(self, event):
        return self.drive_s

    def no_timer(self, event):
        return None

    def post_next(self, event):
        self.log.append("post_next")
        self.machine.post("next")

    def loop(self, event):
        self.machine.post("again")


# A -> B(B1, B2) -> C; D is a composite with a transient child
STATES = [
    StateDef("A", enter=("enter_A",), exit=("exit_A",)),
    StateDef("B", initial="B1", enter=("enter_B",), exit=("exit_B",)),
    StateDef("B1", parent="B", enter=("enter_B1",), exit=("exit_B1",)),
    StateDef("B2", parent="B", enter=("enter_B2",), exit=("exit_B2",), timers=(Timer(6.0),)),
    StateDef("C", enter=("enter_C",), exit=("exit_C",)),
    StateDef("D", initial="D1", enter=("enter_D",)),
    StateDef("D1", parent="D", timers=(Timer("long_drive", "arrived"),)),
    StateDef("D2", parent="D", transient=True),
    StateDef("D3", parent="D"),
]
TRANSITIONS = [
    Transition("A", "go", "B1", action="do_go"),
    Transition("B", "to_c", "C", action="do_to_c"),
    Transition("B1", "next", "B2"),
    Transition("B2", "timeout", "C"),
    Transition("B", "inside", "B2", action="do_inside"),                       # local: B stays
    Transition("B1", "self", "B1", action="do_self"),
    Transition("B1", "internal", None, action="do_internal"),
    Transition("B1", "pick", "A", guard="ok"),
    Transition("B1", "pick", "C", guard="not ok"),
    Transition("B", "fallback", "A", note="from the composite"),
    Transition("B1", "fallback", "C", guard="other"),                           # overrides only when the guard holds
    Transition("C", "back", History("B")),
    Transition("C", "to_d", "D"),
    Transition("C", "chain", "A", action="post_next"),
    Transition("A", "next", "C"),
    Transition("A", "spin", None, action="loop"),
    Transition("A", "again", None, action="loop"),
    Transition("D1", "arrived", "D2"),
    Transition("D2", "hop", "D3"),
    Transition("D3", "back", Previous("D")),
    Transition("D1", "leave", "C"),
    Transition("D3", "leave", "C"),
    Transition("D2", "leave", "C"),
    Transition("C", "resume", History("D")),
    Transition("C", "reenter_prev", Previous("B")),
]


def make(on_transition=None):
    clock, handler = Clock(), Handler()
    spec = MachineSpec(STATES, TRANSITIONS, initial="A")
    machine = Machine(spec, handler, clock, on_transition)
    handler.machine = machine
    return machine, handler, clock


def run(coro):
    return asyncio.run(coro)


async def started(on_transition=None):
    machine, handler, clock = make(on_transition)
    await machine.start()
    handler.log.clear()
    return machine, handler, clock


class NestingAndOrderTest(unittest.TestCase):
    def test_start_enters_the_initial_state(self):
        async def go():
            machine, handler, _ = make()
            await machine.start()
            self.assertEqual((machine.path, machine.state), ("A", "A"))
            self.assertEqual(handler.log, ["enter_A"])
        run(go())

    def test_entering_a_composite_enters_its_initial_child_parents_first(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            self.assertEqual(machine.path, "B.B1")
            self.assertEqual(handler.log, ["exit_A", "do_go", "enter_B", "enter_B1"])    # exit, action, then entries
        run(go())

    def test_leaving_a_composite_exits_the_child_first_and_the_action_runs_between(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            handler.log.clear()
            await machine.dispatch("to_c")                                               # defined on the composite B
            self.assertEqual(machine.path, "C")
            self.assertEqual(handler.log, ["exit_B1", "exit_B", "do_to_c", "enter_C"])
        run(go())

    def test_a_transition_to_a_state_inside_its_source_is_local(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            handler.log.clear()
            await machine.dispatch("inside")                                             # B -> B2: B stays active
            self.assertEqual(machine.path, "B.B2")
            self.assertEqual(handler.log, ["exit_B1", "do_inside", "enter_B2"])
        run(go())

    def test_a_self_transition_leaves_and_re_enters(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            handler.log.clear()
            await machine.dispatch("self")
            self.assertEqual(handler.log, ["exit_B1", "do_self", "enter_B1"])
        run(go())

    def test_an_internal_transition_only_runs_its_action(self):
        async def go():
            records = []
            machine, handler, _ = await started(records.append)
            await machine.dispatch("go")
            handler.log.clear()
            await machine.dispatch("internal")
            self.assertEqual(handler.log, ["do_internal"])
            self.assertEqual(machine.path, "B.B1")
            self.assertTrue(records[-1]["internal"])
            self.assertEqual((records[-1]["from"], records[-1]["to"]), ("B.B1", "B.B1"))
        run(go())

    def test_events_nobody_handles_are_ignored(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("nonsense")
            self.assertEqual((machine.path, handler.log), ("A", []))
        run(go())


class GuardsTest(unittest.TestCase):
    def test_the_first_transition_whose_guard_holds_wins_and_not_inverts(self):
        async def go():
            for ok, expected in ((True, "A"), (False, "C")):
                machine, handler, _ = await started()
                await machine.dispatch("go")
                handler.flags["ok"] = ok
                await machine.dispatch("pick")
                self.assertEqual(machine.path, expected)
        run(go())

    def test_a_child_overrides_its_parent_only_while_its_guard_holds(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            await machine.dispatch("fallback")                                           # the child's guard is false: B's applies
            self.assertEqual(machine.path, "A")
            machine, handler, _ = await started()
            await machine.dispatch("go")
            handler.flags["other"] = True
            await machine.dispatch("fallback")                                           # now the child's own transition wins
            self.assertEqual(machine.path, "C")
        run(go())

    def test_an_unguarded_transition_shadows_every_guarded_one_outside_it(self):
        spec = MachineSpec([StateDef("P", initial="C"), StateDef("C", parent="P"), StateDef("Q"), StateDef("R")],
                           [Transition("C", "e", None),                                   # always matches first
                            Transition("P", "e", "Q", guard="ok"), Transition("P", "other", "R")], "P")
        self.assertEqual([(t.source, t.event) for t in spec.effective_transitions("C")], [("C", "e"), ("P", "other")])

        async def go():
            machine = Machine(spec, Handler(), Clock())
            await machine.start()
            await machine.dispatch("e")
            self.assertEqual(machine.path, "P.C")                                          # the inner one was taken, not P's
        run(go())

    def test_effective_transitions_list_an_overridden_one_once(self):
        spec = MachineSpec(STATES, TRANSITIONS, initial="A")
        events = [(t.source, t.event, t.guard) for t in spec.effective_transitions("B1") if t.event == "fallback"]
        self.assertEqual(events, [("B1", "fallback", "other"), ("B", "fallback", None)])


class TimersTest(unittest.TestCase):
    def test_a_timer_fires_after_its_time_with_source_timeout(self):
        async def go():
            records = []
            machine, handler, clock = await started(records.append)
            await machine.dispatch("go")
            await machine.dispatch("next")                                               # B2 has a 6 s timer
            self.assertEqual(machine.time_left(), 6.0)
            clock.now += 5.9
            self.assertEqual(await machine.tick(), 0)
            self.assertEqual(machine.path, "B.B2")
            clock.now += 0.2
            self.assertEqual(await machine.tick(), 1)
            self.assertEqual(machine.path, "C")
            self.assertEqual((records[-1]["event"], records[-1]["source"]), ("timeout", "timeout"))
        run(go())

    def test_a_timer_is_cancelled_when_its_state_is_left(self):
        async def go():
            machine, handler, clock = await started()
            await machine.dispatch("go")
            await machine.dispatch("next")
            await machine.dispatch("to_c")
            clock.now += 100
            self.assertEqual(await machine.tick(), 0)
            self.assertEqual(machine.time_left(), None)
        run(go())

    def test_the_time_may_come_from_a_handler_method_and_can_be_a_different_event(self):
        async def go():
            machine, handler, clock = await started()
            handler.drive_s = 3.0
            await machine.force("D")
            self.assertEqual(machine.path, "D.D1")
            self.assertEqual(machine.time_left(event="arrived"), 3.0)
            clock.now += 3.0
            await machine.tick()
            self.assertEqual(machine.path, "D.D2")
        run(go())

    def test_a_timer_method_returning_none_starts_no_timer(self):
        spec = MachineSpec([StateDef("W", timers=(Timer("no_timer"),))], [], "W")
        machine = Machine(spec, Handler(), Clock())
        run(machine.start())
        self.assertIsNone(machine.time_left())

    def test_every_due_timer_fires_in_one_tick_in_due_order(self):
        async def go():
            spec = MachineSpec([StateDef("W", timers=(Timer(2.0, "second"), Timer(1.0, "first"))), StateDef("X"), StateDef("Y")],
                               [Transition("W", "first", "X"), Transition("X", "second", "Y")], "W")
            clock = Clock()
            machine = Machine(spec, Handler(), clock)
            await machine.start()
            clock.now += 5
            await machine.tick()
            self.assertEqual(machine.state, "X")             # "first" moved to X, where W's other timer no longer applies
        run(go())


class HistoryTest(unittest.TestCase):
    def test_history_resumes_the_leaf_and_the_time_left_on_its_timer(self):
        async def go():
            machine, handler, clock = await started()
            await machine.dispatch("go")
            await machine.dispatch("next")                                               # B2, 6 s timer
            clock.now += 4
            await machine.dispatch("to_c")
            clock.now += 50                                                              # a long time passes elsewhere
            handler.log.clear()
            await machine.dispatch("back")
            self.assertEqual(machine.path, "B.B2")
            self.assertEqual(handler.log, ["exit_C", "enter_B", "enter_B2"])
            self.assertAlmostEqual(machine.time_left(), 2.0)                             # the 2 s that were left
            clock.now += 2.1
            await machine.tick()
            self.assertEqual(machine.path, "C")
        run(go())

    def test_history_with_nothing_remembered_starts_at_the_initial_child(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            await machine.dispatch("to_c")
            machine.forget("B")
            await machine.dispatch("back")
            self.assertEqual(machine.path, "B.B1")
        run(go())

    def test_a_transient_state_is_skipped_by_history(self):
        async def go():
            machine, handler, clock = await started()
            await machine.force("D")                                                     # D1 with a 6 s drive
            clock.now += 2
            await machine.dispatch("leave")                                              # leaves in D1
            await machine.force("D")
            clock.now += 1
            await machine.force("D2")                                                    # the transient state
            await machine.dispatch("leave")
            await machine.dispatch("resume")
            self.assertEqual(machine.path, "D.D1")                                       # not D2
            self.assertAlmostEqual(machine.time_left(event="arrived"), 6 - 1.0, delta=0.01)
        run(go())

    def test_previous_goes_back_to_the_child_before_the_current_one_with_its_timer(self):
        async def go():
            machine, handler, clock = await started()
            await machine.force("D")
            clock.now += 2
            machine._deadlines["D1"] = [(clock.now + 4.0, "arrived")]                    # 4 s of the drive left
            await machine.force("D3")                                                    # something else takes over
            await machine.dispatch("back")                                               # D3 -> Previous(D)
            self.assertEqual(machine.path, "D.D1")
            self.assertAlmostEqual(machine.time_left(event="arrived"), 4.0)
        run(go())

    def test_history_survives_other_composites_in_between(self):
        async def go():
            machine, handler, _ = await started()
            await machine.dispatch("go")
            await machine.dispatch("next")
            await machine.force("D")
            await machine.force("C")
            await machine.dispatch("back")
            self.assertEqual(machine.path, "B.B2")
        run(go())


class ForceTest(unittest.TestCase):
    def test_force_goes_to_any_state_running_exit_and_entry_actions(self):
        async def go():
            records = []
            machine, handler, _ = await started(records.append)
            await machine.dispatch("go")
            handler.log.clear()
            await machine.force("C")
            self.assertEqual(machine.path, "C")
            self.assertEqual(handler.log, ["exit_B1", "exit_B", "enter_C"])
            self.assertEqual((records[-1]["event"], records[-1]["source"]), ("force", "operator"))
        run(go())

    def test_force_accepts_paths_names_and_composites(self):
        async def go():
            machine, handler, _ = await started()
            await machine.force("B.B2")
            self.assertEqual(machine.path, "B.B2")
            await machine.force("b")
            self.assertEqual(machine.path, "B.B1")                                       # a composite enters its initial child
            await machine.force("D.D3")
            self.assertEqual(machine.path, "D.D3")
        run(go())

    def test_forcing_the_current_state_re_enters_it(self):
        async def go():
            machine, handler, _ = await started()
            await machine.force("C")
            handler.log.clear()
            await machine.force("C")
            self.assertEqual(handler.log, ["exit_C", "enter_C"])
        run(go())

    def test_force_between_siblings_keeps_the_parent(self):
        async def go():
            machine, handler, _ = await started()
            await machine.force("B1")
            handler.log.clear()
            await machine.force("B2")
            self.assertEqual(handler.log, ["exit_B1", "enter_B2"])
        run(go())

    def test_an_unknown_state_is_an_error(self):
        async def go():
            machine, _, _ = await started()
            with self.assertRaises(SpecError):
                await machine.force("NOWHERE")
        run(go())


class RunToCompletionTest(unittest.TestCase):
    def test_an_event_an_action_posts_is_handled_after_the_transition_finishes(self):
        async def go():
            machine, handler, _ = await started()
            await machine.force("C")
            handler.log.clear()
            await machine.dispatch("chain")                       # C -> A with an action that posts "next" (A -> C)
            self.assertEqual(handler.log, ["exit_C", "post_next", "enter_A", "exit_A", "enter_C"])
            self.assertEqual(machine.path, "C")
        run(go())

    def test_a_machine_that_posts_to_itself_forever_is_stopped(self):
        async def go():
            machine, handler, _ = await started()
            with self.assertRaisesRegex(RuntimeError, "kept posting events"):
                await machine.dispatch("spin")
        run(go())


class RecordsTest(unittest.TestCase):
    def test_every_transition_is_reported_with_from_to_event_and_source(self):
        async def go():
            records = []
            machine, handler, _ = await started(records.append)
            self.assertEqual([(r["from"], r["to"], r["event"]) for r in records], [("", "A", "start")])   # the first entry too
            records.clear()
            await machine.dispatch("go", source="operator")
            await machine.dispatch("next", source="sensor")
            self.assertEqual([(r["from"], r["to"], r["event"], r["source"]) for r in records],
                             [("A", "B.B1", "go", "operator"), ("B.B1", "B.B2", "next", "sensor")])
        run(go())

    def test_the_report_comes_after_the_exits_and_before_the_entries(self):
        async def go():
            order = []
            machine, handler, _ = make(lambda r: order.append("reported"))
            await machine.start()
            order.clear()
            handler.log = order                                                          # share one list
            await machine.dispatch("go")
            self.assertEqual(order, ["exit_A", "do_go", "reported", "enter_B", "enter_B1"])
        run(go())


class SpecChecksTest(unittest.TestCase):
    def test_bad_tables_are_rejected(self):
        bad = [
            ([StateDef("A"), StateDef("A")], [], "A"),                                    # twice
            ([StateDef("A", parent="NOPE")], [], "A"),                                    # unknown parent
            ([StateDef("A", initial="B"), StateDef("B", parent="A"), StateDef("C", parent="A")], [], "A"),   # fine: control below
            ([StateDef("A"), StateDef("B", parent="A")], [], "A"),                        # composite with no initial
            ([StateDef("A", initial="X")], [], "A"),                                      # initial on a leaf
            ([StateDef("A")], [], "NOPE"),                                                # unknown initial
            ([StateDef("A")], [Transition("A", "e", "NOPE")], "A"),                       # unknown target
            ([StateDef("A")], [Transition("NOPE", "e", "A")], "A"),                       # unknown source
            ([StateDef("A")], [Transition("A", "e", History("A"))], "A"),                 # history of a leaf
            ([StateDef("P", initial="C"), StateDef("C", parent="P")], [Transition("C", "e", "P")], "P"),   # to its own parent
        ]
        raised = 0
        for states, transitions, initial in bad:
            try:
                MachineSpec(states, transitions, initial)
            except SpecError:
                raised += 1
        self.assertEqual(raised, len(bad) - 1)                                            # all but the control

    def test_a_handler_missing_a_method_the_table_names_is_reported_up_front(self):
        spec = MachineSpec([StateDef("A", enter=("not_there",))], [Transition("A", "e", None, guard="nor_this")], "A")
        with self.assertRaisesRegex(SpecError, "nor_this, not_there"):
            Machine(spec, object())

    def test_reachability_follows_history_and_composite_targets(self):
        spec = MachineSpec(STATES, TRANSITIONS, initial="A")
        self.assertEqual(spec.reachable(), {"A", "B1", "B2", "C", "D1", "D2", "D3"})

    def test_resolve_accepts_paths_and_loose_names(self):
        spec = MachineSpec([StateDef("AWAIT_BOOK")], [], "AWAIT_BOOK")
        for text in ("AWAIT_BOOK", "await book", "ENGAGED.await-book", "await_book"):
            self.assertEqual(spec.resolve(text), "AWAIT_BOOK")

    def test_the_mermaid_diagram_nests_composites_and_draws_every_kind_of_arrow(self):
        text = MachineSpec(STATES, TRANSITIONS, initial="A").mermaid()
        self.assertTrue(text.startswith("stateDiagram-v2\n    [*] --> A\n"))
        self.assertIn("state B {", text)
        self.assertIn("B1 --> B2: next", text)
        self.assertIn("C --> B: back (history)", text)
        self.assertIn("D3 --> D: back (previous)", text)
        self.assertIn("B1 --> A: pick [ok]", text)
        self.assertIn("B1 --> C: pick [not ok]", text)


class PostedEventSourceTest(unittest.TestCase):
    def test_an_event_posted_by_an_action_has_the_source_of_the_event_that_caused_it(self):
        async def go():
            records = []
            machine, handler, clock = make(records.append)
            await machine.start()
            await machine.dispatch("go", source="operator")
            await machine.force("C", source="operator")
            await machine.dispatch("chain", source="sensor")             # C -> A, whose action posts "next" (A -> C)
            return [(r["event"], r["source"]) for r in records]
        events = run(go())
        self.assertIn(("chain", "sensor"), events)
        self.assertIn(("next", "sensor"), events)

    def test_a_posted_event_outside_any_event_is_internal(self):
        machine, handler, clock = make()
        machine.post("x")
        self.assertEqual(machine._queue[0].source, "internal")


if __name__ == "__main__":
    unittest.main()
