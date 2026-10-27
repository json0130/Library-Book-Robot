import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from robot.config import ROOT
from robot.services.books import Book, BookFinder, Catalog, CatalogError, OpenLibrary
from tests.helpers.sample import sample_graph

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
        self.assertIn("space", book.subjects)
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


NEW_HEADER = ("isbn13,title,author,genre,subjects,summary,shelf,slot,call_number,section,floor,series,"
              "series_index,year\n")


class NewColumnsTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            return Catalog.load(write_csv(d, text))

    def test_the_new_columns_are_read(self):
        catalog = self.load(NEW_HEADER + f'{DUNE},Dune,Frank Herbert,sf,"desert;politics",Spice,2-SF-B,1,F HER,'
                                         "Science Fiction,2,Dune Chronicles,1,1965\n")
        book = catalog.lookup(DUNE)
        self.assertEqual((book.subjects, book.call_number, book.section, book.floor),
                         (("desert", "politics"), "F HER", "Science Fiction", 2))
        self.assertEqual((book.series, book.series_index, book.year, book.shelf), ("Dune Chronicles", 1, 1965, "2-SF-B"))

    def test_an_old_file_with_tags_and_no_new_columns_still_loads(self):
        book = self.load(HEADER + f"{DUNE},Dune,FH,sf,a;b,s,S1,1\n").lookup(DUNE)
        self.assertEqual(book.subjects, ("a", "b"))
        self.assertEqual(book.tags, ("a", "b"))                                 # the old name still works
        self.assertEqual((book.call_number, book.section, book.floor, book.series, book.series_index), ("", "", None, "", None))

    def test_new_columns_are_optional_blank_cells(self):
        book = self.load(NEW_HEADER + f"{DUNE},Dune,FH,sf,a,s,S1,1,,,,,,\n").lookup(DUNE)
        self.assertEqual((book.call_number, book.floor, book.series_index, book.year), ("", None, None, None))

    def test_a_bad_number_names_the_line_and_the_column(self):
        for column, row in (("floor", f"{DUNE},Dune,FH,sf,a,s,S1,1,F HER,SF,two,,,\n"),
                            ("series_index", f"{DUNE},Dune,FH,sf,a,s,S1,1,F HER,SF,2,Dune,first,\n"),
                            ("year", f"{DUNE},Dune,FH,sf,a,s,S1,1,F HER,SF,2,Dune,1,nineteen\n")):
            with self.assertRaisesRegex(CatalogError, rf"catalog.csv:2: {column} must be a whole number"):
                self.load(NEW_HEADER + row)

    def test_the_sample_catalogue_has_the_new_values(self):
        book = Catalog.load(ROOT / "data" / "catalog.csv").lookup("9780345539434")           # Cosmos
        self.assertEqual((book.call_number, book.section, book.floor, book.shelf), ("QB44.2 .S24", "Science", 3, "3-SCI-A"))


class FindTitleTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load(ROOT / "data" / "catalog.csv")

    def title(self, query):
        book = self.catalog.find_title(query)
        return book.title if book else None

    def test_loose_titles(self):
        self.assertEqual(self.title("dune"), "Dune")
        self.assertEqual(self.title("DUNE!"), "Dune")
        self.assertEqual(self.title("the martian"), "The Martian")
        self.assertEqual(self.title("martian"), "The Martian")
        self.assertEqual(self.title("hitchhikers guide"), "The Hitchhiker's Guide to the Galaxy")
        self.assertEqual(self.title("a brief history of time"), "A Brief History of Time")
        self.assertEqual(self.title("i robot"), "I Robot")

    def test_misspelling_and_isbn(self):
        self.assertEqual(self.title("frankenstien"), "Frankenstein")
        self.assertEqual(self.title("9780441172719"), "Dune")
        self.assertEqual(self.title("0-441-17271-7"), "Dune")                           # ISBN-10 with hyphens

    def test_no_match(self):
        for query in ("", "   ", "the bathroom", "zzzzzz", "xyz", "i", "a"):
            self.assertIsNone(self.catalog.find_title(query), query)

    def test_an_exact_title_beats_a_longer_one_and_two_letters_is_too_little_to_go_on(self):
        catalog = Catalog([Book("9780000000002", "Dune Messiah"), Book("9780000000019", "Dune")])
        self.assertEqual(catalog.find_title("dune").title, "Dune")
        self.assertEqual(catalog.find_title("dune messiah").title, "Dune Messiah")
        self.assertIsNone(catalog.find_title("du"))

    def test_the_shortest_title_containing_the_words_wins(self):
        catalog = Catalog([Book("9780000000002", "The Long Way Home Again"), Book("9780000000019", "The Long Way Home")])
        self.assertEqual(catalog.find_title("long way").title, "The Long Way Home")


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

    def test_invalid_isbn_is_none_and_recommend(self):
        finder = BookFinder(self.catalog, online_fallback=False)
        self.assertIsNone(finder.find("not an isbn"))
        self.assertEqual(finder.recommend("9780140449136"), [])          # not in the catalogue
        picks = finder.recommend("0345391802")                            # ISBN-10 accepted
        self.assertGreaterEqual(len(picks), 1)
        self.assertNotIn(HITCH, [p.book.isbn13 for p in picks])

    def test_locate_and_reload_go_through_the_graph(self):
        finder = BookFinder(self.catalog, online_fallback=False, graph=sample_graph())
        self.assertEqual(finder.locate(HITCH).shelf, "2-SF-A")
        place, reason = finder.locate_with_reason("9780140449136")
        self.assertIsNone(place)
        self.assertIn("not in the catalogue", reason)
        with self.assertRaises(RuntimeError):                              # a finder over a bare catalogue has no files
            BookFinder(self.catalog, online_fallback=False).reload()       # to re-read


if __name__ == "__main__":
    unittest.main()
