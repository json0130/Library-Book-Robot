# Talking to the robot (speech input)

Press **🎤 Talk** on the operator page (or type `listen` in the simulator), say one sentence, and pause. The robot records from its
microphone, sends the recording to the AI server's speech-to-text, and handles the words exactly like typed text:
`UserUtterance(text, source="mic")`, then the intent router and the dialogue as usual. Press the button again (now **⏹ Done**) to
end the recording early. It is push-to-talk on purpose: the robot never listens while it speaks, so it cannot answer itself.

Needs the real speaker driver and the AI server: `python -m robot sim --speak` or `--live` (the plain simulator has no microphone and
the Talk button is greyed out).

## Where the code is

| | |
|---|---|
| `robot/perception/listener.py` | `capture()` cuts a sentence out of the audio; `Listener.listen()` records, transcribes and publishes |
| `robot/drivers/audio.py` `Audio.stream()` | raw 16 kHz mono PCM from PulseAudio (`parecord`) |
| `robot/services/speech.py` `Speech.transcribe()` | the AI server's STT (vendor/ai_server/stt.py protocol) |
| `robot/operator.py` command `listen` | the same command from the page and the terminal; it does not block the page's other buttons |
| `display/ui/operator.html` | the Talk button and the line under it: listening, hearing, transcribing, what was heard |
| `tools/listen_test.py` | `python -m tools.listen_test`: measure the room and your voice, get a `min_rms`, try the whole chain |

## How it decides when you have finished

It works on 0.1 s pieces. *Speech* is a piece louder than `max(listen.min_rms, 3 x the room's noise)`, where the noise is
the quietest piece in the first half second. It waits up to `listen.wait_s` (6 s) for you to start, keeps going while you talk,
and stops after `listen.silence_s` (1 s) of quiet or `listen.max_s` (15 s) in all. Less than 0.3 s of speech is a click and is
dropped, and nothing is sent to the server unless there was speech, because the speech model invents sentences ("Thank you.",
"No sound right now.") from silence.

## Settings (`config/robot.yaml`)

- `devices.mic`: part of the PulseAudio source name (`pactl list short sources`). Now `MV_USB_MIC`, the USB desk microphone; the
  default source can be the webcam's.
- `listen.min_rms`: 300. Measured here on 2026-11-19: quiet room 60-100, room noise bursts up to ~250, speech 400-1600. Run
  `python -m tools.listen_test` if your room or microphone differ.
- `listen.wait_s`, `listen.max_s`, `listen.silence_s`.

## Known limits

- This microphone starts every recording with a 0.3 s noise burst; `Audio.stream` drops the first 0.4 s.
- A speaker playing next to the microphone is heard faintly (levels 100-250), so the robot's own voice is below the threshold, but do
  not press Talk while it is speaking.
- The first transcription after the server has been idle can take about 10 s (model loading); later ones take 1-3 s.
