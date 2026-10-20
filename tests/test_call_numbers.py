import unittest

from robot.services.call_numbers import (CLOSE_BY, GENERIC_AREA, PREFIX_ONLY_CLOSENESS, CallNumber, closeness,
                                          parse_call_number, shared_area)


def levels(text):
    return parse_call_number(text).levels


class DeweyTest(unittest.TestCase):
    def test_hierarchy(self):
        self.assertEqual(levels("520.1 SAG"), ("500", "520", "520.1"))          # the example in the brief
        self.assertEqual(levels("523.8 HAW"), ("500", "520", "523", "523.8"))
        self.assertEqual(levels("523.81"), ("500", "520", "523", "523.8", "523.81"))
        self.assertEqual(parse_call_number("520.1 SAG").system, "dewey")

    def test_repeated_levels_are_dropped(self):
        self.assertEqual(levels("520"), ("500", "520"))                         # tens and units are both 520
        self.assertEqual(levels("500"), ("500",))
        self.assertEqual(levels("005.1 MAR"), ("000", "005", "005.1"))
        self.assertEqual(levels("909 HAR"), ("900", "909"))

    def test_other_separators_and_spacing(self):
        self.assertEqual(levels("909-HAR"), ("900", "909"))
        self.assertEqual(levels("  520 . 1  sag "), ("500", "520", "520.1"))
        self.assertEqual(parse_call_number("520.1 SAG").deepest, "520.1")


class LibraryOfCongressTest(unittest.TestCase):
    def test_hierarchy(self):
        self.assertEqual(levels("QB981 .S24"), ("Q", "QB", "QB981"))            # the example in the brief
        self.assertEqual(levels("QB44.2 .S24"), ("Q", "QB", "QB44", "QB44.2"))
        self.assertEqual(levels("HF5415.5 .K67"), ("H", "HF", "HF5415", "HF5415.5"))
        self.assertEqual(parse_call_number("QB981 .S24").system, "lc")

    def test_one_letter_classes_spacing_and_case(self):
        self.assertEqual(levels("Q 123"), ("Q", "Q123"))
        self.assertEqual(levels("qb981 .s24"), ("Q", "QB", "QB981"))
        self.assertEqual(levels("QA76.76 .C65"), ("Q", "QA", "QA76", "QA76.76"))


class FictionStyleTest(unittest.TestCase):
    def test_only_the_prefix_counts(self):
        for text in ("F HER", "FIC-FIT", "SF ADA", "F", "fic.smith", "MYST/CHR"):
            call = parse_call_number(text)
            self.assertEqual(call.system, "other", text)
            self.assertEqual(len(call.levels), 1, text)
        self.assertEqual(levels("F HER"), ("F",))
        self.assertEqual(levels("FIC-FIT"), ("FIC",))


class NumberFieldsTest(unittest.TestCase):
    def test_the_class_number_division_and_main_class(self):
        dewey, lc, other = parse_call_number("523.8 HAW"), parse_call_number("QB44.2 .S24"), parse_call_number("F HER")
        self.assertEqual((dewey.main, dewey.division, dewey.number), ("500", "520", 523.8))
        self.assertEqual((lc.main, lc.division, lc.number), ("Q", "QB", 44.2))
        self.assertEqual((other.main, other.division, other.number), ("F", "", None))
        self.assertEqual(parse_call_number("005.1").division, "000")                    # the tens of 005


class AreaNameTest(unittest.TestCase):
    def area(self, a, b):
        return shared_area(parse_call_number(a), parse_call_number(b))

    def test_dewey_tens_then_hundreds(self):
        self.assertEqual(self.area("520.1", "523.8"), "astronomy")                      # the same ten
        self.assertEqual(self.area("520", "530"), "science")                            # the same hundred only
        self.assertEqual(self.area("909 HAR", "909 ABC"), "history")
        self.assertEqual(self.area("005.1 MAR", "005.3"), "computing and general works")
        self.assertEqual(self.area("820 SHA", "821"), "English literature")
        self.assertEqual(self.area("640", "699"), "technology")                         # 640 and 690 differ: the hundred

    def test_lc_subclasses_then_classes(self):
        self.assertEqual(self.area("QB981 .S24", "QB44.2"), "astronomy")
        self.assertEqual(self.area("QB981", "QC5"), "science")
        self.assertEqual(self.area("PR6045 .A", "PR4000"), "English literature")
        self.assertEqual(self.area("HF5415", "HG20"), "the social sciences")
        self.assertEqual(self.area("TK5105", "TK7800"), "electronics and computing")

    def test_shelf_codes(self):
        self.assertEqual(self.area("F HER", "F ADA"), "fiction")
        self.assertEqual(self.area("FIC-FIT", "FIC SMI"), "fiction")
        self.assertEqual(self.area("SF ADA", "SF HER"), "science fiction")
        self.assertEqual(self.area("MYST CHR", "MYST DOY"), "mystery")

    def test_codes_the_tables_do_not_cover_fall_back_to_a_general_phrase(self):
        self.assertEqual(self.area("ZZZ1", "ZZZ2"), "library science")                  # LC class Z is known; "Z" itself
        self.assertEqual(self.area("XQ 12", "XQ 13"), GENERIC_AREA)                     # an LC-shaped class that isn't one
        self.assertEqual(self.area("OBSCURE 1", "OBSCURE 2"), GENERIC_AREA)
        self.assertEqual(self.area("360", "360.4"), "the social sciences")              # a Dewey ten not in the table
        self.assertEqual(self.area("1", "2"), GENERIC_AREA)

    def test_nothing_in_common_or_unreadable_is_the_general_phrase_and_never_raises(self):
        for a, b in (("520", "QB981"), ("520", "620"), ("QB981", "HF5415"), ("", "520"), ("???", "???"), ("F HER", "SF ADA")):
            self.assertEqual(self.area(a, b), GENERIC_AREA, (a, b))
        self.assertEqual(shared_area(CallNumber(), CallNumber()), GENERIC_AREA)


class OddInputTest(unittest.TestCase):
    def test_nonsense_gives_an_empty_call_number_and_never_raises(self):
        for text in ("", "   ", None, 123, 4.5, [], "???", "5201", "12", "-", "\n", "日本語", "·" * 50):
            call = parse_call_number(text)
            self.assertIsInstance(call, CallNumber)
            self.assertFalse(call, repr(text))
            self.assertEqual(call.levels, (), repr(text))
            self.assertEqual(call.system, "", repr(text))

    def test_a_junk_tail_after_a_valid_number_is_ignored(self):
        self.assertEqual(levels("520x"), ("500", "520"))
        self.assertEqual(levels("QB981?!"), ("Q", "QB", "QB981"))

    def test_an_empty_call_number_is_not_close_to_anything(self):
        self.assertEqual(closeness(CallNumber(), parse_call_number("520.1")), 0.0)
        self.assertEqual(closeness(CallNumber(), CallNumber()), 0.0)


class ClosenessTest(unittest.TestCase):
    def close(self, a, b):
        return closeness(parse_call_number(a), parse_call_number(b))

    def test_the_same_place_is_1(self):
        self.assertEqual(self.close("520.1 SAG", "520.1 ABC"), 1.0)             # cutters don't matter
        self.assertEqual(self.close("QB981 .S24", "QB981 .H37"), 1.0)

    def test_closer_numbers_in_the_same_class_are_closer(self):
        scores = [self.close("523.8", other) for other in ("523.9", "526", "530", "590", "700")]
        self.assertEqual(scores, sorted(scores, reverse=True))                  # nearer number, higher closeness
        self.assertGreater(scores[0], 0.99)                                     # 523.8 and 523.9 are neighbours
        self.assertLess(scores[-1], 0.01)                                       # 500s and 700s share nothing

    def test_numeric_distance_inside_one_lc_subclass(self):
        self.assertGreater(self.close("QB981", "QB982"), 0.95)                  # next to each other
        self.assertLess(self.close("QB44.2", "QB981"), 0.25)                    # same subclass, nowhere near
        self.assertLess(self.close("QB44.2", "QB981"), CLOSE_BY)                # so not "close by"
        self.assertGreater(self.close("QB981", "QB982"), CLOSE_BY)
        self.assertGreater(self.close("QB981", "QB990"), self.close("QB981", "QB1100"))
        self.assertGreater(self.close("QB44.2", "QB44.9"), 0.95)                # decimals count too

    def test_the_same_subclass_beats_the_same_class_beats_nothing(self):
        same_sub = self.close("QB981", "QB1500")
        same_class = self.close("QB981", "QC5")
        other_class = self.close("QB981", "HF5415")
        self.assertGreater(same_sub, same_class)
        self.assertGreater(same_class, other_class)
        self.assertEqual(other_class, 0.0)

    def test_dewey_numbers_are_compared_inside_a_hundred(self):
        self.assertGreater(self.close("520.1", "520.2"), self.close("520.1", "530.2"))
        self.assertGreater(self.close("520", "523"), CLOSE_BY)
        self.assertLess(self.close("520", "599"), CLOSE_BY)                     # same hundred, far apart
        self.assertEqual(self.close("520", "620"), 0.0)                         # different hundreds

    def test_symmetric(self):
        for a, b in (("520.1", "523.8"), ("QB981", "QB44.2"), ("F HER", "F ADA"), ("520", "QB981")):
            self.assertEqual(self.close(a, b), self.close(b, a))

    def test_different_systems_are_not_close(self):
        self.assertEqual(self.close("520.1", "QB981"), 0.0)
        self.assertEqual(self.close("520.1", "F HER"), 0.0)
        self.assertEqual(self.close("QB981", "F HER"), 0.0)

    def test_fiction_codes_share_only_a_prefix_so_they_are_loosely_close(self):
        self.assertEqual(self.close("F HER", "F ADA"), PREFIX_ONLY_CLOSENESS)
        self.assertLess(PREFIX_ONLY_CLOSENESS, 0.5)
        self.assertEqual(self.close("F HER", "SF ADA"), 0.0)                    # different prefixes


if __name__ == "__main__":
    unittest.main()
