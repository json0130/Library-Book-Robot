"""Robot-side LLM client: sends the chat to the AI server and splits the reply into sentences.

The wire protocol and request() are the AI server's (vendor/ai_server/llm.py); this module
only builds the prompt, wraps errors and cuts the reply into sentences for speech and the face.
"""

from __future__ import annotations

import logging
import socket
import time
import uuid

from robot.persona import NAME
from vendor.ai_server.llm import ask, receive_json, send_json, validate_llm_request

log = logging.getLogger("robot.llm")

# Who the model is. Shared by the chat test tool and the book dialogue (robot/services/book_llm.py).
LIBRARY_SYSTEM_PROMPT = (
    f"You are {NAME}, the Library Book Robot: a warm, upbeat, energetic robot with the easy manner of a "
    "friendly young man in his twenties who helps out at a university library. "
    f"Your name is {NAME}. You help people find, return and choose books: you explain what a book is "
    "about, recommend similar titles, and point people to the help desk. "
    "You can chat naturally about everyday things too, like how someone's day is going or studying, and "
    "bring the conversation back to books only when it fits. "
    "Read the conversation so far and carry on from it: answer what the visitor just said, do not greet "
    "them or introduce yourself again once you have, and do not end every reply with an offer of help. "
    "If you are not sure a book exists or the library holds it, say so and suggest the help desk; never "
    "invent titles, authors or shelf locations. "
    "Your replies are spoken aloud: one to three short, plain sentences, with no markdown, lists or emoji."
)

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


class _FirstByte:
    """A socket that notes when the first byte of the answer arrives (everything else is passed through)."""

    def __init__(self, sock):
        self._sock, self.first = sock, None

    def recv(self, size):
        data = self._sock.recv(size)
        if data and self.first is None:
            self.first = time.perf_counter()
        return data

    def __getattr__(self, name):
        return getattr(self._sock, name)


def timed_ask(host: str, port: int, prompt: str, timeout: float = 300.0) -> tuple:
    """(reply text, {"first_byte_ms", "total_ms", "bytes"}): the AI server's `ask` with the time to the first byte of the
    answer measured. The protocol sends the whole reply as one message after the model has finished (nothing arrives while
    it generates), so first byte and total are close: this measures that rather than assuming it."""
    message = dict(robot_id=socket.gethostname(), request_id=str(uuid.uuid4()), command=["LLM"], parameters={}, content=prompt)
    validate_llm_request(message)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        send_json(sock, message)
        sent = time.perf_counter()
        probe = _FirstByte(sock)
        response = receive_json(probe)
        done = time.perf_counter()
    for key in ("robot_id", "request_id"):
        if response.get(key) != message[key]:
            raise ValueError(f"Response {key} does not match the request")
    if response.get("command") == ["ERR"]:
        raise ValueError(f"Server error: {response.get('content')}")
    content = response.get("content")
    if response.get("command") != [] or not isinstance(content, str):
        raise ValueError("Server returned an incomplete LLM response")
    first = probe.first if probe.first is not None else done
    return content, {"first_byte_ms": round((first - sent) * 1000), "total_ms": round((done - sent) * 1000),
                     "bytes": len(content.encode("utf-8"))}


class LLMClient:
    def __init__(self, host: str, port: int = 7898, timeout: float = 300.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_timing: dict = {}                   # first_byte_ms / total_ms of the latest chat(), for the logs

    def check(self) -> None:
        """Open and close a connection to confirm the server is listening."""
        try:
            socket.create_connection((self.host, self.port), timeout=5).close()
        except OSError as exc:
            raise LLMError(self._unreachable_message(exc)) from exc

    def chat(self, messages: list[dict]) -> str:
        """Send a conversation and return the reply text."""
        try:
            reply, self.last_timing = timed_ask(self.host, self.port, to_prompt(messages), self.timeout)
            log.info("[llm] first byte after %d ms, whole reply after %d ms (%d bytes, sent as one message: not streamed)",
                     self.last_timing["first_byte_ms"], self.last_timing["total_ms"], self.last_timing["bytes"])
            return reply
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


