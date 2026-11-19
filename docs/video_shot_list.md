# Shot list: Stuart's 3-minute video (HRI 2027 Student Design Competition)

The story, from the paper: **Stuart changes how it communicates by zone, not what it does.** Common area: it greets, speaks aloud, and the
screen shows only its face. Quiet zone: it never starts, answers as text on the screen, and makes no sound.

The cut is `video/storyboard.yaml` (173.5 s now). `python tools/video/edit.py --plan` prints the timing and every shot that is still a
placeholder (a "Shot needed" card of the planned length). Drop a shot's file in place, render again, and nothing else changes.

## The cut, in order

| Time | Segment | What it shows | Source | State |
|---|---|---|---|---|
| 0:00 | hook | a quiet library, then a lively one; narration explains | Wikimedia Commons photos (credited on frame) | done |
| 0:15 | title | "Stuart" | drawn | done |
| 0:19 | robot | concept (AI-generated), CAD model, CAD render, prototype | the paper's figures | done |
| 0:29 | styles | the common-area and quiet-zone interaction | Fig. 6 of the paper, cropped | done |
| 0:37 | system, parts | system diagram; the prototype photo with its parts named | drawn + your photo | done; RFID reader photo missing |
| 0:55 | maps | the map Stuart navigates on, and the sound map | the two images from the paper (Fig. 3) | done |
| 1:08 | common area | a person approaches (webcam with the face found + the robot's face), asks to return a book | webcam **to film**; face screen simulated | webcam = placeholder |
| 1:24 | common area | the webcam view swaps to the RFID reader scanning the book; Stuart answers aloud | RFID clip **from you**; face screen simulated | RFID = placeholder |
| 1:38 | loading | the book goes into the robot | your CAD simulation, played in reverse, x2 | done (a real shot can replace it) |
| 1:43 | trip to Fiction | Stuart drives to the Fiction section (phone) + the map | your 18 Nov clip + a simulated map | done |
| 2:03 | unload | the books go into the return tray | your clip, later | placeholder |
| 2:08 | trip back | back to the station, same two views | your 18 Nov clip + a simulated map | done |
| 2:17 | quiet zone | another person asks questions (no return): webcam, then the robot's face with the answers as text, no sound | webcam **to film**; face screen simulated | webcam = placeholder |
| 2:48 | end | title, the team, the photo credits | drawn | done |

What is simulated, and says so on the frame (paper, section 5): the face screens come from the robot program run on a fake base, with the
visitor's lines typed (speech recognition is not wired in); the maps next to the drive clips show a simulated robot on the real floor plan
and the real sound map. The CAD clip says "CAD simulation"; the drive started by an operator says so.

## What is left to film

### 1. The two webcam takes (you and I, at the Jetson)
These replace the "Shot needed" cards on the left at 1:08 and 2:17: the robot's own webcam, with the person's face boxed. Nobody is saved
but the take you choose to keep (`recordings/` is not in git); the paper says no images are stored, so delete a take you do not use.

Before filming (once), three terminals on the Jetson (a separate display page on 8775, so a running robot's page on 8765 is never touched):
```
python -m display.server --port 8775 --host 127.0.0.1
cd ~/lbr-local-ai && scripts/start_local_ai.sh          # the local AI (LLM, voice); or the commands in docs/local_ai.md
python tools/get_face_models.py                          # only if models/face/ is empty: the face numbers ("person 1")
```
**Common area** (someone walks up to the robot, Stuart greets, the person says `hi Stuart, can I return this book?` which you type,
then holds the printed Cosmos barcode `data/test_barcodes/9780345539434-cosmos.png` up to the webcam, which plays the RFID scan; Stuart
confirms; the person says `no thanks, that's all` and walks away):
```
python tools/video/film.py record --take common8 --sources face,audio --port 8775
AI_HOST=127.0.0.1 AI_PORT=7898 python -m robot sim --base fake --zone common --film common8 --camera --faces --local-faces \
    --record recordings/common8/camera.mp4 --speak --no-persist --no-log \
    --config-set display.port=8775 --config-set styles.common_alone.screen_content=false --config-set styles.common_group.screen_content=false
```
**Quiet zone** (someone walks up, Stuart waits and then shows text; they ask `tell me about Dune`, `where is the science section?`, then
`thanks, that's all`; they never return a book; no sound at all):
```
python tools/video/film.py record --take quiet4 --sources face,audio --port 8775
AI_HOST=127.0.0.1 AI_PORT=7898 python -m robot sim --base fake --zone quiet --film quiet4 --camera --faces --local-faces \
    --record recordings/quiet4/camera.mp4 --no-persist --no-log --config-set display.port=8775
```
Type each line into the sim terminal after the person says it. Enter in the film.py terminal stops the recording, `quit` stops the sim.
Then in `video/storyboard.yaml` change `common7` to `common8` and `quiet3` to `quiet4` (the face and the voice come from the same take).
- **Retake if:** the box flickers off the person, the face screen is hidden, someone else walks through the shot, the barcode takes more
  than 5 s to read, or any sound comes from the robot in the quiet take (its audio.wav must be silent).

### 2. The RFID clip (from you) - `video/footage/rfid_scan.mp4`
The book on the RFID reader, with the UI showing which book was scanned; about 13 s, filmed with the phone or a screen recording. Set
`in:`/`out:` in the storyboard if it is longer. The line in the narration that mentions the reader is already there.

### 3. Photos of the parts (optional, from you) - `video/assets/parts/`
`rfid_reader.jpg` is needed (its tile says "photo needed"); `lidar.jpg` and `screen.jpg` add close-ups. Then `python tools/video/make_assets.py`.

### 4. The unload (later) - `video/footage/unload.mp4`, about 5 s
At the Fiction return tray, the books go in (by hand: the lift is not built; the cut marks it "Operator-driven"). The loading shot can be
replaced the same way: put a real clip at `video/footage/cad_book_return.mp4`.

## Already made, and how to redo it
- **Simulated visits** (the face screens): `python tools/video/sim_take.py common --take common7 --zone common --no-llm` and
  `... quiet --take quiet3 --zone quiet --no-llm`; the visits are `video/takes/common.txt` and `quiet.txt`. `--no-llm` words the answers from
  the catalogue: the local Qwen3 once described A Brief History of Time with Cosmos's summary.
- **Simulated map** (`recordings/nav1`): `python tools/video/sim_take.py nav --take nav1 --zone quiet --sources map --no-speak --no-llm`.
  The simulated robot drives to the taught Fiction shelf and back on the real floor plan and sound map. Nothing drives.
- **CAD clip**: `video/footage/cad_book_retrieve_original.webm` is your screen recording; `cad_book_return.mp4` is its last 10.4 s (the lift
  taking the book out), cropped to the 3D view (no browser tabs) and reversed so the book goes in.

## Credits and licences
Hook photos from Wikimedia Commons: Suzzallo Reading Room, University of Washington, by Guywelch2000 (CC BY 4.0); Seattle Central
Library, by brewbooks (CC BY-SA 2.0). Each carries its credit on the frame and in the end card; `video/assets/photos/credits.json` has the
file pages. CC BY-SA asks for derivatives to be shared alike: a still photo inside a video is normally a collection, but check this with
the competition rules, or swap that photo for a public-domain or your own one.

## Rendering
```
python tools/video/edit.py --plan          # timing, and every shot still missing
python tools/video/edit.py --preview       # 540p check, about 2 minutes
python tools/video/edit.py                 # 1080p final -> video/out/stuart_hri2027_sdc.mp4 (fails above 180 s)
```
The voiceover: record `video/voiceover.md` as `video/voiceover.wav` (48 kHz, from 0:00). Until then the render uses the local TTS for each
line (the AI server's if `AI_HOST` points at it), or silence when no voice is reachable.

## What the editor checks for you
- Every word Stuart says is captioned (each sentence as it starts playing); what the visitor types is captioned "Visitor: ...", and a
  quiet-zone answer is captioned "(shown on screen, no sound)".
- The dB meter is drawn only from levels the real base measured: a take from a fake base gets none.
- A storyboard over 180 s is refused before anything is rendered.
