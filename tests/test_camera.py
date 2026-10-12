import time
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

    def release(self):
        self.opened = False

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

    def test_config_asks_for_a_camera_size(self):
        from robot.config import load_config
        dev = load_config().robot["devices"]
        self.assertEqual((dev["camera_width"], dev["camera_height"]), (640, 480))


class CountingCapture(FakeCapture):
    """A capture that produces numbered frames at about 100 per second, like a real camera, and can
    be told to fail."""

    def __init__(self, index):
        super().__init__(index)
        self.n, self.fail = 0, False

    def read(self):
        time.sleep(0.01)
        if self.fail:
            return False, None
        self.n += 1
        return True, types.SimpleNamespace(shape=(480, 640, 3), n=self.n)


@unittest.skipIf(cv2 is None, "needs OpenCV (sudo apt install python3-opencv)")
class BackgroundCameraTest(unittest.TestCase):
    def make(self):
        from robot.drivers.camera import Camera
        with mock.patch("cv2.VideoCapture", side_effect=CountingCapture):
            cam = Camera(0, background=True)
        self.addCleanup(cam.close)
        return cam

    def test_a_slow_reader_gets_the_newest_frame_not_a_queued_old_one(self):
        cam = self.make()
        cam.read_frame()
        time.sleep(0.3)                          # the reader is busy (decoding) while ~30 frames go by
        frame = cam.read_frame()
        self.assertGreater(frame.n, cam.cap.n - 5)               # one of the latest, not frame 7
        self.assertLess((time.monotonic() - cam.last_frame_time) * 1000, 100)

    def test_each_call_waits_for_a_frame_it_has_not_seen(self):
        cam = self.make()
        first = cam.read_frame()
        second = cam.read_frame()
        self.assertGreater(second.n, first.n)

    def test_close_stops_the_thread_and_releases_the_camera(self):
        cam = self.make()
        released = []
        cam.cap.release = lambda: released.append(True)
        thread = cam._thread
        cam.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(released, [True])

    def test_a_dead_camera_raises_instead_of_hanging(self):
        cam = self.make()
        cam.read_frame()
        cam.cap.fail = True
        time.sleep(0.2)
        with self.assertRaises(OSError):
            cam.read_frame(timeout=0.3)

    def test_without_background_nothing_runs_on_a_thread(self):
        from robot.drivers.camera import Camera
        with mock.patch("cv2.VideoCapture", side_effect=CountingCapture):
            cam = Camera(0)
        self.assertIsNone(cam._thread)
        self.assertEqual(cam.read_frame().n, cam.cap.n)


if __name__ == "__main__":
    unittest.main()
