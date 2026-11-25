import asyncio
import threading
import unittest
import urllib.request

from display.server import Hub, make_server
from robot.camera_feed import CameraFeed, draw_faces

JPG = b"\xff\xd8\xff\xe0jpeg\xff\xd9"


class JpegCamera:
    def peek_jpeg(self, quality=85):
        return JPG


class CameraFeedTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.server = make_server(self.hub, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def test_the_robot_sends_the_picture_and_the_page_can_fetch_it(self):
        feed = CameraFeed(JpegCamera(), None, "127.0.0.1", self.port)
        self.assertTrue(asyncio.run(feed.step()))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/camera.jpg", timeout=5) as r:
            self.assertEqual(r.read(), JPG)

    def test_a_missing_display_server_is_reported_so_the_feed_backs_off(self):
        feed = CameraFeed(JpegCamera(), None, "127.0.0.1", 1)
        self.assertFalse(asyncio.run(feed.step()))

    def test_a_camera_without_pictures_is_not_an_error(self):
        self.assertTrue(asyncio.run(CameraFeed(object(), None, "127.0.0.1", self.port).step()))

    def test_faces_are_boxed_with_their_person_number(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV is not installed")
        frame = np.zeros((100, 100, 3), np.uint8)
        draw_faces(cv2, frame, [{"box": [10, 60, 60, 10], "person": 2}, {"box": [70, 90, 95, 70]}])
        self.assertGreater(int(frame.sum()), 0)


class FrameCamera:
    """Looks like the real Camera: peek() gives the same frame object until a new one arrives."""

    def __init__(self, width=640, height=480):
        import numpy as np
        self.frame = np.zeros((height, width, 3), np.uint8)

    def peek(self):
        return self.frame

    def encode_jpeg(self, frame, quality=85):
        import cv2
        return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()


class FeedSpeedTest(unittest.TestCase):
    def setUp(self):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV is not installed")

    def test_the_picture_is_shrunk_and_a_frame_is_not_sent_twice(self):
        import cv2
        import numpy as np
        feed = CameraFeed(FrameCamera(640, 480), None, width=320)
        first = feed.jpeg()
        self.assertEqual(cv2.imdecode(np.frombuffer(first, np.uint8), 1).shape[:2], (240, 320))
        self.assertIsNone(feed.jpeg())                                   # same frame: nothing new to send
        feed.camera.frame = feed.camera.frame.copy()                     # a new frame arrives
        self.assertIsNotNone(feed.jpeg())

    def test_a_small_picture_is_not_enlarged(self):
        import cv2
        import numpy as np
        feed = CameraFeed(FrameCamera(200, 100), None, width=480)
        self.assertEqual(cv2.imdecode(np.frombuffer(feed.jpeg(), np.uint8), 1).shape[:2], (100, 200))

    def test_the_feed_slows_down_when_nobody_is_watching(self):
        hub = Hub()
        server = make_server(hub, port=0, host="127.0.0.1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        feed = CameraFeed(JpegCamera(), None, "127.0.0.1", server.server_address[1])
        self.assertTrue(feed.post(JPG))
        self.assertEqual(feed.viewers, 0)                                # the server says: no page has the stream open
        stream = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/camera.mjpg", timeout=5)
        self.addCleanup(stream.close)
        stream.readline()
        self.assertTrue(feed.post(JPG))
        self.assertEqual(feed.viewers, 1)


if __name__ == "__main__":
    unittest.main()
