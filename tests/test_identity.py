import math
import unittest

from robot.perception.identity import MAX_EMBEDDINGS, PersonTracker, iou


def unit(*parts):
    norm = math.sqrt(sum(p * p for p in parts))
    return tuple(p / norm for p in parts)


class Vec(tuple):
    """A tuple that supports `@` as a dot product, standing in for a numpy vector."""

    def __matmul__(self, other):
        return sum(a * b for a, b in zip(self, other))


def vec(*parts):
    return Vec(unit(*parts))


ALICE = vec(1, 0, 0)
ALICE_TURNED = vec(0.3, 0.954, 0)           # cosine 0.30 with ALICE: below the 0.40 threshold, above 0.24
BOB = vec(0, 1, 0)
CAROL = vec(0, 0, 1)
BOX_A, BOX_B = [100, 300, 300, 100], [100, 600, 300, 400]
BOX_A_NEAR = [110, 310, 310, 110]


class FakeEmbedder:
    """The 'frame' is a dict {tuple(box): vector}; a missing box means too small to embed."""

    def embed(self, frame, box):
        return frame.get(tuple(box))


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def detections(*boxes):
    return [{"box": list(b), "detection_confidence": 0.99} for b in boxes]


def tracker(**kwargs):
    clock = Clock()
    return PersonTracker(FakeEmbedder(), clock=clock, **kwargs), clock


class PersonTrackerTest(unittest.TestCase):
    def test_the_first_face_is_person_1_and_stays_person_1(self):
        t, clock = tracker()
        self.assertEqual(t({tuple(BOX_A): ALICE}, detections(BOX_A)), [1])
        clock.now += 1
        self.assertEqual(t({tuple(BOX_A): ALICE}, detections(BOX_A)), [1])

    def test_a_different_face_is_person_2(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        self.assertEqual(t({tuple(BOX_A): BOB}, detections(BOX_A)), [2])

    def test_a_person_who_left_and_comes_back_is_recognised(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        clock.now += 1
        t({tuple(BOX_B): BOB}, detections(BOX_B))                       # someone else is there meanwhile
        clock.now += 600                                                # ten minutes, nobody known in view
        self.assertEqual(t({tuple(BOX_B): ALICE}, detections(BOX_B)), [1])     # at a different place
        self.assertEqual(t({tuple(BOX_A): BOB}, detections(BOX_A)), [2])

    def test_two_faces_in_one_frame_are_two_people(self):
        t, _ = tracker()
        self.assertEqual(t({tuple(BOX_A): ALICE, tuple(BOX_B): BOB}, detections(BOX_A, BOX_B)), [1, 2])
        self.assertEqual(t({tuple(BOX_A): BOB, tuple(BOX_B): ALICE}, detections(BOX_A, BOX_B)), [2, 1])

    def test_one_person_is_never_given_to_two_faces(self):
        t, _ = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        twin = vec(0.99, 0.141, 0)                                      # nearly the same as ALICE
        result = t({tuple(BOX_A): ALICE, tuple(BOX_B): twin}, detections(BOX_A, BOX_B))
        self.assertEqual(sorted(result), [1, 2])

    def test_turning_the_head_in_place_keeps_the_person(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        clock.now += 1
        self.assertEqual(t({tuple(BOX_A_NEAR): ALICE_TURNED}, detections(BOX_A_NEAR)), [1])

    def test_the_same_odd_view_somewhere_else_is_a_new_person(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        clock.now += 1
        self.assertEqual(t({tuple(BOX_B): ALICE_TURNED}, detections(BOX_B)), [2])

    def test_continuity_expires(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        clock.now += 30
        self.assertEqual(t({tuple(BOX_A_NEAR): ALICE_TURNED}, detections(BOX_A_NEAR)), [2])

    def test_a_face_too_small_to_tell_apart_is_not_a_person(self):
        t, _ = tracker()
        self.assertEqual(t({}, detections(BOX_A)), [None])
        self.assertEqual(t.people, [])

    def test_views_are_diverse_and_capped(self):
        t, clock = tracker()
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        t({tuple(BOX_A): ALICE}, detections(BOX_A))
        self.assertEqual(len(t.people[0].views), 1)                     # the same view isn't stored twice
        for i in range(MAX_EMBEDDINGS + 3):
            angle = 0.05 * (i + 1)
            clock.now += 1
            t({tuple(BOX_A): vec(math.cos(angle * 6), math.sin(angle * 6), 0)}, detections(BOX_A))
        self.assertLessEqual(len(t.people[0].views), MAX_EMBEDDINGS)

    def test_a_stricter_threshold_makes_more_people(self):
        for threshold, expected in ((0.2, [1]), (0.9, [2])):
            t, _ = tracker(threshold=threshold)
            t({tuple(BOX_B): ALICE}, detections(BOX_B))
            self.assertEqual(t({tuple(BOX_A): vec(0.6, 0.8, 0)}, detections(BOX_A)), expected)


class IouTest(unittest.TestCase):
    def test_overlap(self):
        self.assertEqual(iou(BOX_A, BOX_A), 1.0)
        self.assertEqual(iou(BOX_A, BOX_B), 0.0)
        self.assertGreater(iou(BOX_A, BOX_A_NEAR), 0.8)


if __name__ == "__main__":
    unittest.main()
