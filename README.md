# Library-Book-Robot

A library robot that adapts how it interacts to where it is (quiet zone vs common area) and who it is with. It scans a book placed on its pad, then returns it, explains it, or recommends something similar.

The robot software runs on a Jetson Orin Nano. The language model runs on a separate AI server and is reached over TCP with the protocol in `modules/llm.py`.

## Current step: Jetson to AI server link

`python main.py chat` sends a prompt to the AI server and prints the reply one sentence at a time. Later, the same sentences will feed text-to-speech and the on-screen face.

### 1. On the AI server

```bash
python llm.py receive --host 0.0.0.0              # default port 7898
```

Allow inbound TCP 7898 through its firewall and give it a fixed IP (or a DHCP reservation).

### 2. On the Jetson

```bash
git clone https://github.com/json0130/Library-Book-Robot.git
cd Library-Book-Robot

export AI_HOST=<server-ip>                        # default 10.42.0.118, AI_PORT default 7898

python main.py chat --check        # is the server reachable?
python main.py chat                # --llm: type, read the reply (default)
python main.py chat --stt          # speak, read the transcript
python main.py chat --tts          # type, hear it spoken
python main.py chat --talk         # type, hear the reply: LLM -> TTS
python main.py chat --chat         # speak, hear the reply: STT -> LLM -> TTS

python main.py chat --prompt "Recommend a book about robots."   # one --llm or --tts turn
python main.py chat --chat --wav question.wav                   # one voice turn from a file
```

Voice modes: press Enter, speak, press Enter again. Recording and playback use PulseAudio (`parecord`/`paplay`). Use `--mic Webcam` (or `export MIC=Webcam`) to pick a microphone and `--speaker USB` (or `export SPEAKER=USB`) to pick a speaker, by part of the name in `pactl list short sources` / `sinks`. Recordings quieter than `--min-rms` (default 500) are skipped, because STT invents text like "Thank you." from silence and room noise; each recording prints its level so the threshold can be tuned.

The system prompt casts the model as the Library Book Robot (books, returns, recommendations, directions; spoken, at most three sentences; `--system` overrides it). Each prompt carries the last 10 exchanges of the session, so follow-up questions work; `--prompt` runs are single turns with no history. The server takes one prompt string, so the system prompt and history are flattened into it as `System:` / `User:` / `Assistant:` lines.

### Testing without the server

```bash
python -m modules.llm receive --mock &            # echo server on 127.0.0.1:7898
python main.py chat --host 127.0.0.1 --prompt "hi"
python -m unittest discover -s tests -t .         # sentence splitter tests
```

No packages to install: everything uses the standard library.

## Face: detection, recognition, emotion

`modules/face.py` and `modules/emotion.py` are the AI server team's single-image clients; keep them unmodified so updates can be dropped in. `features/camera.py` runs them on the webcam and needs OpenCV.

```bash
python main.py camera --host <server-ip>                  # live names (same as --recognition), q quits
python main.py camera --host <server-ip> --detect         # live face boxes only
python main.py camera --host <server-ip> --emotion        # live emotion per face
python main.py camera --host <server-ip> --enroll jay     # enroll 5 webcam photos as "jay"

python modules/face.py --host <server-ip> --image photo.jpg --enroll --name jay
python modules/face.py --host <server-ip> --image photo.jpg --operation recognize
python modules/emotion.py --host <server-ip> --image photo.jpg
```

The server stores enrollment photos in its `models/face/known_faces/<name>/` and needs exactly one face per photo, so `--enroll` crops each webcam frame to the largest face before sending it. Recognition matches when similarity is at least 0.6. The emotion model finds faces with its own detector, which misses faces turned well away from the camera.

## Robot face display

`python main.py display` serves a full-screen face (`ui/index.html`: glowing cyan eyes and mouth on black, standard library only) and pushes emotions to it over Server-Sent Events. The page starts in idle (blinking, wandering gaze) and returns to idle when an emotion's hold time ends.

```bash
scripts/start_display.sh          # server + Chromium kiosk on http://localhost:8765 (falls back to chromium-browser, firefox)
python main.py display            # server only; then open http://localhost:8765 in any browser (--port 9000 to change)
```

Control it by typing in the terminal running the server: an emotion with optional hold seconds (`happy`, `sad 5`), `idle`, `talk [seconds]`, `list`, `quit`. Or over HTTP:

```bash
curl -X POST localhost:8765/emotion -H 'Content-Type: application/json' -d '{"emotion":"happy","hold_s":3}'
curl 'localhost:8765/emotion?name=surprised&hold=5'
curl 'localhost:8765/talk?seconds=3'              # move the mouth for 3 s; seconds=0 stops
```

### Talking mouth

With the display running, `chat` moves the mouth whenever the robot replies (start the display first, in another terminal):

```bash
python main.py chat --tts --prompt "Hello, I am the library robot."   # mouth follows the speech audio
python main.py chat --talk --speaker USB                             # type, hear each reply on the USB speaker
python main.py chat --chat                                           # every spoken reply
python main.py chat                                                  # text only: mouth moves for the reply's reading time
```

In `--tts`, `--talk` and `--chat` the mouth follows the loudness of the TTS audio: chat computes a 30 fps envelope from the WAV and sends it to the page (`POST /talk`) as `paplay` starts. If the mouth runs ahead of or behind the sound on the USB speaker, tune `--lip-delay` (seconds the mouth waits, default 0.1). `--face-port` picks the display's port, `--no-face` turns this off; chat runs normally when no display is running.

Emotions: happy, sad, angry, surprise, fear, disgust, neutral (aliases such as `scared`, `mad`, `joy`, `surprised` work, so the emotion model's labels can be passed straight in). From Python, `modules.expression.set_emotion("happy", hold_s=3.0)` does the same POST; emotions are not wired into `chat` yet. Add `&talk=0.7` to the frozen-face URL below to see the mouth open. Open `ui/index.html?emotion=happy` to view one frozen face without a server. If the Jetson drops frames, add `?glow=cheap` (no blur filter) or `?glow=off` to the URL.

## Layout

```
main.py              starts the robot: python main.py <feature> [options]
modules/             building blocks, each talks to one service
  llm.py             server protocol + request(), LLM client, sentence splitter; also the server's LLM script
  face.py            server team's face client (detect/recognize/enroll one image), kept unmodified
  emotion.py         server team's emotion client (one image), kept unmodified
  stt.py, tts.py     server team's speech clients (one WAV / one text), kept unmodified
  expression.py      emotion names, aliases and set_emotion() for the display
features/            what the robot does, built from modules
  chat.py            text or voice chat: --llm, --stt, --tts, --chat
  camera.py          live webcam detection, recognition, emotion, enrollment
  display.py         robot face server: serves ui/, pushes emotions over SSE, terminal control
ui/index.html        the face page (canvas, no libraries)
scripts/             start_display.sh launches the server and a kiosk browser
tests/               splitter and display server tests
```

New services go in `modules/`; new behaviours go in `features/` with a `main(argv)` and its name in `FEATURES` in `main.py`.

## Next

1. Drive the face's emotion from `chat` (LLM emotion tags blended with the detected user emotion).
2. Text-to-speech, then the scan, context and style pipeline.
