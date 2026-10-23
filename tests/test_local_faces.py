import unittest

from robot.services import local_faces
from robot.services.local_faces import LocalFaceDetector, LocalFacesUnavailable, find_cascade

try:
    import cv2
    import numpy as np
except ImportError:                                     # the tests that need OpenCV skip
    cv2 = None


@unittest.skipIf(cv2 is None or find_cascade(cv2) is None, "OpenCV or its face cascade is not installed")
class LocalFaceDetectorTest(unittest.TestCase):
    def jpeg(self, image):
        return cv2.imencode(".jpg", image)[1].tobytes()

    def test_a_picture_with_no_face_gives_no_detections(self):
        detector = LocalFaceDetector()
        self.assertEqual(detector.detect(self.jpeg(np.zeros((240, 320, 3), np.uint8))), [])
        noise = np.random.default_rng(1).integers(0, 255, (240, 320, 3), dtype=np.uint8)
        self.assertEqual(detector.detect(self.jpeg(noise)), [])

    def test_bad_data_is_a_value_error_like_the_server_client(self):
        with self.assertRaises(ValueError):
            LocalFaceDetector().detect(b"not an image")

    def test_detections_have_the_servers_shape(self):
        detector = LocalFaceDetector()
        detector.cascade = type("C", (), {"detectMultiScale": lambda self, *a, **k: [(10, 20, 50, 60)]})()
        (found,) = detector.detect(self.jpeg(np.zeros((100, 100), np.uint8)))
        self.assertEqual(found["box"], [20, 60, 80, 10])                          # top, right, bottom, left
        self.assertIn("detection_confidence", found)


class MissingCascadeTest(unittest.TestCase):
    def test_a_missing_cascade_says_what_to_install(self):
        original = local_faces.find_cascade
        local_faces.find_cascade = lambda cv2=None: None
        self.addCleanup(setattr, local_faces, "find_cascade", original)
        if cv2 is None:
            self.skipTest("OpenCV is not installed")
        with self.assertRaises(LocalFacesUnavailable) as caught:
            LocalFaceDetector()
        self.assertIn("haarcascade_frontalface_default.xml", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
