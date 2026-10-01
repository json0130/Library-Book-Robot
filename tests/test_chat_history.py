import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from unittest import mock

from features.chat import DEFAULT_SYSTEM, HISTORY_EXCHANGES, Face, run_turn
from modules.llm import LLMError, to_prompt


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def chat(self, messages):
        self.sent.append([dict(m) for m in messages])
        if self.fail:
            raise LLMError("down")
        return f"Reply {len(self.sent)}."


def turn(client, history, text):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return run_turn(client, DEFAULT_SYSTEM, history, text, on_sentence=lambda s: None)


class ChatHistoryTest(unittest.TestCase):
    def test_system_prompt_and_history_are_sent(self):
        client, history = FakeClient(), []
        turn(client, history, "Hi, I'm Sam.")
        turn(client, history, "What's my name?")
        sent = client.sent[-1]
        self.assertEqual(sent[0], {"role": "system", "content": DEFAULT_SYSTEM})
        self.assertIn("library", DEFAULT_SYSTEM.lower())
        self.assertEqual([m["content"] for m in sent[1:]], ["Hi, I'm Sam.", "Reply 1.", "What's my name?"])
        prompt = to_prompt(sent)
        self.assertTrue(prompt.startswith("System: "))
        self.assertIn("User: Hi, I'm Sam.", prompt)

    def test_keeps_last_ten_exchanges(self):
        client, history = FakeClient(), []
        for i in range(15):
            turn(client, history, f"question {i}")
        self.assertEqual(HISTORY_EXCHANGES, 10)
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0], {"role": "user", "content": "question 5"})
        turn(client, history, "question 15")
        sent = client.sent[-1]
        self.assertEqual(len(sent), 1 + 20 + 1)          # system + 10 past exchanges + the new question
        self.assertEqual(sent[1]["content"], "question 5")

    def test_failed_turn_leaves_history_unchanged(self):
        history = []
        turn(FakeClient(), history, "hello")
        before = list(history)
        self.assertFalse(turn(FakeClient(fail=True), history, "lost"))
        self.assertEqual(history, before)


class FaceTest(unittest.TestCase):
    def test_reading_time_and_quiet_when_display_is_down(self):
        sent = []
        with mock.patch("features.chat.talk", side_effect=lambda **kw: sent.append(kw) or False):
            face = Face(port=1)
            out = io.StringIO()
            with redirect_stdout(out):
                face.talk_text("x" * 28)               # 2 s at 14 characters per second
                face.talk_text("again")                # display down: waits before retrying
        self.assertEqual(len(sent), 1)
        self.assertAlmostEqual(sent[0]["seconds"], 2.0)
        self.assertEqual(out.getvalue().count("face display not running"), 1)

    def test_disabled(self):
        with mock.patch("features.chat.talk") as t:
            Face(port=1, enabled=False).talk_text("hello")
        t.assert_not_called()


if __name__ == "__main__":
    unittest.main()
