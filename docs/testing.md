# Scenario tests

A harness that plays scripted visitor scenarios against the real robot core many times, records how the robot reacts,
checks the results automatically and writes a report you can review by hand. It only **observes** the robot (the
recorder subscribes to the event bus and wraps the actuators); it never changes how the robot behaves.

```
scenarios/*.yaml ──> runner (fake drivers + the real core, a scaled clock) ──> data/eval_runs/<timestamp>/
                       pretend visitor: engage, say, place_book, tap...         runs.jsonl   every run, in full
                       recorder: states, speech, screens, commands, LLM          transcripts/<run id>.md
                       checks: generic + the scenario's expectations             summary.md, review.csv
```

## Commands

```bash
python -m robot scenarios list
python -m robot scenarios run --scenario all --runs 10 --zones quiet,common --llm mock --seed 1 --speed 10
python -m robot scenarios report data/eval_runs/<timestamp>          # (run already does this) summary.md + review.csv
python -m robot scenarios replay return_book-quiet-003               # the same seed and choices again
python -m robot scenarios compare data/eval_runs/A data/eval_runs/B  # did a fix help?
python -m robot scenarios live --scenario return_book --zone quiet   # a human tester on the real robot
```

- `--runs N` is per scenario, split evenly over the zones (10 runs = 5 quiet + 5 common).
- `--llm mock` uses the repo's mock receiver (`python -m vendor.ai_server.llm receive --mock`, started on a free local
  port): it only echoes the prompt, so it tests the flow, the screens and the fallbacks, not the wording. `--llm real`
  uses the AI server in `config/robot.yaml` and stops with a clear message if `python -m robot check` would fail.
- `--speed 10` runs the robot's own timers (the auto-return delay, simulated driving, the goodbye, timeouts) ten times
  faster through an injectable clock (`robot/scenarios/clock.py`). The LLM and the network keep their real latency, which
  is what the latency checks measure. Waits in a scenario are in robot-seconds.
- `--seed N` is the base seed. Every run gets its own seed (derived, recorded in `runs.jsonl` and at the top of its
  transcript). Everything random (which paraphrase, which book, typos, branches) comes from it, so
  `replay RUN_ID` repeats the same visitor exactly (with a real LLM the replies can still differ). `--typos P` sets the
  chance of typos per `say` (default 0.2, `scenarios.typo_probability` in `config/robot.yaml`).
- The runs use the robot settings in `scenarios.robot_overrides` (auto-return on, timeouts on), because `config/robot.yaml`
  has them off for manual tests.

## Writing a scenario

One YAML file in `scenarios/`:

```yaml
name: return_book                  # lower-case letters, digits, underscores
description: What it covers.
zones: [quiet, common]             # default both
setup: {stored: 2}                 # optional: books already in the storage ring
steps:
  - engage                         # engage | busy | leave | dropped | start_returning
  - one_of:                        # one branch per run, chosen by the seed; the name becomes a tag
      - name: say_first
        steps:
          - say: ["I'd like to return a book", "can I return a book?"]   # paraphrases: one is picked
          - place_book: random     # random (from the catalogue) | unknown (an ISBN not in it) | a 13-digit ISBN
      - name: place_first
        steps: [{place_book: random}, {say: ["return it"]}]
  - maybe: {name: odd_case, p: 0.2, steps: [{say: ["hello again"]}]}     # sometimes
  - say:
      text: ["who wrote it?", "who is the author of {title}?"]           # {title}, {author}: the last book placed
      answerable: true             # the catalogue can answer: "Sorry, I can't answer" fails the run
  - say: {text: ["how many pages does it have?"], unanswerable: true}    # it must not make something up
  - tap: "Guide me"                # a button label (or part of one) on the screen now; {tap: {item: random}} picks a book in a list
  - wait: 2                        # robot-seconds; {wait: {until_state: IDLE, timeout: 60}} (a timeout = a stuck state)
expect:                            # a list; each item has one kind, plus optional when / unless (tags) and zones
  - {state_path: [ENGAGED.SCAN, ENGAGED.STORE, ENGAGED.FAREWELL]}         # an ordered subsequence of the states entered
  - {views: [book_card, directions]}                                       # views that must have been shown
  - {forbid_phrases: ["guide you"]}                                        # nothing the robot said may contain these
  - {require_phrases_any: ["slot"]}
  - {events: [NavigationRequested]}                                        # events that must / must not (no_events) be published
  - {view_field: {view: book_card, field: badge, matches: slot}, unless: unknown_book}
  - {chime: true, zones: [common]}                                         # a chime in the common zone, none in the quiet zone
  - {resume_same_shelf: true}                                              # an interrupted trip resumes at the same shelf
  - {silent_farewell: true, zones: [quiet]}                                # FAREWELL: eyes and light only
  - {book_list_valid: true}                                                # recommended titles exist in the catalogue, one reason each
```

Tags: a `one_of` branch or `maybe` adds its name; `place_book: unknown` adds `unknown_book`. `when: tag` / `unless: tag`
make an expectation apply only to some runs. A wrong file is reported with the file and the step
(`python -m robot scenarios list` loads them all).

## What is checked

Generic (every run): no exceptions or ERROR logs; no stuck state (every `wait until_state` resolved, the run ended in
IDLE); in the quiet zone no chime and no normal-voice speech before the visitor spoke; the screen updates before or at
the first spoken sentence of each reaction; first screen update and first sentence within the latency limits
(`scenarios.latency`: 500 ms and 3 s, real time); no "Sorry, I can't answer" for `answerable` questions; an honest
"I don't know" for `unanswerable` ones; every `tap` found its button. Then the scenario's own `expect:` items. Each
result has a name, pass or fail and a reason pointing at the transcript line (`L042`).

## What a run records

`runs.jsonl` has one JSON record per run: scenario, run id, seed, zone, the paraphrases and choices, tags, a timeline
(every user step with its time, every state transition with event and source, every sentence with its voice, every
ScreenUpdate with its view and key fields, light, chime and ESP32 commands, NavigationRequested, LLM requests and
replies and whether each reply was used or discarded for the catalogue fallback, warnings and errors), the checks and the
final state. `transcripts/<run id>.md` is the same, readable, with the checks table first.

Times in a transcript are robot-seconds. With `--speed 10` the robot's clock runs ten times faster than the wall clock, so
a real LLM call of 4 s shows as 40 s of robot time, and the robot's own timers (a 30 s answer timeout) can fire during a
slow call. That is a property of the scaled clock, not a robot bug; the latency checks use real milliseconds.

## Reviewing

1. Open `summary.md`: pass rate per scenario and zone, the failing checks with counts and links to example transcripts,
   median and 90th-percentile latency, the LLM fallback rate and the distinct replies to the same input (variety, odd wording).
2. Open the linked transcripts. Fill in `review.csv` (one row per run: `rating_1_to_5`, `notes`) for the ones you read.
3. After a fix, run the same batch again (same `--seed`) and `compare` the two directories: pass rate, latency and
   fallback changes, and which checks now pass or fail.

## Guided live mode

`scenarios live --scenario NAME --zone quiet|common` runs the real robot (camera, face detection, display, speaker) and
guides a human tester through the scenario: each step is a card in the terminal and on the `/operator` page ("Place the
book 'Dune' on the pad", "Ask the robot: ..."). Only the tester presses `next` and `abort` (type them, or use the
buttons on the page). What the tester does arrives as the usual events and is recorded and checked like a scripted run.
No audio is recorded unless `--record-audio` is given (a short clip per spoken step, for team testing only; tell the
tester). `--no-camera` is a dry run with the fake camera.
