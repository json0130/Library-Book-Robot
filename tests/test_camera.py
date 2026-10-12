import types
import unittest
from unittest import mock

try:
    import cv2
except ImportError:
    cv2 = None


class FakeCapture:
    """Pretends to be cv2.VideoCapture: remembers what was set and reports it back, unless refused."""

    def __init__(self, index, accept=True):
        self.props, self.accept = {}, accept
        self.opened = True

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        if self.accept or prop == cv2.CAP_PROP_FOURCC:
            self.props[prop] = value
        return True

    def read(self):
        shape = (self.props.get(cv2.CAP_PROP_FRAME_HEIGHT, 480), self.props.get(cv2.CAP_PROP_FRAME_WIDTH, 640), 3)
        return True, types.SimpleNamespace(shape=shape)


@unittest.skipIf(cv2 is None, "needs OpenCV (sudo apt install python3-opencv)")
class CameraResolutionTest(unittest.TestCase):
    def make(self, *args, accept=True):
        from robot.drivers.camera import Camera
        with mock.patch("cv2.VideoCapture", side_effect=lambda i: FakeCapture(i, accept)):
            return Camera(*args)

    def test_requested_size_sets_mjpg_first_and_is_reported(self):
        cam = self.make(0, 1280, 720)
        self.assertEqual((cam.width, cam.height), (1280, 720))
        self.assertEqual(cam.cap.props[cv2.CAP_PROP_FOURCC], cv2.VideoWriter_fourcc(*"MJPG"))
        self.assertEqual(cam.cap.props[cv2.CAP_PROP_FRAME_WIDTH], 1280)

    def test_no_size_requested_leaves_the_camera_alone(self):
        cam = self.make(0)
        self.assertEqual(cam.cap.props, {})
        self.assertEqual((cam.width, cam.height), (640, 480))

    def test_a_camera_that_refuses_the_size_is_used_as_it_is_with_a_warning(self):
        with self.assertLogs("robot.camera", level="WARNING") as logs:
            cam = self.make(0, 1280, 720, accept=False)
        self.assertEqual((cam.width, cam.height), (640, 480))
        self.assertIn("640x480 instead of the requested 1280x720", logs.output[0])

    def test_config_asks_for_720p(self):
        from robot.config import load_config
        dev = load_config().robot["devices"]
        self.assertEqual((dev["camera_width"], dev["camera_height"]), (1280, 720))


if __name__ == "__main__":
    unittest.main()
