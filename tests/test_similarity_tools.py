import contextlib
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robot.services.book_graph import BookGraph
from robot.services.books import Book, Catalog
from tests.helpers.ean13 import check_digit
from tests.helpers.sample import sample_graph
from tools import graph_view, similarity_report as sr


def isbn(n):
    body = "978" + f"{n:09d}"
    return body + check_digit(body)


def small_graph():
    return BookGraph(Catalog([
        Book(isbn(1), "Alpha", "A", "sf", ("space", "robots"), "robots in space"),
        Book(isbn(2), "Bravo", "B", "sf", ("space", "robots"), "robots in space"),          # alike Alpha
        Book(isbn(3), "Charlie", "C", "sf", ("space",), "just space"),                       # a bit alike
        Book(isbn(4), "Delta", "D", "cooking", ("pasta",), "cooking pasta"),                 # not alike
    ]))


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class ReportTest(unittest.TestCase):
    def test_neighbours_by_each_method(self):
        g = small_graph()
        self.assertEqual([b.title for b in sr.neighbours(g, isbn(1), sr.graph_score, 2)], ["Bravo", "Charlie"])
        self.assertEqual(sr.neighbours(g, isbn(1), sr.text_score, 1)[0].title, "Bravo")
        self.assertNotIn("Alpha", [b.title for b in sr.neighbours(g, isbn(1), sr.graph_score, 10)])

    def test_the_old_jaccard_baseline(self):
        g = small_graph()
        self.assertEqual(sr.jaccard_score(g, isbn(1), isbn(2)), 1.0)
        self.assertAlmostEqual(sr.jaccard_score(g, isbn(1), isbn(3)), 2 / 3)                 # {sf, space} of {sf, space, robots}
        self.assertEqual(sr.jaccard_score(g, isbn(1), isbn(4)), 0.0)

    def test_the_report_lists_every_book_overlap_and_disagreements(self):
        text = sr.report(sample_graph(), 3)
        for book in sample_graph().catalog.books:
            self.assertIn(book.title, text)
        self.assertIn("overlap@3", text)
        self.assertIn("Mean overlap@3:", text)
        self.assertIn("Biggest disagreements", text)

    def test_k_is_limited_to_the_other_books(self):
        text = sr.report(small_graph(), 50)
        self.assertIn("overlap@3 = ", text)

    def test_a_disagreement_with_no_shared_topic_is_pointed_out(self):
        g = BookGraph(Catalog([Book(isbn(1), "Alpha", "A", "x", ("one",), "volcano lava eruption mountain"),
                               Book(isbn(2), "Bravo", "B", "y", ("two",), "volcano lava eruption crater"),
                               Book(isbn(3), "Charlie", "C", "x", ("one",), "tea ceremony kettle")]))
        self.assertIn("share NO topic: a missing or wrong topic?", sr.report(g, 1))


class PairsTest(unittest.TestCase):
    def test_about_twenty_pairs_from_across_the_score_range_with_no_labels(self):
        graph = sample_graph()
        rows = sr.make_pairs(graph, 20)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(r[4] == "" for r in rows))
        self.assertEqual(len({(r[0], r[2]) for r in rows}), 20)                              # no pair twice
        self.assertTrue(all(r[0] != r[2] for r in rows))
        scores = sorted(sr.graph_score(graph, r[0], r[2]) for r in rows)
        pool = sorted(sr.graph_score(graph, a.isbn13, b.isbn13) for i, a in enumerate(graph.catalog.books)
                      for b in graph.catalog.books[i + 1:])
        self.assertLess(scores[0], pool[len(pool) // 3])                                      # some from the low third...
        self.assertGreater(scores[-1], pool[2 * len(pool) // 3])                              # ...and some from the high one

    def test_deterministic_and_shuffled(self):
        graph = sample_graph()
        first, again = sr.make_pairs(graph, 20), sr.make_pairs(graph, 20)
        self.assertEqual(first, again)
        by_score = [sr.graph_score(graph, r[0], r[2]) for r in first]
        self.assertNotEqual(by_score, sorted(by_score))                                       # the order doesn't hint at the answer
        self.assertNotEqual(by_score, sorted(by_score, reverse=True))

    def test_few_books_gives_all_the_pairs(self):
        self.assertEqual(len(sr.make_pairs(small_graph(), 20)), 6)

    def test_the_file_has_instructions_and_the_right_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "eval" / "pairs.csv"
            sr.write_pairs(path, sr.make_pairs(small_graph(), 20))
            text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Label each pair"))
        rows = list(csv.reader(line for line in text.splitlines() if not line.startswith("#")))
        self.assertEqual(rows[0], list(sr.PAIR_COLUMNS))
        self.assertEqual(len(rows), 7)


class LabelsAndEvalTest(unittest.TestCase):
    def labelled(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pairs.csv"
            path.write_text("# note\nisbn_a,title_a,isbn_b,title_b,label\n" + text, encoding="utf-8")
            return sr.read_labels(path)

    def test_labels_1_0_yes_no_and_blank_rows_are_skipped(self):
        labelled, problems = self.labelled(f"{isbn(1)},A,{isbn(2)},B,1\n{isbn(1)},A,{isbn(3)},C,0\n{isbn(1)},A,{isbn(4)},D,\n"
                                           f"{isbn(2)},B,{isbn(3)},C,Yes\n{isbn(2)},B,{isbn(4)},D,NO\n{isbn(3)},C,{isbn(4)},D,maybe\n")
        self.assertEqual([l for *_, l in labelled], [1, 0, 1, 0])
        self.assertEqual(len(problems), 1)
        self.assertIn("pairs.csv:8: label 'maybe' is not 1 or 0", problems[0])

    def test_ranking_accuracy_by_hand(self):
        g = small_graph()
        # similar: (Alpha, Bravo) scores highest; not similar: (Alpha, Delta) and (Charlie, Delta) score lowest
        labelled = [(isbn(1), isbn(2), 1), (isbn(1), isbn(4), 0), (isbn(3), isbn(4), 0)]
        results, skipped, counts = sr.evaluate(g, labelled)
        self.assertEqual((skipped, counts), (0, (1, 2)))
        yes, no, accuracy = results["graph"]
        self.assertGreater(yes, no)
        self.assertEqual(accuracy, 1.0)
        self.assertEqual(results["jaccard (old)"][2], 1.0)
        self.assertEqual(set(results), {"graph", "text only", "jaccard (old)"})

    def test_a_method_that_gets_it_backwards_scores_0_and_ties_score_half(self):
        g = small_graph()
        backwards = [(isbn(1), isbn(4), 1), (isbn(1), isbn(2), 0)]
        self.assertEqual(sr.evaluate(g, backwards)[0]["graph"][2], 0.0)
        tied = [(isbn(1), isbn(4), 1), (isbn(2), isbn(4), 0)]                                  # both score 0 against Delta
        self.assertEqual(sr.evaluate(g, tied)[0]["jaccard (old)"][2], 0.5)

    def test_one_class_only_gives_no_ranking_accuracy(self):
        results, _, counts = sr.evaluate(small_graph(), [(isbn(1), isbn(2), 1), (isbn(1), isbn(3), 1)])
        self.assertEqual(counts, (2, 0))
        self.assertIsNone(results["graph"][2])
        self.assertIsNone(results["graph"][1])
        self.assertIn("needs at least one similar and one not-similar", sr.format_eval(results, counts, 0))

    def test_unknown_books_are_skipped_not_fatal(self):
        results, skipped, counts = sr.evaluate(small_graph(), [(isbn(1), isbn(2), 1), (isbn(1), isbn(99), 0)])
        self.assertEqual((skipped, counts), (1, (1, 0)))

    def test_the_formatted_table(self):
        g = small_graph()
        results, skipped, counts = sr.evaluate(g, [(isbn(1), isbn(2), 1), (isbn(1), isbn(4), 0)])
        text = sr.format_eval(results, counts, skipped)
        self.assertIn("Labelled pairs: 2 (1 similar, 1 not similar)", text)
        for name in ("graph", "text only", "jaccard (old)"):
            self.assertIn(name, text)
        self.assertIn("ranking accuracy", text)


class MainTest(unittest.TestCase):
    def test_default_prints_the_report(self):
        code, out, _ = run_main(sr, ["--k", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Top 2 neighbours", out)

    def test_make_pairs_then_eval_round_trip_and_never_overwrites_labels(self):
        with tempfile.TemporaryDirectory() as d:
            pairs = str(Path(d) / "eval" / "pairs.csv")
            code, out, _ = run_main(sr, ["--make-pairs", "--pairs", pairs, "--count", "9"])
            self.assertEqual(code, 0)
            self.assertIn("Wrote 9 pairs", out)
            code, _, err = run_main(sr, ["--eval", "--pairs", pairs])
            self.assertEqual(code, 1)
            self.assertIn("No labels", err)
            code, _, err = run_main(sr, ["--make-pairs", "--pairs", pairs])                  # would wipe the labels
            self.assertEqual(code, 1)
            self.assertIn("already exists", err)
            lines = Path(pairs).read_text(encoding="utf-8").splitlines()
            header = next(i for i, l in enumerate(lines) if l.startswith("isbn_a"))
            rows = [l.split(",") for l in lines[header + 1:]]
            labelled = lines[:header + 1] + [",".join(r[:-1] + [str(i % 2)]) for i, r in enumerate(rows)]
            Path(pairs).write_text("\n".join(labelled) + "\n", encoding="utf-8")
            code, out, _ = run_main(sr, ["--eval", "--pairs", pairs])
            self.assertEqual(code, 0)
            self.assertIn("Labelled pairs: 9", out)
            self.assertIn("ranking accuracy", out)

    def test_eval_without_a_pairs_file_says_how_to_make_one(self):
        code, _, err = run_main(sr, ["--eval", "--pairs", "/nonexistent/pairs.csv"])
        self.assertEqual(code, 1)
        self.assertIn("--make-pairs", err)


class GraphViewTest(unittest.TestCase):
    def setUp(self):
        self.html = graph_view.render(sample_graph())

    def test_the_page_is_self_contained(self):
        for forbidden in ("<script src", "<link", "@import", "fetch(", "XMLHttpRequest", "<img", "<iframe", "url(http", "import("):
            self.assertNotIn(forbidden, self.html, forbidden)
        urls = re.findall(r"https?://[^\s\"'<>)]+", self.html)
        self.assertEqual(set(urls), {"http://www.w3.org/2000/svg"})                           # only the SVG namespace name

    def test_it_is_a_complete_page(self):
        self.assertTrue(self.html.startswith("<!doctype html>"))
        self.assertEqual(self.html.count("<script>"), 1)
        self.assertEqual(self.html.count("</script>"), 1)
        for needed in ("COLORS", "layout(", "mouseenter", "Recommended next", "Connections"):
            self.assertIn(needed, self.html)

    def test_the_embedded_data_is_valid_and_complete(self):
        graph = sample_graph()
        data = json.loads(re.search(r"const DATA = (\{.*\});\n", self.html).group(1))
        self.assertEqual(len(data["nodes"]), len(graph.nodes))
        self.assertEqual(len(data["edges"]), len(graph.edges))
        self.assertEqual({n["kind"] for n in data["nodes"]}, {n.kind for n in graph.nodes.values()})
        cosmos = data["recs"]["book:9780345539434"]
        self.assertEqual(cosmos[0]["title"], "A Brief History of Time")
        self.assertTrue(any("librarian" in r for r in cosmos[0]["reasons"]))
        book = next(n for n in data["nodes"] if n["label"] == "Cosmos")
        info = dict(book["info"])
        self.assertEqual(info["Author"], "Carl Sagan")
        self.assertEqual(info["Call number"], "QB44.2 .S24")
        self.assertIn("Science section on level 3", info["Location"])

    def test_hostile_titles_cannot_break_out_of_the_page(self):
        evil = "</script><script>alert(1)</script><!--<script>\u2028"
        g = BookGraph(Catalog([Book(isbn(1), evil, "A", "x", ("y",), "s"), Book(isbn(2), "Other", "B", "x", ("y",), "s")]))
        html = graph_view.render(g)
        self.assertEqual(html.count("</script>"), 1)                                          # only the real end tag
        self.assertEqual(html.count("<script"), 1)                                            # no second script, no <!-- trick
        self.assertEqual(html.count("<!--"), 0)
        self.assertNotIn("\u2028", html)
        data = json.loads(re.search(r"const DATA = (\{.*\});\n", html).group(1))
        self.assertIn(evil, [n["label"] for n in data["nodes"]])                              # the text itself survives

    def test_the_drawing_code_never_uses_innerhtml(self):
        self.assertNotIn("innerHTML", self.html)
        self.assertNotIn("insertAdjacentHTML", self.html)
        self.assertNotIn("document.write", self.html)

    def test_main_writes_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sub" / "graph.html"
            code, text, _ = run_main(graph_view, ["--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("81 nodes", text)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
