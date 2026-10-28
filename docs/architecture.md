# Architecture

The robot adapts how it interacts to where it is (a quiet zone or a common area) and who it is with (one visitor or a group). Everything runs on the Jetson as one Python process. The language model, speech and face models run on a separate AI server.

## Data flow

```
perception ──events──> bus ──> core ──events──> bus ──> actuation ──> drivers ──> hardware
 people.py                      context.py               voice.py       audio.py
 noise.py                       style.py                 face.py        camera.py
 book_scanner.py                dialogue.py              lights.py      esp32.py ──> ESP32
                                expression.py            neck.py, ...   fake.py (sim)
                                session_log.py
                services/ (llm, speech, vision, books, book_graph) call the AI server and the catalogue
```

1. **Perception** turns sensor input into events. `people.py` counts faces (AI server face detection, smoothed so a missed frame or a visitor looking down at a book doesn't count as leaving) and `noise.py` measures the room, and both publish `ContextChanged`; its `EngagementDetector` turns the same detections into `PersonEngaged` (a frontal face big enough for long enough), `PersonNearbyBusy` and `PersonLeft`. A `BookReader` (`book_reader.py`; today the camera scanner in `book_scanner.py`, later RFID) reacts to `BookPlaced` and publishes `BookIdentified` (or `ScanFailed`). Speech-to-text gives `UserUtterance`, and the camera's emotion model gives `EmotionDetected`. `noise.py` and the speech and emotion inputs are not wired up yet; in the simulator the keyboard publishes those events instead, while `sim --faces` uses the real `people.py`.
2. **The bus** (`robot/bus.py`) delivers each event to its subscribers in the order they subscribed, one at a time. A failing handler is logged and does not stop the others. An event published inside a handler is fully delivered before the outer one continues, so causes are always logged before their effects.
3. **Core** decides what the robot does:
   - `context.py` combines the map zone, the head count and an optional noise reading into a `Context` (quiet or common, alone or group). A loud reading turns a quiet area common and a silent one turns a common area quiet, using the thresholds in `config/zones.yaml`.
   - `style.py` maps the `Context` to a `StyleVector` (voice, channels, lights, neck pose, screen layout, proactivity, speed limit) using `config/styles.yaml`. `fixed` mode ignores the context: it is the study's baseline.
   - `fsm.py` is a small generic hierarchical state machine (nesting, history, guards, timers, entry and exit actions, `force()`), and `dialogue.py` is the table on top of it: IDLE, ENGAGED (GREET, OFFER, AWAIT_BOOK, SCAN, STORE, ANSWER, RECOMMEND, ANYTHING_ELSE, FAREWELL) and RETURNING (PLAN, NAVIGATING, AT_SHELF, ANNOUNCE, HOMING); see [The state machine](#the-state-machine). Every state reads the current `StyleVector`, so the same state is spoken in a common area but silent (eyes, light, screen) in a quiet one.
   - `storage.py` is the storage ring as data: which slot holds which returned book, saved to `data/state/storage.json`.
   - `expression.py` blends the visitor's detected emotion with the reply's emotion tag into one of seven face expressions.
   - `session_log.py` writes every event with the context and style in force to `data/logs/` for the study.
4. **Actuation** carries the decisions out. `Actuators.apply_style()` sets the ambient behaviour (lights, neck, screen layout, speed) when the style changes, and `Actuators.deliver()` sends a message through the style's channels (speech, screen, print, spotlight, gaze).
5. **Drivers** talk to the hardware: PulseAudio for the speaker and mic, OpenCV for the camera, and a serial link to the ESP32 for everything with a motor or an LED (protocol in `firmware/esp32/README.md`). `drivers/fake.py` has the same methods and only logs, which is what the simulator and tests use.

## The book flow

```
BookPlaced ──> BookReader ──> frames from the camera ──> decode_isbn ──> BookFinder ──┐
 (weight sensor,                 (until scan_timeout_s)    (pyzbar)       catalogue,   │
  sim: `book`)                                                            then Open Library
                                                                                        │
              BookIdentified (isbn, title, author, summary, shelf, slot, found_in_catalog,
                           call_number, section, floor, zone, map pose) <┤
              ScanFailed (reason)                                         <─ nothing read in time
                          │
                          v
   Dialogue: SCAN ──BookIdentified──> STORE / ANSWER / RECOMMEND (the visitor's intent) or OFFER (no intent yet) ──> ANYTHING_ELSE
                                         where is <title> / guide me ──> NavigationRequested ──> navigation (fake: logs)
             SCAN ──ScanFailed──> AWAIT_BOOK  (asks for the book flat, barcode up; the second failure goes back to OFFER)
```

1. Something publishes `BookPlaced`. Today that is the simulator; later the ESP32 driver will publish it when the pad's weight sensor sees a book. Nothing in the scanner changes when that arrives.
2. `BookScanner` (`robot/perception/book_scanner.py`, the camera `BookReader`; `books.reader` chooses it, and `rfid` is a stub that raises "not implemented") starts a scan in the background so the publisher isn't held up. It grabs camera frames for up to `books.scan_timeout_s`, and `decode_isbn` tries each frame as grayscale, contrast-boosted, upscaled and rotated 90, 180 and 270 degrees until it finds a valid ISBN (`robot/services/isbn.py` checks the checksum, converts ISBN-10 to ISBN-13 and ignores other barcodes such as library stickers and price add-ons).
3. `BookFinder` (`robot/services/books.py`) looks the ISBN up in `data/catalog.csv`, then, if enabled, in Open Library (title and author only). A valid ISBN nobody knows still counts as a successful scan, with `found_in_catalog` false. For a catalogue book the scanner also asks the book graph where it lives (section, floor, zone, map pose), and that goes on `BookIdentified`.
4. The reader publishes `BookIdentified`, or `ScanFailed` with a reason (no barcode in time, camera error, pyzbar missing). The dialogue goes on with what the visitor chose (STORE, ANSWER, RECOMMEND), offers the choices when they have not chosen yet, or asks the visitor to try again.
5. The visitor's words (or a screen button) pick the action: *return* (STORE) reserves a free storage slot, turns the ring to it, lights it and pulses the pad, then waits for `BookDropped` and says the slot (it does not read out the shelf); *explain* and *recommend* ask the LLM (`robot/services/book_llm.py`) to word the catalogue record, or up to three books the graph recommends together with its reasons, as speech. After a recommendation the robot offers to guide the visitor to the top pick (not after a return: they just handed that book in): "guide me" publishes `NavigationRequested` with the shelf's map pose, and `app.py` hands it to the navigation actuator (`go_to_pose`, which sends the ESP32 a `navigate` command; the fake driver just logs it). "Where is Dune?" answers the same way for any catalogue book. Every message goes out through the current style's channels, so it is printed in a quiet zone and spoken in a common area. Saying what they like ("something about space") recommends without a book (`BookGraph.recommend_for_interest`).

The LLM only ever sees real catalogue records (and the graph's reasons for each candidate) and is told to use nothing else; a recommendation that names none of the supplied candidates is discarded, returns never go through it, and books that are not in the catalogue are never sent to it. If the server is down, too slow or unusable, the dialogue says the catalogue text instead, so the flow never depends on the model.

`robot/app.py` wires all of this together. The face page is a separate process (`display/server.py`) that the face actuator posts to over HTTP, and the page receives emotions and mouth movement over Server-Sent Events.

## The state machine

[state_machine.md](state_machine.md) has the diagram, every state and every transition, generated from the table in `core/dialogue.py` (`python -m robot states --write`; `tests/test_state_machine.py` fails if the file is out of date). In short:

```
IDLE ──PersonEngaged / BookPlaced──> ENGAGED ──session ends, books in storage, after returning.auto_start_delay_s──> RETURNING
  ^                                     │  GREET → OFFER → AWAIT_BOOK ⇄ SCAN → STORE / ANSWER / RECOMMEND → ANYTHING_ELSE → FAREWELL
  └──── nothing in storage ─────────────┘                                                        (PersonLeft or a timeout → FAREWELL)
RETURNING: PLAN → NAVIGATING → AT_SHELF → (next shelf | HOMING) → IDLE;  PersonNearbyBusy → ANNOUNCE → back to where it was;
           PersonEngaged → pause (stop the base), ENGAGED, and when that visit ends, resume at the same shelf (history)
```

- **The machine** (`core/fsm.py`) is pure Python and unit tested (`tests/test_fsm.py`): states nest, an event is tried in the active state and then in each enclosing one, a transition has a guard, an action and a target (a state, `History`, or `Previous`, or none for an internal one), entry and exit actions run in order, a timer belongs to a state and its remaining time is kept across history, `force(state)` is the operator's goto, and events an action posts are handled after the transition (run to completion). The table is data, so tests check it (every state reachable, every waiting state has a timeout and a `PersonLeft` way out, no dead ends) and it draws itself as Mermaid.
- **Sources.** Every event carries where it came from (`sensor`, `operator`, `screen`, `timeout`); a follow-up event inherits it. `Dialogue` publishes a `StateTransition` for each move, which the session log records with the zone, and a `ScreenUpdate` (`state_path, zone, layout, title, text, options`) when a state is entered; `app.py` forwards that to the display server (`/screen`, `/state`).
- **Styles, not states, differ by zone.** Speaking and the chime need the `speech` channel; buttons need `touch_buttons`; a quiet FAREWELL, greeting and ANNOUNCE use eyes, light and screen text only.
- **Storage.** `core/storage.py` holds the ring: a slot per book, `reserve` / `commit` / `release`, the stored books grouped by shelf as the returning queue, and the JSON file (written atomically; a corrupt file is set aside as `.bad`). The catalogue's `slot` is only a preference. Nothing about visitors goes into it or into the book graph.
- **Operator controls.** `robot/operator.py` is the one implementation behind the sim's terminal commands and the display server's `/operator` page (`display/ui/operator.html`; button taps and operator commands reach the robot through the server's inbox, polled by `display_link.py`). `goto <state>` fills in what a state needs: a sample book for SCAN, STORE, ANSWER and RECOMMEND, two stored books on different shelves for RETURNING.
- **Config** (`config/robot.yaml`): `timeouts` (offer_s, await_book_s, scan_s, store_s, answer_s, anything_else_s, farewell_s), `returning` (auto_start, auto_start_delay_s, sim_drive_s, sim_unload_s, announce_s, home pose), `storage` (slots, path, sim_drop_s: the simulated `BookDropped` after the slot lights, 0 to wait for the operator or the real pad), `engagement` (min_face_px, dwell_s, left_after_s), `display` (host, port, bind_host, poll_s), `books.reader`.
- **Real hardware.** The ESP32 commands the machine uses (`stop`, `slot_light`, `pad_vibrate`, `light_pattern`) are in `firmware/esp32/README.md`. Driving to a shelf is `NavigationRequested` → `Navigation.go_to_pose`; the simulated drive and unload are timers (`returning.sim_drive_s`, `sim_unload_s`) until a real base reports arrival.

## Understanding and feedback

What a visitor says goes to `Dialogue.handle_text` (`core/dialogue.py`):

1. **The intent router** (`core/intents.py`) runs first, with no LLM. It lower-cases, strips punctuation, expands
   contractions, then matches words fuzzily (stdlib `difflib` plus an edit distance where a swapped pair of letters costs
   half; `intents.word_similarity` and a stricter `short_word_similarity` for words up to four letters, and a list of pairs
   that must never match, so "anything else" is not "nothing else" and "dun" is not "Dune"). Its vocabulary is the
   catalogue titles, the floorplan's sections and floors, the topics (`config/topics.yaml` aliases) and a small phrase list
   per intent. Intents: done, yes/no, return, explain, recommend (with or without an interest: "a good sci-fi book",
   "something about space"), where/guide, a section, topic or floor, a book title. It returns ranked candidates with
   confidences. One clear winner above `intents.min_confidence` is acted on directly; two close places ("history": the
   History section or "A Brief History of Time") become a question, shown as an options view; nothing confident goes to
   the LLM. The older regex rules (`classify`) still run after the router.
2. **Repair.** After `intents.repair_after` (2) utterances in a row that were not acted on in one state, the robot stops
   asking again: it shows the current choices as buttons, always with "I'm done", and in a common area also says them in
   a short sentence. A question it cannot answer ("how tall is the Eiffel tower?") is answered honestly ("I'm not sure")
   and does not count as a failure.
3. **Thinking feedback.** Whenever an utterance goes to the LLM, `Dialogue.thinking()` publishes `ThinkingStarted`
   at once: the face shows a thinking expression and the screen adds a subtle progress bar (both zones). In the common
   zone, if no sentence has been said after `intents.thinking_ack_s` (1.5 s) the robot says "Let me check." once per
   answer. `ThinkingEnded` carries how long it waited.
4. **Screen before speech.** `Dialogue.say` puts the text on the screen before it starts speaking, for replies that stay
   in a state (the drop confirmation, small talk) as well as for state changes.
5. **The hello is seen before the options.** The greeting stays on the full face (with its caption) at least
   `screen.greeting_hold_s` (2.5 s) whether it was spoken, failed or the style is silent; only then do the "How can I
   help?" cards replace it. The options view shows the main choices as cards (icon, name, one line of help) and the
   rest ("Done", "Back") as quiet buttons. `python -m robot sim --speak` also checks the text-to-speech server at start-up
   and warns when it cannot be reached, because then nothing is said.
6. **Timeouts wait** while the robot speaks or waits for the LLM (`fsm.suspend_timeouts()` / `resume_timeouts()`), and
   start again, in full, when it has finished.
7. **The AI server does not stream.** The LLM protocol (`vendor/ai_server/llm.py`) sends the whole reply as one message after
   the model has finished, so the time to the first byte and the total time are close; `services/llm.timed_ask()` measures
   both (logged as `[llm] first byte after ... ms, total ... ms`, and recorded by the scenario harness). Nothing in the
   robot pretends to stream: the first sentence is spoken when the reply arrives. Streaming would need a protocol change
   on the AI server.
8. **The model is kept warm.** The first request after the model has been idle is the slow one (about 9 s cold, against 1-3 s).
   With `--llm`, `services/warmup.py` sends one tiny request in the background as the robot starts (start-up does not
   wait for it) and, while the dialogue is IDLE, another every `ai_server.keepalive_s` seconds (default 240; 0 turns the
   keep-alives off, the warm-up stays). They use their own client, so they never touch a visitor's timing, and they
   publish nothing, so nothing is spoken or shown; the only trace is `[llm] warm-up answered in ... ms` in the log.

## The screen composer and views

```
 StateEntered (path, zone, text, options) ─┐
 BookIdentified, BookFocused ──────────────┤
 RecommendationsReady ─────────────────────┤──> ScreenComposer ──> ScreenUpdate (view, payload, layout, ttl_s) ──> app.py
 LocationResolved, ChoicesOffered ─────────┤    (core/screen.py)        ──> display server /screen ──> views.js
 StoringBook, ReturningProgress ───────────┤
 UserUtterance / UserChoice / BookPlaced ──┘ (keep the content up)            buttons ──> /input ──> UserChoice
```

- **Who decides.** The dialogue never picks a view. It publishes `StateEntered` on every state entry and content
  events when it has content (a book it is talking about, recommendations, a place it found, choices to make, a book
  going into a slot, returning progress). `ScreenComposer` maps state + latest content to one of six views: face,
  book_card, book_list, directions, options, status. The rules are in `ScreenComposer.compose()`: face states (IDLE,
  GREET, FAREWELL, PLAN, SCAN) show the face; RETURNING's driving states show status; STORE shows the book's card with
  its slot (and keeps it up afterwards); otherwise the latest content, else the state's choices as an options view
  (any zone), else the face. A new question (AWAIT_BOOK) or a new book (SCAN) clears the old content.
- **Payloads** are dataclasses in `core/screen.py` (`BookCardPayload`, `BookListPayload`, `DirectionsPayload`,
  `OptionsPayload`, `StatusPayload`, `FacePayload`) with `to_json()`; `ScreenUpdate.payload` carries the dict, so it
  goes straight to the page and into the session log.
- **Timing and zones.** Content stays until the next content or visit, or `screen.content_ttl_s` (quiet 30, common 15)
  after the last interaction; `App.run_ticker()` calls `ScreenComposer.tick()`, which brings the face back. Content
  views use the `split` layout: small eyes in a corner, never no face. A quiet zone gets longer summaries and larger
  text; a common area shorter ones (`screen.summary_chars`).
- **Reading, not listening (quiet zone).** A quiet zone does not hear the robot, so `ScreenComposer.with_message()` puts what
  it said (the state's `text`) on the card, list or directions payload as `message`, in full, and the page shows it
  instead of the clipped summary (`display/ui/views.js`; nothing is clamped or trimmed with "…" in a quiet zone: `fit()` sets the text smaller until the view fits, and the card's summary and a pick's reason are sent whole).
  Each state's text only holds what that state said, and the follow-up question ("Is there anything else I can help
  with?") follows an answer at once, so on entering ANYTHING_ELSE from ANSWER, RECOMMEND or STORE the composer carries
  the answer along (`carried`) and keeps it while the state is re-published with the same text; anything new said in the
  state replaces it. A returned book's card says "✓ Returned · storage slot N" once the pad has dropped it. A common
  area gets no message: the speech carries the words.
- **Places.** `services/places.py` resolves what place a visitor means: a floor ("what's on level 2"), a full title,
  a section name (the longest), a topic or alias from topics.yaml (the section holding most of its books), then a
  loosely typed title. Equally good candidates give `ChoicesOffered` and an options view. `services/floorplan.py`
  loads `data/floorplan.yaml`; the directions payload draws the target's floor, the robot (the station pose, or during
  RETURNING the last navigation target), an approximate straight or L-shaped path, the distance, and "Take the lift to
  Level N" when the target is on another floor. There is no real path planning.
- **Covers.** `services/covers.py`: downloaded covers in `data/covers/` (tools/import_books.py --covers), else an SVG
  placeholder (the title on a colour from the ISBN); the display server serves both at `/covers/<isbn>`.
- **The page.** `display/ui/views.js` has one function per view and builds plain DOM (text always as text). It needs
  nothing from the internet. Buttons POST their id to `/input` (`book:`, `where:`, `guide:`, `similar:` + ISBN,
  `section:<name>`, `floor:<n>`); `Dialogue.choice_info` turns them into choices for the same table, so "Show me the
  way" reuses the guide flow and "Something similar" the RECOMMEND state. There is no "Guide me" button on the
  directions view and none at ANYTHING_ELSE: that state shows the face with its question (`options_for("ANYTHING_ELSE")` is
  empty), the visitor answers aloud, and "I'm done" appears only in repair mode.
- **Expectation-aware matching.** `core/expectations.py`: when the robot asks a question with a few answers (it sets
  `session.expect` when it offers to guide, asks "anything else?" or "did you mean A or B?"), `Dialogue.handle_text` first
  tries `match_answers` on a short reply (up to `intents.expected_max_words`, fillers not counted) against only those answers:
  a loose per-word threshold (`expected_word_similarity`), and for a phrase of two or more words of the same length a loose
  whole-text ratio (`expected_phrase_similarity`), with a few phrase variants per answer ("guide me", "take me there",
  "show me", "lead the way"). Every word said must be explained by the answer ("show me Dune" is not "show me"), and the
  `NEVER_MATCH` pairs still hold, typos of them included ("anyting else" is not "nothing else"). The expectation is used
  up by the next utterance (or a button press, or the end of the visit); no match leaves the normal router untouched.
- **Books in the robot's storage.** `Dialogue.asked_for_stored` (a fuzzy "do you have / borrow ... it/with you" check plus a
  title or "it") and `tell_where` (for any book that is stored) call `offer_stored`: the book card, "I have X in my storage, in
  slot N. Would you like it?", and a `lend` expectation (`expectations.expect_lend`). `answer_lend` handles the yes/no itself
  (no state change): yes releases the slot from `Storage`, rotates and lights it (`lent_slot`, switched off when the visit
  ends); no says which shelf it goes back to. Both then ask "anything else?". `session.handed_in` keeps a book returned in this
  visit from being offered back.
- **Facilities.** `places.FACILITIES` maps a landmark kind of the floor plan (toilets, help_desk, exit, entrance) to the ways
  people name it. The router adds a `where` candidate with a `Place("facility")` (a multi-word name needs no cue; a bare
  "exit" or "entrance" needs "where" or "the"), `PlaceResolver.facility_in` does the same for the older rules, and
  `Dialogue.tell_facility` answers from `FloorPlan.find(kind)`: the nearest to the station first (or the named floor),
  what it is next to, the distance when on the robot's floor, and the other floors. It publishes
  `LocationResolved("landmark", ...)` so the screen draws the directions view (lift first on another floor), and it
  is information only (no guide offer, `last_action = "answer"`).
- **Follow-ups after a recommendation.** `Dialogue.focus_on()` makes the top pick the topic (`session.focus`; with no pad
  book it also becomes `session.book`, `virtual`), and `referent()` resolves "that book" / "that one" to it when a pad book
  is the topic. Scanning, "another book" and a new session clear it.

## The book graph

`robot/services/book_graph.py` builds one small graph from the four data files in `data/` at startup (`BookGraph.from_files`, and `reload()` after editing them). It is plain dicts and dataclasses with no graph library, and holds books and the library's layout only, never visitors or person identities (the face/identity code is separate and does not touch it).

```
 catalog.csv ──┐                      nodes: book  author  topic  broader-topic  call-number class  shelf  section  series
 topics.yaml ──┼─> BookGraph ──>      edges: written_by  about  broader  in_class  on_shelf  in_section  in_series  curated(relation)
 shelves.csv ──┤       │
 curated_links ┘       ├─ recommend(isbn, n) -> books with a 0..1 score, its components and a reason for each
                       └─ locate(isbn)       -> section, floor, shelf, zone and map pose (or None and why)
```

- **Topics** (`services/topics.py`): subjects in the catalogue are folded through `topics.yaml` (aliases to one canonical name, optional broader topic). Unknown subjects are kept as typed and reported by `tools/catalog_check.py`.
- **Call numbers** (`services/call_numbers.py`): Dewey, Library of Congress and fiction-style codes are cut into a hierarchy (Dewey 500 > 520 > 520.1; LC Q > QB > QB981); the deeper the shared prefix the closer two books are. Unreadable codes give an empty result, never an error.
- **Scoring** (`BookGraph.score`): seven components, each 0..1 with a reason: IDF-weighted shared topics (general topics, those with a narrower child in `topics.yaml`, count for 0.4), broader-topic overlap, same author, same series (next in series first), call-number closeness (same class and numerically near; its reason names the area, never the number), curated links, and text similarity (`services/text_similarity.py`: TF-IDF cosine behind an `Embedder` interface, so a sentence-embedding model can be dropped in). The weights are `books.similarity` in `config/robot.yaml`. `recommend()` leaves out the book itself and anything under `min_score`, and breaks ties by title then ISBN, so it is deterministic.
- **Two reasons**: `Score.reasons` is the full list (for the LLM prompt and the log); `Recommendation.because` is the one natural sentence the robot speaks, the most specific reason in the order series, author, curated link, specific topic, shelf area. How many picks are named is the style's `recommendations` (1 in a quiet zone, 3 in a common area).
- **Dialogue** (`core/dialogue.py`) uses `BookFinder.recommend` and `locate`; `services/book_llm.py` puts each candidate's full reasons and its single best reason into the prompt and tells the model to give one, and a reply that names none of the candidates (title matching ignores case and punctuation) is still discarded.
- **Tools**: `catalog_check.py` validates the files, `import_books.py` drafts rows from Open Library, `similarity_report.py` compares graph and text similarity and scores them against your labels, `graph_view.py` writes a self-contained `data/graph.html`. [catalog.md](catalog.md) explains the data files for whoever adds the books.

## Where things live

| Want to change | Edit |
|---|---|
| How the robot behaves in each zone and group size | `config/styles.yaml` |
| Which areas are quiet, the noise thresholds | `config/zones.yaml` |
| AI server address, device ports, thresholds | `config/robot.yaml` (or `AI_HOST` / `AI_PORT`) |
| The interaction steps (states, transitions, what each does) | `robot/core/dialogue.py` (then `python -m robot states --write`) |
| How long each state waits, the returning trip, storage, engagement | `timeouts`, `returning`, `storage`, `engagement` in `config/robot.yaml` |
| Operator controls | `robot/operator.py`, `display/ui/operator.html` |
| The books the robot knows, where they live, how they relate | `data/catalog.csv`, `topics.yaml`, `shelves.csv`, `curated_links.csv` (sample data until the real library is loaded; see [catalog.md](catalog.md)) |
| How recommendations are scored | `books.similarity` in `config/robot.yaml`, `robot/services/book_graph.py` |
| How emotions are blended | `robot/core/expression.py` (weights documented at the top) |
| The face's look | `display/ui/index.html` |
| Which view a state shows, timing per zone | `robot/core/screen.py`, `screen:` in `config/robot.yaml` |
| How a view looks | `display/ui/views.js` (and the styles in `index.html`) |
| The floor plan for directions | `data/floorplan.yaml` (docs/catalog.md) |
