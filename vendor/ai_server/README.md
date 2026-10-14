# AI server connection

Everything the robot uses to talk to the AI server: LLM, speech-to-text, text-to-speech, face detection / recognition / enrollment, and facial emotion. The models run on the AI server. The robot (a Jetson Orin Nano) only sends requests over TCP.

The server team's scripts in this folder (`face.py`, `emotion.py`, `stt.py`, `tts.py`, and the protocol half of `llm.py`) are kept unmodified so updates can be dropped in. The robot's own wrappers are outside this folder, in `robot/services/`.

## Files

| Where | What it does |
|---|---|
| `vendor/ai_server/llm.py` | The wire protocol (`send_json`, `receive_json`), the generic `request()` used by every module, `ask()` for the LLM, and the server-side receiver (`receive`, with `--mock`) |
| `vendor/ai_server/stt.py` | Standalone STT client: reads a WAV, sends it, prints the transcript. Also provides `read_wav()` |
| `vendor/ai_server/tts.py` | Standalone TTS client: sends text, saves the returned WAV. Also provides `validate_wav()` |
| `vendor/ai_server/face.py` | Standalone face client: `detect`, `recognize` or `enroll` one JPEG/PNG |
| `vendor/ai_server/emotion.py` | Standalone emotion client for one JPEG/PNG |
| `robot/services/llm.py` | `LLMClient` (chat, reachability check, error wrapping), the library system prompt, `SentenceSplitter` |
| `robot/services/book_llm.py` | Builds the explain / recommend prompts from catalogue records and falls back to catalogue text if the LLM fails |
| `robot/services/speech.py` | `Speech.synthesize()` (TTS) and `Speech.transcribe()` (STT) |
| `robot/services/vision.py` | `Vision.detect()`, `.recognize()`, `.emotions()`, `.enroll()` |
| `tools/server_check.py`, `chat_test.py`, `camera_test.py`, `face_enroll.py` | Command-line test tools for each module (see below) |
| `config/robot.yaml` (`ai_server:`) | Server host, port and timeouts |

## How it works

### Protocol

Every module uses the same request/response over one TCP connection (default port **7898**):

1. The sender writes the length of the JSON body as ASCII digits (for example `1432`).
2. The receiver echoes those digits back.
3. The sender writes the JSON body (UTF-8, at most 8 MiB).
4. The server replies the same way, with its own length header, echo and JSON body.

Request:

```json
{"robot_id": "jetson", "request_id": "<uuid>", "command": ["LLM"], "parameters": {}, "content": "..."}
```

Response: the same `robot_id` and `request_id`, `command: []` on success or `["ERR"]` on failure (the message is in `content`), plus `parameters` and `content`. `request()` in `llm.py` checks the IDs match and raises `ValueError` on an error response, and `OSError` / `EOFError` on connection problems. Keep one request in flight per connection.

### Modules

| Module | `command` | `parameters` | `content` (request) | Response |
|---|---|---|---|---|
| LLM | `["LLM"]` | `{}` | the prompt text | `content` = reply text |
| STT | `["STT"]` | `format: raw_pcm`, `dtype`, `sample_rate`, `channels` | base64 PCM from a WAV (uncompressed, up to 120 s / 5 MiB) | `content` = transcript |
| TTS | `["TTS"]` | `voice` (default `af_heart`) | text, up to 2000 characters | `content` = base64 WAV, `parameters` = `format`, `sample_rate`, `channels`, `dtype` |
| Face | `["FACE"]` | `operation`: `detect`, `recognize` or `enroll`; `format`; `name` (enroll only) | base64 JPEG/PNG, up to 600,000 bytes | `parameters.detections` = list of boxes (and names for recognize) |
| Emotion | `["EMOTION"]` | `format` | base64 JPEG/PNG, up to 600,000 bytes | `parameters.detections` = `[{"emotion", "confidence" (percent), "box"}]` |

Module notes:

- **LLM**: the server takes one prompt string, so `to_prompt()` flattens the system prompt and chat history into `System: ... User: ... Assistant:`. The robot keeps the last 10 exchanges. Replies are meant to be spoken, so the system prompt asks for at most three short sentences with no markdown.
- **STT**: the robot records with PulseAudio, discards recordings quieter than `thresholds.min_speech_rms` (500), and sends the rest.
- **TTS**: the returned WAV is validated (PCM, metadata matches) before it is played. The server currently has one voice only, `af_heart` (a woman's; any other name is an `Unknown local TTS voice` error), so the robot reshapes it with `sox` (`voice.pace` to speak faster and more energetically, `voice.deepen` to lower it; `config/robot.yaml`). A real male voice has to be installed on the server. If TTS fails, the reply is shown as text and the conversation carries on.
- **Face**: enrollment photos are stored by the server under `models/face/known_faces/<name>/` and need exactly one face each, so the robot crops each frame to the largest face before enrolling. Recognition matches at similarity 0.6 or more.
- **Emotion**: results below `thresholds.emotion_min_confidence` (0.5) are ignored by the robot.

## Prerequisites

**On the Jetson (robot side)**

- Python 3.10+. The `vendor/ai_server/*.py` clients use only the standard library.
- PyYAML for config: `sudo apt install python3-yaml`
- Camera tools: `sudo apt install python3-opencv`
- Audio (STT recording, TTS playback): `sudo apt install pulseaudio-utils`
- Network path to the AI server on TCP port 7898.

**On the AI server**

- The server team's orchestrator running and listening on `0.0.0.0:7898` (allow the port through the firewall). It needs the model files for the modules you want: the LLM under `models/llm` (default `qwen2-1_5b-instruct-q6_k`), plus the STT, TTS, face and emotion models. Those models and the full orchestrator are not part of this repo.
- For the LLM only, `llm.py receive` loads `../src/llm_server.py` and the local model from the server's own checkout.

## Setup

Set the server address in `config/robot.yaml`:

```yaml
ai_server:
  host: 172.24.47.255
  port: 7898
  timeout_s: 300
  llm_timeout_s: 20
```

Or override it per shell, which wins over the file:

```bash
export AI_HOST=<server-ip>
export AI_PORT=7898
```

## Running

Run everything from the repo root.

**Check the connection**

```bash
python tools/server_check.py                 # or: python -m robot check
python tools/server_check.py --host <server-ip>
```

**LLM, STT and TTS** (`tools/chat_test.py`)

```bash
python tools/chat_test.py                    # --llm: type, read the reply
python tools/chat_test.py --stt              # speak, read the transcript
python tools/chat_test.py --tts              # type, hear it spoken
python tools/chat_test.py --talk             # type, hear the reply: LLM -> TTS
python tools/chat_test.py --chat             # speak, hear the reply: STT -> LLM -> TTS
python tools/chat_test.py --prompt "Recommend a book about robots."   # one turn
python tools/chat_test.py --chat --wav question.wav                   # one voice turn from a file
```

Options: `--host`, `--port`, `--mic <name part>`, `--speaker <name part>`, `--voice`, `--min-rms`.

**Face detection, recognition, emotion and enrollment** (live webcam, `q` quits)

```bash
python tools/camera_test.py                  # live names (recognition)
python tools/camera_test.py --detect         # boxes only
python tools/camera_test.py --emotion        # live emotions
python tools/face_enroll.py jay              # enroll 5 webcam photos as "jay"
python tools/face_enroll.py jay --count 8
```

**Single files with the server team's clients**

```bash
python vendor/ai_server/face.py    --host <server-ip> --image photo.jpg --operation recognize
python vendor/ai_server/face.py    --host <server-ip> --image photo.jpg --enroll --name jay
python vendor/ai_server/emotion.py --host <server-ip> --image photo.jpg
python vendor/ai_server/stt.py     --host <server-ip> --audio speech.wav
python vendor/ai_server/tts.py     --host <server-ip> --text "Hello there" --output hello.wav
python -m vendor.ai_server.llm send --host <server-ip> --prompt "Say hello."
```

Images must be JPEG or PNG and at most 600,000 bytes.

**In the robot**

```bash
python -m robot sim --live      # real webcam + speaker + LLM
python -m robot sim --llm       # LLM only
python -m robot sim --speak     # TTS only
```

**Server side** (on the AI server machine)

```bash
python llm.py receive --host 0.0.0.0              # real model
python llm.py receive --mock --host 0.0.0.0       # echo server, no model needed
```

## Testing without the server

The mock receiver covers the LLM path only (STT, TTS, face and emotion need the real server):

```bash
python -m vendor.ai_server.llm receive --mock &   # echo server on 127.0.0.1:7898
python tools/chat_test.py --host 127.0.0.1 --prompt "hi"
python -m unittest discover -s tests -t .
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Could not reach the AI server at host:port" | Wrong IP, receiver started without `--host 0.0.0.0`, or the firewall blocks port 7898 |
| `Response request_id does not match the request` | Two requests were sent on one connection, or the server is not the expected orchestrator |
| `Server error: ...` | The server rejected the request. The message is the server's own |
| Timeout on a request | The server is busy. LLM and TTS can be slow. Raise `ai_server.timeout_s`. Book explanations fall back to catalogue text after `llm_timeout_s` |
| STT returns nothing | The recording was quieter than `min_speech_rms`. Check the printed level and choose the mic with `--mic` |
| Enrollment error | The server needs exactly one face per photo. Face the camera alone |
