"""Robot-side LLM client: sends the chat to the AI server and splits the reply into sentences.

The wire protocol and request() are the AI server's (vendor/ai_server/llm.py); this module
only builds the prompt, wraps errors and cuts the reply into sentences for speech and the face.
"""

from __future__ import annotations

import socket

from vendor.ai_server.llm import ask

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


