# Shot list: Stuart's 3-minute video (HRI 2027 Student Design Competition)

The story, from the paper: **Stuart changes how it communicates by zone, not what it does.** Common area: it greets, speaks aloud, and the
screen shows only its face. Quiet zone: it never starts, answers as text on the screen, and makes no sound. The same return task in both.

The cut is `video/storyboard.yaml`; `python tools/video/edit.py --plan` prints the timing and every shot that is still a placeholder (a
"Shot needed" card of the planned length). Drop a shot's file in place and render again: nothing else changes.

## Draft status (19 Nov) and what is left to film

`video/out/stuart_hri2027_sdc.mp4` (1080p, 2:58, 17 MB) renders now. Done: the title, the system diagram, the common-area and the
quiet-zone visits (simulated, screen-recorded: see below), the two trip clips from 18 Nov, and the closing stills
(`video/assets/still_common.png`, `still_quiet.png`, frames of those takes). Every other part is a "Shot needed" card of the
right length. To fill one, put the file at the path below and run `python tools/video/edit.py` again: nothing else changes.

| In the cut | Length | File to add | What it shows |
|---|---|---|---|
| 0:00 | 5 s | `video/footage/hook_quiet.mp4` | quiet study area, students working, slow pan |
| 0:05 | 5 s | `video/footage/hook_lively.mp4` | lively area, people talking and walking |
| 0:26 left | 19 s (76 s at x4) | `video/footage/survey_drive.mp4` | the robot driving its sound survey (phone, landscape) |
| 0:26 right | 19 s | `recordings/survey1/map.mp4` | the live map + sound map during that drive (`film.py record --take survey1 --sources map`) |
| 0:45 | 5 s | `video/assets/sound_map.png` | the finished sound map, quiet and common areas visible |
| 0:53 left | 52.5 s | `recordings/common1/camera.mp4` (or set `src:` to your clip) | the patron at the robot in the common area: walks up, RFID scan, the screen showing the book |
| 1:45 left | 34 s | `recordings/quiet1/camera.mp4` (or `src:`) | the same in the quiet zone, bystanders studying |
| 2:19 right | 19 s | `recordings/trip1/map.mp4` | the live map while it drives to Fiction |
| 2:38 | 4 s | `video/footage/unload.mp4` | books into the return tray (operator-driven; can wait) |

Your own phone clips (RFID scan, the patron) can go straight into the storyboard with `src: video/footage/<file>.mp4` and an `in:`/`out:`
in seconds: they do not need a take.

### The simulated visits (the right-hand panes at 0:53 and 1:45)

Made on the Jetson with the robot program on the fake base (nothing drives), its face page recorded, and Stuart's voice from its own
speaker, using the local AI (`scripts/start_local_ai.sh` in the local-ai worktree, or llama-server + `tools/local_ai_server.py`):

```
python -m display.server --port 8775 --host 127.0.0.1            # a separate page, so a running robot's 8765 is untouched
AI_HOST=127.0.0.1 AI_PORT=7898 python tools/video/sim_take.py common --take common6 --zone common --no-llm
AI_HOST=127.0.0.1 AI_PORT=7898 python tools/video/sim_take.py quiet --take quiet2 --zone quiet --no-llm
```

The visit is `video/takes/common.txt` / `quiet.txt`. `--no-llm` words the recommendation from the catalogue: the local Qwen3-1.7B
described A Brief History of Time with Cosmos's summary in three takes out of three (`common2`, `common5` keep that version), which the
paper's "honest answers" rules out. In the common area the takes use `screen_content: false` (only the face, as the paper describes it).

## Before filming (once)

- On the Jetson: `python tools/video/film.py check`. Everything should say yes/ok. Each browser source needs about 800 MB free: close other
  browsers first (film.py refuses rather than let the Jetson run out of memory).
- **Clocks.** Every log uses `time.time()`. On the Jetson nothing to do. Anything recorded on the Dell: `timedatectl` there must say
  `System clock synchronized: yes`.
- **Simulated vs real.** The interaction shots (4, 5) run the robot program with `--base fake`: nothing can drive. The driving shots (3, 6)
  need the real base: start that stack yourself, only with the area clear and someone at the power switch.
- **Disclosures** (paper, section 5): the book is read by the barcode camera, not RFID (the reader is not fitted yet), so shots 4 and 5 carry
  the badge "Book scan simulated (RFID reader not fitted yet)"; the unload is done by hand, so shot 7 carries "Operator-driven".
- **Props:** printed "Quiet Zone" and "Common Area" signs (A4, readable on camera); the RFID-tagged books, at least Cosmos, with the
  printed barcode `data/test_barcodes/9780345539434-cosmos.png` taped on (the camera reads it); a return tray labelled "Fiction";
  2-3 seated bystanders with laptops and books; a phone sound-meter app (in frame in shot 5); a phone on a small tripod, landscape.

## Filming order

Grouped by place, so each location is set up once. Times are where the shot sits in the cut.

### Shot 1: hook, quiet study area (0:00, 5 s) - `video/footage/hook_quiet.mp4`
- **Where / who:** a quiet study area with the "Quiet Zone" sign; 2-3 students working; no robot needed.
- **Camera:** phone, landscape, slow pan or still, 8-10 s.
- **Must happen:** nobody looks at the camera; silence.
- **Retake if:** shaky, someone walks out of frame mid-shot, faces of non-consenting people are recognisable.

### Shot 5: quiet zone, same task (1:40, 34 s) - `recordings/quiet1/`
- **Where / who:** same quiet area, sign in frame. The patron (one team member) and the 2-3 bystanders studying behind. The phone sound
  meter on the desk next to Stuart, facing the camera.
- **Robot (Jetson, no driving):** three terminals, in this order:
  ```
  python -m display.server
  python tools/video/film.py record --take quiet1 --sources face,audio
  python -m robot sim --base fake --zone quiet --film quiet1 --camera --faces --local-faces --record recordings/quiet1/camera.mp4 --llm
  ```
  (add `--speak` too: in the quiet zone Stuart must stay silent anyway, and the empty audio.wav proves it.)
- **Must happen, in order:** Stuart idles (no greeting while the patron is far). The patron walks up and faces it: the eyes look up, the
  screen shows the hello as text, **no sound**. The patron types or says quietly: `can I return this book?`; then in the sim: `book` and
  holds the Cosmos barcode to the camera; then `dropped`; the screen shows the card with "Returned" and the question. The patron: `yes,
  something similar`; the screen lists books with one reason each. The patron leaves. Then Enter in the film.py terminal, `quit` in the sim.
- **Retake if:** any sound from the robot, a bystander looks up at it, the barcode takes more than 5 s to read, the text is cut off.

### Shot 2: hook, lively area (0:05, 5 s) - `video/footage/hook_lively.mp4`
- **Where / who:** a lively area (foyer, group study) with the "Common Area" sign; people talking, walking past.
- **Camera / retake:** as shot 1; retake if it looks staged or empty.

### Shot 4: common area (1:00, 40 s) - `recordings/common1/`
- **Where / who:** the lively area, "Common Area" sign in frame; the patron; people passing in the background.
- **Robot (Jetson, no driving):**
  ```
  python -m display.server
  python tools/video/film.py record --take common1 --sources face,audio
  python -m robot sim --base fake --zone common --film common1 --camera --faces --local-faces --record recordings/common1/camera.mp4 --speak --llm
  ```
  `--speak` needs the AI server's TTS (`python -m robot check`); `audio` records Stuart's speaker output clean.
- **Must happen:** the patron walks up; Stuart greets aloud ("Hello! My name is Stuart."). `can I return this book?`, `book` + Cosmos
  barcode, `dropped`; Stuart says it is returned and offers something similar; `yes`; Stuart names the related books aloud. The screen shows
  only the face (with expressions). The patron says thanks and leaves.
- **Retake if:** the greeting is cut off, the LLM is slow (more than 3 s of silence), the person box flickers off the patron, the patron
  blocks the face screen.

### Shot 3: learning the zones (0:30, 24 s at x4 = 96 s of driving) - `video/footage/survey_drive.mp4` + `recordings/survey1/map.mp4`
- **DRIVES the real robot.** Area clear, someone at the power switch. Start the robot link and the sound survey yourself (docs/sound_map.md).
- **On the Jetson**, with the robot program running against the real base (`python -m robot sim --base turtlebot --film survey1`) and the
  display server up: `python tools/video/film.py record --take survey1 --sources map`
- **Phone:** follow the robot from 2 m, landscape, for the whole drive (at least 100 s).
- **Must happen:** the map shows the robot moving; the sound map fills in along its path. Then `python tools/noise_map.py ...` and take
  `video/assets/sound_map.png` (shot 3b) of the finished sound map with the Quiet zone / Common area labels.
- **Retake if:** the robot loses localisation, the map page is not updating, the phone loses the robot.

### Shot 6: returning trip (2:14, 22 s at x2) - `video/footage/trip_to_fiction.mp4` (filmed 2026-11-18) + `recordings/trip1/map.mp4`
- The phone clips of the drive to Fiction and back already exist. Still needed: the live map of a trip.
- **DRIVES the real robot:** `python -m robot sim --base turtlebot --film trip1`, `python tools/video/film.py record --take trip1 --sources map`,
  then the trip to the Fiction shelf (`python tools/goto_test.py shelf 1-FIC-A`, which asks `yes`). Film the robot with the phone too, so the
  two halves match; then replace `trip_to_fiction.mp4` with the new phone clip.
- **Retake if:** the robot stops for an obstacle or the map loses it.

### Shot 7: unload (2:36, 6 s) - `video/footage/unload.mp4`
- **Where / who:** the Fiction shelf with the return tray; the robot parked there; a team member as the librarian.
- **Must happen:** the books go from the robot into the tray (by hand: the lift is not built), then "the librarian" picks them up.
  The cut marks it "Operator-driven".

### Stills for the close (2:51, 8 s) - `video/assets/still_common.png`, `video/assets/still_quiet.png`
- After shots 4 and 5 are in: `python tools/video/edit.py --preview --stills <s>,<s>` at a moment of each (the face speaking; the text on the
  screen), and copy the two PNGs to those names. Or render the full video and use `--stills`.

## Rendering

```
python tools/video/edit.py --plan          # timing; lists every shot still missing
python tools/video/edit.py --preview       # 540p check, about 1 minute
python tools/video/edit.py                 # 1080p final -> video/out/stuart_hri2027_sdc.mp4 (fails above 180 s)
```

The voiceover: record `video/voiceover.md` as `video/voiceover.wav`. Until then the render uses the AI server's TTS for each line, or
silence when the server is off.

## What the editor checks for you
- Every word Stuart says is captioned from the take's `reply` events; quiet-zone answers are captioned "(shown on screen, no sound)".
- The dB meter is drawn only from levels the real base measured: a take from `--base fake` gets none (its levels are made up).
- Over 180 s is refused before anything is rendered.
