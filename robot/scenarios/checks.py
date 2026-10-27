"""Automatic checks on one recorded run.

Generic checks run on every run (no exceptions, no stuck state, quiet-zone voice and chime, screen before speech,
latency, no needless "sorry"); per-scenario expectations come from the scenario's `expect:` list. Each result has a
name, passed, a short reason and `line`: the transcript line (L<n>) the reason points at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

USER_ACTIONS = ("engage", "busy", "leave", "dropped", "place_book", "say", "tap")
SORRY = re.compile(r"sorry|not sure how to answer|can't answer|cannot answer|i couldn't find", re.I)
NOT_SURE = re.compile(r"not sure|don't know|do not know|can't|cannot|couldn't|don't have|no information|isn't listed|"
                      r"not (?:in|listed)|sorry", re.I)
DEFAULT_LIMITS = {"first_feedback_ms": 500, "first_sentence_ms": 1000, "first_sentence_llm_ms": 6000}
INVENTED_PAGES = re.compile(r"\b\d{2,4}\s*(?:pages|p\b)", re.I)


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""
    line: Optional[int] = None

    def to_json(self) -> dict:
        return {"name": self.name, "passed": self.passed, "reason": self.reason, "line": self.line}


def ok(name: str, reason: str = "") -> CheckResult:
    return CheckResult(name, True, reason)


def fail(name: str, reason: str, line: Optional[int] = None) -> CheckResult:
    return CheckResult(name, False, reason, line)


def of_kind(timeline: list, kind: str) -> list:
    return [e for e in timeline if e["kind"] == kind]


def windows(timeline: list) -> list:
    """[(step entry, entries until the next step)] for every pretend-visitor action."""
    steps = [e for e in timeline if e["kind"] == "step" and e.get("action") in USER_ACTIONS]
    out = []
    for step in steps:
        later = [e for e in timeline if e["n"] > step["n"] and e["kind"] == "step"]
        end = later[0]["n"] if later else float("inf")
        out.append((step, [e for e in timeline if step["n"] < e["n"] < end]))
    return out


def state_path(timeline: list) -> list:
    return [e["to"] for e in of_kind(timeline, "state") if not e.get("internal")]


def is_subsequence(wanted: list, path: list) -> tuple:
    """(True, None) if `wanted` appears in `path` in order (a name matches the full path or just the state's own name);
    else (False, the first wanted item that was not found)."""
    i = 0
    for item in wanted:
        while i < len(path) and not (path[i] == item or path[i].endswith("." + item)):
            i += 1
        if i == len(path):
            return False, item
        i += 1
    return True, None


# ---------------------------------------------------------------------------------------------- generic checks
def no_exceptions(run: dict) -> CheckResult:
    bad = [e for e in run["timeline"] if e["kind"] == "error" and e.get("where") != "wait"]
    bad += [e for e in run["timeline"] if e["kind"] == "log" and (e["level"] in ("ERROR", "CRITICAL") or e.get("exception"))]
    if bad:
        e = bad[0]
        return fail("no_exceptions", f"{len(bad)} error(s); first: {e.get('message') or e.get('where')}", e["n"])
    return ok("no_exceptions")


def no_stuck_state(run: dict) -> CheckResult:
    if run.get("stuck"):
        first = next((e for e in run["timeline"] if e["kind"] == "error" and e.get("where") == "wait"), None)
        return fail("no_stuck_state", run["stuck"][0], first["n"] if first else None)
    if run.get("error"):
        return fail("no_stuck_state", f"the run did not finish: {run['error']}")
    if run["final_state"] != "IDLE":
        return fail("no_stuck_state", f"the run ended in {run['final_state']}, not IDLE")
    return ok("no_stuck_state")


def quiet_zone_voice(run: dict) -> Optional[CheckResult]:
    if run["zone"] != "quiet":
        return None
    user_spoke = False
    for e in run["timeline"]:
        if e["kind"] == "step" and e.get("action") == "say":
            user_spoke = True
        elif e["kind"] == "chime":
            return fail("quiet_zone_voice", "a chime in the quiet zone", e["n"])
        elif e["kind"] == "said" and e.get("spoken") and e.get("voice") == "normal" and not user_spoke:
            return fail("quiet_zone_voice", f"normal-voice speech before the visitor spoke: {e['text'][:60]!r}", e["n"])
    return ok("quiet_zone_voice")


def screen_before_speech(run: dict) -> CheckResult:
    for step, window in windows(run["timeline"]):
        said = next((e for e in window if e["kind"] == "said"), None)
        if said is None:
            continue
        screens = [e for e in window if e["kind"] == "screen" and e["n"] < said["n"]]
        if not screens:
            later = next((e for e in window if e["kind"] == "screen" and e["n"] > said["n"]), None)
            where = f"; it changed at L{later['n']}, after the sentence" if later else "; it never changed"
            return fail("screen_before_speech", f"the first sentence after {step['detail']!r} came before any screen update{where}",
                        said["n"])
    return ok("screen_before_speech")


FILLERS = ("let me check.",)                       # the spoken "still working" filler: feedback, not the answer


def is_content(entry: dict) -> bool:
    return entry["kind"] == "said" and entry.get("text", "").strip().lower() not in FILLERS


def latency(run: dict) -> list:
    """Two checks, in real milliseconds after what the visitor did: the first feedback (a screen update or the thinking
    indication) and the first content sentence, which gets a longer limit when the answer waited for the LLM."""
    limits = run.get("thresholds") or {}
    worst = {"feedback": (0.0, None, None, False), "sentence": (0.0, None, None, False)}
    for step, window in windows(run["timeline"]):
        if step.get("action") in ("leave", "busy"):
            continue
        feedback = next((e for e in window if e["kind"] == "screen" or (e["kind"] == "thinking" and e.get("phase") == "start")), None)
        said = next((e for e in window if is_content(e)), None)
        waited = said is not None and any(e["kind"] == "llm" and e.get("phase") == "request" and e["n"] < said["n"] for e in window)
        for key, entry in (("feedback", feedback), ("sentence", said)):
            if entry is not None:
                ms = (entry["rt"] - step["rt"]) * 1000
                limit_key = "first_sentence_llm_ms" if key == "sentence" and waited else (
                    "first_sentence_ms" if key == "sentence" else "first_feedback_ms")
                over = ms - float(limits.get(limit_key, DEFAULT_LIMITS[limit_key]))
                if worst[key][1] is None or over > worst[key][0]:
                    worst[key] = (over, step, entry, waited, ms)
    results = []
    for key, name, limit_key in (("feedback", "latency_first_feedback", "first_feedback_ms"),
                                 ("sentence", "latency_first_sentence", "first_sentence_ms")):
        if worst[key][1] is None:
            results.append(ok(name, "nothing to measure"))
            continue
        over, step, entry, waited, ms = worst[key]
        if key == "sentence" and waited:
            limit_key = "first_sentence_llm_ms"
        limit = float(limits.get(limit_key, DEFAULT_LIMITS[limit_key]))
        what = " (waited for the LLM)" if waited and key == "sentence" else ""
        if over > 0:
            results.append(fail(name, f"{ms:.0f} ms after {step['detail']!r}{what} (limit {limit:.0f} ms)", entry["n"]))
        else:
            results.append(ok(name, f"worst {ms:.0f} ms{what}"))
    return results


def no_needless_sorry(run: dict) -> Optional[CheckResult]:
    asked = [(s, w) for s, w in windows(run["timeline"]) if s.get("answerable")]
    if not asked:
        return None
    for step, window in asked:
        for e in window:
            if e["kind"] == "said" and SORRY.search(e["text"]):
                return fail("no_needless_sorry", f"{step['detail']!r} can be answered from the catalogue but the robot said "
                            f"{e['text'][:80]!r}", e["n"])
    return ok("no_needless_sorry")


def honest_about_unknowns(run: dict) -> Optional[CheckResult]:
    asked = [(s, w) for s, w in windows(run["timeline"]) if s.get("unanswerable")]
    if not asked:
        return None
    for step, window in asked:
        said = [e for e in window if e["kind"] == "said"]
        if not said:
            return fail("honest_about_unknowns", f"no reply to {step['detail']!r}", step["n"])
        text = " ".join(e["text"] for e in said)
        if text.lstrip().startswith("[MOCK]"):
            return ok("honest_about_unknowns", "not judged: the mock LLM only echoes the prompt (run with --llm real)")
        if INVENTED_PAGES.search(text):
            return fail("honest_about_unknowns", f"{step['detail']!r} was answered with a made-up number: {text[:80]!r}", said[0]["n"])
        if not NOT_SURE.search(text):
            return fail("honest_about_unknowns", f"{step['detail']!r} (not in the catalogue) got an answer that does not say "
                        f"it does not know: {text[:80]!r}", said[0]["n"])
    return ok("honest_about_unknowns")


def taps_found(run: dict) -> CheckResult:
    for e in run["timeline"]:
        if e["kind"] == "step" and e.get("error"):
            return fail("tap_found", e["error"], e["n"])
    return ok("tap_found")


GENERIC = (no_exceptions, no_stuck_state, quiet_zone_voice, screen_before_speech, no_needless_sorry,
           honest_about_unknowns, taps_found)


# ---------------------------------------------------------------------------------------------- expectations
def applies(item: dict, run: dict) -> bool:
    tags = set(run.get("tags", []))
    if "zones" in item and run["zone"] not in item["zones"]:
        return False
    if "when" in item and item["when"] not in tags:
        return False
    if "unless" in item and item["unless"] in tags:
        return False
    return True


def evaluate(item: dict, run: dict) -> CheckResult:
    timeline = run["timeline"]
    kind = next(k for k in item if k not in ("when", "unless", "zones", "note"))
    value = item[kind]
    label = f"expect:{kind}"
    said = of_kind(timeline, "said")
    if kind == "state_path":
        found, missing = is_subsequence(value, state_path(timeline))
        return ok(label, " > ".join(value)) if found else fail(
            label, f"{missing} was not reached in order; path: {' > '.join(state_path(timeline))}")
    if kind == "views":
        seen = {e["view"] for e in of_kind(timeline, "screen")}
        missing = [v for v in value if v not in seen]
        return fail(label, f"view(s) never shown: {', '.join(missing)} (seen: {', '.join(sorted(seen))})") if missing else ok(label)
    if kind == "forbid_phrases":
        for e in said:
            for phrase in value:
                if phrase.lower() in e["text"].lower():
                    return fail(label, f"the robot said {phrase!r}: {e['text'][:80]!r}", e["n"])
        return ok(label)
    if kind == "require_phrases_any":
        for e in said:
            if any(p.lower() in e["text"].lower() for p in value):
                return ok(label)
        return fail(label, f"the robot never said any of: {', '.join(value)}")
    if kind in ("events", "no_events"):
        names = [n for _, n in run.get("events", [])]
        present = [v for v in value if v in names]
        if kind == "events":
            missing = [v for v in value if v not in names]
            return fail(label, f"event(s) never published: {', '.join(missing)}") if missing else ok(label)
        return fail(label, f"event(s) published that should not be: {', '.join(present)}") if present else ok(label)
    if kind == "view_field":
        pattern = re.compile(str(value["matches"]), re.I)
        for e in of_kind(timeline, "screen"):
            if e["view"] == value["view"] and pattern.search(str(e["key"].get(value["field"], ""))):
                return ok(label, f"{value['view']}.{value['field']} ~ {value['matches']}")
        return fail(label, f"no {value['view']} screen had {value['field']} matching {value['matches']!r}")
    if kind == "chime":
        count = len(of_kind(timeline, "chime"))
        if value and count == 0:
            return fail(label, "no chime, but one was expected")
        if not value and count:
            return fail(label, f"{count} chime(s), but none were expected", of_kind(timeline, "chime")[0]["n"])
        return ok(label)
    if kind == "silent_farewell":
        return silent_farewell(run, bool(value))
    if kind == "book_list_valid":
        return book_list_valid(run)
    return resume_same_shelf(run)


def book_list_valid(run: dict) -> CheckResult:
    """Every book in a recommendation list exists in the catalogue and has exactly one short reason."""
    label = "expect:book_list_valid"
    known = {t.lower() for t in run.get("catalogue_titles", [])}
    lists = [e for e in of_kind(run["timeline"], "screen") if e["view"] == "book_list"]
    if not lists:
        return fail(label, "no book list was shown")
    for e in lists:
        items = e["key"].get("items") or []
        if not items:
            return fail(label, "a book list with no books", e["n"])
        for item in items:
            if (item.get("title") or "").lower() not in known:
                return fail(label, f"{item.get('title')!r} is not in the catalogue", e["n"])
            reason = item.get("reason") or ""
            if not reason.strip():
                return fail(label, f"no reason for {item['title']!r}", e["n"])
            if reason.count("because") > 1 or reason.count(";") or reason.count(" and ") > 1:
                return fail(label, f"more than one reason for {item['title']!r}: {reason!r}", e["n"])
    return ok(label, f"{len(lists)} list(s), all titles in the catalogue")


def silent_farewell(run: dict, silent: bool) -> CheckResult:
    """In a quiet zone FAREWELL says nothing aloud and uses the light; in a common zone it is spoken."""
    label = "expect:silent_farewell"
    timeline = run["timeline"]
    states = of_kind(timeline, "state")
    entry = next((e for e in states if e["to"].endswith("FAREWELL")), None)
    if entry is None:
        return fail(label, "the visit never reached FAREWELL")
    after = [e for e in states if e["n"] > entry["n"]]
    end = after[0]["n"] if after else float("inf")
    within = [e for e in timeline if entry["n"] < e["n"] < end]
    spoken = [e for e in within if e["kind"] == "said"]
    light = [e for e in within if e["kind"] == "cmd" and e["cmd"] == "light_pattern"]
    if silent:
        if spoken:
            return fail(label, f"FAREWELL said {spoken[0]['text'][:60]!r} in the quiet zone", spoken[0]["n"])
        return ok(label) if light else fail(label, "FAREWELL was silent but showed no light pattern", entry["n"])
    return ok(label) if spoken else fail(label, "FAREWELL said nothing in the common zone", entry["n"])


def resume_same_shelf(run: dict) -> CheckResult:
    """The trip interrupted by a person resumes at the shelf it was heading to."""
    label = "expect:resume_same_shelf"
    timeline = run["timeline"]
    states = of_kind(timeline, "state")
    pause = next((e for e in states if e["from"].startswith("RETURNING") and e["to"].startswith("ENGAGED")), None)
    if pause is None:
        return fail(label, "the trip was never interrupted by a person")
    navs = of_kind(timeline, "nav")
    before = [n for n in navs if n["n"] < pause["n"] and n.get("shelf")]
    resume = next((e for e in states if e["n"] > pause["n"] and e["from"].endswith("FAREWELL") and e["to"].startswith("RETURNING")), None)
    if not before or resume is None:
        return fail(label, "no trip to resume (no earlier destination, or it never went back to RETURNING)", pause["n"])
    after = [n for n in navs if n["n"] > resume["n"] and n.get("shelf")]
    if not after:
        return fail(label, "after the visit the robot did not head to a shelf again", resume["n"])
    if after[0]["shelf"] != before[-1]["shelf"]:
        return fail(label, f"it was heading to {before[-1]['shelf']} but resumed towards {after[0]['shelf']}", after[0]["n"])
    return ok(label, f"both times shelf {after[0]['shelf']}")


def run_checks(run: dict, scenario) -> list:
    results = [check(run) for check in GENERIC]
    results.extend(latency(run))
    results = [r for r in results if r is not None]
    for item in scenario.expect:
        if applies(item, run):
            results.append(evaluate(item, run))
    return results
