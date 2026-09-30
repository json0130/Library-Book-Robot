import unittest

from modules.llm import SentenceSplitter


def split(fragments: list[str]) -> list[str]:
    s = SentenceSplitter()
    out: list[str] = []
    for f in fragments:
        out += s.feed(f)
    return out + s.flush()


class SentenceSplitterTest(unittest.TestCase):
    def test_basic_split_across_fragments(self):
        self.assertEqual(split(["Hello the", "re. How are", " you? Fine."]),
                         ["Hello there.", "How are you?", "Fine."])

    def test_sentence_released_only_when_confirmed(self):
        s = SentenceSplitter()
        self.assertEqual(s.feed("First one."), [])           # "." at end could be "3.14"
        self.assertEqual(s.feed(" Second"), ["First one."])  # whitespace after confirms it
        self.assertEqual(s.flush(), ["Second"])

    def test_decimals_and_abbreviations_do_not_split(self):
        self.assertEqual(split(["It scores 4.5 stars. Dr. Smith agrees."]),
                         ["It scores 4.5 stars.", "Dr. Smith agrees."])

    def test_list_numbers_do_not_split(self):
        self.assertEqual(split(["1. Dune\n2. Emma\n"]), ["1. Dune", "2. Emma"])

    def test_newline_splits(self):
        self.assertEqual(split(["Line one\nLine two"]), ["Line one", "Line two"])

    def test_cjk_endings(self):
        self.assertEqual(split(["안녕하세요。 반갑습니다！"]), ["안녕하세요。", "반갑습니다！"])

    def test_flush_returns_unfinished_text(self):
        self.assertEqual(split(["No ending here"]), ["No ending here"])

    def test_empty(self):
        self.assertEqual(split([]), [])


if __name__ == "__main__":
    unittest.main()
