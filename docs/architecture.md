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

1. **Perception** turns sensor input into events. `people.py` counts faces (AI server face detection, smoothed so a missed frame or a visitor looking down at a book doesn't count as leaving) and `noise.py` measures the room, and both publish `ContextChanged`. `book_scanner.py` publishes `BookPlaced` and then `BookScanned`. Speech-to-text gives `UserUtterance`, and the camera's emotion model gives `EmotionDetected`. `noise.py` and the speech and emotion inputs are not wired up yet; in the simulator the keyboard publishes those events instead, while `sim --faces` uses the real `people.py`.
2. **The bus** (`robot/bus.py`) delivers each event to its subscribers in the order they subscribed, one at a time. A failing handler is logged and does not stop the others. An event published inside a handler is fully delivered before the outer one continues, so causes are always logged before their effects.
3. **Core** decides what the robot does:
   - `context.py` combines the map zone, the head count and an optional noise reading into a `Context` (quiet or common, alone or group). A loud reading turns a quiet area common and a silent one turns a common area quiet, using the thresholds in `config/zones.yaml`.
   - `style.py` maps the `Context` to a `StyleVector` (voice, channels, lights, neck pose, screen layout, proactivity, speed limit) using `config/styles.yaml`. `fixed` mode ignores the context: it is the study's baseline.
   - `dialogue.py` is the interaction state machine (idle, invited, book_placed, scanning, offering, acting, closing). Every step reads the current `StyleVector`, so the same step is spoken in a common area but whispered and printed in a quiet one.
   - `expression.py` blends the visitor's detected emotion with the reply's emotion tag into one of seven face expressions.
   - `session_log.py` writes every event with the context and style in force to `data/logs/` for the study.
4. **Actuation** carries the decisions out. `Actuators.apply_style()` sets the ambient behaviour (lights, neck, screen layout, speed) when the style changes, and `Actuators.deliver()` sends a message through the style's channels (speech, screen, print, spotlight, gaze).
5. **Drivers** talk to the hardware: PulseAudio for the speaker and mic, OpenCV for the camera, and a serial link to the ESP32 for everything with a motor or an LED (protocol in `firmware/esp32/README.md`). `drivers/fake.py` has the same methods and only logs, which is what the simulator and tests use.

## The book flow

```
BookPlaced ──> BookScanner ──> frames from the camera ──> decode_isbn ──> BookFinder ──┐
 (weight sensor,                 (until scan_timeout_s)    (pyzbar)       catalogue,   │
  sim: `book`)                                                            then Open Library
                                                                                        │
              BookScanned (isbn, title, author, summary, shelf, slot, found_in_catalog,
                           call_number, section, floor, zone, map pose) <┤
              ScanFailed (reason)                                         <─ nothing read in time
                          │
                          v
   Dialogue: scanning ──BookScanned──> offering ──return / explain / recommend──> acting ──> closing
                                         where is <title> / guide me ──> NavigationRequested ──> navigation (fake: logs)
             scanning ──ScanFailed───> idle  (asks for the book flat, barcode up)
```

1. Something publishes `BookPlaced`. Today that is the simulator; later the ESP32 driver will publish it when the pad's weight sensor sees a book. Nothing in the scanner changes when that arrives.
2. `BookScanner` (`robot/perception/book_scanner.py`) starts a scan in the background so the publisher isn't held up. It grabs camera frames for up to `books.scan_timeout_s`, and `decode_isbn` tries each frame as grayscale, contrast-boosted, upscaled and rotated 90, 180 and 270 degrees until it finds a valid ISBN (`robot/services/isbn.py` checks the checksum, converts ISBN-10 to ISBN-13 and ignores other barcodes such as library stickers and price add-ons).
3. `BookFinder` (`robot/services/books.py`) looks the ISBN up in `data/catalog.csv`, then, if enabled, in Open Library (title and author only). A valid ISBN nobody knows still counts as a successful scan, with `found_in_catalog` false. For a catalogue book the scanner also asks the book graph where it lives (section, floor, zone, map pose), and that goes on `BookScanned`.
4. The scanner publishes `BookScanned`, or `ScanFailed` with a reason (no barcode in time, camera error, pyzbar missing). The dialogue offers the three choices, or asks the visitor to try again.
5. In `offering`, the visitor's words pick the action: *return* gives the storage slot and where the book belongs (level, section and shelf) and turns the storage ring, *explain* and *recommend* ask the LLM (`robot/services/book_llm.py`) to word the catalogue record, or up to three books the graph recommends together with its reasons, as speech. After a return or a recommendation the robot offers to guide the visitor: "guide me" publishes `NavigationRequested` with the shelf's map pose, and `app.py` hands it to the navigation actuator (`go_to_pose`, which sends the ESP32 a `navigate` command; the fake driver just logs it). "Where is Dune?" answers the same way for any catalogue book. Every message goes out through the current style's channels, so it is printed in a quiet zone and spoken in a common area.

The LLM only ever sees real catalogue records (and the graph's reasons for each candidate) and is told to use nothing else; a recommendation that names none of the supplied candidates is discarded, returns never go through it, and books that are not in the catalogue are never sent to it. If the server is down, too slow or unusable, the dialogue says the catalogue text instead, so the flow never depends on the model.

`robot/app.py` wires all of this together. The face page is a separate process (`display/server.py`) that the face actuator posts to over HTTP, and the page receives emotions and mouth movement over Server-Sent Events.

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
| The interaction steps | `robot/core/dialogue.py` |
| The books the robot knows, where they live, how they relate | `data/catalog.csv`, `topics.yaml`, `shelves.csv`, `curated_links.csv` (sample data until the real library is loaded; see [catalog.md](catalog.md)) |
| How recommendations are scored | `books.similarity` in `config/robot.yaml`, `robot/services/book_graph.py` |
| How emotions are blended | `robot/core/expression.py` (weights documented at the top) |
| The face's look | `display/ui/index.html` |
