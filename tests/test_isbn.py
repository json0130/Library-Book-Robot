import unittest

from robot.services.isbn import (is_valid_isbn10, is_valid_isbn13, isbn10_to_isbn13, normalize, to_isbn13)


class IsbnTest(unittest.TestCase):
    def test_valid_isbn13(self):
        self.assertTrue(is_valid_isbn13("9780345391803"))
        self.assertTrue(is_valid_isbn13("978-0-345-39180-3"))
        self.assertTrue(is_valid_isbn13("979-10-90636-07-1"))        # 979 prefix
        self.assertEqual(to_isbn13("978 0 345 39180 3"), "9780345391803")

    def test_invalid_checksum(self):
        self.assertFalse(is_valid_isbn13("9780345391804"))
        self.assertIsNone(to_isbn13("9780345391804"))
        self.assertFalse(is_valid_isbn10("0345391803"))
        self.assertIsNone(to_isbn13("0345391803"))

    def test_isbn10_converts_to_isbn13(self):
        self.assertEqual(isbn10_to_isbn13("0-345-39180-2"), "9780345391803")
        self.assertEqual(to_isbn13("0345391802"), "9780345391803")
        self.assertEqual(to_isbn13("080442957X"), "9780804429573")      # check digit X
        self.assertEqual(to_isbn13("0-8044-2957-x"), "9780804429573")   # lower-case x
        with self.assertRaises(ValueError):
            isbn10_to_isbn13("0345391803")

    def test_hyphens_and_spaces(self):
        self.assertEqual(normalize(" 978-0 345-39180-3 "), "9780345391803")
        self.assertEqual(normalize("0-8044-2957-x"), "080442957X")

    def test_ean13_that_is_not_an_isbn(self):
        self.assertIsNone(to_isbn13("5901234123457"))     # valid EAN-13, but not 978/979
        self.assertFalse(is_valid_isbn13("5901234123457"))
        self.assertIsNone(to_isbn13("4006381333931"))

    def test_price_addons_and_junk_are_rejected(self):
        self.assertIsNone(to_isbn13("51299"))             # 5-digit price add-on on its own
        self.assertIsNone(to_isbn13("12"))                # 2-digit add-on
        self.assertEqual(to_isbn13("978034539180351299"), "9780345391803")   # EAN-13 + add-on run together
        for junk in ("", "abc", "97803453918", "97803453918033", "http://example.com", None):
            self.assertIsNone(to_isbn13(junk if junk is not None else ""))


if __name__ == "__main__":
    unittest.main()
