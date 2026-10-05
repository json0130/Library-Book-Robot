# Library-Book-Robot

A library robot that adapts how it interacts to where it is (quiet zone or common area) and who it is with (one visitor or a group). It scans a book placed on its pad, then returns it, explains it, or recommends something similar.

The robot software runs on a Jetson Orin Nano (JetPack 6, Python 3.10+). The language model, speech and face models run on a separate AI server, reached over TCP. Motors, lights and the printer will hang off an ESP32. See [docs/architecture.md](docs/architecture.md) for how the parts fit together.

## Layout

```
config/            robot.yaml (server, devices, thresholds), styles.yaml (behaviour per context), zones.yaml
robot/             the robot program: python -m robot check | sim | run
  app.py           wires drivers, core logic and the bus together
  bus.py           in-process asyncio publish/subscribe
  events.py        the events on the bus (ContextChanged, BookScanned, UserUtterance, ...)
  config.py        loads config/*.yaml
  perception/      book_scanner (barcode -> ISBN -> BookScanned); people counting and noise level (stubs)
  core/            context, style, dialogue state machine, expression blending, session log
  services/        AI server wrappers: llm (client + sentence splitter), speech, vision;
                   isbn (validation), books (catalogue, similar books, Open Library fallback)
  actuation/       voice, face display, lights, neck, storage ring, printer, navigation
  drivers/         audio (PulseAudio), camera (OpenCV), esp32 (not yet), fake (logs only)
  sim/             keyboard simulator
display/           face display: server.py (SSE) + ui/index.html, start_display.sh (kiosk)
vendor/ai_server/  the AI server team's clients (face, emotion, stt, tts) and the protocol + LLM server script
tools/             test tools: chat_test, camera_test, face_enroll, server_check, book_scan_test, make_test_barcodes
firmware/esp32/    planned ESP32 serial protocol
tests/             unittest suite
docs/              architecture notes, reference images
data/              catalog.csv (SAMPLE catalogue), cache/ and logs/ and test_barcodes/ (generated, gitignored)
```

`vendor/ai_server/` holds the AI server team's files. Keep face.py, emotion.py, stt.py and tts.py unmodified so updates can be dropped in.

## Setup

```bash
git clone https://github.com/json0130/Library-Book-Robot.git
cd Library-Book-Robot
sudo apt install -y python3-yaml python3-opencv pulseaudio-utils libzbar0
pip install pyzbar                  # barcode reading (needs libzbar0); --user --break-system-packages on Ubuntu 24.04
```

PyYAML is the only required package. OpenCV (the camera tools and driver) comes from apt on the Jetson. pyzbar and OpenCV are only imported when a barcode is actually decoded, so the simulator and most tests run without them. pyserial will be needed once the ESP32 driver exists (`pip install -e .[serial]`). Set the AI server address in `config/robot.yaml`, or per shell with `export AI_HOST=<server-ip>` (and `AI_PORT`, default 7898).

## Running the robot

```bash
python -m robot check     # is the AI server reachable?
python -m robot sim       # try the logic from the keyboard: fake hardware, no AI server needed
python -m robot run       # on the hardware; for now exits naming the drivers that are missing (esp32)
```

`python main.py ...` forwards to `python -m robot ...`.

### Simulator

```
quiet alone | common group   set the zone (or an area from zones.yaml, e.g. reading_room) and alone/group
noise 62 | noise off         a noise reading in dB, which can override the zone
book                         place a random sample book on the pad (the fake camera shows its barcode)
book <isbn>                  place the book with this ISBN (a catalogue book, or any valid ISBN)
book none                    place a book whose barcode can't be read: the scan fails
scan <image path>            place a book and let the real barcode decoder read a photo of it
return | explain | recommend say that to the robot once it has offered
say <text>                   the visitor says something
feel <emotion> [confidence]  the camera sees this emotion on the visitor
leave                        everyone walks away
fixed on | fixed off         baseline condition: one style everywhere
help, quit
```

After each command it prints the context, the style and the dialogue state, and the fake drivers print what the hardware would do (`[esp32] neck pose=lowered`, `[voice whisper, volume 0.12] ...`). If the face display is running, expressions and the talking mouth show on it; otherwise they are only printed. Each session is logged to `data/logs/session-<time>.jsonl` (`--no-log` turns that off).

```bash
printf 'quiet alone\nbook\ncommon group\nquit\n' | python -m robot sim
```

### Books

When a book is placed on the pad the robot reads its ISBN barcode, looks the book up, and offers to take it back, explain it or recommend something similar.

```bash
python tools/make_test_barcodes.py                       # EAN-13 PNGs for every catalogue book in data/test_barcodes/
python tools/book_scan_test.py --list                    # the catalogue
python tools/book_scan_test.py --image data/test_barcodes/9780345391803-the-hitchhiker-s-guide-to-the-galaxy.png
python tools/book_scan_test.py --live                    # webcam with an overlay, q quits
```

Point the webcam at a barcode PNG on a screen, or print them. The scanner tries the grayscale image, a contrast-boosted copy, a 2x upscale and the 90, 180 and 270 degree rotations; library stickers and other non-ISBN codes are ignored. If no ISBN is read within `books.scan_timeout_s` (5 s) the robot asks the visitor to place the book flat with the barcode up.

`data/catalog.csv` is **sample data** (about a dozen well-known books, with summaries written for this project). Replace it with the library's real catalogue: same columns (`isbn13, title, author, genre, tags, summary, shelf, slot`; tags separated by semicolons; lines starting with `#` are ignored). Loading names the file and line of any bad row. For an ISBN not in the catalogue the robot can ask Open Library for the title and author (`books.online_fallback` in `config/robot.yaml`, 3 s timeout, answers cached in `data/cache/`); shelf, slot and summary stay empty and the book can't be returned or recommended from. Recommendations are the catalogue books with the most genre and tag overlap (Jaccard), ties broken by title.

### Behaviour rules

How the robot behaves in each context is data, not code: [config/styles.yaml](config/styles.yaml) has one entry per `quiet_alone`, `quiet_group`, `common_alone` and `common_group`, plus `fixed` (the study baseline) and `default` (the safe fallback, and the source of any field an entry leaves out). Each entry sets the voice (normal or whisper) and volume, the channels a message goes through (speech, screen, gaze, spotlight, print), light colour and brightness, the resting eye expression, neck pose, screen layout, whether the robot approaches people, and the speed limit. [config/zones.yaml](config/zones.yaml) maps named areas to zones and sets the noise thresholds.

## Face display

```bash
display/start_display.sh          # server + Chromium kiosk on http://localhost:8765 (falls back to chromium-browser, firefox)
python -m display.server          # server only; open http://localhost:8765 (--port 9000 to change)
```

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

The system prompt casts the model as the Library Book Robot, and each prompt carries the last 10 exchanges of the session. The server takes one prompt string, so the system prompt and history are flattened into it as `System:` / `User:` / `Assistant:` lines. If the face display is running, the mouth moves with each reply (with the speech audio in `--tts`, `--talk` and `--chat`; tune `--lip-delay` if it is out of sync).

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
2. Perception: people counting and noise level feeding `ContextChanged`; the pad's weight sensor publishing `BookPlaced` from the ESP32.
3. Replace the sample catalogue with the library's real one, then the LLM for explanations and recommendations (step 2 of the book pipeline).
4. Speak replies sentence by sentence to cut the TTS delay (about 5 s for a three-sentence reply today).
