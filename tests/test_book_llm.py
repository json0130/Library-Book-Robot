import asyncio
import unittest

from robot.config import ROOT
from robot.services.book_llm import (GROUNDING, HISTORY_EXCHANGES, BookAssistant, clean, explain_request,
                                     mentions_any, recommend_request, why_text)
from robot.services.books import Catalog
from robot.services.llm import LIBRARY_SYSTEM_PROMPT, LLMError
from tests.helpers.sample import sample_graph

CATALOG = Catalog.load(ROOT / "data" / "catalog.csv")
MARTIAN = CATALOG.lookup("9780553418026")
CANDIDATES = sample_graph().recommend(MARTIAN.isbn13, 3)        # Recommendation objects: book, score, reasons


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

    def test_recommend_prompt_lists_the_candidates_with_the_graphs_reasons(self):
        text = recommend_request(MARTIAN, CANDIDATES)
        for c in CANDIDATES:
            self.assertIn(f"Title: {c.book.title}", text)
            self.assertIn("Why it fits: " + "; ".join(c.reasons), text)             # the FULL list
        self.assertIn("choose only from these", text)
        self.assertIn("both about", text)                           # a real reason from the graph, not boilerplate
        self.assertNotRegex(text, r"\b[01]\.\d")                       # the model gets reasons, never a numeric score

    def test_each_candidate_also_carries_the_one_reason_to_speak_and_the_model_is_told_to_use_one(self):
        text = recommend_request(MARTIAN, CANDIDATES)
        for c in CANDIDATES:
            self.assertIn(f"Best single reason to give: {c.because}", text)
        self.assertIn("ONE reason for each", text)
        self.assertIn("Do not list several topics", text)
        self.assertIn("do not mention descriptions", text)
        self.assertEqual(text.count("Best single reason to give:"), len(CANDIDATES))
        self.assertEqual(text.count("Why it fits:"), len(CANDIDATES))                       # the full list is still there

    def test_a_candidate_with_no_spoken_reason_gets_a_plain_one(self):
        class Rec:
            book = MARTIAN
            reasons = ["x"]
            because = ""
        self.assertIn("Best single reason to give: it is similar", recommend_request(MARTIAN, [Rec]))

    def test_the_model_gets_the_full_reason_list_never_empty(self):
        class Rec:
            book = MARTIAN
            reasons = ["a", "b", "c", "d", "e"]
        self.assertEqual(why_text(Rec), "a; b; c; d; e")
        Rec.reasons = []
        self.assertEqual(why_text(Rec), "similar subjects")

    def test_no_raw_call_number_reaches_the_model(self):
        for rec in sample_graph().recommend("9780345539434", 5) + sample_graph().recommend("9780553294385", 5):
            text = why_text(rec)
            for book in sample_graph().catalog.books:
                if book.call_number:
                    self.assertNotIn(book.call_number, text)
        prompt = recommend_request(CATALOG.lookup("9780345539434"), sample_graph().recommend("9780345539434", 3))
        self.assertNotIn("QB", prompt)
        self.assertNotIn("S24", prompt)

    def test_system_prompt_is_the_library_robot_plus_grounding(self):
        system = BookAssistant(FakeClient()).system
        self.assertTrue(system.startswith(LIBRARY_SYSTEM_PROMPT))
        self.assertTrue(system.endswith(GROUNDING))
        self.assertIn("Do not add plot details", GROUNDING)

    def test_clean_makes_one_short_spoken_paragraph(self):
        self.assertEqual(clean("Assistant: **Dune** is long.\n\nIt has *worms*. Spice! Sand. More."),
                         "Dune is long. It has worms. Spice!")
        self.assertEqual(clean("   \n "), "")

    def test_a_title_may_lose_its_hyphens_and_punctuation_but_not_its_words(self):
        class B:
            def __init__(self, title):
                self.title = title
        orwell, dune, martian = B("Nineteen Eighty-Four"), B("Dune"), B("The Martian")
        for said in ("I'd suggest Nineteen Eighty Four by George Orwell.", "try NINETEEN EIGHTY-FOUR!",
                     "How about \"Nineteen Eighty-Four\"?", "nineteen   eighty\nfour is great"):
            self.assertTrue(mentions_any(said, [orwell]), said)
        self.assertTrue(mentions_any("You might like Dune.", [dune]))
        self.assertTrue(mentions_any("the martian is fun", [martian]))
        for said in ("Nineteen Eighty is a year", "Dunes of sand", "Dunegeon", "Eighty Four Nineteen",
                     "I recommend War and Peace", ""):
            self.assertFalse(mentions_any(said, [orwell, dune, martian]), said)

    def test_a_reply_naming_the_book_without_its_hyphen_is_accepted_and_a_wrong_one_still_discarded(self):
        orwell = CATALOG.find_title("nineteen eighty four")
        cand = sample_graph().recommend(CATALOG.find_title("frankenstein").isbn13, 1)
        self.assertEqual(cand[0].book.title, orwell.title)
        said = "I'd suggest Nineteen Eighty Four by George Orwell, it warns about unchecked power."
        self.assertEqual(run(BookAssistant(FakeClient([said])).recommend(CATALOG.find_title("frankenstein"), cand)), said)
        with self.assertLogs("robot.book_llm", level="WARNING"):
            self.assertIsNone(run(BookAssistant(FakeClient(["I'd suggest War and Peace."])).recommend(
                CATALOG.find_title("frankenstein"), cand)))

    def test_mentions_any(self):
        books = [c.book for c in CANDIDATES]
        self.assertTrue(mentions_any(f"you may like {books[0].title.upper()}", books))
        self.assertFalse(mentions_any("read War and Peace", books))


class AssistantTest(unittest.TestCase):
    def test_explain_returns_the_models_cleaned_answer(self):
        client = FakeClient(["**The Martian** is about survival on Mars."])
        self.assertEqual(run(BookAssistant(client).explain(MARTIAN)), "The Martian is about survival on Mars.")
        system, user = client.sent[0][0], client.sent[0][-1]
        self.assertEqual(system["role"], "system")
        self.assertIn("Summary: An astronaut", user["content"])

    def test_recommend_must_name_a_candidate(self):
        good = f"I'd suggest {CANDIDATES[0].book.title}, it is similar."
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

    def test_history_is_what_was_really_said_capped_and_reset(self):
        assistant = BookAssistant(FakeClient())
        assistant.said("Hello! I'm Stuart.")
        assistant.hear("how are you?")
        run(assistant.chat("how are you?"))
        assistant.said("Great!")                                  # the dialogue records the robot's words
        assistant.hear("tell me about it")
        run(assistant.explain(MARTIAN))
        sent = assistant.client.sent[-1]
        self.assertEqual([m["role"] for m in sent], ["system", "assistant", "user", "assistant", "user"])
        self.assertIn("Summary: An astronaut", sent[-1]["content"])    # the catalogue request stands in for the last turn
        self.assertEqual(sent[1]["content"], "Hello! I'm Stuart.")
        for i in range(HISTORY_EXCHANGES + 5):
            assistant.hear(f"q{i}")
            assistant.said(f"a{i}")
        self.assertEqual(len(assistant.history), 2 * HISTORY_EXCHANGES)
        self.assertEqual(assistant.history[-1]["content"], f"a{HISTORY_EXCHANGES + 4}")
        assistant.reset()
        self.assertEqual(assistant.history, [])

    def test_asking_does_not_change_the_history_by_itself(self):
        assistant = BookAssistant(FakeClient(["It is about Mars."]))
        run(assistant.explain(MARTIAN))
        self.assertEqual(assistant.history, [])                   # the dialogue says what was said
        failing = BookAssistant(FakeClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(failing.explain(MARTIAN))
        self.assertEqual(failing.history, [])


if __name__ == "__main__":
    unittest.main()
