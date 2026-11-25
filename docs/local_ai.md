# Running the AI on the Jetson (local mode)

The robot normally sends its language model, speech and face requests to the AI server over TCP (`vendor/ai_server/README.md`).
In **local mode** the LLM, speech-to-text and text-to-speech run on the Jetson itself, behind a small server that speaks the
same protocol. The robot code does not change: only the address it connects to does.

```
robot  --TCP, the AI server's protocol-->  tools/local_ai_server.py  --HTTP-->  llama-server   Qwen3-1.7B Q4_K_M (GPU)
        (LLMClient, Speech)                  127.0.0.1:7898          child --->  whisper-server whisper.cpp base.en (GPU)
                                                                     in process  Piper         en_US Ryan, a male voice (CPU)
```

Faces are not handled by the local server (a `FACE` or `EMOTION` request gets the protocol's error response). Local mode therefore
detects faces on the robot with OpenCV (`perception.detector: local`), which the config switch sets for you.

The measurements behind the choices are in [local_ai_benchmark.md](local_ai_benchmark.md) (written against the main checkout).

## Read this first: the local LLM invents facts

`honest_about_unknowns` is the scenario check that asks "how many pages does it have?" about a book whose page count is not in the
catalogue. The right answer is "I don't have that". Thirty `ask_about_book` runs per setting (3 seeds x 10), real LLM, same visitors:

| LLM | Runs passed | Honest-answers failures | Of which a made-up number |
|---|---|---|---|
| **Real AI server** (Qwen2 1.5B Q6_K) | 29 / 30 | **0** | 0 |
| Local Qwen3-1.7B Q4_K_M, temperature 0.7 (the default) | 18 / 30 | **12** | 9 |
| Local Qwen3-1.7B Q4_K_M, temperature 0.2 | 19 / 30 | **11** | 8 |
| Local Qwen2.5-1.5B Q4_K_M, temperature 0.7 | 24 / 30 | **6** | 3 |

(The one real-server failure was a latency check, not honesty.) A lower temperature does not help; Qwen2.5 halves the failures
and still fails one run in five. Typical failures: "The Martian is 336 pages", "It has over 300 pages", or a description of the book
instead of "I don't know". Across all 190 LLM replies per setting, the real server never gave a number that is not in the catalogue;
the local models did 14 to 16 times.

**So local mode is faster and works offline, but it should not be used with visitors until this is fixed.** What would fix it, none
of which was done here because the brief was to leave the robot code alone:

- a grounding check in `robot/services/book_llm.py` like the one recommendations already have (`mentions_any`): discard a free-question
  answer that contains a number or year that is not in the book's catalogue entry, and say the catalogue text instead;
- a bigger or better-quantised model (Qwen3-1.7B at Q8_0 is 1.8 GB; Qwen3-4B Q4_K_M is about 2.5 GB, which does not fit with the rest, see below);
- re-running the scenario harness after any such change: `AI_MODE=local python -m robot scenarios run --scenario ask_about_book --runs 10 --llm real --seed 1`
  (also `--seed 2` and `--seed 3`; one seed of ten runs missed the problem at first).

To try Qwen2.5-1.5B instead: `LLM_MODEL=~/local-ai/models/qwen2.5-1.5b-instruct-q4_k_m.gguf scripts/start_local_ai.sh restart`.

## Switching modes

`config/robot.yaml`:

```yaml
ai_server:
  mode: remote            # remote (default) | local
  host: 172.24.30.251     # used in remote mode
  port: 7898
  local: {host: 127.0.0.1, port: 7898}    # used in local mode
```

- `mode: local` sets the robot's AI address from `ai_server.local` and `perception.detector: local`. Everything else in the file is unchanged.
- Per shell, without editing the file: `export AI_MODE=local` (or `remote`). `AI_HOST` and `AI_PORT` still override the address in either mode.
- Check which mode a command uses: `python -m robot sim` prints the address it talks to (`LLM at 127.0.0.1`).

## Starting and stopping

```bash
scripts/start_local_ai.sh                 # start llama-server and the local server, wait until they answer, run the check
scripts/start_local_ai.sh start --stt small.en    # a bigger speech model: hears noisy rooms better, +0.4 s, +0.2 GB
scripts/start_local_ai.sh status | check | stop | restart
export AI_MODE=local
python -m robot check                     # is it reachable?
python -m robot sim --live --local-faces  # the robot, fully local (webcam, faces, LLM, speaker)
```

The script needs the builds and models in `~/local-ai/` (set `LOCAL_AI_HOME` to move them): llama.cpp and whisper.cpp built with CUDA for
sm_87, the Qwen3 GGUF, the whisper model, the Piper voice and a Python venv with `piper-tts` (`venv-piper`). Nothing is built or
downloaded by the script. Start-up took about 11 s with the models already in the disk cache (a cold start from a fresh boot was not timed). It refuses to
start if too little memory is free (`--force` overrides) and lists the largest memory users instead.

Settings it applies (from the benchmark): `-c 2048 -np 1` (a 2048-token context and one slot; the benchmark's prompts were 350 to 750 tokens,
and every extra slot or token of context costs memory), thinking switched off for Qwen3, flash attention on, all layers on the GPU,
`127.0.0.1` only. Environment overrides: `LLM_MODEL`, `LLAMA_BIN`, `LOCAL_AI_TEMPERATURE` (default 0.7), `LOCAL_AI_PORT`, `LLAMA_PORT`,
`WHISPER_PORT`, `RUN_DIR` (pid files and logs, default `~/local-ai/run`).

`python tools/local_ai_server.py check` sends an LLM request, speaks a sentence, transcribes that sentence and confirms that a face
request is refused. The server also has options for the whisper model/binary, the Piper voice and pace (`--help`).

Voice: Piper has one male voice (Ryan). The robot's `voice.tts_voice: af_heart` is accepted and mapped to it, and `voice.pace` (1.12)
still speeds it up through sox on the robot, as it does for the AI server's voice.

## Memory budget

The board has 7.4 GiB shared by CPU and GPU and 2 GiB of swap. Measured (details in the benchmark):

| Part | Memory |
|---|---|
| llama-server, Qwen3-1.7B Q4_K_M, 2048 context, one slot | 0.8 to 1.6 GB (budget 1.6; the readings varied) |
| whisper.cpp base.en / small.en | 0.22 GB / 0.26 to 0.42 GB |
| Piper Ryan medium + the local server | 0.15 to 0.25 GB |
| **The three models together** (measured by starting them: MemAvailable 3860 to 1561 MB) | **about 2.3 GB** |
| Firefox with the face page, and the display server | 0.4 to 0.8 GB |
| Robot sim (camera, OpenCV faces) | 0.04 to 0.15 GB |
| **Everything the robot needs** | **about 2.7 to 3.3 GB** with base.en, 0.2 to 0.4 GB more with small.en |

### Measured: the robot fully local, on a development desktop

`python -m robot sim --live --local-faces` (real webcam, OpenCV faces, spoken replies) with `AI_MODE=local`, the display server, a Firefox
window with the face page and the local AI (base.en), while VS Code, Claude Code, a browser and GNOME stayed open. Five typed turns
(hello, explain, who wrote it, recommend, pages, small talk) and two turns through the real microphone and speaker; MemAvailable and swap
sampled every second.

| Step | MemAvailable |
|---|---|
| Local AI running, nothing else (swap already 1.96 GB used by the desktop) | 1335 MB |
| + display server and Firefox face page | 843 MB (-492) |
| + robot sim (webcam, faces, LLM, speaker) | 669 MB (-174) |
| All running, after the turns | 596 MB |

- **Minimum MemAvailable: 562 MB. Swap: completely full (2048 of 2048 MB) from the moment the browser started. OOM-killer kills: 0.**
  Peak RAM in use 6779 of 7547 MB. This configuration works but has no margin; one more application would start killing processes.
- Per process (PSS, which leaves out the GPU memory that `cudaMalloc` allocates on Jetson): Firefox 509 MB (13 processes), llama-server 648 MB,
  local AI server with whisper 542 MB, robot sim 142 MB, display server 15 MB.
- GPU load peaked at 99 % (about 41 % while a model was working); the junction temperature went from 60 to 64 °C; board power averaged
  10.4 W with a peak of 18.8 W in the 25 W mode.
- The LLM took 1.2 to 2.6 s per reply inside the robot (longer prompts with history than in the benchmark, and the camera and browser
  share the board); the two microphone turns took 3.1 and 3.6 s from the end of the question to the first sound, with 0.45 s speech
  recognition and 1.3 s for the LLM.
- The robot said "It has over 200 pages." when asked how many pages a catalogue book has: the honesty problem above, seen live.
- The face-identity model (`models/face/...sface...onnx`, gitignored) is missing from this worktree, so people were counted, not told apart.

A first attempt was stopped by a safety rule I had set (stop if swap is nearly full) when swap filled during the first typed turns;
MemAvailable was 650 MB at the time. The second run stopped only if MemAvailable fell under 250 MB.

**Conclusion: with the desktop and development tools running, local mode fits only barely. Local mode needs the robot to boot without the
desktop and without development tools.**

### Recommended production boot (described, not applied)

Aim: nothing on the robot except the robot, its display and the AI. This is a description; nothing in the system was changed.

1. **Boot to the text console**: `systemctl set-default multi-user.target` (the desktop, gdm, gnome-shell, gnome-software and the
   portals go: 0.25 to 0.9 GB). Do not run VS Code, Claude Code or a second browser on the robot (1.6 to 3.5 GB); develop over SSH.
2. **A kiosk display**: a small X session started by a service (for example `xinit` with `openbox`, or the `cage` Wayland kiosk compositor)
   that runs `display/start_display.sh`'s browser step in kiosk mode. Chromium is lighter than the Firefox used for the measurements
   (`sudo snap install chromium`; not measured). Run it as an unprivileged user with autologin on tty1.
3. **Services, started in this order** (systemd units, `Restart=on-failure`):
   `local-ai.service` runs `scripts/start_local_ai.sh start` as a `oneshot` with `RemainAfterExit=yes` (or two units: llama-server, then
   `tools/local_ai_server.py`); `robot-display.service` runs `python -m display.server`; `robot.service` runs the robot with
   `Environment=AI_MODE=local`, `After=local-ai.service`, `Requires=local-ai.service`.
4. **Protect the AI from the OOM killer**: `OOMScoreAdjust=-500` on the AI units and `+500` on the browser, so that if memory runs out the
   browser is the first to go. Keep the 2 GiB swap; consider zram (`zram-config`) so that swap is compressed memory and not the disk.
5. **Power and heat**: the 25 W mode peaked at about 19 W and 64 °C during the tests; check the enclosure before long runs
   (`tegrastats`).
6. **Trim background services** that are not needed on a robot: `snapd` (if Chromium is installed with apt or a deb instead of the snap),
   `gnome-software`, `fwupd`, `packagekit`, Bluetooth helpers, `docker`/`containerd` if unused (each 20 to 40 MB).
7. **Keep the remote server as a fallback**: `AI_MODE=remote` needs no other change. The robot already falls back to the catalogue text
   when the LLM is slow or unavailable.

Expected headroom on such a robot: about 4 GB free with base.en (an estimate from the numbers above; it has not been measured on a real
dedicated boot).

## Latency

Measured on this board, one spoken turn (speech-to-text, LLM, text-to-speech, to the first sound), audio playback not included:

| | Speech to text | LLM | Text to speech | Turn |
|---|---|---|---|---|
| Local, base.en | 0.28 s | 1.15 s | 0.48 s (first sentence) | about 1.9 s |
| Local, small.en | 0.64 s | 1.24 s | 0.50 s | about 2.4 s |
| AI server, warm | 0.79 s | 2.4 s | 0.72 s | about 3.9 s |
| Local, real microphone and speaker (the robot's own `Speech`, whole reply as one TTS call) | 0.42 s | 1.31 s | 1.52 s | 3.24 s |

The last row is the live voice turn: a question spoken by the speaker and recorded by the webcam microphone (the recording was real,
the voice was not: nobody could speak in the session), then the robot's real chain. The text-to-speech time there is the whole reply
synthesised at once; speaking sentence by sentence, as the robot does, starts sooner.

## Troubleshooting

- **`python -m robot check` says unreachable**: `scripts/start_local_ai.sh status`; logs are in `~/local-ai/run/` (`llama-server.log`,
  `local-ai-server.log`, `whisper-server.log`).
- **The script refuses to start for lack of memory**: close the desktop applications it lists, or boot without the desktop; `--force` starts anyway
  (and the machine may swap heavily).
- **The first answer after a long idle is slower**: the robot's keep-alive (`ai_server.keepalive_s`) also keeps the local LLM warm.
- **Speech is not understood**: the recording is silence (`thresholds.min_speech_rms`), the microphone is the wrong source (set
  `devices.mic`; on this board the default input is the unplugged analog jack and the webcam microphone is the one that works), or
  try `--stt small.en`.
- **Face detection fails in local mode**: it should not; if `perception.detector` is `server` in an unusual config, set it to `local`.
- **Models are missing**: the script names the missing file; the models are not downloaded by it.
