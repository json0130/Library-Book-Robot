# Library-Book-Robot

A library robot that adapts how it interacts to where it is (quiet zone or common area) and who it is with (one visitor or a group). It scans a book placed on its pad, then returns it, explains it, or recommends something similar.

The robot software runs on a Jetson Orin Nano (JetPack 6, Python 3.10+). The language model, speech and face models run on a separate AI server, reached over TCP. Motors, lights and the printer will hang off an ESP32. See [docs/architecture.md](docs/architecture.md) for how the parts fit together.

## First prototype design

![Robotic Library Assistant concept blueprint, design revision 02](Gemini_Generated_Image_cf59gbcf59gbcf59.jpeg)

*Concept blueprint, design revision 02, generated with Gemini. It is an AI-generated illustration of the idea, not an engineering drawing: the dimensions, the "patent" number and some details are made up.*

| Label | Part | Where it lives in this repo |
|---|---|---|
| A | Compartmentalised book storage (the ring of slots) | `robot/actuation/storage_ring.py`, `slot` in the catalogue |
| B | Multi-directional omni-wheels | `robot/actuation/navigation.py` |
| C | Lidar and sensor suite | not started |
| D | Presentation / sorting platform (the scan pad) | `robot/perception/book_scanner.py` |
| E | Neck and body structure | `robot/actuation/neck.py` |
| F | HMI and face plate (the screen) | `display/` |
| G | High-resolution camera | `robot/drivers/camera.py` |
| H | Internal illumination array | `robot/actuation/lights.py` |

The servo motors and circuit boards sit in the pylon behind the neck; the ESP32 plan is in `firmware/esp32/README.md`.

## Layout

```
config/            robot.yaml (server, devices, thresholds), styles.yaml (behaviour per context), zones.yaml
robot/             the robot program: python -m robot check | sim | run
  app.py           wires drivers, core logic and the bus together
  bus.py           in-process asyncio publish/subscribe
  events.py        the events on the bus (ContextChanged, PersonEngaged, BookIdentified, UserChoice, StateTransition, ScreenUpdate, ...)
  operator.py      operator controls (goto, zone, engage, busy, dropped, pick, book, say, reset): the sim's terminal and the /operator page share it
  display_link.py  collects button taps and /operator commands from the display server
  config.py        loads config/*.yaml
  perception/      book_reader (the BookReader interface: camera scanner, RFID stub), book_scanner (barcode -> ISBN -> BookIdentified), book_watch (barcode in view -> BookPlaced); people + identity (faces -> person numbers -> ContextChanged; EngagementDetector -> PersonEngaged / PersonNearbyBusy / PersonLeft); noise level (stub)
  core/            context, style, fsm (the generic state machine), dialogue (the table: IDLE / ENGAGED / RETURNING), screen (the screen composer: which view to show), storage (which book is in which slot), state_doc, expression blending, session log
  services/        AI server wrappers: llm (client + sentence splitter), speech, vision;
                   isbn (validation), books (catalogue, Open Library fallback), book_graph (the book knowledge graph:
                   recommendations and shelf locations), places (what place a visitor means), floorplan, covers,
                   topics, call_numbers, text_similarity
  actuation/       voice, face display, lights, neck, storage ring, printer, navigation
  drivers/         audio (PulseAudio), camera (OpenCV), esp32 (not yet), fake (logs only)
  scenarios/       the scenario test harness (python -m robot scenarios ...; scenarios/*.yaml are the scenarios)
  sim/             keyboard simulator
display/           face display: server.py (SSE, /screen, /input, /operator, /covers) + ui/index.html (face) + ui/views.js (content views), ui/operator.html, start_display.sh (kiosk)
vendor/ai_server/  the AI server team's clients (face, emotion, stt, tts) and the protocol + LLM server script
tools/             test tools: chat_test, camera_test, face_enroll, identity_test, get_face_models, server_check, book_scan_test, make_test_barcodes;
                   book data tools: catalog_check, import_books, similarity_report, graph_view
firmware/esp32/    planned ESP32 serial protocol
tests/             unittest suite
docs/              architecture notes, state_machine.md (generated), testing.md (scenario tests), reference images
data/              catalog.csv, topics.yaml, shelves.csv, curated_links.csv, floorplan.yaml (SAMPLE data, see docs/catalog.md), test_barcodes/ (barcode PNGs, committed),
                   cache/, covers/, logs/, screens/, state/, graph.html and catalog_draft.csv (generated, gitignored)
```

`vendor/ai_server/` holds the AI server team's files. Keep face.py, emotion.py, stt.py and tts.py unmodified so updates can be dropped in.

## Setup

```bash
git clone https://github.com/json0130/Library-Book-Robot.git
cd Library-Book-Robot
sudo apt install -y python3-yaml python3-opencv pulseaudio-utils libzbar0 sox
pip install pyzbar                  # barcode reading (needs libzbar0); --user --break-system-packages on Ubuntu 24.04
```

PyYAML is the only required package. OpenCV (the camera tools and driver) comes from apt on the Jetson. pyzbar and OpenCV are only imported when a barcode is actually decoded, so the simulator and most tests run without them. pyserial will be needed once the ESP32 driver exists (`pip install -e .[serial]`). Set the AI server address in `config/robot.yaml`, or per shell with `export AI_HOST=<server-ip>` (and `AI_PORT`, default 7898).

## Running the robot

```bash
python -m robot check     # is the AI server reachable?
python -m robot sim       # try the logic from the keyboard: fake hardware, no AI server needed
python -m robot sim --live   # webcam + camera window, face detection, auto-scan, speaker and LLM: step up (hello), show a book, ask, hear the answer
python -m robot states --mermaid   # the state machine as a Mermaid diagram, from its transition table
python -m robot run       # on the hardware; for now exits naming the drivers that are missing (esp32)
```

`python main.py ...` forwards to `python -m robot ...`. `python -m robot sim --llm` also uses the AI server's LLM for explain and recommend (see Books).

### Simulator

```
quiet alone | common group   set the zone (or an area from zones.yaml, e.g. reading_room) and alone/group
noise 62 | noise off         a noise reading in dB, which can override the zone
book                         place a book on the pad: scanned by the webcam with --camera, else a random sample book
book <isbn>                  fake camera only: place the book with this ISBN (a catalogue book, or any valid ISBN)
book none                    fake camera only: place a book whose barcode can't be read: the scan fails
scan <image path>            fake camera only: the real barcode decoder reads a photo of a book
return | explain | recommend say that to the robot once it has offered
<anything else you type>     the visitor says it, e.g. "can you explain it?" (`say <text>` also works)
feel <emotion> [confidence]  the camera sees this emotion on the visitor
leave                        everyone walks away (also PersonLeft)
fixed on | fixed off         baseline condition: one style everywhere
state                        where the state machine is, the zone, what is in storage
goto <state>                 force a state (OFFER, ENGAGED.SCAN, NAVIGATING...); fills in a sample book, or two stored books for RETURNING
zone quiet | zone common     change the zone only (a style setting, not a state)
engage | busy                a person engages / is near but busy (PersonEngaged / PersonNearbyBusy)
dropped                      the pad dropped the book into its slot (BookDropped)
pick return|ask|recommend|more|guide|done   press that screen button for the visitor
pick book:<isbn> | similar:<isbn> | section:<name>   tap a book, a card button or a choice on the screen
screen <view> [isbn | section]   show a screen view with sample data (face book_card book_list directions options status)
reset | reset all            back to IDLE (`all` also empties the storage)
wait <seconds> | wait idle   let the timers run, for scripted runs
help, quit
```

Typing `engage` is how a visit starts in the simulator (with `--faces` the camera does it: any frontal face for `engagement.dwell_s`; the closeness check is off until `engagement.require_close: true`). `--no-persist` starts with an empty storage ring and does not save it. Piped input waits at the end for the goodbye and any trip putting books back to finish (`--settle 0` to quit at once).

Options: `--llm` (the AI server's LLM words explain and recommend), `--camera` (scan with the real webcam; the camera picture shows on the operator page), `--faces` (find the people the webcam sees with the AI server's face detection: any frontal face that stays in view for `engagement.dwell_s` (0.5 s) makes the robot engage and say hello (set `engagement.require_close: true` to also require `min_face_px`), and no face for `engagement.left_after_s` ends the visit; needs `--camera`), `--auto-scan` (start a scan as soon as a book's barcode is held up to the camera, so you don't type `book`; the camera stands in for the pad's weight sensor; needs `--camera`), `--local-faces` (detect faces on the robot with OpenCV instead of the AI server, for when the server's face detection is down; also `perception.detector: local`), `--preview` (also open a camera window on the robot; off by default), `--speak` (speak replies through the speaker with TTS from the AI server, and the face display's mouth moves), `--live` (webcam, faces, auto-scan, speaker and LLM together). With `--live`: step in front of the camera and the robot says hello. Faces are told apart (`person 1`, `person 2`...), so each person is greeted once, even after leaving the view and coming back, and a new face is greeted (see Person IDs below); hold a book's barcode up to the camera (or type `book`); then type "can you explain it?" or "can you recommend something?" and the robot answers aloud. Replies are spoken only in styles with the speech channel (a common area, the default); in a quiet zone they are printed or go through other channels instead, and a quiet zone doesn't say hello aloud (the robot looks at the person, pulses its light and shows the greeting on the screen). With `--faces` the head count comes from the camera, so typing `common group` or `leave` is overridden at the next change. If the TTS server fails, the reply is only shown and the conversation carries on.

After each command it prints the context, the style and the dialogue state, every state transition with its path (`ENGAGED.OFFER -> ENGAGED.AWAIT_BOOK  (Choice, operator)`) and every screen the display is sent, and the fake drivers print what the hardware would do (`[esp32] neck pose=lowered`, `[voice whisper, volume 0.12] ...`). If the face display is running, expressions and the talking mouth show on it; otherwise they are only printed. Each session is logged to `data/logs/session-<time>.jsonl` (`--no-log` turns that off).

```bash
printf 'quiet alone\nbook\ncommon group\nquit\n' | python -m robot sim
printf 'zone common\nengage\npick return\nbook\ndropped\ndone\n' | python -m robot sim   # a return, then the automatic trip to the shelf
```

### Scenario tests

`python -m robot scenarios run --scenario all --runs 10 --llm mock --speed 10` plays scripted visitor scenarios
(`scenarios/*.yaml`: return a book, ask about a book, get a recommendation, find a section, an interrupted returning trip)
against the real robot core in both zones, records every state, sentence, screen and command, checks them automatically and
writes `data/eval_runs/<timestamp>/summary.md`, `review.csv` (rate runs 1-5 by hand) and one transcript per run. `replay`,
`compare` and a guided `live` mode for a human tester are in the same command. How to write a scenario, run, replay, review
and compare: [docs/testing.md](docs/testing.md). The harness only observes the robot; it never changes its behaviour.

### Understanding what the visitor says

Before the LLM sees a sentence, an intent router (`robot/core/intents.py`) tries to understand it with fuzzy matching on
the catalogue, the floorplan and the topics: "the computing section", "a good sci-fi book", "wear is the science
section" and "I'm done" are all handled without the LLM. Close calls ("history") are shown as a question. After two
misses in a row the robot shows buttons (with "I'm done") instead of asking again, it shows it is thinking within a third
of a second, and in a common area says "Let me check." if the LLM is slow. Details in
[docs/architecture.md](docs/architecture.md#understanding-and-feedback).

### The state machine

The robot's behaviour is a hierarchical, table-driven state machine (`robot/core/fsm.py` is the generic machine, `robot/core/dialogue.py` the table). The diagram and every transition are in [docs/state_machine.md](docs/state_machine.md), generated from the table (`python -m robot states --write`; a test fails if it is out of date).

- **IDLE** waits. **ENGAGED** is a visit: GREET, OFFER (return / ask about a book / recommend), AWAIT_BOOK, SCAN, STORE, ANSWER, RECOMMEND, ANYTHING_ELSE, FAREWELL. **RETURNING** puts the stored books back: PLAN, NAVIGATING, AT_SHELF, ANNOUNCE, HOMING.
- A person engaging (or a book placed) starts a visit. When it ends with books in storage, the robot waits `returning.auto_start_delay_s` (10 s) and starts putting them back, shelf by shelf. **Switched off for now** (`returning.auto_start: false`), so returned books stay in storage for testing; `goto RETURNING` on the operator page starts a trip by hand. Someone engaging during the trip pauses it; when that visit ends the trip resumes at the shelf it was heading for, with the drive time that was left. Someone nearby who isn't engaging gets an "excuse me" (a soft chime and a light pattern in a common zone; the light pattern and text on the screen only in a quiet one) and the trip carries on.
- Every waiting state has a timeout value (`timeouts` in `config/robot.yaml`) and a way out when the person leaves; both end in FAREWELL, which is silent in a quiet zone (eyes and light only). **The timeouts are switched off for now (`timeouts.enabled: false`)**: the robot waits for the visitor to answer, and only the person leaving, or saying bye, ends the visit.
- **Only returning needs the book on the pad.** Explaining, recommending and general talk work without one: name a title ("tell me about Dune", "something like The Martian"), say what you like ("something about space"), or ask for a place ("where is the history section", "where are the astronomy books", "take me to science fiction"; the robot says the level and shelf and offers to guide you there). A book you only named can't be returned: that still asks you to place it.
- **It knows what it is holding.** "Are you holding any books?", "what's in your storage?" are answered from the storage ring itself (titles, slots, the shelves they go back to, and the book on the pad), never by the LLM.
- **Every visit starts with an introduction** ("Hello! My name is Stuart."), even for the same person who was here a minute ago; once per visit.
- The zone (quiet or common) and fixed mode are style settings, never states. The quiet styles show touch buttons on the screen (`touch_buttons` in `config/styles.yaml`); tapping one sends a `UserChoice` back to the robot.
- Every transition is written to the session log with the time, from, to, event, source (`sensor`, `operator`, `screen` for a touch button, or `timeout`) and the zone.

**Operator controls.** Besides the sim's terminal commands above there is a web page. Start the display server (`python -m display.server`) and open `http://localhost:8765/operator`: it shows the live state path, zone and storage, has a quiet/common toggle, a map of the states with the current one highlighted and the ones it can go to next outlined (double-click a state to go there), the list of what can happen next with arrows to where each leads, why, and a button that makes it happen (person engages, book placed, visitor picks a choice...; timers and the robot's own steps show as "by itself" with the seconds left), a dropdown to go to any state, a box to say something as the visitor, and reset. From another computer open `http://<the robot's address>:8765/operator` (`display.bind_host` is `0.0.0.0` in `config/robot.yaml`; set `127.0.0.1` to allow only the robot's own computer). The page is laid out in landscape for a monitor (three columns: the robot camera plus a view-only face and screen preview, the controls, and the books on board with a log). It shows the robot's camera with the faces boxed (the simulator posts the picture to the display server when run with `--camera`), and the books the robot is holding in their slots (it holds up to `storage.slots`, 6, and remembers them across restarts in `data/state/storage.json`). Another computer can use only `/operator` (and that preview); the face page's buttons, `/screen`, `/input`, `/inbox` and the emotion and talk endpoints answer 403 unless the request comes from the robot itself. There is no login, so use a network you trust. Commands travel through the server's inbox, which the robot polls every `display.poll_s`, so the robot (or `python -m robot sim`) must be running too.

### Books

When a book is placed on the pad the robot reads its ISBN barcode, looks the book up, and offers to take it back, explain it or recommend something similar.

```bash
python tools/make_test_barcodes.py                       # regenerates the EAN-13 PNGs in data/test_barcodes/ (already committed) from the catalogue
python tools/book_scan_test.py --list                    # the catalogue
python tools/book_scan_test.py --image data/test_barcodes/9780345391803-the-hitchhiker-s-guide-to-the-galaxy.png
python tools/book_scan_test.py --live                    # webcam with an overlay, q quits
python tools/book_scan_test.py --live --size 1280x720    # sharper but about 8 fps; the default is 640x480 (smooth, 30 fps)
```

Point the webcam at a barcode PNG on a screen, or print them. The scanner tries the grayscale image, a contrast-boosted copy, a 2x upscale and the 90, 180 and 270 degree rotations; library stickers and other non-ISBN codes are ignored. This webcam delivers 30 fps at 640x480 but only about 8 fps at 1280x720 (a limit of the camera, not the code), so there is a trade-off between a sharper barcode and a smoother picture. 640x480 is the default (it read barcodes fine in testing); try `--size 1280x720` for small or distant barcodes, and set `devices.camera_width`/`camera_height` in `config/robot.yaml` for the robot. The live view always shows the newest frame and decodes on a second thread, so the scanner never makes the picture lag.

If no ISBN is read within `books.scan_timeout_s` (5 s) the robot asks the visitor to place the book flat with the barcode up.

The book data is **sample data** (a dozen well-known books, seven shelves, with summaries written for this project). Replace it with the library's real data: [docs/catalog.md](docs/catalog.md) explains every column of `data/catalog.csv`, `topics.yaml`, `shelves.csv` and `curated_links.csv`, and how to add a book or a shelf. Loading names the file and line of any bad row. For an ISBN not in the catalogue the robot can ask Open Library for the title and author (`books.online_fallback` in `config/robot.yaml`, 3 s timeout, answers cached in `data/cache/`); shelf, slot and summary stay empty and the book can't be returned or recommended from.

#### The book graph

At startup the data files become a small knowledge graph (plain dicts and dataclasses, no extra packages; `robot/services/book_graph.py`): books, authors, topics (with the broader topics above them), call-number classes, shelves, sections and series, joined by typed edges. It holds books and the library's layout only, never visitors or people. It answers two questions:

- **What else would they like?** Two books are compared on seven things, each with a short reason: shared topics weighted by rarity (IDF; general topics like "science" count for less), topics that meet under a broader one, the same author, the same series (the next book in a series comes first), how close the Dewey / Library of Congress call numbers are (same class and numerically near: `QB981` and `QB982` yes, `QB44.2` and `QB981` no), a librarian's curated link, and TF-IDF text similarity of the summaries. The weights are `books.similarity` in `config/robot.yaml`. The full reason list goes to the LLM and the log; what is *spoken* is one natural reason, the most specific one (series, author, curated link, most specific shared topic, shelf area), phrased like "because, like Cosmos, it's about astronomy": never a list of topics and never a call number. The LLM must name one of the candidates or its reply is discarded for the catalogue wording. In a quiet zone only the top pick is named; in a common area up to three (`recommendations` in `config/styles.yaml`).
- **Where does it live?** The shelf's section, floor, zone and map pose. Returning a book says the storage slot only. After a recommendation, or "where is Dune?", the robot offers to guide the visitor, and "guide me" publishes a `NavigationRequested` event with the shelf's pose (the fake base just logs it). "Where is Dune?" works too.

```bash
python tools/catalog_check.py                            # validate the data files: errors, warnings, statistics
python tools/book_scan_test.py --live --append isbns.txt # collect ISBNs by scanning a pile of books: each new one is appended (no duplicates), with a beep and its title
python tools/import_books.py isbns.txt                   # draft catalogue rows from Open Library into data/catalog_draft.csv
python tools/similarity_report.py                        # graph vs text similarity per book; --make-pairs / --eval to measure against your labels
python tools/graph_view.py                               # writes data/graph.html: open it to explore the graph
```

In the simulator: `book <isbn>`, `recommend`, `guide`, `where <title or isbn>` (the title is matched loosely) and `reload` (read the data files again after editing them).

**The LLM's part.** *Explain* and *recommend* are worded by the AI server's LLM, but only from catalogue records: it is shown the book's entry (explain) or the book plus up to three catalogue books the graph found similar, each with its reasons, to choose from (recommend), and told to use nothing else. A recommendation that names none of those candidates is discarded. **Before a book is scanned,** a plain hello gets a greeting and anything else ("what's your name?", "what can you do?") goes to the LLM as small talk with no catalogue, so it names no books and asks the visitor to place one on the pad; without the LLM, who-are-you is still answered. **Follow-ups:** after a recommendation the top pick is the topic, so "explain that book", "who wrote it?" and "where is it?" are about it, not "Which book?" (with a book on the pad, "that one" means the recommendation and "it" the pad book); the scanned book stays the topic until the visitor leaves or scans another, so after an answer they can ask for something else about it ("what's it about again?", "any related books?") or a free question ("who wrote it?", "does he get food somehow?"). Free questions go to the LLM with the book's catalogue entry and the conversation so far, and are told to use only that entry; if the LLM is down, who-wrote-it and genre questions are still answered from the catalogue, and anything else gets a short "I can't answer that right now". Saying "another book" after an answer asks for the next one, and "yes" to "anything else?" offers the choices again. Saying what you like ("something about space") recommends without a book. *Return* (storage slot, section, floor and shelf) never goes through the model, and a book that is not in the catalogue is never sent to it. If the server is down, slower than `ai_server.llm_timeout_s` (20 s), or gives an unusable answer, the robot says the plain catalogue text instead. Set `books.use_llm: false` to turn it off; the simulator uses the LLM only with `--llm`, so it still needs no server by default. Each new book starts a fresh conversation, and the last 10 exchanges are kept for the model.

### Person IDs

With `--faces` the first face the camera sees becomes `person 1`, the next new face `person 2`, and a face that comes back after being out of view is recognised as the same person: the robot doesn't greet them again, and the camera window labels each box with the number. The AI server only finds the faces; each box is turned into a face embedding on the robot (OpenCV's SFace model) and compared with the people seen so far. **Privacy:** the embeddings stay in memory for the run only. Nothing is sent to the server or saved, so "person 1" means one person for one run, and nothing about them survives quitting the simulator. (The server's own enrollment, `tools/face_enroll.py`, keeps photos for good and is for named people.)

```bash
python tools/get_face_models.py     # once: downloads the 38.7 MB model into models/face/ (gitignored)
python tools/identity_test.py       # live check: look at the camera, step out and back, let someone else step in
```

`identity_test.py` shows each face's similarity to every person it knows. Set `perception.identity_threshold` in `config/robot.yaml` from what it shows (0.40 by default): if one person keeps turning into a new number, lower it; if two people are merged, raise it. Without the model file the sim says so and falls back to counting faces, greeting once per visit with `dialogue.greet_cooldown_s`. A face that is too small (under 40 px) is not given a number.

### Behaviour rules

How the robot behaves in each context is data, not code: [config/styles.yaml](config/styles.yaml) has one entry per `quiet_alone`, `quiet_group`, `common_alone` and `common_group`, plus `fixed` (the study baseline) and `default` (the safe fallback, and the source of any field an entry leaves out). Each entry sets the voice (normal or whisper) and volume, the channels a message goes through (speech, screen, gaze, spotlight, print), light colour and brightness, the resting eye expression, neck pose, screen layout, whether the robot approaches people, the speed limit, and how many books a recommendation names (`recommendations`: the top pick only in a quiet zone, up to three in a common area) and whether the screen shows touch buttons (`touch_buttons`: yes in quiet zones). [config/zones.yaml](config/zones.yaml) maps named areas to zones and sets the noise thresholds.

## Face display

```bash
display/start_display.sh          # server + Chromium kiosk on http://localhost:8765 (falls back to chromium-browser, firefox)
python -m display.server          # server only; open http://localhost:8765 (--port 9000 to change)
```

The page also shows content next to the face: the **screen composer** (`robot/core/screen.py`) turns what the robot does into one of six views, drawn by `display/ui/views.js` (plain JS, no internet needed):

| View | When | What it shows |
|---|---|---|
| `face` | idle, hello, goodbye, scanning | the face full screen (in a quiet zone with a caption of what was said) |
| `book_card` | a book was scanned, explained, picked from a list, or is being stored | cover, title, author, a two-line summary, level / section / shelf, "Show me the way" and "Something similar" (a returned book shows its storage slot and, once stored, ✓ Returned, with no buttons; in a quiet zone the robot's words are under it, in full) |
| `book_list` | a recommendation | up to three covers with one short reason each; tap one (or say "the second one") to open its card |
| `directions` | "where is Dune", "take me to history", "where are the science books", "what's on level 2" | the floor plan with the section highlighted, "You are here", an approximate path, the distance; "Take the lift to Level N" for another floor (also for the toilets, the help desk, the exit and the entrance: "where is the toilet"); there is no "Guide me" button, say it instead |
| `options` | OFFER, AWAIT_BOOK, and "did you mean A or B?" | the choices as big buttons, in every zone |
| `status` | returning books | where it is going and how many books are left |

**Answers to its own questions.** Right after a question with a few possible answers (the guide offer, "Is there anything else?", "Did you mean A or B?"), a short reply (up to 4 words, fillers like "er" and "please" not counted) is matched against only those answers, more loosely than the rest of the router: "guy me", "er tqke me thre" and "yes plz" all mean *guide me* after the guide offer. The looseness is `intents.expected_word_similarity` / `expected_phrase_similarity` / `expected_max_words` in `config/robot.yaml`. It lasts for one utterance only; the same words with no question pending do nothing special, anything that does not match goes to the normal router unchanged, and "anything else" is never taken for "nothing else".

**"Anything else?"** shows just the face with that question as a caption, in both zones: no "Yes, more help" / "No, that's all" / "Guide me there" buttons (the visitor answers aloud; after two misses the "I'm done" button appears).

**A book the robot is holding.** If the visitor asks for a book that is in the robot's storage ("where is I Robot?", "do you have I Robot with you?", "can I borrow it?"), it does not send them to the shelf and offer to guide them there: it says "I have I Robot in my storage, in slot 3. Would you like it?". A yes (also "yes plz") rotates the ring to the slot, lights it, removes the book from the storage record and asks "anything else?"; a no keeps it and says which shelf it goes back to. A book the visitor handed in during this same visit is never offered back.

**What the robot itself is holding.** "Which book do you hold?", "do you have any book with you?", "what's on your pad?", "what's in your storage?" are about the robot's own books, never a recommendation and never the LLM. It always says both places: what is in its storage (titles, slots, shelves) and what is on its pad ("Nothing is on my pad right now."), and for an unclear question it adds how to look a library book up ("tell me its title or a topic"). "Do you have any books about space?" is still a recommendation, and "do you have I Robot with you?" still offers the stored copy.

**"Can I ask you some questions?"** (also "I have a question") is only permission, so the robot answers it ("Of course! Ask me about a book, ask where something is, or ask for a recommendation.") and keeps the visit going. "Thank you, also can I ask..." is not goodbye: "thank you" ends the visit only when nothing else was said ("thanks, that's all", "no thanks"). A typo in "tell me about" ("aobut") no longer hides the title: "tell me aobut the I Robot book" explains I Robot.

**Recommending with nothing to go on.** The first "recommend me something" asks what kind of books they like (in a quiet zone with buttons for the most common topics and **Surprise me**). Asking again, or saying "just recommend me one", "not sure", "you pick", "surprise me", never repeats the question: the robot chooses a book (the librarians' picks first, then the popular topics; never one it already suggested or just took back), names it and reads the first line of its summary. "Another one" then gives a different book.

**Reading in a quiet zone.** A quiet zone does not hear the robot, so what it says is on the screen, in full: the explanation, the answer, the confirmation and the next question, under the card, the list or the map (the `message` of the payload). Nothing is cut with "…": the page clamps no text in a quiet zone, and text that does not fit is set smaller, a step at a time, until it does (`fit()` in `display/ui/views.js`); a book's whole summary and a pick's whole reason are sent (`screen.summary_chars.quiet: 0`), and the goodbye line is read on the face (not said). An open display page reloads itself when its files change (`/ui-version`), so a kiosk picks up an update without anyone touching it. An answer stays up with "Is there anything else I can help with?" under it, until the next question replaces it. A returned book ends with a "✓ Returned · storage slot N" badge and "Done: ... is in storage slot N."; in a common area the speech carries the words and the screen has no message.

**Toilets, help desk, exit, entrance.** "Where is the toilet / restroom / loo", "the help desk / information desk", "the exit / how do I get out", "the entrance" are answered from `data/floorplan.yaml` (never the LLM, whatever book is being discussed): "The toilets are on the ground floor, next to the lift, about 3 m from here. There are more on level 2 and level 3." The nearest one to the robot comes first; naming a floor ("the toilets on level 3") answers for that floor, and the directions view starts with the lift when it is not the robot's. Information only: the robot does not offer to take you there. A bare "exit" is not treated as a question (it may mean "I'm leaving"). Without the landmark in the plan it says so and points to the help desk.

The state machine gets no new states: one state can show different views (ANSWER shows a book card for "tell me about it" and directions for "where is it"). Content views keep small animated eyes in the top-left corner. While a visitor is being helped the map, the list or the card stays up (it is what the robot is talking about); it clears with the next content, a new question or the end of the visit. `screen.content_ttl_s` (quiet 30 s, common 15 s) only limits content shown outside a visit, such as a view forced from the operator page. In a common area the buttons carry a short heading ("How can I help?"), not the sentence being spoken; a quiet zone shows the text. A quiet zone gets larger text and longer content; a common area shorter summaries, because the speech carries it. Buttons POST their id to `/input` (`book:<isbn>`, `guide:<isbn>`, `similar:<isbn>`, `section:<name>`, `floor:<n>`), which becomes a `UserChoice`. Covers come from `data/covers/` (`python tools/import_books.py --covers-only`) with a generated placeholder for the rest; the map from `data/floorplan.yaml` (docs/catalog.md). Open `/?update=<ScreenUpdate JSON>` to draw one screen without the robot (that is how the screenshots in `data/screens/` are made).

Nobody picks the view during a visit: it follows what the visitor asks and where the state machine is. The `screen` command and the view dropdown on `/operator` are only for testing a view: `screen book_card 9780345539434`, `screen directions History`, `screen face` in the simulator, or the view dropdown on `/operator`. `GET /operator` is the operator page (see The state machine). `display.bind_host` (`0.0.0.0` in `config/robot.yaml`, `127.0.0.1` if unset) is the address the server listens on.

Type in the server's terminal: an emotion with optional hold seconds (`happy`, `sad 5`), `idle`, `talk [seconds]`, `list`, `quit`. Or over HTTP:

```bash
curl -X POST localhost:8765/emotion -H 'Content-Type: application/json' -d '{"emotion":"happy","hold_s":3}'
curl 'localhost:8765/emotion?name=surprised&hold=5'
curl 'localhost:8765/talk?seconds=3'              # move the mouth for 3 s; seconds=0 stops
```

Emotions: happy, sad, angry, surprise, fear, disgust, neutral (aliases such as `scared`, `mad`, `joy` work). From Python: `robot.actuation.face.set_emotion("happy", hold_s=3.0)`. Open `display/ui/index.html?emotion=happy` to view one frozen face without a server (`&talk=0.7` opens the mouth); add `?glow=cheap` or `?glow=off` if the Jetson drops frames.

## Test tools

These talk to the AI server and the hardware directly, without the robot program. Run them from the repo root.

### AI server

On the server: `python llm.py receive --host 0.0.0.0` (port 7898; allow it through the firewall). On the Jetson:

```bash
python tools/server_check.py       # is the server reachable?
python tools/chat_test.py          # --llm: type, read the reply (default)
python tools/chat_test.py --stt    # speak, read the transcript
python tools/chat_test.py --tts    # type, hear it spoken
python tools/chat_test.py --talk   # type, hear the reply: LLM -> TTS
python tools/chat_test.py --chat   # speak, hear the reply: STT -> LLM -> TTS

python tools/chat_test.py --prompt "Recommend a book about robots."   # one --llm/--tts/--talk turn
python tools/chat_test.py --chat --wav question.wav                   # one voice turn from a file
```

Voice modes: press Enter, speak, press Enter again. Recording and playback use PulseAudio (`parecord`/`paplay`). `--mic Webcam` and `--speaker USB` (or `MIC` / `SPEAKER`) pick devices by part of the name in `pactl list short sources` / `sinks`. Recordings quieter than `--min-rms` (default 500) are skipped, because STT invents text like "Thank you." from silence.

The system prompt casts the model as Stuart, the Library Book Robot (the name is in `robot/persona.py`): upbeat and energetic, free to chat about everyday things, told not to greet or introduce itself twice. Each prompt carries the conversation as it really went, the last 10 exchanges, including the robot's fixed lines (hello, "You have <title>...") and what the visitor said, so the model knows what was already said; another book starts a new conversation, but the chat before the first book of a visit carries on. The server takes one prompt string, so the system prompt and history are flattened into it as `System:` / `User:` / `Assistant:` lines. If the face display is running, the mouth moves with each reply (with the speech audio in `--tts`, `--talk` and `--chat`; tune `--lip-delay` if it is out of sync).

### Camera and faces

```bash
python tools/camera_test.py                  # live names (same as --recognition), q quits
python tools/camera_test.py --detect         # live face boxes only
python tools/camera_test.py --emotion        # live emotion per face
python tools/face_enroll.py jay              # enroll 5 webcam photos as "jay"

python vendor/ai_server/face.py --host <server-ip> --image photo.jpg --operation recognize
python vendor/ai_server/emotion.py --host <server-ip> --image photo.jpg
```

The server stores enrollment photos in its `models/face/known_faces/<name>/` and needs exactly one face per photo, so enrolling crops each frame to the largest face. Recognition matches when similarity is at least 0.6.

### Without the server

```bash
python -m vendor.ai_server.llm receive --mock &     # echo server on 127.0.0.1:7898
python tools/chat_test.py --host 127.0.0.1 --prompt "hi"
python -m unittest discover -s tests -t .           # the test suite
```

## Next

1. ESP32 firmware and `robot/drivers/esp32.py`, then `python -m robot run` on the hardware.
2. Perception: noise level feeding `ContextChanged` (people counting is done: `robot/perception/people.py`, used by `sim --faces`); the pad's weight sensor publishing `BookPlaced` from the ESP32.
3. Replace the sample catalogue with the library's real one.
4. Speak replies sentence by sentence to cut the TTS delay (about 5 s for a three-sentence reply today).
