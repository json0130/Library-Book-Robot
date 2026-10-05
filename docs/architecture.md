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
                services/ (llm, speech, vision, books) call the AI server and the catalogue
```

1. **Perception** turns sensor input into events. `people.py` counts faces and `noise.py` measures the room, and both publish `ContextChanged`. `book_scanner.py` publishes `BookPlaced` and then `BookScanned`. Speech-to-text gives `UserUtterance`, and the camera's emotion model gives `EmotionDetected`. All three perception modules are stubs; in the simulator the keyboard publishes these events instead.
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
              BookScanned (isbn, title, author, summary, shelf, slot, found_in_catalog) <┤
              ScanFailed (reason)                                         <─ nothing read in time
                          │
                          v
   Dialogue: scanning ──BookScanned──> offering ──return / explain / recommend──> acting ──> closing
             scanning ──ScanFailed───> idle  (asks for the book flat, barcode up)
```

1. Something publishes `BookPlaced`. Today that is the simulator; later the ESP32 driver will publish it when the pad's weight sensor sees a book. Nothing in the scanner changes when that arrives.
2. `BookScanner` (`robot/perception/book_scanner.py`) starts a scan in the background so the publisher isn't held up. It grabs camera frames for up to `books.scan_timeout_s`, and `decode_isbn` tries each frame as grayscale, contrast-boosted, upscaled and rotated 90, 180 and 270 degrees until it finds a valid ISBN (`robot/services/isbn.py` checks the checksum, converts ISBN-10 to ISBN-13 and ignores other barcodes such as library stickers and price add-ons).
3. `BookFinder` (`robot/services/books.py`) looks the ISBN up in `data/catalog.csv`, then, if enabled, in Open Library (title and author only). A valid ISBN nobody knows still counts as a successful scan, with `found_in_catalog` false.
4. The scanner publishes `BookScanned`, or `ScanFailed` with a reason (no barcode in time, camera error, pyzbar missing). The dialogue offers the three choices, or asks the visitor to try again.
5. In `offering`, the visitor's words pick the action, using the catalogue only: *return* gives the shelf and storage slot and turns the storage ring, *explain* reads the catalogue summary, *recommend* names up to three books with the most genre and tag overlap. Every message goes out through the current style's channels, so it is printed in a quiet zone and spoken in a common area.

`robot/app.py` wires all of this together. The face page is a separate process (`display/server.py`) that the face actuator posts to over HTTP, and the page receives emotions and mouth movement over Server-Sent Events.

## Where things live

| Want to change | Edit |
|---|---|
| How the robot behaves in each zone and group size | `config/styles.yaml` |
| Which areas are quiet, the noise thresholds | `config/zones.yaml` |
| AI server address, device ports, thresholds | `config/robot.yaml` (or `AI_HOST` / `AI_PORT`) |
| The interaction steps | `robot/core/dialogue.py` |
| The books the robot knows | `data/catalog.csv` (sample data until the real catalogue is loaded) |
| How emotions are blended | `robot/core/expression.py` (weights documented at the top) |
| The face's look | `display/ui/index.html` |
