"""LLM link between the robot and the AI server, in one script.

Robot side: LLMClient sends the chat to the server and SentenceSplitter cuts the
reply into sentences for speech and the face UI.

Command line: run `python llm.py receive` on the AI server and `python llm.py send
--prompt "Hello"` on the client. Send and mock modes need only the standard library.
Real inference uses ../src/llm_server.py and the server's local model assets.

The existing TCP length/echo handshake is preserved. Keep one request in flight
per connection; its undelimited length header cannot handle arbitrary header
fragmentation without a coordinated change to all clients and servers.
"""

from __future__ import annotations


import argparse
import json
from pathlib import Path
import socket
import sys
import uuid

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ProtocolError(ValueError):
    """Invalid framing; close the connection after this error."""


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(min(size - len(chunks), 65536))
        if not chunk:
            raise EOFError("Peer disconnected before the message was complete")
        chunks.extend(chunk)
    return bytes(chunks)


def send_json(sock: socket.socket, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Message exceeds the 8 MiB example limit")
    header = str(len(payload)).encode("ascii")
    sock.sendall(header)
    if receive_exact(sock, len(header)) != header:
        raise ProtocolError("Server did not echo the message byte length")
    sock.sendall(payload)


def receive_json(sock: socket.socket):
    # The sender waits for this echo before transmitting its JSON body.
    header = sock.recv(32)
    if not header:
        raise EOFError("Peer closed the connection")
    if not header.isdigit() or not 0 < int(header) <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Invalid message length (expected 1..8388608 bytes)")
    sock.sendall(header)
    payload = receive_exact(sock, int(header))
    return json.loads(payload.decode("utf-8"))


def validate_llm_request(message) -> None:
    if not isinstance(message, dict):
        raise ValueError("Request must be a JSON object")
    if not isinstance(message.get("robot_id"), str) or not message["robot_id"].strip():
        raise ValueError("robot_id must be a nonempty string")
    if message.get("command") != ["LLM"]:
        raise ValueError('This example receiver supports only command: ["LLM"]')
    if not isinstance(message.get("parameters"), dict):
        raise ValueError("parameters must be a JSON object; use {} for LLM")
    if not isinstance(message.get("content"), str) or not message["content"].strip():
        raise ValueError("content must be a nonempty prompt string")


def request(host: str, port: int, command: str, parameters: dict, content: str,
            robot_id: str = socket.gethostname(), timeout: float = 300) -> dict:
    """Send one request (LLM, STT, TTS, FACE, EMOTION, ...) and return the full response.

    Raises OSError/EOFError on connection problems and ValueError on a bad or ERR response.
    """
    message = dict(robot_id=robot_id, request_id=str(uuid.uuid4()), command=[command],
                   parameters=parameters, content=content)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        send_json(sock, message)
        response = receive_json(sock)
    if not isinstance(response, dict):
        raise ValueError("Response must be a JSON object")
    for key in ("robot_id", "request_id"):
        if response.get(key) != message[key]:
            raise ValueError(f"Response {key} does not match the request")
    if response.get("command") == ["ERR"]:
        raise ValueError(f"Server error: {response.get('content')}")
    if response.get("command") != [] or not isinstance(response.get("parameters", {}), dict):
        raise ValueError(f"Server returned an incomplete {command} response")
    return response


def ask(host: str, port: int, prompt: str, robot_id: str = socket.gethostname(),
        timeout: float = 300) -> str:
    """Send one LLM request and return the reply text."""
    validate_llm_request(dict(robot_id=robot_id, command=["LLM"], parameters={}, content=prompt))
    content = request(host, port, "LLM", {}, prompt, robot_id, timeout).get("content")
    if not isinstance(content, str):
        raise ValueError("Expected a text content response")
    return content


ASCII_ENDINGS = ".!?"
CJK_ENDINGS = "。！？"
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e"}


class LLMError(RuntimeError):
    """Raised when the language model server cannot be reached or reports an error."""


class SentenceSplitter:
    """Turns a stream of text fragments into complete sentences.

    A sentence ends at ., ! or ? followed by whitespace, at a CJK full stop, or
    at a newline. A period is not treated as an ending when it follows a common
    abbreviation ("Dr.") or a list number ("1."), and "3.14" never splits.
    The last sentence is only released by flush(), because a trailing "." cannot
    be confirmed until the next character arrives.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, fragment: str) -> list[str]:
        self._buf += fragment
        sentences: list[str] = []
        while (end := self._find_end()) is not None:
            sentence, self._buf = self._buf[:end].strip(), self._buf[end:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _find_end(self) -> int | None:
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch == "\n" or ch in CJK_ENDINGS:
                return i + 1
            if ch in ASCII_ENDINGS:
                nxt = buf[i + 1] if i + 1 < len(buf) else None
                if nxt is None or not nxt.isspace():
                    continue
                if ch == "." and self._is_abbreviation(buf[:i]):
                    continue
                return i + 1
        return None

    @staticmethod
    def _is_abbreviation(prefix: str) -> bool:
        if prefix.strip().isdigit():
            return True
        words = prefix.split()
        return bool(words) and words[-1].lower() in ABBREVIATIONS


class LLMClient:
    def __init__(self, host: str, port: int = 7898, timeout: float = 300.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def check(self) -> None:
        """Open and close a connection to confirm the server is listening."""
        try:
            socket.create_connection((self.host, self.port), timeout=5).close()
        except OSError as exc:
            raise LLMError(self._unreachable_message(exc)) from exc

    def chat(self, messages: list[dict]) -> str:
        """Send a conversation and return the reply text."""
        try:
            return ask(self.host, self.port, to_prompt(messages), timeout=self.timeout)
        except (OSError, EOFError) as exc:
            raise LLMError(self._unreachable_message(exc)) from exc
        except ValueError as exc:
            raise LLMError(str(exc)) from exc

    def _unreachable_message(self, exc: Exception) -> str:
        return (
            f"Could not reach the AI server at {self.host}:{self.port} ({exc.__class__.__name__}). "
            "Check its IP address, that the receiver runs with --host 0.0.0.0, "
            "and that the port is allowed through its firewall."
        )


def to_prompt(messages: list[dict]) -> str:
    # ponytail: the server takes one prompt string, so system prompt and history are
    # flattened into it. Send the messages list instead once the server accepts one.
    lines = [f"{m['role'].capitalize()}: {m['content']}" for m in messages]
    return "\n\n".join(lines + ["Assistant:"])


def split_sentences(text: str) -> list[str]:
    s = SentenceSplitter()
    return s.feed(text) + s.flush()


def run_sender(args) -> int:
    try:
        print("LLM reply:", ask(args.host, args.port, args.prompt, args.robot_id, args.timeout))
        return 0
    except (OSError, EOFError, ValueError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


def make_response(request, generate):
    # Preserve caller fields, including the optional request_id, like the orchestrator.
    response = dict(request) if isinstance(request, dict) else {"robot_id": None}
    try:
        validate_llm_request(request)
        reply = generate(request["content"])
        if not isinstance(reply, str):
            raise ValueError("LLM did not return text")
        response.update(command=[], content=reply)
    except Exception as exc:
        response.update(command=["ERR"], mode="ERR", parameters={}, content=str(exc))
    return response


def serve(host, port, timeout, generate, once=False):
    # This example serializes clients and inference. One socket carries both directions.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(5)
        print(f"Listening on {host}:{server.getsockname()[1]}", flush=True)
        while True:
            conn, address = server.accept()
            with conn:
                conn.settimeout(timeout)
                print(f"Connected: {address}", flush=True)
                while True:
                    try:
                        try:
                            request = receive_json(conn)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            response = {"robot_id": None, "command": ["ERR"], "mode": "ERR",
                                        "parameters": {}, "content": f"Invalid JSON: {exc}"}
                        else:
                            print("Received request:", json.dumps(request, ensure_ascii=False), flush=True)
                            response = make_response(request, generate)
                        send_json(conn, response)
                        print("Sent response:", json.dumps(response, ensure_ascii=False), flush=True)
                        if once:
                            return
                    except (OSError, EOFError, ProtocolError) as exc:
                        print(f"Connection ended: {exc}", flush=True)
                        break


def run_receiver(args) -> int:
    llm = None
    try:
        if args.mock:
            print("MOCK mode: responses are echoes, not model inference.", flush=True)
            generate = lambda prompt: f"[MOCK] Received: {prompt}"
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from llm_server import LLM
            print(f"Loading local model: {args.model}", flush=True)
            llm = LLM(model=args.model, stream=False, token_limit=args.max_tokens)
            generate = llm.prompt
        serve(args.host, args.port, args.timeout, generate, once=args.once)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Receiver failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if llm is not None:
            llm.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    sender = modes.add_parser("send", help="Send a prompt and print the LLM reply")
    receiver = modes.add_parser("receive", help="Receive requests and run the local LLM")
    for mode in (sender, receiver):
        mode.add_argument("--host", default="127.0.0.1",
                          help="Server address; receive mode can bind 0.0.0.0 for LAN clients")
        mode.add_argument("--port", type=int, default=7898)
        mode.add_argument("--timeout", type=float, default=300, help="Socket timeout in seconds")
    sender.add_argument("--robot-id", default=socket.gethostname())
    sender.add_argument("--prompt", default="Say hello in one short sentence.")
    receiver.add_argument("--model", default="qwen2-1_5b-instruct-q6_k",
                          help="Directory/file stem under models/llm")
    receiver.add_argument("--max-tokens", type=int, default=128)
    receiver.add_argument("--mock", action="store_true",
                          help="Return an echo without importing model libraries")
    receiver.add_argument("--once", action="store_true", help="Exit after sending one response")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.mode == "receive" and args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    return run_sender(args) if args.mode == "send" else run_receiver(args)


if __name__ == "__main__":
    raise SystemExit(main())
