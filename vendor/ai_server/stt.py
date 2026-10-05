"""Send an uncompressed PCM WAV to the AI orchestrator for transcription.

Copy just this file to the client. Uses only Python's standard library.
"""
import argparse
import base64
import json
from pathlib import Path
import socket
import sys
import uuid
import wave

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_PCM_BYTES = 5 * 1024 * 1024
WAV_DTYPES = {1: "uint8", 2: "int16", 4: "int32"}


class ProtocolError(ValueError):
    pass


def receive_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(min(size - len(chunks), 65536))
        if not chunk:
            raise EOFError("Peer disconnected before the message was complete")
        chunks.extend(chunk)
    return bytes(chunks)


def send_json(sock, message):
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Message exceeds the 8 MiB limit")
    header = str(len(payload)).encode("ascii")
    sock.sendall(header)
    if receive_exact(sock, len(header)) != header:
        raise ProtocolError("Server did not echo the message byte length")
    sock.sendall(payload)


def receive_json(sock):
    header = sock.recv(32)
    if not header:
        raise EOFError("Peer closed the connection")
    if not header.isdigit() or not 0 < int(header) <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Invalid message length")
    sock.sendall(header)
    return json.loads(receive_exact(sock, int(header)).decode("utf-8"))


def read_wav(path):
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ValueError("WAV must contain uncompressed PCM audio")
            width = source.getsampwidth()
            if width not in WAV_DTYPES:
                raise ValueError("WAV sample width must be 8, 16, or 32-bit PCM")
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            frames = source.getnframes()
            if not 1 <= channels <= 8:
                raise ValueError("WAV must contain 1..8 channels")
            if not 8000 <= sample_rate <= 192000:
                raise ValueError("WAV sample rate must be 8000..192000 Hz")
            if frames / sample_rate > 120:
                raise ValueError("WAV duration must not exceed 120 seconds")
            pcm = source.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Invalid WAV file: {exc}") from exc
    if not pcm or len(pcm) > MAX_PCM_BYTES:
        raise ValueError(f"WAV PCM data must contain 1..{MAX_PCM_BYTES} bytes")
    return pcm, WAV_DTYPES[width], sample_rate, channels, frames / sample_rate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7898)
    parser.add_argument("--robot-id", default=socket.gethostname())
    parser.add_argument("--audio", "--file", dest="audio", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        pcm, dtype, sample_rate, channels, duration = read_wav(args.audio)
        request = dict(
            robot_id=args.robot_id,
            request_id=str(uuid.uuid4()),
            command=["STT"],
            parameters=dict(format="raw_pcm", dtype=dtype,
                            sample_rate=sample_rate, channels=channels),
            content=base64.b64encode(pcm).decode("ascii"),
        )
        print(f"Sending STT: {args.audio.name} ({duration:.2f}s, {sample_rate} Hz, {channels} channel(s), {dtype})", flush=True)
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
            send_json(sock, request)
            response = receive_json(sock)
        if not isinstance(response, dict):
            raise ValueError("Response must be a JSON object")
        for key in ("robot_id", "request_id"):
            if response.get(key) != request[key]:
                raise ValueError(f"Response {key} does not match request")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        if response.get("command") == ["ERR"]:
            return 1
        if response.get("command") != [] or not isinstance(response.get("content"), str):
            raise ValueError("Server returned an invalid STT response")
        print("\nTranscript:", response["content"])
        return 0
    except (OSError, EOFError, ValueError) as exc:
        print(f"STT request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
