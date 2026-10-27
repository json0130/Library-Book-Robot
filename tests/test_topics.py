import tempfile
import unittest
from pathlib import Path

from robot.config import ROOT
from robot.services.books import Catalog, CatalogError
from robot.services.topics import TopicVocabulary, norm_key

VOCAB = {
    "science": {"aliases": ["sciences"]},
    "astronomy": {"broader": "science", "aliases": ["space", "cosmos", "Outer-Space"]},
    "physics": {"broader": "science"},
    "fiction": None,
    "science fiction": {"broader": "fiction", "aliases": ["sci-fi"]},
    "dystopia": {"broader": "science fiction"},
    "humour": {"aliases": ["comedy", "humor"]},
}


class NormaliseKeyTest(unittest.TestCase):
    def test_case_hyphens_underscores_and_spaces(self):
        self.assertEqual(norm_key("Popular-Science"), "popular science")
        self.assertEqual(norm_key("  popular   _science "), "popular science")
        self.assertEqual(norm_key("Sci/Fi"), "sci fi")
        self.assertEqual(norm_key(None), "")
        self.assertEqual(norm_key("humour."), "humour")


class CanonicalTest(unittest.TestCase):
    def setUp(self):
        self.vocab = TopicVocabulary(VOCAB)

    def test_aliases_fold_into_the_topic(self):
        self.assertEqual(self.vocab.canonical("cosmos"), "astronomy")
        self.assertEqual(self.vocab.canonical("Space"), "astronomy")
        self.assertEqual(self.vocab.canonical("outer space"), "astronomy")          # "Outer-Space" in the file
        self.assertEqual(self.vocab.canonical("OUTER_SPACE"), "astronomy")
        self.assertEqual(self.vocab.canonical("sci-fi"), "science fiction")
        self.assertEqual(self.vocab.canonical("Comedy"), "humour")

    def test_a_topic_is_its_own_canonical_name(self):
        self.assertEqual(self.vocab.canonical("astronomy"), "astronomy")
        self.assertEqual(self.vocab.canonical("Science Fiction"), "science fiction")

    def test_unknown_is_none(self):
        self.assertIsNone(self.vocab.canonical("zeitgeist"))
        self.assertIsNone(self.vocab.canonical(""))

    def test_membership_and_listing(self):
        self.assertIn("Cosmos".lower(), self.vocab.words)
        self.assertIn("astronomy", self.vocab)
        self.assertNotIn("zeitgeist", self.vocab)
        self.assertEqual(len(self.vocab), 7)
        self.assertEqual(self.vocab.topics[0], "astronomy")


class BroaderTest(unittest.TestCase):
    def setUp(self):
        self.vocab = TopicVocabulary(VOCAB)

    def test_ancestors_nearest_first(self):
        self.assertEqual(self.vocab.ancestors("astronomy"), ("science",))
        self.assertEqual(self.vocab.ancestors("dystopia"), ("science fiction", "fiction"))
        self.assertEqual(self.vocab.ancestors("science"), ())
        self.assertEqual(self.vocab.ancestors("not a topic"), ())

    def test_several_broader_topics_as_a_list(self):
        vocab = TopicVocabulary({"a": None, "b": None, "c": {"broader": ["a", "b"]}, "d": {"broader": "c"}})
        self.assertEqual(vocab.ancestors("c"), ("a", "b"))
        self.assertEqual(vocab.ancestors("d"), ("c", "a", "b"))
        self.assertEqual(vocab.problems, [])


class NormaliseSubjectsTest(unittest.TestCase):
    def setUp(self):
        self.vocab = TopicVocabulary(VOCAB)

    def test_synonyms_collapse_and_order_is_kept(self):
        topics, unknown = self.vocab.normalise(["Space", "comedy", "cosmos", "Sci-Fi"])
        self.assertEqual(topics, ("astronomy", "humour", "science fiction"))
        self.assertEqual(unknown, ())

    def test_unknown_subjects_are_kept_and_reported(self):
        topics, unknown = self.vocab.normalise(["space", "Zeitgeist", "zeit-geist", "  ", ""])
        self.assertEqual(topics, ("astronomy", "zeitgeist", "zeit geist"))
        self.assertEqual(unknown, ("zeitgeist", "zeit geist"))

    def test_an_empty_vocabulary_keeps_everything_as_unknown(self):
        topics, unknown = TopicVocabulary().normalise(["Space", "Comedy"])
        self.assertEqual((topics, unknown), (("space", "comedy"), ("space", "comedy")))


class ProblemsTest(unittest.TestCase):
    def problems(self, data):
        return TopicVocabulary(data, source="t.yaml").problems

    def test_unknown_broader(self):
        (problem,) = self.problems({"a": {"broader": "nothing"}})
        self.assertIn("t.yaml", problem)
        self.assertIn("'nothing'", problem)

    def test_alias_claimed_twice(self):
        problems = self.problems({"a": {"aliases": ["x"]}, "b": {"aliases": ["X"]}})
        self.assertEqual(len(problems), 1)
        self.assertIn("'x' is used for both 'a' and 'b'", problems[0])

    def test_alias_equal_to_another_topic(self):
        self.assertIn("both", self.problems({"a": {"aliases": ["b"]}, "b": None})[0])

    def test_a_loop(self):
        vocab = TopicVocabulary({"a": {"broader": "b"}, "b": {"broader": "c"}, "c": {"broader": "a"}}, source="t.yaml")
        self.assertTrue(any("loop" in p for p in vocab.problems))
        self.assertEqual(vocab.ancestors("a")[-1] in ("a", "b", "c"), True)         # still terminates

    def test_own_broader_topic_and_bad_shapes(self):
        self.assertIn("its own broader", self.problems({"a": {"broader": "a"}})[0])
        self.assertIn("must be a mapping", self.problems(["a", "b"])[0])
        self.assertIn("must have aliases", self.problems({"a": "oops"})[0])
        self.assertIn("listed twice", self.problems({"Science": None, "science": None})[0])

    def test_a_clean_file_has_no_problems(self):
        self.assertEqual(TopicVocabulary(VOCAB).problems, [])


class LoadTest(unittest.TestCase):
    def test_the_sample_vocabulary_loads_without_problems(self):
        vocab = TopicVocabulary.load(ROOT / "data" / "topics.yaml")
        self.assertEqual(vocab.problems, [])
        self.assertEqual(vocab.canonical("space"), "astronomy")
        self.assertEqual(vocab.ancestors("astronomy"), ("science",))                # the example in the brief

    def test_every_subject_in_the_sample_catalogue_is_known(self):
        vocab = TopicVocabulary.load(ROOT / "data" / "topics.yaml")
        for book in Catalog.load(ROOT / "data" / "catalog.csv").books:
            _, unknown = vocab.normalise([book.genre, *book.subjects])
            self.assertEqual(unknown, (), book.title)

    def test_missing_and_broken_files_raise_catalog_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(CatalogError, "not found"):
                TopicVocabulary.load(Path(d) / "nope.yaml")
            bad = Path(d) / "bad.yaml"
            bad.write_text("a: [unclosed\n", encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "bad.yaml"):
                TopicVocabulary.load(bad)
            loop = Path(d) / "loop.yaml"
            loop.write_text("a:\n  broader: b\nb:\n  broader: a\nc:\n  broader: ghost\n", encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, r"loop.yaml.*\(and 1 more\)"):
                TopicVocabulary.load(loop)

    def test_an_empty_file_is_an_empty_vocabulary(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "empty.yaml"
            empty.write_text("# nothing yet\n", encoding="utf-8")
            self.assertEqual(len(TopicVocabulary.load(empty)), 0)


if __name__ == "__main__":
    unittest.main()
