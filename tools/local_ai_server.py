"""A local AI server for the Jetson: the AI server's TCP protocol (vendor/ai_server) backed by models on this machine.

    LLM   forwarded to a llama-server (Qwen3-1.7B Q4_K_M, thinking off); the robot's flattened prompt is turned back into chat messages
    STT   whisper.cpp (whisper-server, base.en by default, small.en with --stt-model); any PCM format is converted to 16 kHz mono
    TTS   Piper (en_US Ryan, a male voice), returned as a 16-bit mono WAV that Speech.synthesize() accepts
    FACE / EMOTION   not handled: the reply is the protocol's error response (faces run on the robot: perception.detector: local)

    scripts/start_local_ai.sh start        # llama-server + this server, with the memory settings of docs/local_ai.md
    python tools/local_ai_server.py check  # ask a running server for an LLM reply, a spoken sentence and its transcript

It runs under any Python with numpy; text-to-speech also needs `piper-tts` (the start script uses the venv in ~/local-ai).
Config is not read: ports and paths are options, so the robot's `ai_server.local` section and the start script agree.
Models are loaded once, at start, and a first request of each kind is made so the first visitor does not wait.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/local_ai_server.py`

from vendor.ai_server.llm import ProtocolError, receive_json, request, send_json, validate_llm_request  # noqa: E402
from vendor.ai_server.stt import MAX_PCM_BYTES  # noqa: E402

log = logging.getLogger("local_ai")

LOCAL_AI_HOME = Path(os.environ.get("LOCAL_AI_HOME", Path.home() / "local-ai"))
WHISPER_RATE = 16000
MAX_TTS_CHARS = 2000                      # the protocol's limit (vendor/ai_server/tts.py)
STT_DTYPES = {"uint8": "u1", "int16": "<i2", "int32": "<i4"}     # the dtypes vendor/ai_server/stt.py can send
ROLES = ("System", "User", "Assistant")


class BackendError(RuntimeError):
    """A model backend failed; the message goes to the robot in the error response."""


# ---- LLM ----------------------------------------------------------------------------------------

def parse_prompt(prompt: str) -> list:
    """The robot's flattened conversation ("System: ...\\n\\nUser: ...\\n\\nAssistant:", services/llm.py to_prompt) as chat messages.

    Contents may hold blank lines (catalogue entries do), so a message ends only where the next "Role: " starts a paragraph.
    A prompt without role labels is one user message."""
    text = re.sub(r"\n*\s*Assistant:\s*$", "", prompt.strip())                   # the trailing cue for the reply
    parts = re.split(r"(?:^|\n\n)(" + "|".join(ROLES) + r"): ", text)
    if len(parts) < 3:
        return [{"role": "user", "content": prompt.strip()}]
    messages = [{"role": role.lower(), "content": content.strip()} for role, content in zip(parts[1::2], parts[2::2])]
    return [m for m in messages if m["content"]] or [{"role": "user", "content": prompt.strip()}]


class LlamaBackend:
    """A llama-server (llama.cpp) over its OpenAI-style HTTP API. Qwen3 gets its thinking switched off."""

    def __init__(self, url: str, max_tokens: int = 160, temperature: float = 0.7, timeout: float = 60.0):
        self.url, self.max_tokens, self.temperature, self.timeout = url.rstrip("/"), max_tokens, temperature, timeout

    def chat(self, messages: list) -> str:
        body = {"model": "local", "messages": messages, "max_tokens": self.max_tokens, "temperature": self.temperature,
                "top_p": 0.8, "top_k": 20, "stream": False, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False}}      # no hidden reasoning before the spoken answer
        req = urllib.request.Request(self.url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                reply = json.load(response)["choices"][0]["message"]["content"]
        except (OSError, KeyError, IndexError, ValueError) as exc:
            raise BackendError(f"the llama-server at {self.url} failed: {exc}") from exc
        reply = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S).strip()
        if not reply:
            raise BackendError("the LLM returned an empty reply")
        return reply

    def generate(self, prompt: str) -> str:
        return self.chat(parse_prompt(prompt))

    def ready(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=2) as response:
                return json.load(response).get("status") == "ok"
        except (OSError, ValueError):
            return False


# ---- STT ----------------------------------------------------------------------------------------

def to_whisper_wav(pcm: bytes, dtype: str, rate: int, channels: int) -> bytes:
    """Raw PCM in any of the protocol's formats as a 16 kHz, mono, 16-bit WAV (what whisper.cpp reads)."""
    if dtype not in STT_DTYPES:
        raise ValueError(f"unsupported dtype {dtype!r} (use one of {', '.join(STT_DTYPES)})")
    width = np.dtype(STT_DTYPES[dtype]).itemsize
    samples = np.frombuffer(pcm[:len(pcm) // (width * channels) * (width * channels)], dtype=STT_DTYPES[dtype]).astype(np.float32)
    if dtype == "uint8":
        samples = (samples - 128.0) / 128.0
    else:
        samples /= float(2 ** (8 * width - 1))
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate > WHISPER_RATE and len(samples) > 1:                         # a moving average before decimating, so high tones don't alias
        k = max(1, round(rate / WHISPER_RATE))
        csum = np.concatenate(([0.0], np.cumsum(samples, dtype=np.float64)))
        samples = ((csum[k:] - csum[:-k]) / k).astype(np.float32) if len(samples) >= k else samples
    if rate != WHISPER_RATE and len(samples) > 1:
        count = max(1, int(len(samples) * WHISPER_RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples).astype(np.float32)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(WHISPER_RATE)
        w.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return out.getvalue()


def clean_transcript(text: str) -> str:
    """Whisper's non-speech tags ([BLANK_AUDIO], (music), [ Silence ]) are not words: drop them."""
    return " ".join(re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text).split())


class WhisperBackend:
    """A whisper.cpp whisper-server kept warm: the model is loaded once. Started here unless `url` points at one already running."""

    def __init__(self, model: Path, binary: Path, port: int = 8090, threads: int = 4, log_path: Optional[Path] = None,
                 url: Optional[str] = None):
        self.model, self.binary, self.port, self.threads, self.log_path = model, binary, port, threads, log_path
        self.url = (url or f"http://127.0.0.1:{port}").rstrip("/")
        self.external = url is not None
        self.proc: Optional[subprocess.Popen] = None

    def start(self, wait_s: float = 120.0) -> None:
        if not self.external:
            for path in (self.binary, self.model):
                if not Path(path).exists():
                    raise BackendError(f"whisper.cpp file not found: {path}")
            env = dict(os.environ, PATH="/usr/local/cuda/bin:" + os.environ.get("PATH", ""))
            sink = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
            # -nfa: the build skips the CUDA flash-attention kernels (docs/local_ai_benchmark.md)
            self.proc = subprocess.Popen([str(self.binary), "-m", str(self.model), "-nfa", "-t", str(self.threads), "-l", "en",
                                          "--port", str(self.port), "--host", "127.0.0.1"], stdout=sink, stderr=sink, env=env)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BackendError(f"whisper-server exited with code {self.proc.returncode} (see {self.log_path})")
            try:
                urllib.request.urlopen(self.url + "/", timeout=2).close()
                return
            except urllib.error.HTTPError:
                return
            except OSError:
                time.sleep(0.3)
        raise BackendError(f"whisper-server did not answer at {self.url} within {wait_s:.0f} s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def transcribe(self, pcm: bytes, dtype: str, rate: int, channels: int) -> str:
        wav = to_whisper_wav(pcm, dtype, rate, channels)
        edge = uuid.uuid4().hex
        body = (f'--{edge}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                + wav + f'\r\n--{edge}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
                        f'--{edge}\r\nContent-Disposition: form-data; name="temperature"\r\n\r\n0.0\r\n--{edge}--\r\n'.encode())
        req = urllib.request.Request(self.url + "/inference", body, {"Content-Type": f"multipart/form-data; boundary={edge}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return clean_transcript(json.load(response).get("text", ""))
        except (OSError, ValueError) as exc:
            raise BackendError(f"the whisper-server at {self.url} failed: {exc}") from exc


# ---- TTS ----------------------------------------------------------------------------------------

class PiperBackend:
    """Piper, in this process (onnxruntime on the CPU). One voice; whatever voice name the robot asks for (af_heart...) gets it."""

    def __init__(self, voice: Path, length_scale: float = 1.0):
        self.voice_path, self.length_scale = Path(voice), length_scale
        self.voice = None

    def load(self) -> None:
        try:
            from piper import PiperVoice                                    # piper-tts is only in the venv: import it here, not at the top
        except ImportError as exc:
            raise BackendError("piper-tts is not installed for this Python (the start script uses ~/local-ai/venv-piper)") from exc
        if not self.voice_path.exists():
            raise BackendError(f"Piper voice not found: {self.voice_path}")
        self.voice = PiperVoice.load(str(self.voice_path), use_cuda=False)

    @property
    def name(self) -> str:
        return self.voice_path.name.removesuffix(".onnx")

    def synthesize(self, text: str) -> tuple:
        """(WAV bytes, sample rate): 16-bit mono PCM."""
        from piper import SynthesisConfig
        pcm = bytearray()
        rate = self.voice.config.sample_rate
        for chunk in self.voice.synthesize(text, syn_config=SynthesisConfig(length_scale=self.length_scale)):
            pcm += chunk.audio_int16_bytes
        if not pcm:
            raise BackendError("Piper produced no audio")
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(pcm))
        return out.getvalue(), rate


# ---- the protocol -------------------------------------------------------------------------------

class Handler:
    """One request in, one response out. Never raises: a failure is an error response (command ["ERR"]), like the AI server's."""

    def __init__(self, llm, stt, tts):
        self.llm, self.stt, self.tts = llm, stt, tts
        self.locks = {"LLM": threading.Lock(), "STT": threading.Lock(), "TTS": threading.Lock()}     # one job at a time per model
        self._voice_noted = False

    def handle(self, message) -> dict:
        response = {"robot_id": message.get("robot_id") if isinstance(message, dict) else None}
        if isinstance(message, dict) and "request_id" in message:
            response["request_id"] = message["request_id"]
        command = message.get("command") if isinstance(message, dict) else None
        started = time.perf_counter()
        try:
            if not isinstance(message, dict) or not isinstance(command, list) or len(command) != 1:
                raise ValueError("command must be a list with one entry, for example [\"LLM\"]")
            name = command[0]
            if name == "LLM":
                parameters, content = {}, self.do_llm(message)
            elif name == "STT":
                parameters, content = {}, self.do_stt(message)
            elif name == "TTS":
                parameters, content = self.do_tts(message)
            elif name in ("FACE", "EMOTION"):
                raise ValueError(f"{name} is not handled by the local AI server: faces run on the robot "
                                 "(perception.detector: local in config/robot.yaml, or --local-faces)")
            else:
                raise ValueError(f"unknown command {name!r} (the local AI server handles LLM, STT and TTS)")
            response.update(command=[], parameters=parameters, content=content)
            log.info("%s ok in %.2f s", name, time.perf_counter() - started)
        except Exception as exc:                                              # noqa: BLE001 - everything becomes an error response
            response.update(command=["ERR"], mode="ERR", parameters={}, content=str(exc))
            log.warning("%s failed after %.2f s: %s", command, time.perf_counter() - started, exc)
        return response

    def do_llm(self, message: dict) -> str:
        validate_llm_request(message)
        with self.locks["LLM"]:
            return self.llm.generate(message["content"])

    def do_stt(self, message: dict) -> str:
        p = message.get("parameters") or {}
        if p.get("format") != "raw_pcm":
            raise ValueError("STT parameters.format must be raw_pcm")
        try:
            rate, channels = int(p["sample_rate"]), int(p["channels"])
            dtype = p["dtype"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("STT needs parameters dtype, sample_rate and channels") from exc
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            raise ValueError("STT sample_rate must be 8000..192000 and channels 1..8")
        try:
            pcm = base64.b64decode(message.get("content") or "", validate=True)
        except ValueError as exc:
            raise ValueError("STT content must be Base64 PCM") from exc
        if not 0 < len(pcm) <= MAX_PCM_BYTES:
            raise ValueError(f"STT audio must be 1..{MAX_PCM_BYTES} bytes")
        with self.locks["STT"]:
            return self.stt.transcribe(pcm, dtype, rate, channels)

    def do_tts(self, message: dict) -> tuple:
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text must be nonempty")
        if len(text) > MAX_TTS_CHARS:
            raise ValueError(f"TTS text must not exceed {MAX_TTS_CHARS} characters")
        voice = (message.get("parameters") or {}).get("voice")
        if voice and not self._voice_noted and voice not in (getattr(self.tts, "name", ""), "ryan"):
            self._voice_noted = True
            log.info("TTS asked for voice %r: this server has one voice (%s) and uses it for every name", voice, getattr(self.tts, "name", "?"))
        with self.locks["TTS"]:
            wav, rate = self.tts.synthesize(text)
        return {"format": "wav", "sample_rate": rate, "channels": 1, "dtype": "int16"}, base64.b64encode(wav).decode("ascii")


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(handler: Handler, host: str, port: int, timeout: float = 300.0) -> ThreadedServer:
    """A TCP server that answers requests on each connection until the client closes it (port 0 picks a free one)."""

    class Connection(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(timeout)
            while True:
                try:
                    try:
                        message = receive_json(self.request)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        response = {"robot_id": None, "command": ["ERR"], "mode": "ERR", "parameters": {}, "content": f"Invalid JSON: {exc}"}
                    else:
                        response = handler.handle(message)
                    try:
                        send_json(self.request, response)
                    except ProtocolError as exc:                              # a reply too big for the protocol (8 MiB)
                        send_json(self.request, {**{k: response.get(k) for k in ("robot_id", "request_id")},
                                                 "command": ["ERR"], "mode": "ERR", "parameters": {}, "content": str(exc)})
                except (OSError, EOFError, ProtocolError):
                    return

    return ThreadedServer((host, port), Connection)


# ---- running it ---------------------------------------------------------------------------------

def warm_up(handler: Handler) -> None:
    """One request of each kind now, so the first visitor does not pay for cold kernels and caches."""
    started = time.perf_counter()
    silence = base64.b64encode(np.zeros(WHISPER_RATE, "<i2").tobytes()).decode()
    steps = [("LLM", {"robot_id": "warmup", "command": ["LLM"], "parameters": {}, "content": "User: Say hello.\n\nAssistant:"}),
             ("TTS", {"robot_id": "warmup", "command": ["TTS"], "parameters": {}, "content": "Hello, I am ready to help."}),
             ("STT", {"robot_id": "warmup", "command": ["STT"], "content": silence,
                      "parameters": {"format": "raw_pcm", "dtype": "int16", "sample_rate": WHISPER_RATE, "channels": 1}})]
    for name, message in steps:
        response = handler.handle(message)
        if response["command"] != []:
            log.warning("warm-up %s: %s", name, response["content"])
    log.info("warm-up done in %.1f s", time.perf_counter() - started)


def serve(args) -> int:
    stt_model = LOCAL_AI_HOME / "models" / "whisper" / f"ggml-{args.stt_model}.bin"
    whisper = WhisperBackend(Path(args.whisper_model) if args.whisper_model else stt_model, Path(args.whisper_bin), args.whisper_port,
                             args.whisper_threads, Path(args.log_dir) / "whisper-server.log" if args.log_dir else None, args.whisper_url)
    piper = PiperBackend(Path(args.piper_voice), args.piper_length_scale)
    llm = LlamaBackend(args.llm_url, args.max_tokens, args.temperature)
    try:
        if not llm.ready():
            log.error("no llama-server answering at %s (scripts/start_local_ai.sh starts it)", args.llm_url)
            return 1
        log.info("loading %s ...", Path(whisper.model).name)
        whisper.start()
        log.info("loading Piper voice %s ...", piper.name)
        piper.load()
    except BackendError as exc:
        log.error("%s", exc)
        whisper.stop()
        return 1
    handler = Handler(llm, whisper, piper)
    warm_up(handler)
    server = make_server(handler, args.host, args.port, args.timeout)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Listening on {args.host}:{server.server_address[1]}  (LLM {args.llm_url}, STT {Path(whisper.model).name}, TTS {piper.name})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        whisper.stop()
    return 0


def check(args) -> int:
    """Ask a running server for each kind of answer, over the protocol, and say how long each took."""
    failed = 0

    def step(label, call):
        nonlocal failed
        started = time.perf_counter()
        try:
            detail = call()
            print(f"  ok   {label:5s} {time.perf_counter() - started:5.2f} s  {detail}")
        except (OSError, EOFError, ValueError) as exc:
            failed += 1
            print(f"  FAIL {label:5s} {exc}", file=sys.stderr)

    def llm():
        reply = request(args.host, args.port, "LLM", {}, "System: You are a friendly library robot. Reply in one short sentence.\n\n"
                                                      "User: Say hello.\n\nAssistant:", timeout=60)["content"]
        if not reply.strip():
            raise ValueError("empty reply")
        return repr(reply[:70])

    spoken = {}

    def tts():
        from vendor.ai_server import tts as tts_client
        response = request(args.host, args.port, "TTS", {"voice": "af_heart"}, "Can you recommend a book about space?", timeout=60)
        raw, info = tts_client.validate_wav(response["content"], response["parameters"])
        spoken["wav"] = raw
        return f"{info['frames'] / info['sample_rate']:.1f} s of audio at {info['sample_rate']} Hz"

    def stt():
        if "wav" not in spoken:
            raise ValueError("needs the TTS step")
        with wave.open(io.BytesIO(spoken["wav"])) as w:
            params = {"format": "raw_pcm", "dtype": "int16", "sample_rate": w.getframerate(), "channels": w.getnchannels()}
            pcm = w.readframes(w.getnframes())
        text = request(args.host, args.port, "STT", params, base64.b64encode(pcm).decode(), timeout=60)["content"]
        if "book" not in text.lower():
            raise ValueError(f"heard {text!r}, expected the sentence that was just spoken")
        return f"heard {text!r}"

    def face():
        try:
            request(args.host, args.port, "FACE", {"operation": "detect", "format": "jpeg"}, "AAAA", timeout=10)
        except ValueError as exc:
            if "Server error" in str(exc):
                return "refused as intended: " + str(exc).split(": ", 1)[1][:60]
            raise
        raise ValueError("a FACE request was answered; this server should refuse it")

    print(f"Local AI server at {args.host}:{args.port}")
    for label, call in (("LLM", llm), ("TTS", tts), ("STT", stt), ("FACE", face)):
        step(label, call)
    return 1 if failed else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("serve", help="run the server (the default)")
    run.add_argument("--host", default="127.0.0.1", help="bind address (127.0.0.1: only this computer)")
    run.add_argument("--port", type=int, default=7898)
    run.add_argument("--timeout", type=float, default=300.0, help="socket timeout per connection, seconds")
    run.add_argument("--llm-url", default="http://127.0.0.1:8081", help="the llama-server (start_local_ai.sh starts it)")
    run.add_argument("--max-tokens", type=int, default=160, help="longest LLM reply in tokens (replies are spoken: 1-3 sentences)")
    run.add_argument("--temperature", type=float, default=0.7)
    run.add_argument("--stt-model", choices=("base.en", "small.en"), default="base.en",
                     help="whisper model: base.en is quicker and smaller, small.en hears real rooms better")
    run.add_argument("--whisper-model", help="a whisper .bin file instead of --stt-model")
    run.add_argument("--whisper-bin", default=str(LOCAL_AI_HOME / "src/whisper.cpp/build/bin/whisper-server"))
    run.add_argument("--whisper-port", type=int, default=8090)
    run.add_argument("--whisper-url", help="use a whisper-server that is already running instead of starting one")
    run.add_argument("--whisper-threads", type=int, default=4)
    run.add_argument("--piper-voice", default=str(LOCAL_AI_HOME / "models/piper/en_US-ryan-medium.onnx"))
    run.add_argument("--piper-length-scale", type=float, default=1.0,
                     help="Piper speaking pace (below 1 is faster). 1.0: the robot's voice.pace (sox) already speeds the voice up")
    run.add_argument("--log-dir", help="where whisper-server writes its log")
    chk = sub.add_parser("check", help="ask a running server for an LLM reply, a spoken sentence and its transcript")
    chk.add_argument("--host", default="127.0.0.1")
    chk.add_argument("--port", type=int, default=7898)
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["serve"] + argv                                 # `serve` is the default command
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return check(args) if args.command == "check" else serve(args)


if __name__ == "__main__":
    sys.exit(main())
