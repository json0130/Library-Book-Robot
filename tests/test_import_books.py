import contextlib
import csv
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from robot.config import load_config
from robot.services.books import Catalog
from robot.services.topics import TopicVocabulary
from tests.helpers.ean13 import check_digit
from tools import import_books as ib


def isbn(n):
    body = "978" + f"{n:09d}"
    return body + check_digit(body)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def details(title="Cosmos", authors=("Carl Sagan",), subjects=("Astronomy", "Mars (Planet)", "Open Library Staff Picks"),
            description="A sweeping journey.", publish_date="October 1, 1980"):
    return {"title": title, "authors": [{"name": a} for a in authors], "subjects": list(subjects),
            "description": description, "publish_date": publish_date}


def answer(code, **kw):
    return FakeResponse(json.dumps({f"ISBN:{code}": {"details": details(**kw)}}).encode())


VOCAB = TopicVocabulary({"astronomy": {"aliases": ["space", "mars"]}, "fiction": {"aliases": []},
                         "science fiction": {"aliases": ["sci-fi"]}})
PATCH_URLOPEN = "tools.import_books.urllib.request.urlopen"


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / "cache"

    def fetch(self, code=None, cache=True, delay=0.0, last=None):
        return ib.fetch_details(code or isbn(1), self.cache if cache else None, delay, last or [0.0])

    def test_fetches_details_and_asks_politely(self):
        with mock.patch(PATCH_URLOPEN, return_value=answer(isbn(1))) as urlopen:
            got = self.fetch()
        self.assertEqual(got["title"], "Cosmos")
        request = urlopen.call_args.args[0]
        self.assertIn("jscmd=details", request.full_url)
        self.assertIn(f"ISBN:{isbn(1)}", request.full_url.replace("%3A", ":"))
        self.assertIn("Library-Book-Robot", request.get_header("User-agent"))               # says who is asking
        self.assertEqual(urlopen.call_args.kwargs["timeout"], ib.TIMEOUT_S)

    def test_answers_are_cached_and_the_cache_is_used(self):
        with mock.patch(PATCH_URLOPEN, return_value=answer(isbn(1))) as urlopen:
            self.fetch()
            self.assertTrue((self.cache / f"{isbn(1)}.details.json").is_file())
            self.assertEqual(self.fetch()["title"], "Cosmos")
        self.assertEqual(urlopen.call_count, 1)

    def test_a_corrupt_cache_entry_is_asked_for_again(self):
        self.cache.mkdir()
        (self.cache / f"{isbn(1)}.details.json").write_text("{not json", encoding="utf-8")
        with mock.patch(PATCH_URLOPEN, return_value=answer(isbn(1))) as urlopen:
            self.assertEqual(self.fetch()["title"], "Cosmos")
        self.assertEqual(urlopen.call_count, 1)

    def test_not_found_is_none_and_not_cached(self):
        with mock.patch(PATCH_URLOPEN, return_value=FakeResponse(b"{}")):
            self.assertIsNone(self.fetch())
        self.assertFalse(list(self.cache.glob("*")) if self.cache.exists() else [])

    def test_network_errors_and_bad_answers_are_none_and_reported(self):
        stderr = io.StringIO()
        with mock.patch(PATCH_URLOPEN, side_effect=urllib.error.URLError("offline")), contextlib.redirect_stderr(stderr):
            self.assertIsNone(self.fetch())
        self.assertIn("request failed", stderr.getvalue())
        for body in (b"not json", b"[]", json.dumps({f"ISBN:{isbn(1)}": {"details": {"no": "title"}}}).encode()):
            with mock.patch(PATCH_URLOPEN, return_value=FakeResponse(body)), contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(self.fetch(), body)

    def test_the_polite_delay_is_between_network_requests_only(self):
        clock = iter(range(100, 200))
        last = [0.0]
        with mock.patch(PATCH_URLOPEN, side_effect=lambda *a, **k: answer(isbn(1))), \
                mock.patch("tools.import_books.time.sleep") as sleep, \
                mock.patch("tools.import_books.time.monotonic", side_effect=lambda: next(clock)):
            self.fetch(isbn(1), delay=5.0, last=last)                 # the first request: no wait
            sleep.assert_not_called()
            self.fetch(isbn(2), delay=5.0, last=last)                 # the second: waits out the rest of the 5 s
            self.assertEqual(sleep.call_count, 1)
            self.assertGreater(sleep.call_args.args[0], 0)
            self.assertLessEqual(sleep.call_args.args[0], 5.0)
            self.fetch(isbn(1), delay=5.0, last=last)                 # cached: no request, no wait
            self.assertEqual(sleep.call_count, 1)

    def test_zero_delay_never_sleeps(self):
        with mock.patch(PATCH_URLOPEN, side_effect=lambda *a, **k: answer(isbn(1))), \
                mock.patch("tools.import_books.time.sleep") as sleep:
            last = [0.0]
            self.fetch(isbn(1), last=last)
            self.fetch(isbn(2), last=last)
        sleep.assert_not_called()


class ParsingTest(unittest.TestCase):
    def test_subjects_as_strings_or_objects(self):
        self.assertEqual(ib.subject_names({"subjects": ["Space", {"name": "Mars"}, "", None, 5, {"url": "x"}]}), ["Space", "Mars"])
        self.assertEqual(ib.subject_names({}), [])

    def test_authors(self):
        self.assertEqual(ib.author_names({"authors": [{"name": " Ann Lee "}, {"name": ""}, "x", {}]}), ["Ann Lee"])

    def test_descriptions_are_one_line_and_not_too_long(self):
        self.assertEqual(ib.description_text({"description": "Line one.\n\nLine   two."}), "Line one. Line two.")
        self.assertEqual(ib.description_text({"description": {"type": "/type/text", "value": "As a dict"}}), "As a dict")
        self.assertEqual(ib.description_text({}), "")
        long = ib.description_text({"description": "word " * 500})
        self.assertLessEqual(len(long), ib.MAX_SUMMARY + 3)
        self.assertTrue(long.endswith("..."))

    def test_publish_year(self):
        for text, year in (("June 1, 1988", "1988"), ("2011", "2011"), ("c1985", "1985"), ("Jan 2020", "2020"),
                           ("unknown", ""), ("", ""), ("page 12345", "")):
            self.assertEqual(ib.publish_year({"publish_date": text}), year, text)
        self.assertEqual(ib.publish_year({}), "")

    def test_subjects_map_through_the_vocabulary(self):
        topics, unmatched = ib.map_subjects(["Astronomy", "Space", "Mars (Planet)", "Sci-Fi", "Open Library Staff Picks",
                                             "Fiction, general"], VOCAB)
        self.assertEqual(topics, ["astronomy", "science fiction"])                       # synonyms collapse, order kept
        self.assertEqual(unmatched, ["Open Library Staff Picks", "Fiction, general"])

    def test_compound_subjects_are_tried_whole_then_in_parts(self):
        topics, unmatched = ib.map_subjects(["Space flight -- Mars (Planet)", "Science fiction -- Classic", "Nonsense -- More"], VOCAB)
        self.assertEqual(topics, ["astronomy", "science fiction"])
        self.assertEqual(unmatched, ["Nonsense -- More"])

    def test_draft_row_has_every_column_and_leaves_the_unknowns_blank(self):
        row, unmatched = ib.draft_row(isbn(1), details(authors=("Carl Sagan", "Ann Druyan")), VOCAB)
        cells = dict(zip(ib.DRAFT_COLUMNS, row))
        self.assertEqual(len(row), len(ib.DRAFT_COLUMNS))
        self.assertEqual((cells["isbn13"], cells["title"], cells["author"]), (isbn(1), "Cosmos", "Carl Sagan; Ann Druyan"))
        self.assertEqual((cells["subjects"], cells["year"], cells["summary"]), ("astronomy", "1980", "A sweeping journey."))
        for blank in ("genre", "shelf", "slot", "call_number", "section", "floor", "series", "series_index"):
            self.assertEqual(cells[blank], "", blank)
        self.assertEqual(unmatched, ["Open Library Staff Picks"])

    def test_isbn_file_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isbns.txt"
            path.write_text(f"# my list\n{isbn(1)}\n\n  {isbn(2)}  # with a comment\nnot-an-isbn\n{isbn(1)}\n9780345391804\n", encoding="utf-8")
            isbns, problems = ib.read_isbns(path)
        self.assertEqual(isbns, [isbn(1), isbn(2)])                                      # no repeats
        self.assertEqual(len(problems), 2)
        self.assertIn("isbns.txt:5: 'not-an-isbn' is not a valid ISBN", problems[0])
        self.assertIn("9780345391804", problems[1])


class ImportTest(unittest.TestCase):
    def test_rows_unmatched_not_found_and_skipped(self):
        def respond(request, timeout=None):
            for n in (1, 2, 4):
                if isbn(n) in request.full_url:
                    return answer(isbn(n), title=f"Book {n}", subjects=("Astronomy", "Zeitgeist"))
            return FakeResponse(b"{}")
        with tempfile.TemporaryDirectory() as d, mock.patch(PATCH_URLOPEN, side_effect=respond), \
                contextlib.redirect_stdout(io.StringIO()):
            rows, unmatched, not_found, skipped = ib.import_books([isbn(1), isbn(2), isbn(3), isbn(4)], VOCAB,
                                                                  Path(d), 0.0, skip={isbn(4)})
        self.assertEqual([r[1] for r in rows], ["Book 1", "Book 2"])
        self.assertEqual(unmatched, {"Zeitgeist": 2})
        self.assertEqual((not_found, skipped), ([isbn(3)], [isbn(4)]))

    def test_suggestions_for_near_misses(self):
        self.assertIn("looks like 'science fiction'", ib.suggestion("science-fiction!", VOCAB))
        self.assertEqual(ib.suggestion("zzz", VOCAB), "")

    def test_the_draft_is_a_valid_catalogue_with_a_review_note(self):
        row, _ = ib.draft_row(isbn(1), details(), VOCAB)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "draft.csv"
            ib.write_draft(path, [row])
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# DRAFT"))
            self.assertEqual(Catalog.load(path).lookup(isbn(1)).title, "Cosmos")            # the loader reads it unchanged
            self.assertEqual(next(csv.reader(line for line in text.splitlines() if not line.startswith("#"))),
                             list(ib.DRAFT_COLUMNS))


class MainTest(unittest.TestCase):
    CATALOG = ("isbn13,title,author,genre,subjects,summary,shelf,slot\n"
               f"{isbn(9)},Already Here,A,fiction,space,s,,\n")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        self.catalog, self.topics, self.cache, self.out = d / "catalog.csv", d / "topics.yaml", d / "cache", d / "draft.csv"
        self.catalog.write_text(self.CATALOG, encoding="utf-8")
        self.topics.write_text("astronomy:\n  aliases: [space, mars]\nfiction:\n  aliases: []\n", encoding="utf-8")
        self.isbns = d / "isbns.txt"
        self.isbns.write_text(f"{isbn(1)}\n{isbn(2)}\nbad-line\n{isbn(9)}\n", encoding="utf-8")
        cfg = load_config()
        cfg.robot["books"] = {"catalog_path": str(self.catalog), "topics_path": str(self.topics), "cache_dir": str(self.cache)}
        self.patches = [mock.patch("tools.import_books.load_config", return_value=cfg)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *args, respond=None):
        respond = respond or (lambda request, timeout=None: answer(
            isbn(1) if isbn(1) in request.full_url else isbn(2), title="One" if isbn(1) in request.full_url else "Two",
            subjects=("Astronomy", "Zeitgeist", "Zeitgeist")))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(PATCH_URLOPEN, side_effect=respond) as urlopen, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = ib.main([str(self.isbns), "--out", str(self.out), "--delay", "0", *args])
        return code, out.getvalue(), err.getvalue(), urlopen

    def test_writes_a_draft_and_reports_what_needs_attention(self):
        code, out, err, urlopen = self.run_main()
        self.assertEqual(code, 0)
        draft = Catalog.load(self.out)
        self.assertEqual([b.title for b in draft.books], ["One", "Two"])
        self.assertEqual(draft.lookup(isbn(1)).subjects, ("astronomy",))
        self.assertIn("Wrote 2 book(s)", out)
        self.assertIn("Skipped 1 already in the catalogue", out)                         # isbn(9) is in catalog.csv
        self.assertRegex(out, r"2  Zeitgeist")                                           # unmatched subjects, counted
        self.assertIn("topics.yaml", out)
        self.assertIn("'bad-line' is not a valid ISBN", err)
        self.assertEqual(urlopen.call_count, 2)

    def test_the_catalogue_is_never_touched_and_cannot_be_the_output(self):
        before = self.catalog.read_text(encoding="utf-8")
        self.run_main()
        self.assertEqual(self.catalog.read_text(encoding="utf-8"), before)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(PATCH_URLOPEN) as urlopen, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ib.main([str(self.isbns), "--out", str(self.catalog), "--delay", "0"])
        self.assertEqual(code, 2)
        self.assertIn("Refusing to write over the catalogue", err.getvalue())
        urlopen.assert_not_called()
        self.assertEqual(self.catalog.read_text(encoding="utf-8"), before)

    def test_include_existing_drafts_them_anyway(self):
        def respond(request, timeout=None):
            code = next(isbn(n) for n in (1, 2, 9) if isbn(n) in request.full_url)
            return answer(code, title=f"T{code[-4:]}")
        code, out, _, _ = self.run_main("--include-existing", respond=respond)
        self.assertEqual(len(Catalog.load(self.out)), 3)
        self.assertNotIn("Skipped", out)

    def test_a_second_run_uses_the_cache(self):
        self.run_main()
        _, _, _, urlopen = self.run_main()
        urlopen.assert_not_called()
        _, _, _, urlopen = self.run_main("--no-cache")
        self.assertEqual(urlopen.call_count, 2)

    def test_not_found_isbns_are_listed_not_written(self):
        code, out, _, _ = self.run_main(respond=lambda request, timeout=None: FakeResponse(b"{}"))
        self.assertEqual(code, 0)
        self.assertIn(f"Not found on Open Library, so not in the draft: {isbn(1)}, {isbn(2)}", out)
        self.assertEqual(len(Catalog.load(self.out)), 0)                                  # a header and no rows

    def test_a_missing_isbn_file_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ib.main([str(self.isbns) + ".nope", "--out", str(self.out)]), 2)
        self.assertIn("ISBN file not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
