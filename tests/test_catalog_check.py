import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from robot.config import ROOT
from tests.helpers.ean13 import check_digit
from tools import catalog_check
from tools.catalog_check import Report, check, format_report

HEADER = "isbn13,title,author,genre,subjects,summary,shelf,slot,call_number,section,floor,series,series_index\n"


def isbn(n):
    body = "978" + f"{n:09d}"
    return body + check_digit(body)


TOPICS = "science:\n  aliases: []\nastronomy:\n  broader: science\n  aliases: [space]\nfiction:\n  aliases: []\n"
SHELVES = ("shelf,section,floor,zone,map_x,map_y,map_yaw,description\n"
           "S1,Science,3,quiet,4.0,1.0,3.14,ok\nF1,Fiction,1,common,,,,no pose yet\n")


def good_catalog():
    return (HEADER +
            f"{isbn(1)},One,A,science,space;astronomy,s,S1,0,520,Science,3,,\n"
            f"{isbn(2)},Two,B,science,space;astronomy,s,S1,1,523,Science,3,,\n")


class Files:
    def __init__(self, test, catalog=None, topics=TOPICS, shelves=SHELVES, links=None):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        self.catalog = self.make(d / "catalog.csv", good_catalog() if catalog is None else catalog)
        self.topics = self.make(d / "topics.yaml", topics)
        self.shelves = self.make(d / "shelves.csv", shelves)
        self.links = self.make(d / "links.csv", links)

    @staticmethod
    def make(path, text):
        if text is None:
            return None
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, **kw):
        return check(self.catalog, self.topics, self.shelves, self.links, **kw)


class CleanFilesTest(unittest.TestCase):
    def test_good_files_have_no_errors(self):
        report = Files(self).check()
        self.assertEqual(report.errors, [])
        self.assertTrue(report.ok)

    def test_the_sample_data_has_no_errors(self):
        from robot.config import load_config
        cfg = load_config()
        b = cfg.books
        report = check(cfg.path(b["catalog_path"]), cfg.path(b["topics_path"]), cfg.path(b["shelves_path"]),
                       cfg.path(b["links_path"]), b["similarity"])
        self.assertEqual(report.errors, [])
        self.assertEqual(report.stats["books"], 12)
        self.assertGreaterEqual(len(report.summary), 4)

    def test_statistics(self):
        stats = Files(self).check().stats
        self.assertEqual(stats["books"], 2)
        self.assertEqual(list(stats["topics"])[:2], ["astronomy", "science"])           # equal counts: alphabetical
        self.assertEqual(stats["topics"]["astronomy"], 2)
        self.assertEqual(stats["sections"], {"Science": 2})
        self.assertEqual(stats["floors"], {"level 3": 2})
        self.assertEqual(stats["shelves"], {"S1": 2})


class CatalogErrorsTest(unittest.TestCase):
    def errors(self, rows, **kw):
        return Files(self, HEADER + rows, **kw).check().errors

    def test_a_bad_isbn_checksum_names_the_line(self):
        errors = self.errors(f"{isbn(1)},One,A,science,space,s,S1,0,,,,,\n9780345391804,Bad,A,science,space,s,S1,1,,,,,\n")
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], r"catalog.csv:3: bad ISBN '9780345391804'")

    def test_duplicate_isbns_name_both_lines(self):
        errors = self.errors(f"{isbn(1)},One,A,science,space,s,S1,0,,,,,\n{isbn(1)},Again,A,science,space,s,S1,1,,,,,\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate ISBN", errors[0])
        self.assertIn("catalog.csv:3", errors[0])
        self.assertIn("also on line 2", errors[0])

    def test_a_shelf_that_is_not_defined(self):
        errors = self.errors(f"{isbn(1)},One,A,science,space,s,NOPE,0,,,,,\n")
        self.assertTrue(any("shelf 'NOPE' is not defined in shelves.csv" in e for e in errors), errors)

    def test_slots_outside_the_ring(self):
        errors = self.errors(f"{isbn(1)},One,A,science,space,s,S1,8,,,,,\n{isbn(2)},Two,A,science,space,s,S1,-1,,,,,\n")
        self.assertEqual(len([e for e in errors if "outside the storage ring (0..7)" in e]), 2)
        self.assertEqual(Files(self, HEADER + f"{isbn(1)},One,A,science,space,s,S1,3,,,,,\n").check(slots=4).errors, [])
        self.assertTrue(Files(self, HEADER + f"{isbn(1)},One,A,science,space,s,S1,5,,,,,\n").check(slots=4).errors)

    def test_bad_numbers_and_empty_titles_are_errors_not_crashes(self):
        errors = self.errors(f"{isbn(1)},,A,science,space,s,S1,0,,,,,\n{isbn(2)},Two,A,science,space,s,S1,0,,,high,,\n")
        self.assertEqual(len(errors), 2)
        self.assertIn("empty title", errors[0])
        self.assertIn("floor must be a whole number", errors[1])

    def test_missing_files_and_columns_are_errors(self):
        report = check(Path("/nonexistent/catalog.csv"), None, None, None)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("not found", report.errors[0])
        files = Files(self, "isbn13,title\n" + f"{isbn(1)},One\n")
        self.assertIn("missing column", files.check().errors[0])

    def test_all_problems_are_reported_together(self):
        errors = self.errors(f"{isbn(1)},One,A,science,space,s,NOPE,9,,,,,\n9780345391804,Bad,A,science,space,s,S1,0,,,,,\n")
        self.assertGreaterEqual(len(errors), 3)                                         # bad ISBN, bad shelf, bad slot


class VocabularyShelfAndLinkErrorsTest(unittest.TestCase):
    def test_topics_problems_are_errors(self):
        report = Files(self, topics="a:\n  broader: ghost\nb:\n  aliases: [x]\nc:\n  aliases: [x]\n").check()
        text = " | ".join(report.errors)
        self.assertIn("'ghost', which is not defined", text)
        self.assertIn("'x' is used for both", text)

    def test_unreadable_yaml_is_an_error(self):
        self.assertTrue(any("not valid YAML" in e for e in Files(self, topics="a: [unclosed\n").check().errors))

    def test_shelf_rows_with_problems_name_the_line(self):
        report = Files(self, shelves="shelf,section,floor,zone\nS1,Science,3,quiet\nS1,Again,3,quiet\nS2,Hist,3,loud\n").check()
        self.assertTrue(any("shelves.csv:3: duplicate shelf" in e for e in report.errors), report.errors)
        self.assertTrue(any("shelves.csv:4: zone must be quiet or common" in e for e in report.errors), report.errors)

    def test_links_to_unknown_books_and_bad_relations(self):
        links = (f"from_isbn,to_isbn,relation,note\n{isbn(1)},{isbn(2)},related,fine\n{isbn(1)},{isbn(77)},related,x\n"
                 f"{isbn(1)},{isbn(2)},sequel,x\n")
        errors = Files(self, links=links).check().errors
        self.assertTrue(any("names " + isbn(77) + ", which is not in the catalogue" in e for e in errors), errors)
        self.assertTrue(any("links.csv:4: relation must be one of" in e for e in errors), errors)


class WarningsTest(unittest.TestCase):
    def warnings(self, rows, **kw):
        return Files(self, HEADER + rows, **kw).check().warnings

    def test_unknown_subjects_are_reported_with_a_suggestion_and_kept(self):
        files = Files(self, HEADER + f"{isbn(1)},One,A,science,astronomi;zeitgeist,s,S1,0,,,,,\n"
                                     f"{isbn(2)},Two,A,science,astronomy;zeitgeist,s,S1,1,,,,,\n")
        report = files.check()
        text = "\n".join(report.warnings)
        self.assertIn("unknown subject 'zeitgeist', used by 2 book(s): One, Two", text)
        self.assertIn("unknown subject 'astronomi'", text)
        self.assertIn("did you mean 'astronomy'", text)
        self.assertEqual(report.errors, [])                                              # unknown is a warning, not an error

    def test_books_with_fewer_than_two_topics(self):
        warnings = self.warnings(f"{isbn(1)},One,A,,space,s,S1,0,,,,,\n")
        self.assertTrue(any("One: only 1 topic(s) (astronomy)" in w for w in warnings), warnings)
        warnings = self.warnings(f"{isbn(1)},One,A,,,s,S1,0,,,,,\n")
        self.assertTrue(any("only 0 topic(s) (none)" in w for w in warnings), warnings)

    def test_books_with_no_neighbours(self):
        rows = (f"{isbn(1)},One,A,science,space,s,S1,0,,,,,\n{isbn(2)},Two,B,science,space,s,S1,1,,,,,\n"
                f"{isbn(3)},Loner,C,fiction,,s,F1,2,,,,,\n")
        warnings = self.warnings(rows)
        self.assertTrue(any("Loner: no neighbour scores 0.08 or more" in w for w in warnings), warnings)
        self.assertFalse(any("One: no neighbour" in w for w in warnings))
        high = Files(self, HEADER + rows).check(min_score=0.99).warnings                  # threshold is adjustable
        self.assertTrue(any("One: no neighbour scores 0.99 or more" in w for w in high), high)

    def test_shelf_section_floor_call_number_and_series_oddities(self):
        warnings = self.warnings(
            f"{isbn(1)},One,A,science,space;astronomy,s,,0,,,,,\n"                              # no shelf
            f"{isbn(2)},Two,A,science,space;astronomy,s,S1,1,???,History,2,,\n"                # junk call number, wrong section+floor
            f"{isbn(3)},Three,A,science,space;astronomy,s,S1,2,520,Science,3,,4\n")           # series_index but no series
        text = "\n".join(warnings)
        self.assertIn("One: no shelf", text)
        self.assertIn("Two: call number '???'", text)
        self.assertIn("section 'History' but shelf S1 is in 'Science'", text)
        self.assertIn("floor 2 but shelf S1 is on floor 3", text)
        self.assertIn("Three: series_index 4 but no series", text)

    def test_shelves_without_a_map_pose_are_a_warning(self):
        self.assertTrue(any("shelf F1 has no map position" in w for w in Files(self).check().warnings))

    def test_a_missing_optional_file_is_a_warning(self):
        report = check(Files(self).catalog, Path("/nope/topics.yaml"), Path("/nope/shelves.csv"), None)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("topics file not found" in w for w in report.warnings))
        self.assertTrue(any("shelves file not found" in w for w in report.warnings))


class OutputAndExitCodeTest(unittest.TestCase):
    def run_main(self, files, *extra):
        argv = ["--catalog", str(files.catalog), "--topics", str(files.topics), "--shelves", str(files.shelves),
                "--links", str(files.links or "/nonexistent/links.csv"), *extra]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = catalog_check.main(argv)
        return code, out.getvalue()

    def test_exit_code_is_0_for_good_files_and_prints_a_report(self):
        files = Files(self, links="from_isbn,to_isbn,relation,note\n")
        code, out = self.run_main(files)
        self.assertEqual(code, 0)
        self.assertIn("Result: OK", out)
        self.assertIn("STATS", out)
        self.assertIn("books per shelf: S1 2", out)

    def test_exit_code_is_1_when_there_are_errors(self):
        files = Files(self, HEADER + f"{isbn(1)},One,A,science,space,s,NOPE,0,,,,,\n", links="from_isbn,to_isbn,relation,note\n")
        code, out = self.run_main(files)
        self.assertEqual(code, 1)
        self.assertIn("ERRORS (1)", out)
        self.assertIn("Result: FAILED", out)

    def test_strict_makes_warnings_fail_too(self):
        files = Files(self, HEADER + f"{isbn(1)},One,A,science,space,s,S1,0,,,,,\n", links="from_isbn,to_isbn,relation,note\n")
        self.assertEqual(self.run_main(files)[0], 0)                                      # warnings only
        self.assertEqual(self.run_main(files, "--strict")[0], 1)

    def test_the_real_data_passes_from_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(catalog_check.main([]), 0)
        self.assertIn("data/catalog.csv (12 books)", out.getvalue())

    def test_report_formatting_handles_an_empty_report(self):
        text = format_report(Report())
        self.assertIn("Nothing was read", text)
        self.assertIn("Result: OK", text)


if __name__ == "__main__":
    unittest.main()
