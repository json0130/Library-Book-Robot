import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from robot.config import ROOT
from robot.services.books import Book, BookFinder, Catalog, CatalogError, OpenLibrary

HEADER = "isbn13,title,author,genre,tags,summary,shelf,slot\n"
DUNE, HITCH = "9780441172719", "9780345391803"


def write_csv(directory, text, name="catalog.csv"):
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def open_library_answer(isbn, title="Online Book", authors=("Some Author",)):
    entry = {"title": title, "authors": [{"name": a} for a in authors]}
    return FakeResponse(json.dumps({f"ISBN:{isbn}": entry}).encode())


class CatalogLoadTest(unittest.TestCase):
    def test_sample_catalogue_loads(self):
        catalog = Catalog.load(ROOT / "data" / "catalog.csv")
        self.assertGreaterEqual(len(catalog), 10)
        book = catalog.lookup("0-345-39180-2")                 # ISBN-10 with hyphens
        self.assertEqual(book.isbn13, HITCH)
        self.assertIn("space", book.tags)
        self.assertIsInstance(book.slot, int)

    def test_missing_file(self):
        with self.assertRaisesRegex(CatalogError, "not found.*nope.csv"):
            Catalog.load("/nonexistent/nope.csv")

    def test_missing_column_names_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, "# comment\nisbn13,title,author\n9780345391803,T,A\n")
            with self.assertRaisesRegex(CatalogError, r"catalog.csv:2: missing column.*genre.*slot"):
                Catalog.load(path)

    def test_bad_isbn_names_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, HEADER + f"{DUNE},Dune,FH,sf,a;b,s,S1,1\n9780345391804,Bad,X,sf,a,s,S2,2\n")
            with self.assertRaisesRegex(CatalogError, r"catalog.csv:3: bad ISBN '9780345391804'"):
                Catalog.load(path)

    def test_bad_slot_empty_title_duplicates_and_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(CatalogError, r":2: slot must be a whole number"):
                Catalog.load(write_csv(d, HEADER + f"{DUNE},Dune,FH,sf,a,s,S1,x\n", "a.csv"))
            with self.assertRaisesRegex(CatalogError, r":2: empty title"):
                Catalog.load(write_csv(d, HEADER + f"{DUNE},,FH,sf,a,s,S1,1\n", "b.csv"))
            with self.assertRaisesRegex(CatalogError, "duplicate ISBN"):
                Catalog.load(write_csv(d, HEADER + f"{DUNE},A,x,sf,a,s,S1,1\n{DUNE},B,x,sf,a,s,S1,1\n", "c.csv"))
            with self.assertRaisesRegex(CatalogError, "empty"):
                Catalog.load(write_csv(d, "# only a comment\n", "d.csv"))

    def test_blank_optional_fields_and_quoted_commas(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, HEADER + f'{DUNE},"Dune, Part 1",FH,sf,a;b,"Spice, sand, worms",,\n')
            book = Catalog.load(path).lookup(DUNE)
        self.assertEqual((book.title, book.summary, book.shelf, book.slot),
                         ("Dune, Part 1", "Spice, sand, worms", None, None))
        self.assertEqual(book.tags, ("a", "b"))


class SimilarTest(unittest.TestCase):
    def setUp(self):
        def book(isbn, title, genre, tags):
            return Book(isbn, title, "A", genre, tuple(tags))
        self.catalog = Catalog([
            book("9780000000002", "Alpha", "sf", ["space", "robots"]),
            book("9780000000019", "Bravo", "sf", ["space", "robots"]),      # identical topics to Alpha
            book("9780000000026", "Charlie", "sf", ["space"]),
            book("9780000000033", "Delta", "history", ["space", "robots"]),
            book("9780000000040", "Echo", "cooking", ["pasta"]),            # nothing in common
            book("9780000000057", "Foxtrot", "sf", ["space"]),              # ties with Charlie
        ])
        self.alpha = self.catalog.lookup("9780000000002")

    def titles(self, n=3):
        return [b.title for b in self.catalog.similar(self.alpha, n)]

    def test_ranked_by_overlap_with_deterministic_ties(self):
        # topics: Alpha {sf, space, robots}. Bravo is identical (1.0); Charlie and Foxtrot share
        # {sf, space} (2/3, tied, so title order); Delta shares {space, robots} (2/4); Echo nothing.
        self.assertEqual(self.titles(5), ["Bravo", "Charlie", "Foxtrot", "Delta"])

    def test_never_returns_itself_or_unrelated_books(self):
        titles = self.titles(10)
        self.assertNotIn("Alpha", titles)
        self.assertNotIn("Echo", titles)

    def test_limit_and_stability(self):
        self.assertEqual(self.titles(2), ["Bravo", "Charlie"])
        self.assertEqual(self.catalog.similar(self.alpha, 0), [])
        self.assertEqual(self.titles(5), self.titles(5))

    def test_sample_catalogue_neighbours(self):
        catalog = Catalog.load(ROOT / "data" / "catalog.csv")
        similar = [b.title for b in catalog.similar(catalog.lookup("9780553294385"), 3)]   # I, Robot
        self.assertEqual(len(similar), 3)
        self.assertNotIn("I Robot", similar)
        self.assertTrue(all(catalog.lookup(b.isbn13) for b in catalog.similar(catalog.lookup(HITCH))))


class OpenLibraryTest(unittest.TestCase):
    ISBN = "9780140449136"

    def test_found_online_is_cached(self):
        with tempfile.TemporaryDirectory() as d:
            ol = OpenLibrary(cache_dir=d)
            with mock.patch("robot.services.books.urllib.request.urlopen",
                            return_value=open_library_answer(self.ISBN, "Crime and Punishment",
                                                             ("Fyodor Dostoevsky",))) as urlopen:
                book = ol.lookup(self.ISBN)
                self.assertEqual((book.title, book.author), ("Crime and Punishment", "Fyodor Dostoevsky"))
                self.assertFalse(book.found_in_catalog)
                self.assertEqual((book.shelf, book.slot, book.summary), (None, None, None))
                url = urlopen.call_args.args[0]
                self.assertIn(f"bibkeys=ISBN:{self.ISBN}", url)
                self.assertIn("jscmd=data", url)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.0)
            self.assertTrue((Path(d) / f"{self.ISBN}.json").is_file())
            with mock.patch("robot.services.books.urllib.request.urlopen",
                            side_effect=AssertionError("must use the cache")):
                self.assertEqual(ol.lookup(self.ISBN).title, "Crime and Punishment")

    def test_network_errors_return_none_quietly(self):
        with tempfile.TemporaryDirectory() as d:
            for error in (urllib.error.URLError("no network"), TimeoutError(), ConnectionResetError()):
                with mock.patch("robot.services.books.urllib.request.urlopen", side_effect=error):
                    self.assertIsNone(OpenLibrary(cache_dir=d).lookup(self.ISBN))
            self.assertEqual(list(Path(d).iterdir()), [])        # failures are not cached

    def test_bad_answers_return_none(self):
        for body in (b"{}", b"not json", b'{"ISBN:x": 5}', b"[]"):
            with mock.patch("robot.services.books.urllib.request.urlopen", return_value=FakeResponse(body)):
                self.assertIsNone(OpenLibrary().lookup(self.ISBN))

    def test_corrupt_cache_entry_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{self.ISBN}.json").write_text("{broken")
            with mock.patch("robot.services.books.urllib.request.urlopen",
                            return_value=open_library_answer(self.ISBN)):
                self.assertEqual(OpenLibrary(cache_dir=d).lookup(self.ISBN).title, "Online Book")


class BookFinderTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load(ROOT / "data" / "catalog.csv")

    def test_catalogue_first_without_touching_the_network(self):
        finder = BookFinder(self.catalog, online_fallback=True)
        with mock.patch("robot.services.books.urllib.request.urlopen", side_effect=AssertionError("no network")):
            book = finder.find(HITCH)
        self.assertTrue(book.found_in_catalog)

    def test_fallback_only_when_enabled(self):
        unknown = "9780140449136"
        with mock.patch("robot.services.books.urllib.request.urlopen",
                        return_value=open_library_answer(unknown)) as urlopen:
            self.assertIsNone(BookFinder(self.catalog, online_fallback=False).find(unknown))
            urlopen.assert_not_called()
            self.assertFalse(BookFinder(self.catalog, online_fallback=True).find(unknown).found_in_catalog)

    def test_invalid_isbn_is_none_and_similar_to(self):
        finder = BookFinder(self.catalog, online_fallback=False)
        self.assertIsNone(finder.find("not an isbn"))
        self.assertEqual(finder.similar_to("9780140449136"), [])          # not in the catalogue
        self.assertEqual(len(finder.similar_to("0345391802")), 3)         # ISBN-10 accepted


if __name__ == "__main__":
    unittest.main()
