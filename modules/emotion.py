"""Send a JPEG/PNG to the AI orchestrator for facial emotion classification.

Copy just this file to the client. Uses only Python's standard library.
"""
import argparse
import base64
import json
from pathlib import Path
import socket
import sys
import uuid

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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7898)
    parser.add_argument("--robot-id", default=socket.gethostname())
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        with args.image.open("rb") as source:
            raw = source.read(600_001)
        if not 0 < len(raw) <= 600_000:
            raise ValueError("Image must be at most 600,000 bytes; resize/recompress it first")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "png"
        elif raw.startswith(b"\xff\xd8\xff"):
            fmt = "jpeg"
        else:
            raise ValueError("Image must be a JPEG or PNG file")
        request = dict(
            robot_id=args.robot_id,
            request_id=str(uuid.uuid4()),
            command=["EMOTION"],
            parameters=dict(format=fmt),
            content=base64.b64encode(raw).decode("ascii"),
        )
        print(f"Sending emotion image: {args.image.name} ({len(raw)} bytes)", flush=True)
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
        if response.get("command") != []:
            raise ValueError("Server returned an incomplete response")
        return 0
    except (OSError, EOFError, ValueError) as exc:
        print(f"Emotion request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
