"""Talk to the robot through the AI server: LLM only, speech-to-text, text-to-speech, or all three.

Examples:
    python tools/chat_test.py                 # --llm: type, read the reply (default)
    python tools/chat_test.py --stt           # speak, read the transcript
    python tools/chat_test.py --tts           # type, hear it spoken
    python tools/chat_test.py --talk          # type, hear the reply: LLM -> TTS
    python tools/chat_test.py --chat          # speak, hear the reply: STT -> LLM -> TTS
    python tools/chat_test.py --check         # is the server reachable?
    python tools/chat_test.py --prompt "Recommend a book about robots."   # one --llm/--tts turn
    python tools/chat_test.py --chat --wav question.wav                   # one voice turn from a file

Recording and playback use PulseAudio (parecord/paplay). Pick a microphone with
--mic, e.g. --mic Webcam, and a speaker with --speaker, e.g. --speaker USB, if the system
default input or output is not the right one.

If the face display is running (python -m display.server), its mouth moves while the robot talks:
with the loudness of the speech in --tts/--chat, or for the reply's estimated reading time in --llm.
"""

from __future__ import annotations

import argparse
import base64
import math
import os
from array import array
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/chat_test.py`

from robot.actuation.face import CHARS_PER_SECOND, DEFAULT_PORT as FACE_PORT, MAX_TALK_S, mouth_levels, talk  # noqa: E402
from robot.drivers.audio import find_device  # noqa: E402
from robot.services.llm import LLMClient, LLMError, split_sentences  # noqa: E402
from vendor.ai_server import stt, tts  # noqa: E402
from vendor.ai_server.llm import request  # noqa: E402

DEFAULT_SYSTEM = (
    "You are the Library Book Robot, a friendly robot that helps visitors in a university library. "
    "You help people find, return and choose books: explain what a book is about, recommend similar "
    "titles, and point them to the right section or the help desk. "
    "Keep to library topics and gently steer other questions back to books and study. "
    "If you are not sure a book exists or the library holds it, say so and suggest asking the help desk "
    "or searching the catalogue; never invent titles, authors or shelf locations. "
    "Remember what the visitor said earlier in the conversation. "
    "Your replies are spoken aloud, so use at most three short, plain sentences. "
    "Do not use markdown, lists or emoji."
)
HISTORY_EXCHANGES = 10  # past user/assistant pairs sent with each prompt
MAX_HISTORY_MESSAGES = 2 * HISTORY_EXCHANGES
MODES = {
    "llm": "type text, read the reply (default)",
    "stt": "speak, read the transcript",
    "tts": "type text, hear it spoken",
    "talk": "type text, hear the reply (LLM -> TTS)",
    "chat": "speak, hear the reply (STT -> LLM -> TTS)",
}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--host", default=os.environ.get("AI_HOST", "10.42.0.118"),
                   help="AI server address, e.g. 192.168.1.50 (env: AI_HOST)")
    p.add_argument("--port", type=int, default=int(os.environ.get("AI_PORT", 7898)),
                   help="AI server port (env: AI_PORT)")
    p.add_argument("--timeout", type=float, default=300, help="seconds to wait for a reply")
    mode = p.add_mutually_exclusive_group()
    for name, help_text in MODES.items():
        mode.add_argument(f"--{name}", dest="mode", action="store_const", const=name, help=help_text)
    p.set_defaults(mode="llm")
    p.add_argument("--system", default=DEFAULT_SYSTEM, help="system prompt")
    p.add_argument("--prompt", help="run one --llm/--tts turn with this text and exit")
    p.add_argument("--wav", type=Path, help="run one --stt/--chat turn from this WAV instead of the mic")
    p.add_argument("--mic", default=os.environ.get("MIC"),
                   help="part of a PulseAudio input name, e.g. Webcam (env: MIC); default: system input")
    p.add_argument("--speaker", default=os.environ.get("SPEAKER"),
                   help="part of a PulseAudio output name, e.g. USB (env: SPEAKER); default: system output")
    # ponytail: whole-recording RMS gate; lab room noise measured 260-384 on the webcam mic.
    # Replace with voice activity detection if noise still gets transcribed.
    p.add_argument("--min-rms", type=float, default=500,
                   help="recordings quieter than this RMS level count as silence and skip STT;\n"
                        "each recording prints its level so you can tune this")
    p.add_argument("--voice", default="af_heart", help="TTS voice")
    p.add_argument("--check", action="store_true", help="check the server is reachable and exit")
    p.add_argument("--face-port", type=int, default=int(os.environ.get("FACE_PORT", FACE_PORT)),
                   help=f"port of the face display (python -m display.server), default {FACE_PORT}")
    p.add_argument("--no-face", action="store_true", help="don't move the face display's mouth")
    p.add_argument("--lip-delay", type=float, default=0.1,
                   help="seconds the mouth waits after playback starts, to match speaker latency (default 0.1)")
    args = p.parse_args(argv)
    voice_in = args.mode in ("stt", "chat")
    if args.prompt and voice_in:
        p.error("--prompt works with --llm/--tts/--talk; use --wav for --stt/--chat")
    if args.wav and not voice_in:
        p.error("--wav works with --stt/--chat")
    return args


def print_sentence(text: str) -> None:
    print(f"  {text}", flush=True)


def run_turn(
    client: LLMClient,
    system: str,
    history: list[dict],
    user_text: str,
    on_sentence: Callable[[str], None] = print_sentence,
) -> bool:
    """Run one exchange. on_sentence is where speech and the face UI will plug in later."""
    history.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": system}] + history
    t0 = time.perf_counter()
    try:
        reply = client.chat(messages)
    except LLMError as exc:
        history.pop()
        print(f"error: {exc}", file=sys.stderr)
        return False
    for sentence in split_sentences(reply):
        on_sentence(sentence)
    history.append({"role": "assistant", "content": reply})
    del history[:-MAX_HISTORY_MESSAGES]
    print(f"  total {time.perf_counter() - t0:.2f}s")
    return True


def find_mic(name: str) -> str:
    return find_device("sources", name)


def record(mic: str | None) -> Path:
    """Record from the mic until Enter is pressed; returns a temporary 16 kHz mono WAV."""
    fd, name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["parecord", "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav"]
    if mic:
        cmd.append(f"--device={mic}")
    proc = subprocess.Popen(cmd + [name])
    try:
        input("  listening... [Enter] when done ")
    finally:
        proc.terminate()
        proc.wait()
    return Path(name)


def rms(pcm: bytes) -> float:
    samples = array("h", pcm)  # int16, native (little-endian) byte order
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def transcribe(args, wav: Path) -> str:
    """Return the transcript, or "" when the recording is silence (STT invents text for silence)."""
    pcm, dtype, rate, channels, _ = stt.read_wav(wav)
    if dtype == "int16":
        level = rms(pcm)
        print(f"  level {level:.0f} (min {args.min_rms:.0f})", flush=True)
        if level < args.min_rms:
            return ""
    response = request(args.host, args.port, "STT",
                       dict(format="raw_pcm", dtype=dtype, sample_rate=rate, channels=channels),
                       base64.b64encode(pcm).decode("ascii"), timeout=args.timeout)
    return response["content"].strip()


def listen(args) -> str:
    if args.wav:
        return transcribe(args, args.wav)
    wav = record(args.mic)
    try:
        return transcribe(args, wav)
    finally:
        wav.unlink(missing_ok=True)


class Face:
    """Moves the face display's mouth while the robot talks. Does nothing if the display isn't running,
    and tries again a little later in case it is started after chat."""

    RETRY_S = 20.0

    def __init__(self, port: int, enabled: bool = True):
        self.port = port
        self.enabled = enabled
        self._retry_at = 0.0
        self._warned = False

    def _send(self, **kwargs) -> None:
        if not self.enabled or time.monotonic() < self._retry_at:
            return
        if talk(port=self.port, timeout=0.3, **kwargs):
            self._warned = False
            return
        self._retry_at = time.monotonic() + self.RETRY_S
        if not self._warned:
            print(f"  (face display not running on port {self.port}; start it with: python -m display.server)",
                  flush=True)
            self._warned = True

    def talk_audio(self, wav: bytes, delay_s: float = 0.0) -> None:
        """Mouth follows the loudness of this WAV; call it as playback starts."""
        try:
            levels = mouth_levels(wav)
        except (ValueError, EOFError) as exc:
            print(f"  (no lip sync: {exc})", file=sys.stderr)
            return
        self._send(levels=levels, delay_s=delay_s)

    def talk_text(self, text: str) -> None:
        """Mouth moves for about as long as reading the text aloud would take."""
        self._send(seconds=min(MAX_TALK_S, max(0.6, len(text) / CHARS_PER_SECOND)))

    def stop(self) -> None:
        self._send(seconds=0)


def speak(args, text: str, face: Face) -> None:
    response = request(args.host, args.port, "TTS", {"voice": args.voice}, text, timeout=args.timeout)
    raw, _ = tts.validate_wav(response.get("content"), response["parameters"])
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(raw)
        f.flush()
        cmd = ["paplay"] + ([f"--device={args.speaker}"] if args.speaker else [])
        proc = subprocess.Popen(cmd + [f.name])
        face.talk_audio(raw, args.lip_delay)
        try:
            code = proc.wait()
        except BaseException:          # Ctrl+C: stop the sound and close the mouth
            proc.terminate()
            proc.wait()
            face.stop()
            raise
        if code != 0:
            face.stop()
            raise subprocess.CalledProcessError(code, "paplay")


def step(args, client: LLMClient, history: list[dict], text: str | None, face: Face) -> bool:
    """One exchange in args.mode. text is the typed input for --llm/--tts; voice modes listen."""
    try:
        if args.mode in ("stt", "chat"):
            text = listen(args)
            if not text:
                print("  (didn't hear anything)", flush=True)
                return False
            print(f"  heard: {text}", flush=True)
            if args.mode == "stt":
                return True
        if args.mode == "tts":
            speak(args, text, face)
            return True
        if not run_turn(client, args.system, history, text):
            return False
        reply = history[-1]["content"]
        if args.mode in ("chat", "talk"):
            speak(args, reply, face)
        else:
            face.talk_text(reply)
        return True
    except (OSError, EOFError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False


def main(argv=None) -> int:
    args = parse_args(argv)
    client = LLMClient(args.host, args.port, args.timeout)

    try:
        client.check()
        if args.mic:
            args.mic = find_mic(args.mic)
        if args.speaker:
            args.speaker = find_device("sinks", args.speaker)
    except (LLMError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"AI server reachable at {args.host}:{args.port}.")
        return 0

    history: list[dict] = []
    face = Face(args.face_port, enabled=not args.no_face)

    if args.prompt or args.wav:
        return 0 if step(args, client, history, args.prompt, face) else 1

    voice_in = args.mode in ("stt", "chat")
    print(f"{args.mode.upper()} mode, AI server at {args.host}:{args.port}. "
          + ("Ctrl+C to exit." if voice_in else "Type 'quit' to exit."))
    while True:
        try:
            if voice_in:
                input("[Enter] to talk ")
                text = None
            else:
                text = input("you> ").strip()
                if text.lower() in {"quit", "exit"}:
                    return 0
                if not text:
                    continue
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            step(args, client, history, text, face)
        except KeyboardInterrupt:
            print()
            return 0


if __name__ == "__main__":
    sys.exit(main())
