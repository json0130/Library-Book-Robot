# Shot list: Stuart's 3-minute video (HRI 2027 Student Design Competition)

The story, from the paper: **Stuart changes how it communicates by zone, not what it does.** Common area: it greets, speaks aloud, and the
screen shows only its face. Quiet zone: it never starts, answers as text on the screen, and makes no sound.

The cut is `video/storyboard.yaml` (176.8 s now). `python tools/video/edit.py --plan` prints the timing and every shot that is still a
placeholder (a "Shot needed" card of the planned length). Drop a shot's file in place, render again, and nothing else changes.

## The cut, in order

| Time | Segment | What it shows | Source | State |
|---|---|---|---|---|
| 0:00 | hook | a quiet library, then a lively one; narration explains | Wikimedia Commons photos, credited on the frame | done |
| 0:13 | title | "Stuart" | drawn | done |
| 0:17 | robot | "So we present Stuart, the library robot ...": concept (AI-generated), CAD model, CAD render, prototype | the paper's figures | done |
| 0:27 | styles | the common-area and quiet-zone interaction | Fig. 6 of the paper, cropped | done |
| 0:34 | system, parts | system diagram; the prototype photo with its parts named; the RFID reader photo | drawn + your photos | done |
| 0:52 | maps | the map Stuart navigates on, and the sound map | the two images from the paper (Fig. 3) | done |
| 1:04 | common area | the live operator view: the webcam with the face found, Stuart's face, the state machine; a person walks up, Stuart greets, the request is typed, the RFID reader reads the book, Stuart confirms (the waits are sped up) | **your screen recording of 19 Nov** + the robot's session log | done |
| 1:44 | loading | the book goes into the robot | your CAD simulation, played in reverse, normal speed (10.4 s) | done (a real shot can replace it) |
| 1:55 | trip to Fiction | Stuart drives to the Fiction section (phone) next to the floor plan, where the dot is where the robot was | your 18 Nov clip + the replay of that drive's real track | done |
| 2:13 | unload | the books go into the return tray | your clip, later | placeholder |
| 2:18 | trip back | back to the station, same two views (the clip is the last 26 s of that drive) | your 18 Nov clip + the real track | done |
| 2:27 | quiet zone | another person asks questions (no return): the robot's face with the answers as text, no sound | simulated visit; **the webcam half is a placeholder** | see below |
| 2:53 | end | "Stuart. A library robot that reads the room." and "CARES Lab, University of Auckland" | drawn | done |

What is simulated or added, and says so on the frame (paper, section 5):
- The common-area scene is a real run. The visitor's words are **typed by the operator** (speech recognition is not wired in): badge.
- **Stuart's voice in that scene is the TTS**, made again from the log's text and placed where the log says he finished each line: the screen
  recording has no sound. The words and timing are the robot's, the voice is not a recording of that run.
- The maps beside the drive clips replay the robot's real tracks from its session log on the real floor plan (a simulated robot following
  recorded poses; nothing drives). The CAD clip says "CAD simulation"; the drives started by an operator say so.
- The quiet-zone visit is a simulated robot (fake base, visitor's lines typed).

## What is left to film

### 1. The quiet zone, like the common one (you, at the Jetson)
Record the live operator page the same way you did for the common area, with `--zone quiet`: someone walks up, you type what they ask
(`tell me about Dune`, `where is the science section?`, `thanks, that's all`), nothing is returned, no sound. Steps:
```
python -m display.server                                  # the robot's page on 8765, as for the first recording
python -m robot sim --zone quiet --camera --faces --local-faces --config-set returning.auto_start=false    # no --speak: it stays silent
# open http://localhost:8765/operator, screen-record it (Ctrl+Alt+Shift+R in GNOME), do the visit, stop the recording
python tools/video/session_to_take.py --log data/logs/session-<the run>.jsonl --video "<the screencast>.mp4" --start HH:MM:SS.ss --take quiet4
```
`--start` is the time of the recording's first frame: the file name's time plus about half a second (a GNOME screencast starts 0.5 s after
its name; check it against a state change you can see in both). Then in `video/storyboard.yaml` replace the `quiet` segment by a `clip` of
`take: quiet4, source: screen` with the same `crop: [84, 116, 1820, 930]` and `valign: bottom` as the live ones, `zone: quiet`,
`auto_captions: true` (the answers are captioned "(shown on screen, no sound)"). If the recording is a different screen size the crop changes.
- **Retake if:** any sound from the robot, the face box flickers off the person, someone else walks through, an answer is cut off.

### 2. The unload (later) - `video/footage/unload.mp4`, about 5 s
At the Fiction return tray, the books go in (by hand: the lift is not built; the cut marks it "Operator-driven").

### 3. Close-ups of the parts (optional) - `video/assets/parts/lidar.jpg`, `screen.jpg`
The RFID reader photo (`rfid_reader.jpg`, the RC522 on its Arduino) is in. Then `python tools/video/make_assets.py`.

## The voiceover
You record it; the TTS narration stays as the backup.
```
python tools/video/voiceover.py cues --write      # the script, with the time each line starts and how long it has -> video/voiceover.md
python tools/video/voiceover.py record            # reads each line to you: Enter starts, Enter stops, r records again
python tools/video/edit.py --preview              # hear it in the cut
```
Each line is saved as `video/voiceover/<segment id>.wav`; a line you recorded replaces the TTS for that line, and a line you have not recorded
keeps the TTS. (Record elsewhere if you like: save each line under that name, any of .wav .m4a .mp3 .flac .ogg, or the whole thing as one
`video/voiceover.wav` from 0:00.) The render warns if a line is longer than the time it has. To go back to the TTS for a line, rename its file.
The TTS lines are kept in `video/voiceover_tts/`, and the first version of the whole video with the TTS narration is
`video/out/backup/stuart_hri2027_sdc_v1_tts_narration.mp4`. Leave Stuart's own speech in the common-area scene alone: the narration is
silent there.

## Already made, and how to redo it
- **The common-area scene**: `python tools/video/session_to_take.py --log ~/Library-Book-Robot/data/logs/session-20261119-171941.jsonl --video
  video/footage/rfid_return_screencast.mp4 --start 17:19:57.55 --take rfid1 --voice` (the local AI must be up for `--voice`). It writes
  `recordings/rfid1/` (events, the video, Stuart's voice) from the robot's log and the screen recording; the editor crops it to the operator
  page and speeds up the waits.
- **The replayed drives**: `python tools/video/trajectory.py <log> --window 18:27:35-18:28:26 --name to_fiction --window 18:28:38-18:29:35
  --name back_to_station -o video/takes/trips_18nov.json` makes the tracks from the 18 Nov session log (`data/logs/session-20261118-182712.jsonl`
  in the dell-link worktree); `python tools/video/sim_take.py nav --take nav2 --zone quiet --sources map --no-speak --no-llm --replay
  video/takes/trips_18nov.json` records the map page while a simulated base replays them. The phone clips start at 18:27:36 and 18:29:06.
- **The simulated quiet visit**: `python tools/video/sim_take.py quiet --take quiet3 --zone quiet --no-llm` (`video/takes/quiet.txt`).
- **The CAD clip**: `video/footage/cad_book_retrieve_original.webm` is your screen recording; `cad_book_return.mp4` is its last 10.4 s (the lift
  taking the book out), cropped to the 3D view and reversed so the book goes in.
- The services these use (a separate display page on 8775, the local AI on 7898) are the commands in `docs/local_ai.md` and `python -m
  display.server --port 8775 --host 127.0.0.1`.

## Credits and licences
Hook photos from Wikimedia Commons: Suzzallo Reading Room, University of Washington, by Guywelch2000 (CC BY 4.0); Seattle Central
Library, by brewbooks (CC BY-SA 2.0). Each carries its credit on the frame; `video/assets/photos/credits.json` has the file pages. The end card
shows only the lab, as asked. CC BY-SA asks for derivatives to be shared alike: a still photo inside a video is normally a collection, but
check this with the competition rules, or swap that photo for a public-domain or your own one.

## Rendering
```
python tools/video/edit.py --plan          # timing, and every shot still missing
python tools/video/edit.py --preview       # 540p check, about 2 minutes
python tools/video/edit.py                 # 1080p final -> video/out/stuart_hri2027_sdc.mp4 (fails above 180 s)
```

## What the editor checks for you
- Every word Stuart says is captioned (each sentence as it starts); what the visitor types is captioned "Visitor: ...", and a quiet-zone answer
  is captioned "(shown on screen, no sound)".
- A recorded voiceover line longer than its time is reported; a line never talks over the one before it.
- The dB meter is drawn only from levels the real base measured: a take from a fake base gets none.
- A storyboard over 180 s is refused before anything is rendered.
