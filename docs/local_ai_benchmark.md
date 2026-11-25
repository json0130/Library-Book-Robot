# Running the robot's AI models locally on the Jetson Orin Nano 8 GB

Measured 2026-10-28 on the robot's own Jetson (Seeed reComputer Orin Nano 8G, JetPack 7.2 / L4T R39.2, CUDA 13.2, 25 W mode, Ubuntu 24.04, 6 CPU cores at about 1.5 GHz).
Nothing in the robot code was changed. All tools, models, scripts and raw results are in `~/local-ai/` (see the last section).

**Short answer: yes, it fits and it is faster than the AI server, but only if the robot stops being a development machine.**
With a normal desktop, VS Code, Claude Code and a browser open, the whole stack ran but left only 0.5 to 1.0 GB free and filled swap to 99 %.
Without the development tools there is about 4 GB of headroom (an estimate, see section 6).

## Contents

1. Memory baseline
2. LLM
3. Speech to text
4. Text to speech, and whether Kokoro on the GPU is possible
5. Everything together
6. Recommendation, expected latency, and what has to change
7. Things to know about how this was measured

---

## 1. Memory baseline

The machine has 7.4 GiB of RAM shared by CPU and GPU, plus 2 GiB of swap. Measured before I started any work:

| | At session start | Later (after the desktop session changed) |
|---|---|---|
| MemAvailable | 2.0 GB | 3.9 GB |
| Used / total | 5.4 / 7.4 GiB | n/a |
| Swap used | 0.1 GB at the very first check; 1.0 to 1.4 GB by the time the tests started | |

Memory by group, using PSS (proportional set size, so memory shared between processes is counted once; RSS adds up to far more than the real use):

| Group | Start (PSS) | Later (PSS) | What it is |
|---|---|---|---|
| VS Code | 1808 MB | 632 MB | 15+ processes: editor, language servers, extensions |
| Firefox | 1181 MB | 616 MB | your browser (content processes plus a Claude sign-in tab at the start) |
| GNOME desktop | 1111 MB | 396 MB | gnome-shell 278, Xorg 203, gnome-software 166, portals, ding, evolution, pipewire-pulse and others |
| Claude Code | 467 MB | 346 MB | two `claude` processes: this session and another |
| System services | about 470 MB | 173 MB | dockerd, snapd, containerd, NetworkManager, journald. (At the start this group also held about 780 MB of my CUDA compiler.) |
| jtop and small Python daemons | about 200 MB | about 35 MB | |

**Without the desktop (kiosk display only):** the GNOME session without Xorg is about 0.9 GB at the start state and about 0.25 GB later (Xorg alone is 0.14 to 0.2 GB and a kiosk still needs an X server or a small compositor, so I leave it out of the saving). The saving is **roughly 0.25 to 0.9 GB**, depending on how much the desktop had grown. That is an estimate from the process list, not a measurement of an actual kiosk setup.

The bigger saving is not running the development tools on the robot: VS Code plus Claude Code plus a second browser were **1.6 to 3.5 GB**, depending on what was open.

Nothing was killed in this step. Raw process lists: `~/local-ai/results/memory_baseline_processes.txt` and `..._final.txt`.

## 2. LLM

llama.cpp (commit 7aca0d2) built with CUDA for sm_87 only, `-j2`. Models: Q4_K_M, fully offloaded to the GPU, flash attention on.

Sources:
- Qwen2.5-1.5B-Instruct: `Qwen/Qwen2.5-1.5B-Instruct-GGUF` (official).
- Qwen3-1.7B: `unsloth/Qwen3-1.7B-GGUF`. The official `Qwen/Qwen3-1.7B-GGUF` repo only has Q8_0. There is no separate "Qwen3-1.7B-Instruct" release: Qwen3-1.7B is the hybrid thinking model, and I ran it with thinking switched off (`enable_thinking: false`).

`llama-bench` (pp512 / tg128, 3 repeats):

| Model | File | Prompt processing | Generation |
|---|---|---|---|
| Qwen3-1.7B Q4_K_M | 1.03 GiB | 1530 ± 18 tokens/s | 40.0 ± 0.4 tokens/s |
| Qwen2.5-1.5B Q4_K_M | 1.04 GiB | 1763 ± 51 tokens/s | 44.4 ± 0.6 tokens/s |

Five real robot prompts built with the robot's own functions in `robot/services/book_llm.py` (system prompt plus catalogue entry plus question: explain Dune, recommend from Cosmos, a question about A Brief History of Time, a question with chat history, and small talk). 350 to 740 prompt tokens each. Streamed from `llama-server`, `-c 4096`:

| Model | Memory (see note) | Load | Time to first token | Whole reply | Reply length |
|---|---|---|---|---|---|
| Qwen3-1.7B Q4_K_M | 0.8 to 1.6 GB | 4.1 s | 0.34 s cold, 0.21 s with the system prompt cached | 1.28 s cold, 1.19 s cached (max 1.8 s) | about 170 characters |
| Qwen2.5-1.5B Q4_K_M | 0.68 to 0.76 GB | 3.1 s | 0.28 s cold, 0.16 s cached | 1.28 s (max 2.05 s) | about 200 characters |
| **Current AI server** (Qwen2 1.5B Q6_K, over Wi-Fi, 4 to 14 ms ping) | n/a | n/a | not streamed: the first byte arrives with the whole reply | **4.0 s mean (3.1 to 5.4 s)** on these prompts; 1.8 to 3.0 s in the later voice-turn test; 0.6 to 0.9 s for a one-line prompt | about 170 characters |

Memory note: the numbers are the drop in system MemAvailable when the server was started, repeated twice with `-c 2048 -np 1`. They varied between repeats (Qwen3: 798 and 1595 MB) because the desktop and the page cache move at the same time, so plan on 1.6 GB for Qwen3. Qwen3 needs more than Qwen2.5 because its KV cache is 4 times larger per token (8 KV heads against 2). Process RSS and PSS under-report here (about 0.4 GB) because the GPU weights are allocated with `cudaMalloc` and are not in the process's RSS on Jetson.

"Cached" is what the robot would see: the system prompt is the same on every turn, so llama-server reuses it.

**Answer quality** (one reply per prompt at temperature 0.7, so this is a sample and not a statistic):

- **Current server (Qwen2 1.5B Q6_K):** good, closest to the catalogue text. Small inventions: "The shelves are looking a bit tidier this morning", "buzzing with students". Correctly said "I don't have information about how long the book is".
- **Qwen3-1.7B Q4_K_M:** stays in character and speaks as Stuart. Concise, names a candidate when recommending. In the voice-turn test it said "It's a long book", which is **not in the catalogue** (the current server declined to say). The recommendation reasons for Dune were thin ("Foundation is about epic and politics").
- **Qwen2.5-1.5B Q4_K_M:** leaks its instructions in 2 of 5 replies, for example "The visitor is looking for a book similar to Cosmos…" and "Not knowing the specific book, I can't comment". The robot would speak those aloud. This is the main reason I do not recommend it.

Because the sample is five prompts, the robot's own scenario harness (`python -m robot scenarios`, docs/testing.md) should be run with the local model before relying on it. Raw replies: `~/local-ai/results/llm_*.json`.

## 3. Speech to text

whisper.cpp (commit 231638a) with CUDA for sm_87. Three short clips (2.4, 2.8 and 5.2 s), generated by the AI server's Kokoro TTS and converted to 16 kHz mono, because nobody was here to speak into the microphone. A resident `whisper-server`, 4 passes per clip.

| Model | Weights | Memory | Load | Latency per clip, warm | Latency, first pass | Transcripts |
|---|---|---|---|---|---|---|
| base.en | 147 MB | 0.22 GB | 0.6 s | 0.24 to 0.42 s | 0.26 to 0.46 s | exact |
| small.en | 487 MB | 0.26 to 0.42 GB | 1.3 s | 0.64 to 0.96 s | 0.64 to 0.93 s | exact |

The current server's STT took 0.79 s per clip warm and 5.5 s on a cold first request.

The clip with two questions came back as two lines from both models (the robot would join them). **Accuracy is optimistic**: clean synthetic speech is the easy case. Real voices, a noisy library and distance from the microphone are not tested. Whisper's `small.en` is generally more robust than `base.en` on such audio, but that is general knowledge, not something this test shows.

whisper.cpp was built with `GGML_CUDA_FA=OFF` (and run with `-nfa`) to skip the very slow flash-attention compile. That may make it slightly slower than a full build.

## 4. Text to speech

Piper 1.8.0 in a Python venv, CPU only (onnxruntime 1.31.1). Voice: **en_US Ryan** (male). The robot's pace setting of 1.12 was applied as Piper's `length_scale` 0.89. Three typical replies (100, 178 and 254 characters), 6 runs each.

| Voice | Model file | Memory | Load | Time to first audio (first sentence) | Real-time factor | Usable? |
|---|---|---|---|---|---|---|
| **ryan-medium** | 63 MB | 133 MB (peak 338 MB) | 3.5 s | 0.25 to 1.1 s (0.5 s mean in the combined test) | 0.14 to 0.17 | **yes** |
| ryan-high | 121 MB | 196 MB (peak 411 MB) | 2.5 s | 1.9 to 7.3 s | 0.9 to 1.1 | no, as slow as real time |
| Current server (Kokoro af_heart plus sox pace) | n/a | n/a | n/a | 0.44 to 1.2 s for the whole reply | n/a | n/a |

Audio samples to listen to (I cannot judge how it sounds): `~/local-ai/results/tts_wav/ryan-medium_1.wav` to `_3.wav`. This is a real male voice, so the sox pitch-shifting of the female `af_heart` voice would no longer be needed.

### Is Kokoro on the GPU an option?

Checked without installing anything large. Python 3.12 on aarch64 is what this machine has.

| Package | Where | CUDA | Has sm_87 (Orin) kernels? | How I know |
|---|---|---|---|---|
| onnxruntime-gpu 1.31.1 | PyPI (`manylinux_2_34_aarch64`, 212 MB) | 13 | **No.** Cubins only for sm_89, 90a, 120a, 121a; no PTX | `cuobjdump` on the wheel's `libonnxruntime_providers_cuda.so` |
| onnxruntime-gpu 1.24.0 | `pypi.jetson-ai-lab.io/sbsa/cu130` (124 MB) | 13 | **Yes** (sm_87, 110, 120a, 121) | same method. **I did not install or run it**: the sandbox blocked installing a wheel from that index, and I did not work around that |
| torch 2.9 to 2.11 | `pypi.jetson-ai-lab.io/sbsa/cu130` (about 340 MB) | 13.0 | **No.** Cubins only for sm_100, 110, 121; no PTX | `cuobjdump` on torch 2.11's `libtorch_cuda.so` |
| torch 2.14.1 | PyPI (454 MB) | 13.0 | **Not verified** | `cuobjdump` needed more than 4 GB on this library and was killed by the OOM killer. A web search found a report that PyPI's plain `torch` targets datacenter GPUs |
| torch 2.14.1+cu132 (also 2.12.1, 2.13, 2.14.0) | `download.pytorch.org/whl/cu132` (460 MB, `manylinux_2_28_aarch64`) | 13.2 | **Reported yes, not verified here** | NVIDIA forum and community guides for JetPack 7.2 Orin ([forum thread](https://forums.developer.nvidia.com/t/jetpack-7-2-jetson-orin-nano-super-8gb-pytorch-for-compute-capability-8-7/379752), [JP7.2 install guide](https://github.com/iuliaferoli/jetson-jp7.2-install)): install with `--index-url https://download.pytorch.org/whl/cu132`, and a compute-capability 8.7 warning is described as harmless |

So a CUDA 13 PyTorch for the Orin **exists** (the `cu132` index matches the installed CUDA 13.2), and an ONNX route also exists (the AI Lab onnxruntime-gpu). Neither has been run here.

**It is not worth it for this robot.** Torch plus its CUDA 13 libraries add roughly 0.5 to 1 GB of RAM on a board that is already tight (section 5), and Piper on the CPU already gives a first sentence in about 0.5 s. Kokoro on the GPU would only matter if the Ryan voice sounds too flat.

## 5. Everything together

Running at the same time:
- the robot display server, plus **Firefox** showing its face page (Chromium is not installed, see section 7); 13 processes, 567 to 776 MB PSS
- `python -m robot sim --camera --local-faces` on the real webcam (barcode scanning and OpenCV faces, all local; 111 MB)
- llama-server with Qwen3-1.7B Q4_K_M (`-c 2048`)
- whisper-server, then Piper (resident worker, 140 MB)

while your VS Code, Claude Code, Firefox and GNOME session stayed open. Two rounds of the same 5 back-to-back voice turns (chat, explain, recommend, a follow-up question, chat): the first with whisper base.en, the second after swapping the whisper server to small.en. Memory sampled every second from `/proc/meminfo`, plus `tegrastats` every second. Every process I started had the highest OOM score so that the kernel would pick mine first, and the test aborted itself if MemAvailable stayed under 200 MB for 3 s.

**MemAvailable after each component started** (one run; single readings drift by a few hundred MB):

| Step | MemAvailable | Change |
|---|---|---|
| Before starting anything | 3244 MB | |
| + display server and Firefox | 2693 MB | -551 |
| + camera | 2657 MB | -36 |
| + LLM (Qwen3-1.7B) | 2099 MB | -558 |
| + whisper base.en | 1125 MB | -974 |
| + Piper | 1081 MB | -44 |
| whisper small.en replacing base.en | 663 MB | -418 |

**System results**

| | base.en round | small.en round |
|---|---|---|
| Minimum MemAvailable | **952 MB** | **528 MB** |
| Swap used, peak | 1717 MB | **2032 of 2047 MB** (it started at 1364 MB, left over from your desktop) |
| OOM-killer kills during the test | **0** | **0** |
| Peak RAM in `tegrastats` | 6848 of 7547 MB (whole test) | |
| GPU load (GR3D) | peak 99 %, mean 16 % over the whole test, 36 % while a model was busy | |
| Temperature (tj) | 56.6 °C at start, **62.5 °C peak** | |
| Board power (VDD_IN) | mean 8.9 W, **peak 19.5 W** | |

Nothing was killed, but with small.en the system was within about 500 MB of running out, with swap almost exhausted. An earlier run without a working browser (Firefox failed to start, see section 7) had a minimum of 725 MB with base.en and 432 MB with small.en.

**Per-turn latency**, mean of 5 turns, audio playback not included:

| | Speech to text | LLM, whole reply | Piper, first audio | **Turn, whole reply then speak** (as the robot works today) | **Turn, speak after the first sentence** |
|---|---|---|---|---|---|
| Local, base.en | 0.28 s | 1.15 s | 0.48 s | **1.9 s** (max 2.9 s) | 1.4 s |
| Local, small.en | 0.64 s | 1.24 s | 0.50 s | **2.4 s** (max 3.0 s) | 1.8 s |
| Current AI server, warm (second pass) | 0.79 s | 2.4 s | 0.72 s | **3.9 s** (3.0 to 4.9 s) | not possible (the server sends one message) |
| Current AI server, first pass | 1.7 s | 3.1 s | 1.2 s | 6.1 s, of which the first turn was **14 s** (cold models) | |

Local replies read well in all 10 turns except the invented "It's a long book" (section 2). Full replies are in `~/local-ai/results/combined_local.json`.

## 6. Recommendation

| Part | Use | Why |
|---|---|---|
| LLM | **Qwen3-1.7B Q4_K_M**, llama-server with `-c 2048 -np 1 -fa on --jinja`, thinking off | stays in persona and does not leak instructions; about 1.2 s per reply. Qwen2.5-1.5B is a little faster and smaller but leaks its instructions. Check grounding with the scenario harness: consider a lower temperature (about 0.3) to reduce inventions, and keep the existing `mentions_any` check |
| Speech to text | **small.en** if the memory budget below holds, otherwise base.en | base.en is 0.36 s faster and 0.2 GB smaller, but small.en is the safer choice for a real microphone in a noisy room. Test with real voices before deciding |
| Text to speech | **Piper en_US Ryan medium**, CPU | real male voice, first audio about 0.5 s; the high-quality voice is far too slow |
| Kokoro on GPU | not needed | see section 4 |

**Expected time for one spoken turn, local:** about **2.4 s** with small.en (1.9 s with base.en) to the first sound, against about **3.9 s** on the current server when it is warm. If the robot spoke the first sentence as soon as the LLM finished it (the code already has a `SentenceSplitter`), that falls to about 1.8 s (1.4 s). The LLM step alone drops from about 2.4 s to about 1.2 s. The local setup also has no cold-start penalty after the first load and does not depend on the Wi-Fi link or the AI server being up.

**What has to change to fit comfortably**

1. **Do not run VS Code, Claude Code or a second browser on the robot.** That alone is worth 1.6 to 3.5 GB, far more than anything else. Develop over SSH.
2. **Kiosk display, not the full desktop** (0.25 to 0.9 GB, section 1), and prefer Chromium kiosk to Firefox if it is lighter (not measured).
3. Keep `-c 2048 -np 1` for llama-server and the base.en or small.en choice above.
4. **Estimated headroom on a dedicated robot:** about 0.6 GB for the system, 0.2 GB for a kiosk display, and 2.2 GB (base.en) to 2.6 GB (small.en) for the display page, camera and all three models, which leaves **roughly 4 GB free**. This is an estimate built from the numbers above, not a measurement of such a configuration.
5. Keep the AI server as a fallback: the robot already falls back to catalogue text when the LLM fails.
6. Sustained use should be checked: the combined test lasted about 3 minutes, and the board reached 62.5 °C with a peak of 19.5 W in 25 W mode. A long run in the real enclosure has not been done.

## 7. Things to know about how this was measured

- **The development session was running.** Memory results are therefore pessimistic, and swap behaved badly (see below).
- **Chromium is not installed**, so I used Firefox (snap) for the face page. Firefox is probably heavier than Chromium. To measure Chromium: `sudo snap install chromium`.
- **The camera ran through `python -m robot sim --camera --local-faces`**, not `--live`, because `--live` sends faces, LLM and speech to the AI server, which is what is being replaced.
- **Test clips are synthetic** (Kokoro through the AI server). I could not record real speech.
- **Two OOM kills happened during the work, both my own mistakes and both my own processes** (not your desktop, VS Code or this session): a CUDA compiler process during the llama.cpp build when I ran another heavy check at the same time, and `cuobjdump` on a 461 MB PyTorch library, which grew to 4 GB. The build was resumed and finished. Kernel counter `oom_kill` went 0 to 2 and stayed at 2 during the combined tests.
- **The first combined run had no working browser** (Firefox needed its profile directory to exist first); I fixed that and reran. Its results are kept as `*_run1_no_browser.*`.
- **Swap is almost full now (about 65 MB free of 2 GB)** because your desktop's idle pages were pushed out while the tests ran. Memory is available again (3.9 GB), but your open applications may feel slow the first time you switch to them. To clear it, when nothing important is mid-write, run: `sudo swapoff -a && sudo swapon -a`.
- Firefox is a snap and rejects signals sent from this session, so the test closes its window through the window manager (`close_window.py`) instead of killing it.
- Qwen3's per-reply quality and Piper's sound could only be judged from text, not from listening.

### Files

All under `~/local-ai/` (3.7 GB, mostly models and llama.cpp / whisper.cpp builds):

- `src/llama.cpp`, `src/whisper.cpp`: sources and builds (`build/bin/`)
- `models/`: the two GGUFs, the two whisper models, the two Piper voices
- `venv-piper/`: Piper
- `prompts.json`, `wav/`: the 5 prompts and the test clips
- `bench_llm.py`, `bench_server.py`, `bench_whisper.py`, `bench_piper.py`, `mem_footprint.py`, `combined.py`, `piper_worker.py`, `close_window.py`, `make_prompts.py`, `make_wavs.py`, `mem_baseline.py`: the scripts
- `results/`: JSON and CSV results, `combined_tegrastats.log`, `tts_wav/` (Piper samples)
- `logs/`: build and server logs

Nothing I started is still running.
