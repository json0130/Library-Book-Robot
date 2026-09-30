"""Send text to the AI orchestrator and save the returned WAV audio.

Copy just this file to the client. Uses only Python's standard library.
"""
import argparse
import base64
import binascii
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import uuid
import wave

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


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


def validate_wav(encoded, parameters):
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise ValueError("Server returned invalid Base64 audio") from exc
    try:
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if audio.getcomptype() != "NONE":
                raise ValueError("Server WAV is not uncompressed PCM")
            actual = dict(sample_rate=audio.getframerate(), channels=audio.getnchannels(),
                          dtype={1: "uint8", 2: "int16", 4: "int32"}.get(audio.getsampwidth()),
                          frames=audio.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Server returned an invalid WAV: {exc}") from exc
    for key in ("sample_rate", "channels", "dtype"):
        if parameters.get(key) != actual[key]:
            raise ValueError(f"WAV {key} does not match response metadata")
    return raw, actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7898)
    parser.add_argument("--robot-id", default=socket.gethostname())
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file", type=Path)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--output", type=Path, default=Path("tts-output.wav"))
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        text = args.text if args.text is not None else args.text_file.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("TTS text must be nonempty")
        if len(text) > 2000:
            raise ValueError("TTS text must not exceed 2000 characters")
        request = dict(
            robot_id=args.robot_id,
            request_id=str(uuid.uuid4()),
            command=["TTS"],
            parameters=dict(voice=args.voice),
            content=text,
        )
        print(f"Sending TTS: {len(text)} characters, voice={args.voice}", flush=True)
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
            send_json(sock, request)
            response = receive_json(sock)
        if not isinstance(response, dict):
            raise ValueError("Response must be a JSON object")
        for key in ("robot_id", "request_id"):
            if response.get(key) != request[key]:
                raise ValueError(f"Response {key} does not match request")
        if response.get("command") == ["ERR"]:
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 1
        if response.get("command") != [] or response.get("parameters", {}).get("format") != "wav":
            raise ValueError("Server returned an invalid TTS response")
        raw, metadata = validate_wav(response.get("content"), response["parameters"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=".tts-", delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, args.output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        seconds = metadata["frames"] / metadata["sample_rate"]
        summary = {key: value for key, value in response.items() if key != "content"}
        summary["audio_bytes"] = len(raw)
        summary["duration_seconds"] = round(seconds, 3)
        summary["output"] = str(args.output)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, EOFError, ValueError) as exc:
        print(f"TTS request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
