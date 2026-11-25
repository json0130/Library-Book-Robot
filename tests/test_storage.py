import json
import logging
import tempfile
import unittest
from pathlib import Path

from robot.core.storage import Storage, StoredBook


def book(isbn, shelf="2-SF-A", floor=2, title=None, pose=(1.0, 2.0, 1.57), section="Science Fiction"):
    x, y, yaw = pose if pose else (None, None, None)
    return StoredBook(isbn=isbn, title=title or f"Book {isbn}", shelf=shelf, section=section, floor=floor,
                      zone="common", map_x=x, map_y=y, map_yaw=yaw)


class StorageTest(unittest.TestCase):
    def test_slots_start_empty_and_the_lowest_free_one_is_reserved(self):
        s = Storage(4)
        self.assertTrue(s.empty)
        self.assertEqual(s.free_slots(), [0, 1, 2, 3])
        self.assertEqual(s.reserve(), 0)
        self.assertEqual(s.reserve(), 1)                              # a promised slot is not offered again
        self.assertEqual(len(s), 0)                                   # and is not a stored book
        s.unreserve(0)
        self.assertEqual(s.reserve(), 0)

    def test_the_preferred_slot_is_used_when_it_is_free_else_the_lowest(self):
        s = Storage(8)
        self.assertEqual(s.reserve(preferred=5), 5)
        s.commit(5, book("a"))
        self.assertEqual(s.reserve(preferred=5), 0)
        self.assertEqual(s.reserve(preferred=99), 1)                  # out of range: any free slot

    def test_commit_release_and_the_queue_view(self):
        s = Storage(8)
        s.commit(s.reserve(3), book("a"))
        s.commit(s.reserve(1), book("b"))
        self.assertEqual([b.isbn for b in s.stored()], ["b", "a"])    # in slot order
        self.assertEqual(s.slot_of("a"), 3)
        self.assertEqual(s.release(3).isbn, "a")
        self.assertIsNone(s.slot_of("a"))
        self.assertEqual(len(s), 1)
        with self.assertRaises(ValueError):
            s.commit(1, book("c"))                                    # occupied
        with self.assertRaises(ValueError):
            s.commit(8, book("c"))                                    # outside the ring

    def test_full_means_no_free_and_no_promised_slot(self):
        s = Storage(2)
        s.reserve()
        self.assertFalse(s.full)
        s.reserve()
        self.assertTrue(s.full)
        self.assertIsNone(s.reserve())

    def test_stops_group_books_by_shelf_ordered_by_floor_then_shelf(self):
        s = Storage(8)
        for isbn, shelf, floor in (("a", "3-HIS-A", 3), ("b", "2-SF-B", 2), ("c", "2-SF-A", 2), ("d", "2-SF-B", 2)):
            s.commit(s.reserve(), book(isbn, shelf, floor))
        stops = s.stops()
        self.assertEqual([x.shelf for x in stops], ["2-SF-A", "2-SF-B", "3-HIS-A"])
        self.assertEqual(stops[1].slots, [1, 3])
        self.assertEqual(stops[1].titles, ["Book b", "Book d"])
        self.assertEqual((stops[1].x, stops[1].y, stops[1].yaw), (1.0, 2.0, 1.57))
        self.assertEqual([x.shelf for x in s.stops(first="3-HIS-A")], ["3-HIS-A", "2-SF-A", "2-SF-B"])

    def test_books_without_a_map_pose_are_stored_but_not_driven_to(self):
        s = Storage(4)
        s.commit(s.reserve(), book("a", pose=None))
        s.commit(s.reserve(), book("b"))
        self.assertEqual(len(s), 2)
        self.assertEqual([b.isbn for b in s.returnable()], ["b"])
        self.assertEqual([x.shelf for x in s.stops()], ["2-SF-A"])


class StoragePersistenceTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state" / "storage.json"

    def test_what_is_stored_survives_a_restart(self):
        s = Storage(8, self.path)
        s.commit(s.reserve(4), book("a", title="The Martian"))
        again = Storage(8, self.path)
        self.assertEqual(again.slot_of("a"), 4)
        self.assertEqual(again.books[4].title, "The Martian")
        self.assertEqual(again.books[4].map_yaw, 1.57)
        again.release(4)
        self.assertTrue(Storage(8, self.path).empty)

    def test_a_reserved_slot_is_not_saved(self):
        s = Storage(8, self.path)
        s.reserve(2)
        s.commit(s.reserve(), book("a"))
        self.assertEqual(Storage(8, self.path).free_slots(), [1, 2, 3, 4, 5, 6, 7])

    def test_the_file_holds_books_only(self):
        s = Storage(8, self.path)
        s.commit(s.reserve(), book("a"))
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data), {"slots", "books"})
        self.assertEqual(set(data["books"][0]) - {"stored_at"}, {"isbn", "title", "shelf", "section", "floor", "zone",
                                                                 "map_x", "map_y", "map_yaw", "slot"})

    def test_a_missing_file_is_an_empty_ring_and_a_corrupt_one_is_set_aside(self):
        self.assertTrue(Storage(8, self.path).empty)
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        s = Storage(8, self.path)
        self.assertTrue(s.empty)
        self.assertTrue(self.path.with_suffix(".bad").is_file())
        self.assertFalse(self.path.exists())
        s.commit(s.reserve(), book("a"))                              # and the robot carries on
        self.assertEqual(len(Storage(8, self.path)), 1)

    def test_wrong_shaped_data_is_also_set_aside(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"slots": 8, "books": "oops"}))
        self.assertTrue(Storage(8, self.path).empty)
        self.assertTrue(self.path.with_suffix(".bad").is_file())

    def test_saved_books_beyond_a_smaller_ring_are_left_out(self):
        s = Storage(8, self.path)
        s.commit(s.reserve(6), book("a"))
        s.commit(s.reserve(1), book("b"))
        small = Storage(4, self.path)
        self.assertEqual([b.isbn for b in small.stored()], ["b"])

    def test_no_path_means_memory_only(self):
        s = Storage(4)
        s.commit(s.reserve(), book("a"))
        self.assertEqual(len(s), 1)

    def test_clear_empties_the_ring_and_the_file(self):
        s = Storage(8, self.path)
        s.commit(s.reserve(), book("a"))
        s.clear()
        self.assertTrue(Storage(8, self.path).empty)

    def test_a_ring_needs_a_slot(self):
        with self.assertRaises(ValueError):
            Storage(0)


if __name__ == "__main__":
    unittest.main()
