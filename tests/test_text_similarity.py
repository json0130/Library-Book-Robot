import unittest

from robot.services.book_graph import BookGraph
from robot.services.books import Book, Catalog
from robot.services.text_similarity import Embedder, TfidfEmbedder, stem, tokens, words_of


class TokenTest(unittest.TestCase):
    def test_stop_words_short_words_and_case_go(self):
        self.assertEqual(words_of("The Martian is a story about an astronaut"), ["martian", "story", "astronaut"])
        self.assertEqual(words_of("It's on to go"), [])                              # nothing left but stop words
        self.assertEqual(tokens(""), [])
        self.assertEqual(tokens(None), [])

    def test_possessives_lose_their_s(self):
        self.assertEqual(words_of("A young noble's family; it's the Emperor's"), ["young", "noble", "family", "emperor"])

    def test_plurals_match_but_words_ending_in_s_survive(self):
        self.assertEqual(stem("robots"), "robot")
        self.assertEqual(tokens("Robots robot"), ["robot", "robot"])
        for word in ("cosmos", "class", "focus", "analysis", "mars"[:3]):
            self.assertEqual(stem(word), word)


class TfidfTest(unittest.TestCase):
    def fit(self, docs):
        embedder = TfidfEmbedder()
        embedder.fit(docs)
        return embedder

    def test_identical_texts_are_1_and_disjoint_texts_0(self):
        e = self.fit({"a": "survival on mars with potatoes", "b": "survival on mars with potatoes",
                      "c": "romance in the jazz age"})
        self.assertAlmostEqual(e.similarity("a", "b"), 1.0)
        self.assertEqual(e.similarity("a", "c"), 0.0)

    def test_symmetric_and_within_0_and_1(self):
        e = self.fit({"a": "space survival mars", "b": "space exploration mars rover", "c": "cooking pasta"})
        for x, y in (("a", "b"), ("a", "c"), ("b", "c")):
            self.assertEqual(e.similarity(x, y), e.similarity(y, x))
            self.assertTrue(0.0 <= e.similarity(x, y) <= 1.0)

    def test_a_rare_shared_word_counts_for_more_than_a_common_one(self):
        # "story" is in every text, "volcano" in two: sharing the rare word is the stronger link
        e = self.fit({"a": "story volcano", "b": "story ocean", "c": "story volcano", "d": "story forest", "e": "story desert"})
        self.assertGreater(e.similarity("a", "c"), e.similarity("a", "b"))

    def test_unknown_isbns_and_empty_texts_are_0(self):
        e = self.fit({"a": "mars", "b": "", "c": "the of and"})
        self.assertEqual(e.similarity("a", "nope"), 0.0)
        self.assertEqual(e.similarity("b", "a"), 0.0)
        self.assertEqual(e.similarity("c", "a"), 0.0)
        self.assertEqual(TfidfEmbedder().similarity("a", "b"), 0.0)                  # not fitted at all

    def test_shared_terms_are_real_words_ranked_by_weight(self):
        e = self.fit({"a": "cosmos astronomers history probes", "b": "cosmos history physics", "c": "cooking"})
        terms = e.shared_terms("a", "b", 3)
        self.assertEqual(set(terms), {"cosmos", "history"})                          # "cosmos", not the stem "cosmo"
        self.assertEqual(e.shared_terms("a", "c"), [])
        self.assertEqual(e.shared_terms("a", "missing"), [])

    def test_refitting_replaces_what_was_learnt(self):
        e = self.fit({"a": "mars", "b": "mars"})
        self.assertAlmostEqual(e.similarity("a", "b"), 1.0)
        e.fit({"a": "mars", "b": "venus"})
        self.assertEqual(e.similarity("a", "b"), 0.0)


class InterfaceTest(unittest.TestCase):
    def test_the_base_class_must_be_implemented(self):
        with self.assertRaises(NotImplementedError):
            Embedder().fit({})
        with self.assertRaises(NotImplementedError):
            Embedder().similarity("a", "b")
        self.assertEqual(Embedder().shared_terms("a", "b"), [])

    def test_a_better_model_can_be_plugged_into_the_graph(self):
        class Constant(Embedder):
            fitted = None

            def fit(self, documents):
                self.fitted = dict(documents)

            def similarity(self, a, b):
                return 0.5

            def shared_terms(self, a, b, n=3):
                return ["vectors"]
        books = [Book("9780000000002", "Alpha", "A", "sf", ("space",), "Alpha summary"),
                 Book("9780000000019", "Bravo", "B", "sf", ("space",), "Bravo summary")]
        embedder = Constant()
        graph = BookGraph(Catalog(books), embedder=embedder)
        self.assertIn("Alpha summary", embedder.fitted["9780000000002"])            # summary + subjects are what it learns from
        self.assertIn("space", embedder.fitted["9780000000002"])
        text = graph.score("9780000000002", "9780000000019").component("text")
        self.assertEqual(text.value, 0.5)
        self.assertEqual(text.reason, "similar descriptions (both mention vectors)")


if __name__ == "__main__":
    unittest.main()
