import tempfile
import unittest
from pathlib import Path

from robot.config import ROOT, SIMILARITY_DEFAULTS
from robot.services.book_graph import (COMPONENTS, DEFAULT_WEIGHTS, EDGE_KINDS, NODE_KINDS, BookGraph, CuratedLink,
                                       Location, Shelf, load_links, load_shelves, parse_links, parse_shelves)
from robot.services.books import Book, Catalog, CatalogError
from robot.services.text_similarity import Embedder
from robot.services.topics import TopicVocabulary
from tests.helpers.ean13 import check_digit
from tests.helpers.sample import sample_graph

VOCAB = TopicVocabulary({
    "science": None,
    "astronomy": {"broader": "science", "aliases": ["space"]},
    "physics": {"broader": "science"},
    "fiction": None,
    "volcano": None,
})


def isbn(n: int) -> str:
    body = "978" + f"{n:09d}"
    return body + check_digit(body)


def book(n, title=None, author="", genre="", subjects=(), summary="", **kw):
    return Book(isbn(n), title or f"Book {n}", author, genre, tuple(subjects), summary or None, **kw)


def graph_of(books, vocab=VOCAB, shelves=None, links=(), **kw):
    return BookGraph(Catalog(books), vocab, shelves, links, **kw)


def link(a, b, relation="related", note=""):
    return CuratedLink(isbn(a), isbn(b), relation, note)


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.graph = sample_graph()

    def edges(self, kind):
        return [e for e in self.graph.edges if e.kind == kind]

    def test_only_the_documented_node_and_edge_kinds_and_nothing_about_visitors(self):
        self.assertEqual({n.kind for n in self.graph.nodes.values()} - set(NODE_KINDS), set())
        self.assertEqual({e.kind for e in self.graph.edges} - set(EDGE_KINDS), set())
        self.assertFalse(any(word in n.id.lower() or word in n.kind
                             for n in self.graph.nodes.values() for word in ("person", "visitor", "face", "user")))

    def test_every_edge_joins_two_nodes_that_exist(self):
        for e in self.graph.edges:
            self.assertIn(e.src, self.graph.nodes)
            self.assertIn(e.dst, self.graph.nodes)

    def test_every_book_has_an_author_topics_and_a_shelf(self):
        for book_ in self.graph.catalog.books:
            kinds = {e.kind for e in self.graph.neighbours(f"book:{book_.isbn13}")}
            self.assertTrue({"written_by", "about", "on_shelf", "in_section", "in_class"} <= kinds, book_.title)

    def test_author_edges_split_several_authors(self):
        g = graph_of([book(1, author="Ann Lee; Bo Chan"), book(2, author="bo chan")])
        authors = {e.dst for e in g.edges if e.kind == "written_by"}
        self.assertEqual(authors, {"author:ann lee", "author:bo chan"})
        self.assertEqual(len([e for e in g.edges if e.kind == "written_by"]), 3)

    def test_call_number_classes_form_a_hierarchy(self):
        ids = {n.id for n in self.graph.nodes.values() if n.kind == "class"}
        for needed in ("class:dewey:900", "class:dewey:909", "class:lc:Q", "class:lc:QB", "class:lc:QB44.2", "class:other:F"):
            self.assertIn(needed, ids)
        broader = {(e.src, e.dst) for e in self.edges("broader")}
        self.assertIn(("class:dewey:909", "class:dewey:900"), broader)
        self.assertIn(("class:lc:QB44.2", "class:lc:QB44"), broader)
        self.assertIn(("class:lc:QB", "class:lc:Q"), broader)
        in_class = {e.dst for e in self.edges("in_class")}
        self.assertIn("class:dewey:005.1", in_class)                                  # the deepest class of Clean Code
        self.assertEqual(len(self.edges("broader")), len(set(map(lambda e: (e.src, e.dst), self.edges("broader")))))

    def test_shelves_belong_to_sections_and_books_to_series(self):
        self.assertIn(("shelf:3-sci-a", "section:science"), {(e.src, e.dst) for e in self.edges("in_section")})
        series = [e for e in self.edges("in_series") if e.dst == "series:asimov's future history"]
        self.assertEqual(sorted(e.note for e in series), ["book 1", "book 2"])

    def test_curated_edges_carry_their_relation_and_note(self):
        found = [e for e in self.edges("curated") if e.relation == "librarian_pick"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].src, found[0].dst), ("book:9780345539434", "book:9780553380163"))
        self.assertIn("physics", found[0].note)

    def test_topics_a_book_has_are_topics_and_parents_only_are_broader(self):
        kinds = {n.label: n.kind for n in self.graph.nodes.values() if n.id.startswith("topic:")}
        self.assertEqual(kinds["astronomy"], "topic")
        self.assertEqual(kinds["technology"], "topic")                                # used by Clean Code and a parent
        self.assertEqual(kinds["philosophy"], "broader")                              # only the parent of ethics
        self.assertIn(("topic:ethics", "topic:philosophy"), {(e.src, e.dst) for e in self.edges("broader")})

    def test_neighbours_lists_edges_in_both_directions(self):
        edges = self.graph.neighbours("topic:astronomy")
        self.assertTrue(any(e.kind == "about" for e in edges))                        # book -> topic (incoming)
        self.assertTrue(any(e.kind == "broader" for e in edges))                      # topic -> science (outgoing)
        self.assertEqual(self.graph.neighbours("nothing here"), [])

    def test_topics_are_the_genre_and_subjects_folded_through_the_vocabulary(self):
        cosmos = self.graph.topics_of("9780345539434")
        self.assertEqual(set(cosmos), {"science", "astronomy", "popular science", "history"})
        self.assertEqual(self.graph.topics_of("0000000000000"), ())


class TopicsComponentTest(unittest.TestCase):
    def setUp(self):
        # "fiction" is on 6 books, "volcano" on 2: sharing the rare one must count for more
        self.graph = graph_of([book(1, subjects=["fiction", "volcano"]), book(2, subjects=["volcano", "y"]),
                               book(3, subjects=["fiction", "z"]), book(4, subjects=["fiction"]),
                               book(5, subjects=["fiction"]), book(6, subjects=["fiction"])])

    def topics(self, a, b):
        return self.graph.score(isbn(a), isbn(b)).component("topics")

    def test_idf_rare_topics_are_worth_more_and_always_positive(self):
        g = self.graph
        self.assertGreater(g.idf("volcano"), g.idf("fiction"))
        self.assertGreater(g.idf("fiction"), 0)
        self.assertGreater(g.idf("never seen"), g.idf("volcano"))

    def test_a_rare_shared_topic_beats_a_common_one(self):
        rare, common = self.topics(1, 2), self.topics(1, 3)           # same sizes: share {volcano} vs share {fiction}
        self.assertGreater(rare.value, common.value)
        self.assertEqual(rare.reason, "both about volcano")
        self.assertEqual(common.reason, "both about fiction")

    def test_value_is_between_0_and_1(self):
        for a in range(1, 7):
            for b in range(1, 7):
                if a != b:
                    self.assertTrue(0.0 <= self.topics(a, b).value <= 1.0)

    def test_identical_topics_score_1_and_nothing_shared_scores_0_with_no_reason(self):
        g = graph_of([book(1, subjects=["a", "b"]), book(2, subjects=["b", "a"]), book(3, subjects=["c"])])
        self.assertAlmostEqual(g.score(isbn(1), isbn(2)).component("topics").value, 1.0)
        none = g.score(isbn(1), isbn(3)).component("topics")
        self.assertEqual((none.value, none.reason), (0.0, ""))

    def test_the_reason_names_at_most_three_topics_rarest_first(self):
        g = graph_of([book(1, subjects=["a", "b", "c", "d"]), book(2, subjects=["a", "b", "c", "d"]),
                      book(3, subjects=["a"]), book(4, subjects=["a", "b"])])
        reason = g.score(isbn(1), isbn(2)).component("topics").reason
        self.assertEqual(reason, "both about c, d and b")                          # c, d are the rarest; a is shown last-cut


class BroaderComponentTest(unittest.TestCase):
    def broader(self, a_subjects, b_subjects):
        g = graph_of([book(1, subjects=a_subjects), book(2, subjects=b_subjects)])
        return g.score(isbn(1), isbn(2)).component("broader")

    def test_siblings_meet_under_their_broader_topic(self):
        c = self.broader(["astronomy"], ["physics"])
        self.assertGreater(c.value, 0)
        self.assertEqual(c.reason, "both fall under science")
        g = graph_of([book(1, subjects=["astronomy"]), book(2, subjects=["physics"])])
        self.assertEqual(g.score(isbn(1), isbn(2)).component("topics").value, 0.0)   # no topic in common at all

    def test_a_parent_and_its_child_are_related(self):
        self.assertGreater(self.broader(["science"], ["astronomy"]).value, 0)

    def test_a_topic_already_shared_is_not_counted_again(self):
        c = self.broader(["astronomy", "science"], ["physics", "science"])           # science is shared directly
        self.assertEqual((c.value, c.reason), (0.0, ""))

    def test_partial_credit_is_less_than_a_direct_match(self):
        g = graph_of([book(1, subjects=["astronomy"]), book(2, subjects=["astronomy"]), book(3, subjects=["physics"])])
        direct = g.score(isbn(1), isbn(2))
        indirect = g.score(isbn(1), isbn(3))
        self.assertGreater(direct.component("topics").value, indirect.component("broader").value)

    def test_unrelated_topics_and_unknown_topics_score_0(self):
        self.assertEqual(self.broader(["fiction"], ["astronomy"]).value, 0.0)
        self.assertEqual(self.broader(["mystery"], ["thriller"]).value, 0.0)         # not in the vocabulary: no parents


class AuthorComponentTest(unittest.TestCase):
    def author(self, a, b):
        g = graph_of([book(1, author=a), book(2, author=b)])
        return g.score(isbn(1), isbn(2)).component("author")

    def test_same_author_ignoring_case_and_spacing(self):
        c = self.author("Carl Sagan", "carl  sagan")
        self.assertEqual((c.value, c.reason), (1.0, "same author: carl  sagan"))

    def test_one_shared_author_of_several(self):
        c = self.author("Ann Lee; Bo Chan", "Bo Chan; Cy Dunn")
        self.assertEqual((c.value, c.reason), (1.0, "same author: Bo Chan"))

    def test_different_or_missing_authors_score_0(self):
        self.assertEqual(self.author("Ann Lee", "Bo Chan").value, 0.0)
        self.assertEqual(self.author("", "").value, 0.0)                              # nobody is their own author twin


class SeriesComponentTest(unittest.TestCase):
    def series(self, a, b, links=()):
        g = graph_of([a, b], links=links)
        return g.score(a.isbn13, b.isbn13), g.score(b.isbn13, a.isbn13)

    def test_the_next_book_scores_highest_and_is_flagged(self):
        one, two = book(1, series="Dune Chronicles", series_index=1), book(2, series="Dune Chronicles", series_index=2)
        forward, backward = self.series(one, two)
        self.assertEqual(forward.component("series").value, 1.0)
        self.assertEqual(forward.component("series").reason, "next in the series Dune Chronicles")
        self.assertTrue(forward.next_in_series)
        self.assertEqual(backward.component("series").value, 0.6)
        self.assertEqual(backward.component("series").reason, "an earlier book in the series Dune Chronicles")
        self.assertFalse(backward.next_in_series)

    def test_other_books_of_the_same_series(self):
        a, b = book(1, series="S", series_index=1), book(2, series="s", series_index=3)          # not adjacent; case differs
        forward, _ = self.series(a, b)
        self.assertEqual((forward.component("series").value, forward.component("series").reason), (0.6, "same series: S"))
        self.assertFalse(forward.next_in_series)
        unnumbered, _ = self.series(book(1, series="S"), book(2, series="S"))
        self.assertEqual(unnumbered.component("series").value, 0.6)
        self.assertFalse(unnumbered.next_in_series)

    def test_different_or_no_series(self):
        forward, _ = self.series(book(1, series="A", series_index=1), book(2, series="B", series_index=2))
        self.assertEqual((forward.component("series").value, forward.next_in_series), (0.0, False))
        forward, _ = self.series(book(1), book(2))
        self.assertEqual(forward.component("series").value, 0.0)

    def test_a_curated_series_next_link_works_across_series_and_has_a_direction(self):
        a, b = book(1, series="Robots"), book(2, series="Foundation")
        forward, backward = self.series(a, b, links=[link(1, 2, "series_next", "robots lead into the empire")])
        self.assertTrue(forward.next_in_series)
        self.assertEqual(forward.component("series").value, 1.0)
        self.assertEqual(forward.component("series").reason, "next in the series Foundation")
        self.assertFalse(backward.next_in_series)
        self.assertEqual(backward.component("series").value, 0.6)
        self.assertEqual(backward.component("series").reason, "an earlier book in the series Robots")

    def test_a_series_next_link_between_books_with_no_series_at_all(self):
        forward, backward = self.series(book(1), book(2), links=[link(1, 2, "series_next")])
        self.assertEqual(forward.component("series").reason, "the next book in the series")
        self.assertEqual(backward.component("series").reason, "an earlier book in the series")


class CallNumberComponentTest(unittest.TestCase):
    def call(self, a, b):
        g = graph_of([book(1, call_number=a), book(2, call_number=b)])
        return g.score(isbn(1), isbn(2)).component("call_number")

    def test_closer_call_numbers_score_higher(self):
        near, mid, far = self.call("523.8", "523.9"), self.call("523.8", "526"), self.call("523.8", "560")
        self.assertGreater(near.value, mid.value)
        self.assertGreater(mid.value, far.value)
        self.assertEqual(near.reason, "both shelved in astronomy")                       # the area, not the numbers

    def test_neighbouring_lc_numbers_are_close_by_and_distant_ones_are_not(self):
        close = self.call("QB981 .S24", "QB982 .H377")
        self.assertGreater(close.value, 0.9)
        self.assertEqual(close.reason, "both shelved in astronomy")
        far = self.call("QB44.2 .S24", "QB981 .H377")                                     # same subclass, 937 apart
        self.assertLess(far.value, 0.25)
        self.assertEqual(far.reason, "")                                                  # not "close by": no reason

    def test_a_reason_never_quotes_a_call_number(self):
        for a, b in (("QB981 .S24", "QB982"), ("520.1 SAG", "523.8 HAW"), ("005.1 MAR", "005.3")):
            reason = self.call(a, b).reason
            self.assertTrue(reason.startswith("both shelved"), reason)
            for raw in (a, b):
                self.assertNotIn(raw.split()[0], reason)
            self.assertFalse(any(ch.isdigit() for ch in reason), reason)

    def test_fiction_codes_share_only_a_prefix(self):
        c = self.call("F HER", "F ADA")
        self.assertEqual((c.value, c.reason), (0.3, ""))                                  # a little credit, nothing to say
        self.assertEqual(self.call("SF ADA", "SF HER").reason, "")

    def test_different_systems_odd_and_missing_codes_score_0_without_crashing(self):
        for a, b in (("520.1", "QB981"), ("520.1", "F HER"), ("???", "520.1"), ("", ""), ("520.1", "")):
            c = self.call(a, b)
            self.assertEqual((c.value, c.reason), (0.0, ""), (a, b))


class CuratedComponentTest(unittest.TestCase):
    def curated(self, relation, note="", reverse=False):
        g = graph_of([book(1), book(2)], links=[link(1, 2, relation, note)])
        a, b = (2, 1) if reverse else (1, 2)
        return g.score(isbn(a), isbn(b)).component("curated")

    def test_librarian_picks_and_related_links(self):
        pick = self.curated("librarian_pick", "start here")
        self.assertEqual((pick.value, pick.reason), (1.0, "librarian's pick: start here"))
        related = self.curated("related", "both funny")
        self.assertEqual((related.value, related.reason), (0.7, "linked by the librarians: both funny"))
        self.assertEqual(self.curated("related").reason, "linked by the librarians")          # no note

    def test_links_work_in_both_directions(self):
        self.assertEqual(self.curated("librarian_pick", "x").value, self.curated("librarian_pick", "x", reverse=True).value)

    def test_series_next_feeds_the_series_component_not_the_curated_bonus(self):
        self.assertEqual(self.curated("series_next").value, 0.0)

    def test_the_strongest_of_several_links_counts(self):
        g = graph_of([book(1), book(2)], links=[link(1, 2, "related", "meh"), link(2, 1, "librarian_pick", "yes")])
        c = g.score(isbn(1), isbn(2)).component("curated")
        self.assertEqual((c.value, c.reason), (1.0, "librarian's pick: yes"))

    def test_a_link_to_a_book_that_is_not_in_the_catalogue_is_ignored_with_a_warning(self):
        with self.assertLogs("robot.graph", level="WARNING") as logs:
            g = graph_of([book(1), book(2)], links=[link(1, 99, "related")])
        self.assertIn("not in the catalogue", logs.output[0])
        self.assertEqual(g.links, [])


class TextComponentTest(unittest.TestCase):
    def test_similar_summaries_score_and_explain_themselves(self):
        g = graph_of([book(1, summary="Survival on Mars with potatoes and duct tape"),
                      book(2, summary="A botanist's survival on Mars, growing potatoes"),
                      book(3, summary="A quiet romance in a seaside town")])
        close, far = g.score(isbn(1), isbn(2)).component("text"), g.score(isbn(1), isbn(3)).component("text")
        self.assertGreater(close.value, far.value)
        self.assertEqual(far.value, 0.0)
        self.assertTrue(close.reason.startswith("similar descriptions (both mention"))
        self.assertIn("mars", close.reason)


class TotalAndWeightsTest(unittest.TestCase):
    def setUp(self):
        self.books = [book(1, author="A", subjects=["astronomy", "x"], series="S", series_index=1, call_number="520"),
                      book(2, author="A", subjects=["astronomy", "y"], series="S", series_index=2, call_number="523")]

    def test_the_total_is_the_weighted_average_of_the_components(self):
        g = graph_of(self.books)
        s = g.score(isbn(1), isbn(2))
        expected = sum(c.value * c.weight for c in s.components) / sum(g.weights.values())
        self.assertAlmostEqual(s.total, expected)
        self.assertTrue(0.0 <= s.total <= 1.0)
        self.assertEqual([c.name for c in s.components], list(COMPONENTS))

    def test_doubling_every_weight_changes_nothing(self):
        a = graph_of(self.books).score(isbn(1), isbn(2)).total
        b = graph_of(self.books, weights={k: v * 2 for k, v in DEFAULT_WEIGHTS.items()}).score(isbn(1), isbn(2)).total
        self.assertAlmostEqual(a, b)

    def test_one_weight_alone_gives_that_component(self):
        weights = {name: 0.0 for name in COMPONENTS}
        weights["author"] = 3.0
        g = graph_of(self.books, weights=weights)
        self.assertEqual(g.score(isbn(1), isbn(2)).total, 1.0)

    def test_reasons_are_ordered_by_contribution_and_skip_zeros(self):
        s = graph_of(self.books).score(isbn(1), isbn(2))
        contributions = [c.contribution for c in s.components if c.value > 0 and c.reason]
        self.assertEqual(len(s.reasons), len(contributions))
        ordered = [c.reason for c in sorted((c for c in s.components if c.value > 0 and c.reason),
                                            key=lambda c: (-c.contribution, c.name))]
        self.assertEqual(s.reasons, ordered)
        for c in s.components:
            if c.value == 0:
                self.assertEqual(c.reason, "", c.name)                                  # nothing to say
            elif c.name != "text":
                self.assertTrue(c.reason, c.name)                                       # every real signal explains itself
        self.assertFalse(any(r == "" for r in s.reasons))

    def test_bad_weights_are_rejected(self):
        for weights in ({"nonsense": 1.0}, {"topics": -0.1}, {name: 0.0 for name in COMPONENTS}):
            with self.assertRaises(ValueError, msg=weights):
                graph_of(self.books, weights=weights)

    def test_the_config_defaults_match_the_graphs(self):
        self.assertEqual({k: SIMILARITY_DEFAULTS[k] for k in COMPONENTS}, DEFAULT_WEIGHTS)
        self.assertAlmostEqual(sum(DEFAULT_WEIGHTS.values()), 1.0)

    def test_scoring_an_unknown_book_raises(self):
        with self.assertRaises(KeyError):
            graph_of(self.books).score(isbn(1), isbn(99))


GENERAL = TopicVocabulary({
    "science": None, "astronomy": {"broader": "science"}, "physics": {"broader": "science"},
    "fiction": None, "science fiction": {"broader": "fiction"}, "dystopia": {"broader": "science fiction"},
    "epic": {"broader": "fiction"}, "volcano": None, "ethics": None,
})


class GeneralTopicsTest(unittest.TestCase):
    """A topic with a narrower child (science, fiction) is general: it counts for less."""

    def test_general_and_specific_topics(self):
        self.assertTrue(GENERAL.is_general("science"))
        self.assertTrue(GENERAL.is_general("science fiction"))                           # dystopia is narrower still
        self.assertFalse(GENERAL.is_general("astronomy"))
        self.assertFalse(GENERAL.is_general("volcano"))
        self.assertFalse(GENERAL.is_general("never heard of it"))
        self.assertEqual((GENERAL.depth("science"), GENERAL.depth("astronomy"), GENERAL.depth("dystopia")), (0, 1, 2))
        self.assertEqual(GENERAL.general, {"science", "fiction", "science fiction"})

    def graph(self, **kw):
        # equal rarity on both sides: "science" and "astronomy" are each on two books
        return graph_of([book(1, subjects=["science", "p"]), book(2, subjects=["science", "q"]),
                         book(3, subjects=["astronomy", "r"]), book(4, subjects=["astronomy", "s"])], vocab=GENERAL, **kw)

    def topics(self, g, a, b):
        return g.score(isbn(a), isbn(b)).component("topics")

    def test_a_shared_general_topic_is_worth_less_than_an_equally_rare_specific_one(self):
        g = self.graph()
        self.assertEqual(g.idf("science"), g.idf("astronomy"))                            # same rarity...
        self.assertLess(g.topic_weight("science"), g.topic_weight("astronomy"))           # ...but science is general
        self.assertAlmostEqual(g.topic_weight("science"), g.idf("science") * 0.4)
        self.assertLess(self.topics(g, 1, 2).value, self.topics(g, 3, 4).value)

    def test_the_reduction_is_configurable(self):
        full = self.graph(weights={"general_topics": 1.0})
        self.assertAlmostEqual(self.topics(full, 1, 2).value, self.topics(full, 3, 4).value)
        none = self.graph(weights={"general_topics": 0.0})
        self.assertEqual(self.topics(none, 1, 2).value, 0.0)                              # sharing only a general topic: nothing
        self.assertGreater(self.topics(none, 3, 4).value, 0.0)

    def test_bad_general_topic_factors_are_rejected(self):
        for factor in (-0.1, 1.5):
            with self.assertRaises(ValueError):
                self.graph(weights={"general_topics": factor})

    def test_the_default_comes_from_the_config(self):
        self.assertEqual(SIMILARITY_DEFAULTS["general_topics"], 0.4)
        self.assertEqual(sample_graph().general_topics, 0.4)

    def test_the_reason_lists_specific_topics_before_general_ones(self):
        g = graph_of([book(1, subjects=["science", "astronomy", "volcano"]), book(2, subjects=["science", "astronomy", "volcano"]),
                      book(3, subjects=["science"]), book(4, subjects=["astronomy"])], vocab=GENERAL)
        self.assertEqual(g.score(isbn(1), isbn(2)).component("topics").reason, "both about volcano, astronomy and science")

    def test_the_sample_vocabulary_marks_science_and_fiction_general(self):
        vocab = sample_graph().vocabulary
        for topic in ("science", "fiction", "science fiction", "technology", "history"):
            self.assertTrue(vocab.is_general(topic), topic)
        for topic in ("astronomy", "popular science", "dystopia", "classic", "ethics", "survival"):
            self.assertFalse(vocab.is_general(topic), topic)


class SpokenReasonTest(unittest.TestCase):
    """Recommendation.because: ONE natural reason for speech; the full list stays in Score.reasons."""

    def because(self, books, a=1, b=2, links=(), vocab=GENERAL, **kw):
        g = graph_of(books, vocab=vocab, links=links, **kw)
        return g.spoken_reason(isbn(a), isbn(b))

    def test_the_most_specific_shared_topic_phrased_like_the_example(self):
        text = self.because([book(1, "Cosmos", subjects=["astronomy"]), book(2, "Other", subjects=["astronomy"])])
        self.assertEqual(text, "because, like Cosmos, it's about astronomy")

    def test_the_deepest_then_rarest_specific_topic_wins_and_only_one_is_named(self):
        books = [book(1, "Base", subjects=["dystopia", "epic", "volcano", "astronomy", "science"]),
                 book(2, "Other", subjects=["dystopia", "epic", "volcano", "astronomy", "science"]),
                 book(3, subjects=["epic", "science"]), book(4, subjects=["epic"])]
        text = self.because(books)
        self.assertEqual(text, "because, like Base, it's about dystopia")                  # depth 2 beats depth 1 and 0
        self.assertEqual(text.count(","), 2)                                                    # "because, like X, it's ..."
        for other in ("epic", "volcano", "astronomy", "science", " and "):
            self.assertNotIn(other, text)

    def test_a_general_topic_is_never_chosen_over_a_specific_one(self):
        books = [book(1, "Base", subjects=["science", "astronomy"]), book(2, "Other", subjects=["science", "astronomy"]),
                 book(3, subjects=["science"]), book(4, subjects=["science"])]              # "science" is also the commonest
        self.assertEqual(self.because(books), "because, like Base, it's about astronomy")

    def test_priority_series_then_author_then_curated_then_topic(self):
        base = dict(subjects=["astronomy"], author="Carl Sagan")
        books = [book(1, "Cosmos", series="S", series_index=1, **base), book(2, "Next", series="S", series_index=2, **base)]
        links = [link(1, 2, "librarian_pick", "go deeper")]
        self.assertEqual(self.because(books, links=links), "because it's the next book in S")           # series wins
        books = [book(1, "Cosmos", **base), book(2, "Next", **base)]
        self.assertEqual(self.because(books, links=links), "because it's also by Carl Sagan")           # then the author
        books = [book(1, "Cosmos", subjects=["astronomy"], author="A"), book(2, "Next", subjects=["astronomy"], author="B")]
        self.assertEqual(self.because(books, links=links), "because the librarians suggest it after Cosmos")   # then the link
        self.assertEqual(self.because(books), "because, like Cosmos, it's about astronomy")             # then the topic

    def test_series_phrases(self):
        a, b, c = (book(1, "A", series="Dune Chronicles", series_index=1), book(2, "B", series="Dune Chronicles", series_index=2),
                   book(3, "C", series="Dune Chronicles", series_index=5))
        g = graph_of([a, b, c], vocab=GENERAL)
        self.assertEqual(g.spoken_reason(a.isbn13, b.isbn13), "because it's the next book in Dune Chronicles")
        self.assertEqual(g.spoken_reason(b.isbn13, a.isbn13), "because it comes earlier in Dune Chronicles")
        self.assertEqual(g.spoken_reason(a.isbn13, c.isbn13), "because it's also part of Dune Chronicles")
        self.assertEqual(self.because([book(1), book(2)], links=[link(1, 2, "series_next")]), "because it's the next book in the series")

    def test_a_librarians_pick_says_after_only_in_its_own_direction(self):
        books = [book(1, "Cosmos"), book(2, "Hawking")]
        pick = [link(1, 2, "librarian_pick", "go deeper")]                                   # read Cosmos, then Hawking
        self.assertEqual(self.because(books, a=1, b=2, links=pick), "because the librarians suggest it after Cosmos")
        self.assertEqual(self.because(books, a=2, b=1, links=pick), "because the librarians link it with Hawking")

    def test_author_curated_and_related_phrases(self):
        self.assertEqual(self.because([book(1, author="Ann Lee; Bo Chan"), book(2, author="Bo Chan; Cy Dunn")]),
                         "because it's also by Bo Chan")
        books = [book(1, "Cosmos"), book(2, "Other")]
        self.assertEqual(self.because(books, links=[link(1, 2, "related")]), "because the librarians link it with Cosmos")

    def test_the_shelf_area_comes_after_topics_and_before_a_general_topic(self):
        close = dict(subjects=["science"])
        books = [book(1, "Base", call_number="QB981", **close), book(2, "Other", call_number="QB982", **close)]
        self.assertEqual(self.because(books), "because, like Base, it's shelved with astronomy")      # beats the general "science"
        books = [book(1, "Base", call_number="QB44.2", **close), book(2, "Other", call_number="QB981", **close)]
        self.assertEqual(self.because(books), "because, like Base, it's about science")             # not near: general topic
        books = [book(1, "Base", subjects=["astronomy"], call_number="QB981"), book(2, "Other", subjects=["astronomy"], call_number="QB982")]
        self.assertEqual(self.because(books), "because, like Base, it's about astronomy")           # a specific topic beats the shelf

    def test_broader_and_generic_fallbacks(self):
        books = [book(1, "Base", subjects=["astronomy"]), book(2, "Other", subjects=["physics"])]
        self.assertEqual(self.because(books), "because, like Base, it falls under science")
        books = [book(1, "Base", summary="volcano lava eruption mountain"), book(2, "Other", summary="volcano lava eruption crater")]
        self.assertEqual(self.because(books, weights={"min_score": 0.0}), "because it's similar to Base")

    def test_recommend_attaches_the_spoken_reason_and_the_full_reasons_stay(self):
        g = sample_graph()
        for isbn_ in ("9780345539434", "9780553294385", "9780441172719", "9780553418026"):
            for rec in g.recommend(isbn_, 3):
                self.assertTrue(rec.because.startswith("because"), rec.because)
                self.assertEqual(rec.because, g.spoken_reason(isbn_, rec.book.isbn13, rec.score))
                self.assertGreaterEqual(len(rec.reasons), 1)                                   # the full list is still there

    def test_a_spoken_reason_never_lists_several_topics(self):
        g = sample_graph()
        for a in g.catalog.books:
            for rec in g.recommend(a.isbn13, 5):
                self.assertEqual(rec.because.count(" and "), 0, rec.because)
                self.assertLessEqual(rec.because.count(","), 2, rec.because)
                self.assertNotIn("both about", rec.because)

    def test_sample_examples(self):
        g = sample_graph()
        cosmos = g.recommend("9780345539434", 3)
        self.assertEqual(cosmos[0].because, "because the librarians suggest it after Cosmos")
        self.assertEqual(cosmos[1].because, "because, like Cosmos, it's about popular science")
        self.assertEqual(g.recommend("9780553294385", 1)[0].because, "because it's the next book in Asimov's Future History")


class RecommendTest(unittest.TestCase):
    def titles(self, g, n, count=10):
        return [r.book.title for r in g.recommend(isbn(n), count)]

    def test_best_first_and_never_the_book_itself(self):
        g = graph_of([book(1, "Base", subjects=["a", "b", "c"]), book(2, "Close", subjects=["a", "b", "c"]),
                      book(3, "Half", subjects=["a", "x", "y"]), book(4, "None", subjects=["q"])],
                     weights={"min_score": 0.001})                                      # anything that scores at all
        picks = g.recommend(isbn(1), 10)
        self.assertEqual([r.book.title for r in picks], ["Close", "Half"])           # "None" shares nothing
        self.assertEqual(picks[0].book.isbn13, isbn(2))
        self.assertGreater(picks[0].total, picks[1].total)
        self.assertTrue(all(r.book.isbn13 != isbn(1) for r in picks))

    def test_ties_break_by_title_then_isbn_and_repeat_exactly(self):
        g = graph_of([book(1, "Base", subjects=["a"]), book(2, "charlie", subjects=["a"]), book(3, "Alpha", subjects=["a"]),
                      book(4, "bravo", subjects=["a"]), book(6, "Alpha", subjects=["a"])])
        order = g.recommend(isbn(1), 10)
        self.assertEqual([r.book.title for r in order], ["Alpha", "Alpha", "bravo", "charlie"])
        self.assertEqual([r.book.isbn13 for r in order[:2]], [isbn(3), isbn(6)])      # same title: ISBN order
        for _ in range(3):
            self.assertEqual([r.book.isbn13 for r in g.recommend(isbn(1), 10)], [r.book.isbn13 for r in order])

    def test_limit_unknown_book_and_isbn_10(self):
        g = graph_of([book(i, f"B{i}", subjects=["a"]) for i in range(1, 6)])
        self.assertEqual(len(g.recommend(isbn(1), 2)), 2)
        self.assertEqual(g.recommend(isbn(1), 0), [])
        self.assertEqual(g.recommend(isbn(1), -3), [])
        self.assertEqual(g.recommend(isbn(99), 3), [])
        digits = isbn(1)[3:12]                                                          # the same book as an ISBN-10
        check = (11 - sum((10 - i) * int(d) for i, d in enumerate(digits)) % 11) % 11
        isbn10 = digits + ("X" if check == 10 else str(check))
        self.assertEqual([r.book.isbn13 for r in g.recommend(isbn10, 10)], [r.book.isbn13 for r in g.recommend(isbn(1), 10)])
        self.assertEqual(len(g.recommend(isbn10, 10)), 4)

    def test_books_below_min_score_are_left_out(self):
        books = [book(1, subjects=["a", "b", "c", "d"]), book(2, subjects=["a", "b", "c", "d"]), book(3, subjects=["a", "x", "y", "z"])]
        low = graph_of(books, weights={"min_score": 0.0}).recommend(isbn(1), 10)
        self.assertEqual(len(low), 2)
        threshold = (low[0].total + low[1].total) / 2                                   # between the two totals
        high = graph_of(books, weights={"min_score": threshold}).recommend(isbn(1), 10)
        self.assertEqual([r.book.isbn13 for r in high], [isbn(2)])
        self.assertTrue(all(r.total >= threshold for r in high))

    def test_every_recommendation_has_reasons_and_components(self):
        for rec in sample_graph().recommend("9780345539434", 3):
            self.assertEqual(len(rec.components), len(COMPONENTS))
            self.assertTrue(rec.reasons, rec.book.title)
            self.assertEqual(rec.total, rec.score.total)

    def test_the_next_book_in_a_series_comes_first_even_with_a_lower_total(self):
        g = graph_of([book(1, "Volume 1", subjects=["a", "b", "c"], series="S", series_index=1),
                      book(2, "Lookalike", subjects=["a", "b", "c"]),                       # a better topic match...
                      book(3, "Volume 2", subjects=["q"], series="S", series_index=2)])     # ...but this is the sequel
        picks = g.recommend(isbn(1), 3)
        self.assertEqual([r.book.title for r in picks], ["Volume 2", "Lookalike"])
        self.assertLess(picks[0].total, picks[1].total)
        self.assertTrue(picks[0].score.next_in_series)

    def test_sample_cosmos_leads_to_hawking_through_the_librarians_pick(self):
        picks = sample_graph().recommend("9780345539434", 3)
        self.assertEqual(picks[0].book.title, "A Brief History of Time")
        self.assertTrue(any(r.startswith("librarian's pick") for r in picks[0].reasons))
        self.assertTrue(any(r.startswith("both about") for r in picks[0].reasons))
        self.assertNotIn("Cosmos", [p.book.title for p in picks])

    def test_sample_i_robot_leads_to_foundation_as_the_next_in_series(self):
        picks = sample_graph().recommend("9780553294385", 3)
        self.assertEqual(picks[0].book.title, "Foundation")
        self.assertTrue(picks[0].score.next_in_series)
        self.assertIn("next in the series Asimov's Future History", picks[0].reasons)
        self.assertIn("same author: Isaac Asimov", picks[0].reasons)

    def test_sample_books_nothing_resembles_get_no_recommendations(self):
        self.assertEqual(sample_graph().recommend("9780132350884", 3), [])             # Clean Code


class LocateTest(unittest.TestCase):
    SHELVES = {"S1": Shelf("S1", "Science", 3, "quiet", 4.0, 1.0, 3.14, "near the lift"),
               "NOPOSE": Shelf("NOPOSE", "History", 2, "common")}

    def locate(self, shelf="", section="", floor=None, shelves=None, n=1):
        g = graph_of([book(n, shelf=shelf or None, section=section, floor=floor)],
                     shelves=self.SHELVES if shelves is None else shelves)
        return g.locate_with_reason(isbn(n))

    def test_a_shelf_gives_section_floor_zone_and_pose(self):
        place, reason = self.locate("S1")
        self.assertEqual(place, Location(isbn(1), "S1", "Science", 3, "quiet", 4.0, 1.0, 3.14, "near the lift"))
        self.assertIsNone(reason)
        self.assertTrue(place.has_pose)

    def test_the_shelf_id_is_matched_ignoring_case(self):
        self.assertEqual(self.locate("s1")[0].section, "Science")

    def test_the_sample_library(self):
        place = sample_graph().locate("9780441172719")                              # Dune
        self.assertEqual((place.shelf, place.section, place.floor, place.zone), ("2-SF-B", "Science Fiction", 2, "common"))
        self.assertEqual((place.map_x, place.map_y, place.map_yaw), (1.0, 4.0, 1.57))
        self.assertIn("TODO", place.description)
        self.assertEqual(sample_graph().locate("9780345539434").zone, "quiet")

    def test_a_shelf_that_is_not_defined_keeps_what_the_catalogue_knows_and_says_why(self):
        place, reason = self.locate("GHOST", section="Poetry", floor=1)
        self.assertEqual((place.shelf, place.section, place.floor, place.zone), ("GHOST", "Poetry", 1, None))
        self.assertFalse(place.has_pose)
        self.assertIn("'GHOST' is not in shelves.csv", reason)

    def test_a_shelf_without_a_pose_is_described_but_cannot_be_driven_to(self):
        place, reason = self.locate("NOPOSE")
        self.assertEqual((place.section, place.floor, place.zone), ("History", 2, "common"))
        self.assertFalse(place.has_pose)
        self.assertIn("NOPOSE has no map position", reason)

    def test_no_shelf_but_a_section_or_floor(self):
        place, reason = self.locate(section="Reference", floor=2)
        self.assertEqual((place.shelf, place.section, place.floor, place.has_pose), (None, "Reference", 2, False))
        self.assertIn("no shelf", reason)
        self.assertEqual(self.locate(floor=0)[0].floor, 0)                          # floor 0 is a floor

    def test_nothing_known_is_none_with_a_reason(self):
        place, reason = self.locate()
        self.assertIsNone(place)
        self.assertIn("no shelf, section or floor", reason)

    def test_an_unknown_book_is_none_with_a_reason(self):
        g = graph_of([book(1)])
        self.assertEqual(g.locate_with_reason(isbn(99)), (None, "the book is not in the catalogue"))
        self.assertIsNone(g.locate(isbn(99)))

    def test_describe_in_words(self):
        self.assertEqual(Location("x", "S1", "Science", 3).describe(), "on shelf S1, in the Science section on level 3")
        self.assertEqual(Location("x", None, "Science", 3).describe(), "in the Science section on level 3")
        self.assertEqual(Location("x", None, None, 3).describe(), "on level 3")
        self.assertEqual(Location("x", "S1").describe(), "on shelf S1")
        self.assertEqual(Location("x").describe(), "")


class LoadersTest(unittest.TestCase):
    def write(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_sample_files_load(self):
        shelves, links = load_shelves(ROOT / "data" / "shelves.csv"), load_links(ROOT / "data" / "curated_links.csv")
        self.assertEqual(len(shelves), 7)
        self.assertTrue(all(s.has_pose and s.zone in ("quiet", "common") for s in shelves.values()))
        self.assertTrue(all("TODO" in s.description for s in shelves.values()))       # placeholder poses are marked
        self.assertEqual({l.relation for l in links}, {"series_next", "librarian_pick", "related"})

    def test_parse_shelves_reports_every_problem_with_its_line(self):
        rows = [(2, {"shelf": "A", "section": "S", "floor": "1", "zone": "quiet"}),
                (3, {"shelf": "a", "section": "S", "floor": "1", "zone": "quiet"}),          # duplicate (case-insensitive)
                (4, {"shelf": "B", "section": "S", "floor": "one", "zone": "quiet"}),
                (5, {"shelf": "C", "section": "S", "floor": "1", "zone": "loud"}),
                (6, {"shelf": "D", "section": "S", "floor": "1", "zone": "", "map_x": "left"}),
                (7, {"shelf": "", "section": "S"})]
        shelves, problems = parse_shelves(rows)
        self.assertEqual(list(shelves), ["A"])
        self.assertEqual([line for line, _ in problems], [3, 4, 5, 6, 7])
        self.assertIn("duplicate shelf", problems[0][1])
        self.assertIn("floor must be a whole number", problems[1][1])
        self.assertIn("zone must be quiet or common", problems[2][1])
        self.assertIn("map_x must be a number", problems[3][1])

    def test_parse_links_reports_every_problem(self):
        a, b = isbn(1), isbn(2)
        rows = [(2, {"from_isbn": a, "to_isbn": b, "relation": "related", "note": "ok"}),
                (3, {"from_isbn": a, "to_isbn": b, "relation": "sequel"}),
                (4, {"from_isbn": a, "to_isbn": a, "relation": "related"}),
                (5, {"from_isbn": "123", "to_isbn": b, "relation": "related"})]
        links, problems = parse_links(rows)
        self.assertEqual(len(links), 1)
        self.assertEqual([line for line, _ in problems], [3, 4, 5])
        self.assertIn("relation must be one of", problems[0][1])
        self.assertIn("itself", problems[1][1])
        self.assertIn("bad ISBN '123'", problems[2][1])

    def test_load_names_the_file_and_line(self):
        with tempfile.TemporaryDirectory() as d:
            bad = self.write(d, "shelves.csv", "shelf,section,floor,zone\nA,S,1,quiet\nB,S,1,sideways\n")
            with self.assertRaisesRegex(CatalogError, r"shelves.csv:3: zone must be quiet or common"):
                load_shelves(bad)
            bad = self.write(d, "links.csv", f"from_isbn,to_isbn,relation\n{isbn(1)},{isbn(2)},friends\n")
            with self.assertRaisesRegex(CatalogError, r"links.csv:2: relation must be one of"):
                load_links(bad)
            with self.assertRaisesRegex(CatalogError, "missing column"):
                load_shelves(self.write(d, "short.csv", "shelf,section\nA,S\n"))
            with self.assertRaisesRegex(CatalogError, "not found"):
                load_links(Path(d) / "nope.csv")


class ReloadTest(unittest.TestCase):
    CATALOG = ("isbn13,title,author,genre,subjects,summary,shelf,slot\n"
               f"{isbn(1)},One,A,fiction,space,s,,\n{isbn(2)},Two,A,fiction,space,s,,\n")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.catalog = Path(self.dir.name) / "catalog.csv"
        self.topics = Path(self.dir.name) / "topics.yaml"
        self.catalog.write_text(self.CATALOG, encoding="utf-8")
        self.topics.write_text("astronomy:\n  aliases: [space]\n", encoding="utf-8")
        self.graph = BookGraph.from_files(self.catalog, self.topics)

    def test_reload_picks_up_edited_files(self):
        self.assertEqual(len(self.graph.catalog), 2)
        self.assertEqual(self.graph.topics_of(isbn(1)), ("fiction", "astronomy"))
        self.catalog.write_text(self.CATALOG + f"{isbn(3)},Three,B,fiction,comedy,s,,\n", encoding="utf-8")
        self.topics.write_text("astronomy:\n  aliases: [space]\nhumour:\n  aliases: [comedy]\n", encoding="utf-8")
        self.graph.reload()
        self.assertEqual(len(self.graph.catalog), 3)
        self.assertEqual(self.graph.topics_of(isbn(3)), ("fiction", "humour"))
        self.assertIn(f"book:{isbn(3)}", self.graph.nodes)

    def test_a_broken_file_keeps_the_old_graph(self):
        self.catalog.write_text("isbn13,title\nbroken\n", encoding="utf-8")
        with self.assertRaises(CatalogError):
            self.graph.reload()
        self.assertEqual(len(self.graph.catalog), 2)
        self.assertEqual(len(self.graph.recommend(isbn(1), 3)), 1)

    def test_optional_files_may_be_missing_or_unnamed(self):
        g = BookGraph.from_files(self.catalog, Path(self.dir.name) / "missing.yaml", None, Path(self.dir.name) / "missing.csv")
        self.assertEqual((len(g.catalog), g.shelves, g.links), (2, {}, []))
        self.assertEqual(g.topics_of(isbn(1)), ("fiction", "space"))                   # no vocabulary: kept as written

    def test_a_graph_built_from_objects_cannot_reload(self):
        with self.assertRaises(RuntimeError):
            graph_of([book(1)]).reload()

    def test_reload_keeps_the_weights(self):
        g = BookGraph.from_files(self.catalog, self.topics, weights={"topics": 0.5, "min_score": 0.33, "general_topics": 0.2})
        g.reload()
        self.assertEqual((g.weights["topics"], g.min_score, g.general_topics), (0.5, 0.33, 0.2))


if __name__ == "__main__":
    unittest.main()
