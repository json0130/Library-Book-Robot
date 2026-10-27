"""Transcripts, the batch summary (summary.md), the hand-review sheet (review.csv) and compare.

    render_transcript(record, scenario)      one readable markdown transcript per run
    write_report(dir)                        summary.md and review.csv in the run directory
    compare(dir_a, dir_b)                    pass rate, latency and LLM fallback changes between two batches
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from robot.scenarios.checks import USER_ACTIONS, windows

ACTION_LABEL = {"say": "says", "tap": "taps", "place_book": "places a book", "engage": "engages", "busy": "busy",
                "leave": "leaves", "dropped": "drops"}


# ---------------------------------------------------------------------------------------------- transcript
def format_entry(e: dict) -> str:
    kind = e["kind"]
    head = f"L{e['n']:03d} +{e['t']:6.2f}s"
    if kind == "step":
        extra = f"   ** {e['error']}" if e.get("error") else ""
        return f"{head} **STEP {e.get('step', '')}** {e.get('detail', e.get('action', ''))}{extra}"
    if kind == "state":
        mark = " (internal)" if e.get("internal") else ""
        return f"{head} state  {e['from'] or '(start)'} -> {e['to']}  [{e['event']}, {e['source']}]{mark}"
    if kind == "said":
        voice = e["voice"] if e["spoken"] else "none (not spoken)"
        return f"{head} ROBOT ({voice}): {e['text']}"
    if kind == "screen":
        key = {k: v for k, v in e["key"].items() if v not in (None, "", [], {})}
        mark = " [thinking]" if e.get("thinking") else ""
        return f"{head} screen {e['view']} ({e['layout']}, {e['state_path']}){mark} {_short(key)}"
    if kind == "thinking":
        return f"{head} think  {'started: thinking face and progress bar' if e['phase'] == 'start' else 'ended, waited ' + str(e.get('ms')) + ' ms'}"
    if kind == "cmd":
        params = " ".join(f"{k}={v}" for k, v in e["params"].items())
        return f"{head} esp32  {e['cmd']} {params}".rstrip()
    if kind == "chime":
        return f"{head} CHIME  (volume {e['volume']})"
    if kind == "nav":
        return f"{head} NAV    to {e['title']} (shelf {e['shelf']}, level {e['floor']})"
    if kind == "llm":
        if e["phase"] == "request":
            return f"{head} llm    request: {e['prompt'][:100]!r}"
        if e["phase"] == "reply":
            first = f", first byte {e['first_byte_ms']} ms (not streamed)" if e.get("first_byte_ms") is not None else ""
            return f"{head} llm    reply ({e['ms']} ms{first}): {e['reply'][:100]!r}"
        if e["phase"] == "error":
            return f"{head} llm    ERROR ({e['ms']} ms): {e['error'][:100]}"
        return f"{head} llm    {e['method']}: reply {'USED' if e['phase'] == 'used' else 'DISCARDED, catalogue fallback'}"
    if kind == "log":
        return f"{head} LOG {e['level']} {e['logger']}: {e['message']}"
    return f"{head} ERROR {e.get('where', '')}: {e.get('message', '')}"


def _short(fields: dict, limit: int = 160) -> str:
    text = ", ".join(f"{k}={v}" for k, v in fields.items())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_transcript(record: dict, scenario=None) -> str:
    lines = [f"# {record['run_id']}", "",
             f"- scenario: **{record['scenario']}**" + (f" - {scenario.description}" if scenario and scenario.description else ""),
             f"- zone: **{record['zone']}**, seed `{record['seed']}`, speed x{record['speed']:g}, llm: {record['llm']}",
             f"- result: **{'PASS' if record['passed'] else 'FAIL'}**, ended in `{record['final_state']}`, "
             f"{record['storage']} book(s) left in storage",
             f"- replay: `python -m robot scenarios replay {record['run_id']}`",
             f"- times in the timeline are robot-seconds (the robot's clock runs x{record['speed']:g} faster than the wall clock, "
             "so a real LLM call of 4 s shows as 40 s); latencies in the checks are real milliseconds"]
    if record.get("tags"):
        lines.append(f"- tags: {', '.join(record['tags'])}")
    lines += ["", "## What the visitor chose (all from the seed)", ""]
    for c in record["choices"]:
        extra = f" (with {c['variation'].replace('+', ' and ')})" if c.get("variation") else (" (with typos)" if c.get("typos") else "")
        lines.append(f"- step {c.get('step', '-')}: {c['type']}: {c['value']}{extra}")
    if not record["choices"]:
        lines.append("- nothing random in this run")
    lines += ["", "## Checks", "", "| check | result | reason |", "|---|---|---|"]
    for c in record["checks"]:
        where = f" (L{c['line']:03d})" if c.get("line") else ""
        lines.append(f"| {c['name']} | {'pass' if c['passed'] else '**FAIL**'} | {c['reason'].replace('|', '/')}{where} |")
    lines += ["", "## Timeline", "", "```"]
    lines += [format_entry(e) for e in record["timeline"] if not (e["kind"] == "cmd" and e["cmd"] in QUIET_COMMANDS)]
    lines += ["```", ""]
    return "\n".join(lines)


QUIET_COMMANDS = {"light", "neck", "speed_limit", "gaze", "spotlight"}      # ambient commands: in runs.jsonl, not the transcript


# ---------------------------------------------------------------------------------------------- numbers
def percentile(values: list, p: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered) + 0.4999) - 1))]


def latencies(record: dict) -> tuple:
    """([ms from each user step to the first screen update], [ms to the first spoken sentence]) for one run."""
    screens, sentences = [], []
    for step, window in windows(record["timeline"]):
        if step.get("action") in ("leave", "busy"):
            continue
        screen = next((e for e in window if e["kind"] == "screen"), None)
        said = next((e for e in window if e["kind"] == "said"), None)
        if screen:
            screens.append((screen["rt"] - step["rt"]) * 1000)
        if said:
            sentences.append((said["rt"] - step["rt"]) * 1000)
    return screens, sentences


def llm_outcomes(record: dict) -> tuple:
    """(replies used, replies discarded or failed) in one run."""
    used = sum(1 for e in record["timeline"] if e["kind"] == "llm" and e["phase"] == "used")
    gone = sum(1 for e in record["timeline"] if e["kind"] == "llm" and e["phase"] in ("fallback", "error"))
    return used, gone


def group(records: list) -> dict:
    out: dict = defaultdict(list)
    for r in records:
        out[(r["scenario"], r["zone"])].append(r)
    return dict(sorted(out.items()))


def stats(records: list) -> dict:
    screens = [ms for r in records for ms in latencies(r)[0]]
    sentences = [ms for r in records for ms in latencies(r)[1]]
    used = sum(llm_outcomes(r)[0] for r in records)
    gone = sum(llm_outcomes(r)[1] for r in records)
    passed = sum(1 for r in records if r["passed"])
    return {"runs": len(records), "passed": passed, "rate": passed / len(records) if records else 0.0,
            "screen_median": percentile(screens, 50), "screen_p90": percentile(screens, 90),
            "sentence_median": percentile(sentences, 50), "sentence_p90": percentile(sentences, 90),
            "llm_calls": used + gone, "fallback_rate": gone / (used + gone) if used + gone else None}


def ms(value) -> str:
    return "-" if value is None else f"{value:.0f} ms"


def pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


# ---------------------------------------------------------------------------------------------- summary
def reply_variety(records: list) -> dict:
    """{(scenario, input): Counter of the robot's first reply} for inputs that were answered in different ways."""
    seen: dict = defaultdict(Counter)
    for r in records:
        for step, window in windows(r["timeline"]):
            said = next((e["text"] for e in window if e["kind"] == "said"), None)
            if said is None or step.get("action") not in ("say", "tap", "place_book"):
                continue
            key = (r["scenario"], step.get("paraphrase") or step.get("detail"))
            seen[key][said] += 1
    return {k: v for k, v in sorted(seen.items()) if len(v) > 1}


def summary_markdown(records: list, directory: Path) -> str:
    stat = stats(records)
    meta = records[0] if records else {}
    lines = ["# Scenario run summary", "",
             f"- directory: `{directory}`", f"- runs: **{stat['runs']}**, passed **{stat['passed']}** ({pct(stat['rate'])})",
             f"- llm: {meta.get('llm', '-')}, speed x{meta.get('speed', 1):g}",
             f"- first screen update: median {ms(stat['screen_median'])}, 90th percentile {ms(stat['screen_p90'])}",
             f"- first spoken sentence: median {ms(stat['sentence_median'])}, 90th percentile {ms(stat['sentence_p90'])}",
             f"- LLM replies discarded for the catalogue fallback: {pct(stat['fallback_rate'])} of {stat['llm_calls']} call(s)",
             "", "## Pass rate by scenario and zone", "",
             "| scenario | zone | runs | pass rate | screen median / p90 | sentence median / p90 | LLM fallback |",
             "|---|---|---|---|---|---|---|"]
    groups = group(records)
    for (scenario, zone), items in groups.items():
        s = stats(items)
        lines.append(f"| {scenario} | {zone} | {s['runs']} | {s['passed']}/{s['runs']} ({pct(s['rate'])}) | "
                     f"{ms(s['screen_median'])} / {ms(s['screen_p90'])} | {ms(s['sentence_median'])} / {ms(s['sentence_p90'])} | "
                     f"{pct(s['fallback_rate'])} |")
    lines += ["", "## Failing checks", ""]
    any_failure = False
    for (scenario, zone), items in groups.items():
        counts: Counter = Counter()
        examples: dict = {}
        for r in items:
            for c in r["checks"]:
                if not c["passed"]:
                    counts[c["name"]] += 1
                    examples.setdefault(c["name"], []).append((r, c))
        if not counts:
            continue
        any_failure = True
        lines += [f"### {scenario} / {zone}", ""]
        for name, count in counts.most_common():
            lines.append(f"- **{name}**: {count} of {len(items)} runs")
            for r, c in examples[name][:2]:
                line = f"#L{c['line']:03d}" if c.get("line") else ""
                lines.append(f"  - [{r['run_id']}](transcripts/{r['run_id']}.md{line}): {c['reason']}")
        lines.append("")
    if not any_failure:
        lines += ["No failing checks.", ""]
    variety = reply_variety(records)
    lines += ["## Distinct robot replies to the same input", "",
              "Inputs that got more than one reply (variety is good; odd wording or a wrong answer shows up here).", ""]
    if not variety:
        lines += ["Every input got the same reply every time.", ""]
    for (scenario, text), counter in list(variety.items())[:40]:
        lines.append(f"- **{scenario}**: `{str(text)[:100]}`")
        for reply, n in counter.most_common(6):
            lines.append(f"  - {n}x {reply[:160]}")
    lines += ["", "## Files", "", "- `runs.jsonl`: every run's full record (timeline, checks, seeds)",
              "- `transcripts/<run id>.md`: one readable transcript per run", "- `review.csv`: rate each run 1-5 by hand", ""]
    return "\n".join(lines)


def review_rows(records: list) -> list:
    rows = []
    for r in records:
        failed = [c["name"] for c in r["checks"] if not c["passed"]]
        rows.append({"scenario": r["scenario"], "zone": r["zone"], "run_id": r["run_id"],
                     "transcript": f"transcripts/{r['run_id']}.md",
                     "automatic_result": "pass" if r["passed"] else "fail: " + ", ".join(failed),
                     "rating_1_to_5": "", "notes": ""})
    return rows


def write_report(directory) -> tuple:
    """Write summary.md and review.csv into a run directory; returns their paths."""
    from robot.scenarios.runner import load_runs
    directory = Path(directory)
    records = load_runs(directory)
    summary = directory / "summary.md"
    summary.write_text(summary_markdown(records, directory), encoding="utf-8")
    review = directory / "review.csv"
    with open(review, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario", "zone", "run_id", "transcript", "automatic_result",
                                               "rating_1_to_5", "notes"])
        writer.writeheader()
        writer.writerows(review_rows(records))
    return summary, review


# ---------------------------------------------------------------------------------------------- compare
def delta(a, b, fmt) -> str:
    if a is None or b is None:
        return f"{fmt(a)} -> {fmt(b)}"
    return f"{fmt(a)} -> {fmt(b)} ({'+' if b - a >= 0 else '-'}{fmt(abs(b - a)).replace('%', ' pts') if fmt is pct else fmt(abs(b - a))})"


def compare_markdown(records_a: list, records_b: list, name_a: str = "A", name_b: str = "B") -> str:
    ga, gb = group(records_a), group(records_b)
    sa, sb = stats(records_a), stats(records_b)
    lines = [f"# Compare {name_a} -> {name_b}", "",
             f"- pass rate: {delta(sa['rate'], sb['rate'], pct)} ({sa['runs']} -> {sb['runs']} runs)",
             f"- first screen median: {delta(sa['screen_median'], sb['screen_median'], ms)}",
             f"- first sentence median: {delta(sa['sentence_median'], sb['sentence_median'], ms)}",
             f"- LLM fallback rate: {delta(sa['fallback_rate'], sb['fallback_rate'], pct)}", "",
             "| scenario | zone | pass rate | first screen (median) | first sentence (median) | LLM fallback |",
             "|---|---|---|---|---|---|"]
    for key in sorted(set(ga) | set(gb)):
        a, b = stats(ga[key]) if key in ga else None, stats(gb[key]) if key in gb else None
        if a is None or b is None:
            lines.append(f"| {key[0]} | {key[1]} | only in {name_b if a is None else name_a} | | | |")
            continue
        lines.append(f"| {key[0]} | {key[1]} | {delta(a['rate'], b['rate'], pct)} | "
                     f"{delta(a['screen_median'], b['screen_median'], ms)} | "
                     f"{delta(a['sentence_median'], b['sentence_median'], ms)} | "
                     f"{delta(a['fallback_rate'], b['fallback_rate'], pct)} |")
    fixed, broke = _check_changes(records_a, records_b)
    lines += ["", "## Checks that now pass / now fail", ""]
    lines += [f"- now passing: {name} ({n} fewer failing run(s))" for name, n in fixed] or ["- nothing newly passing"]
    lines += [f"- now failing: {name} ({n} more failing run(s))" for name, n in broke] or ["- nothing newly failing"]
    lines.append("")
    return "\n".join(lines)


def _check_changes(a: list, b: list) -> tuple:
    def failures(records):
        counts: Counter = Counter()
        for r in records:
            for c in r["checks"]:
                if not c["passed"]:
                    counts[c["name"]] += 1
        return counts
    fa, fb = failures(a), failures(b)
    fixed = [(n, fa[n] - fb[n]) for n in sorted(fa) if fa[n] > fb[n]]
    broke = [(n, fb[n] - fa[n]) for n in sorted(fb) if fb[n] > fa[n]]
    return fixed, broke


def compare(dir_a, dir_b) -> str:
    from robot.scenarios.runner import load_runs
    return compare_markdown(load_runs(dir_a), load_runs(dir_b), Path(dir_a).name, Path(dir_b).name)
