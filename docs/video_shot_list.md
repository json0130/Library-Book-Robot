# Shot list: Stuart's 3-minute video (HRI 2027 Student Design Competition)

The story, from the paper: **Stuart changes how it communicates by zone, not what it does.** Common area: it greets, speaks aloud, and the
screen shows only its face. Quiet zone: it never starts, answers as text on the screen, and makes no sound.

The cut is `video/storyboard.yaml` (176.8 s now; the limit is 180). `python tools/video/edit.py --plan` prints the timing and every shot that is still a
placeholder (a "Shot needed" card of the planned length). Drop a shot's file in place, render again, and nothing else changes.

## The cut, in order

| Time | Segment | What it shows | Source | State |
|---|---|---|---|---|
| 0:00 | hook | a quiet library, then a lively one; narration explains | Wikimedia Commons photos, credited on the frame | done |
| 0:13 | title | "Stuart" | drawn | done |
| 0:17 | robot | "So we present Stuart, the library robot ...": the AI-generated concept image, the CAD model of the prototype, the prototype photo | `video/assets/paper/concept_ai.png`, `cad_prototype.png` (yours) + the paper's prototype photo | done |
| 0:27 | styles | the common-area and quiet-zone interaction | Fig. 6 of the paper, cropped | done |
| 0:34 | system, parts | system diagram; the prototype photo with its parts named, and three close-ups: the LiDAR, the face screen, the RFID reader | drawn + your photos | done |
| 0:52 | maps | the map Stuart navigates on, and the sound map | the two images from the paper (Fig. 3) | done |
| 1:02 | common area | the live operator view (the webcam with the face found, Stuart's face, the state machine): a person walks up, Stuart greets and offers, the visitor asks about Dune (answered), then asks to return a book, the RFID reader reads Clean Code, Stuart confirms, and the visitor says goodbye. **With the real voices**: Stuart's speaker and the microphone, as heard. **When the book is read, a pop-up in the top right corner, "Loading the book (CAD simulation)", shows the whole CAD clip in reverse (x4, 9.4 s).** Waits with nobody speaking are sped up; every stretch with a voice plays at normal speed | **your screen recording of 20 Nov, 11:31** (`video/footage/common3_screencast.mp4`) + the robot's session log + the sound recorded by `film.py` (`recordings/rfid3`) | done |
| 1:59 | trip to Fiction | Stuart drives to the Fiction section (phone) next to the floor plan, where the dot is where the robot was (x5) | your 18 Nov clip + the replay of that drive's real track | done |
| 2:20 | trip back | back to the station, same two views (the clip is the last 26 s of that drive, x5) | your 18 Nov clip + the real track | done |
| 2:28 | quiet zone | another person walks up; the face screen shows the greeting and the answers as text, nothing is said aloud: asks about Dune, asks to be shown the way, says thanks. Slow stretches are sped up | **your screen recording of 19 Nov, 21:06** (`video/footage/quiet2_screencast.mp4`) + the robot's session log | done |
| 2:51 | end | "Stuart. A library robot that reads the room." and "CARES Lab, University of Auckland" | drawn | done |
| 2:55 | thanks | "Thank you" | drawn | done |

What is simulated or added, and says so on the frame (paper, section 5):
- The common-area scene is a real run, with its **real sound**: Stuart's speaker (recorded from the sound card's monitor) and the microphone
  (the visitor's voice, brought down 10 dB and turned down whenever Stuart speaks so his voice is not doubled by the room). Nothing is
  synthesised there. The visitor's words were spoken; in the quiet scene they were **typed by the operator**, and the badge says so.
  Caveats: the microphone clips on the visitor's loudest words (the input was too hot), and a line's caption follows when it was spoken, not
  when the robot recognised it (the log is seconds later).
- The quiet scene is silent, as the robot was.
- The maps beside the drive clips replay the robot's real tracks from its session log on the real floor plan (a simulated robot following
  recorded poses; nothing drives). The CAD clip says "CAD simulation"; the drives started by an operator say so.

## What is left to film

### 1. The unload (later, optional)
Left out of the cut. To add it back: a `clip` segment `unload` between `trip-out` and `trip-back` (`src: video/footage/unload.mp4`, about 5 s,
`operator_driven: true`) and a line in the narration; the cut then needs 5 s from somewhere else.

### Close-ups of the parts: done
`video/assets/parts/` has `lidar.jpg`, `screen.jpg` and `rfid_reader.jpg` (the originals are in `video/footage/originals/`). To change one, replace the file and run `python tools/video/make_assets.py`.

### If the common area is filmed again, with the real voices
The screen recording has no sound and the robot does not keep what its microphone hears, so record the sound beside it, in a second terminal,
started just before the run:
```
python tools/video/film.py check                                      # the two lines "voice would be recorded from": Stuart's speaker and the microphone
python tools/video/film.py record --take rfid3 --sources audio,mic    # --mic MV_USB if the robot listens with that microphone (devices.mic)
#   ... start the screen recording, run the robot, do the visit, stop the screen recording ...
#   press Enter in that terminal: it says whether each of the two heard anything (a silent one is flagged)
python tools/video/session_to_take.py --log data/logs/session-....jsonl --video <the screencast> --start HH:MM:SS.55 --take rfid3 --sound-take rfid3
```
Rehearse first: `film.py record --take test --sources audio,mic --duration 5` while you speak, then play `recordings/test/mic.wav`. Use the same
microphone as the robot (`--mic`): the default input of this Jetson is the analog jack, and it heard nothing when tested. If the voice is a
fraction of a second off the picture, add `--sound-offset 0.3` (positive = the sound later). With `--sound-take` nothing is synthesised: the
voice in the video is yours and the robot's own speaker. Then point the `live-*` segments at the new take.

#### The pop-up
The pop-up comes with it: in `video/storyboard.yaml` the `live-scan` segment has an `inset:` (the CAD clip, `at:` = seconds into the segment when it appears, `speed`, `width`, `label`, `top:` to move it down). Point `in:`/`out:` of the segment at the new take (`take: ..., source: screen`) and set `at:` to when the reader has read the book (`python tools/video/session_to_take.py` prints the `rfid_read` time).

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
- **The common-area scene**: `python tools/video/film.py record --take rfid3 --sources audio,mic --mic MV_USB` during the run, then
  `python tools/video/session_to_take.py --log ~/Library-Book-Robot/data/logs/session-20261120-113145.jsonl --video video/footage/common3_screencast.mp4
  --start 11:31:39.55 --take live3 --sound-take rfid3 --visitor-starts 29.7,63.2,84.4` (the three numbers are when the visitor began each line, in
  seconds of the picture, from listening to the microphone file: the log only knows when the words were recognised). It writes `recordings/live3/`
  (events, the video, the sound); the editor crops it to the operator page. The 19 Nov take (`rfid2`, with a synthetic voice) is kept.
- **The quiet scene**: `python tools/video/session_to_take.py --log ~/Library-Book-Robot/data/logs/session-20261119-210607.jsonl --video
  video/footage/quiet2_screencast.mp4 --start 21:06:15.55 --take quiet4` (no `--voice`: the robot said nothing). The start time is the file name's
  time plus 0.55 s (a GNOME screencast begins that long after its name; checked against the first state change in the picture).
- **The replayed drives**: `python tools/video/trajectory.py <log> --window 18:27:35-18:28:26 --name to_fiction --window 18:28:38-18:29:35
  --name back_to_station -o video/takes/trips_18nov.json` makes the tracks from the 18 Nov session log (`data/logs/session-20261118-182712.jsonl`
  in the dell-link worktree); `python tools/video/sim_take.py nav --take nav2 --zone quiet --sources map --no-speak --no-llm --replay
  video/takes/trips_18nov.json` records the map page while a simulated base replays them. The phone clips start at 18:27:36 and 18:29:06.
- **The CAD clip**: `video/footage/cad_book_retrieve_original.webm` is your screen recording (37.6 s: the lift goes to the slot, pushes the book out, lifts it
  to the top). `python tools/video/reverse.py video/footage/cad_book_retrieve_original.webm video/footage/cad_book_return_full.mp4 --crop 1060:924:640:222 --width 800`
  cuts it to the 3D view and plays all of it backwards (in pieces, to spare memory): the book comes down from the top, is pushed into the slot and the
  lift goes home. The first, shorter version (`cad_book_return.mp4`, only the last 10.4 s) is kept.
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
