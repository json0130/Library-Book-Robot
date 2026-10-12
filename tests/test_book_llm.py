import asyncio
import unittest

from robot.config import ROOT
from robot.services.book_llm import (GROUNDING, HISTORY_EXCHANGES, BookAssistant, clean, explain_request,
                                     mentions_any, recommend_request)
from robot.services.books import Catalog
from robot.services.llm import LIBRARY_SYSTEM_PROMPT, LLMError

CATALOG = Catalog.load(ROOT / "data" / "catalog.csv")
MARTIAN = CATALOG.lookup("9780553418026")
CANDIDATES = CATALOG.similar(MARTIAN, 3)


class FakeClient:
    def __init__(self, replies=None, error=None):
        self.replies, self.error, self.sent = list(replies or []), error, []

    def chat(self, messages):
        self.sent.append([dict(m) for m in messages])
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else "ok"


def run(coro):
    return asyncio.run(coro)


class PromptTest(unittest.TestCase):
    def test_explain_prompt_carries_the_catalogue_record_only(self):
        text = explain_request(MARTIAN)
        for part in ("Title: The Martian", "Author: Andy Weir", "Genre: science fiction", "Topics: ", "Summary: An astronaut"):
            self.assertIn(part, text)
        self.assertNotIn("Level 2", text)                      # shelf and slot are never the model's business

    def test_recommend_prompt_lists_the_candidates(self):
        text = recommend_request(MARTIAN, CANDIDATES)
        for c in CANDIDATES:
            self.assertIn(f"Title: {c.title}", text)
        self.assertIn("choose only from these", text)

    def test_system_prompt_is_the_library_robot_plus_grounding(self):
        system = BookAssistant(FakeClient()).system
        self.assertTrue(system.startswith(LIBRARY_SYSTEM_PROMPT))
        self.assertTrue(system.endswith(GROUNDING))
        self.assertIn("Do not add plot details", GROUNDING)

    def test_clean_makes_one_short_spoken_paragraph(self):
        self.assertEqual(clean("Assistant: **Dune** is long.\n\nIt has *worms*. Spice! Sand. More."),
                         "Dune is long. It has worms. Spice!")
        self.assertEqual(clean("   \n "), "")

    def test_mentions_any(self):
        titles = [c.title for c in CANDIDATES]
        self.assertTrue(mentions_any(f"you may like {titles[0].upper()}", CANDIDATES))
        self.assertFalse(mentions_any("read War and Peace", CANDIDATES))


class AssistantTest(unittest.TestCase):
    def test_explain_returns_the_models_cleaned_answer(self):
        client = FakeClient(["**The Martian** is about survival on Mars."])
        self.assertEqual(run(BookAssistant(client).explain(MARTIAN)), "The Martian is about survival on Mars.")
        system, user = client.sent[0][0], client.sent[0][-1]
        self.assertEqual(system["role"], "system")
        self.assertIn("Summary: An astronaut", user["content"])

    def test_recommend_must_name_a_candidate(self):
        good = f"I'd suggest {CANDIDATES[0].title}, it is similar."
        self.assertEqual(run(BookAssistant(FakeClient([good])).recommend(MARTIAN, CANDIDATES)), good)
        with self.assertLogs("robot.book_llm", level="WARNING"):
            self.assertIsNone(run(BookAssistant(FakeClient(["Try War and Peace."])).recommend(MARTIAN, CANDIDATES)))
        client = FakeClient(["x"])
        self.assertIsNone(run(BookAssistant(client).recommend(MARTIAN, [])))             # nothing to choose from
        self.assertEqual(client.sent, [])                                                 # and no request is made

    def test_server_trouble_returns_none_and_logs(self):
        for client in (FakeClient(error=LLMError("down")), FakeClient([""]), FakeClient(["  \n "])):
            with self.assertLogs("robot.book_llm", level="WARNING"):
                self.assertIsNone(run(BookAssistant(client).explain(MARTIAN)))

    def test_history_is_kept_capped_and_reset(self):
        assistant = BookAssistant(FakeClient())
        run(assistant.explain(MARTIAN))
        run(assistant.recommend(MARTIAN, CANDIDATES))
        self.assertEqual([m["role"] for m in assistant.history], ["user", "assistant", "user", "assistant"])
        sent = assistant.client.sent[-1]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "user"])   # the earlier exchange
        for _ in range(HISTORY_EXCHANGES + 5):
            run(assistant.explain(MARTIAN))
        self.assertEqual(len(assistant.history), 2 * HISTORY_EXCHANGES)
        assistant.reset()
        self.assertEqual(assistant.history, [])

    def test_failed_calls_are_not_added_to_the_history(self):
        assistant = BookAssistant(FakeClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(assistant.explain(MARTIAN))
        self.assertEqual(assistant.history, [])


if __name__ == "__main__":
    unittest.main()
