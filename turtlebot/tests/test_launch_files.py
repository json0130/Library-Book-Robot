"""config/vslam.launch: the camera does only what RTAB-Map needs, and nothing else goes missing."""

import os
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH = os.path.join(HERE, "..", "config", "vslam.launch")


class VslamLaunchTest(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(LAUNCH).getroot()

    def camera_include(self):
        for node in self.root.findall("include"):
            if "3dsensor.launch" in node.get("file", ""):
                return node
        self.fail("the camera is not included")

    def test_the_unused_camera_processing_follows_camera_extras_which_is_off_by_default(self):
        extras = [a for a in self.root.findall("arg") if a.get("name") == "camera_extras"]
        self.assertEqual([a.get("default") for a in extras], ["false"])
        passed = {a.get("name"): a.get("value") for a in self.camera_include().findall("arg")}
        for name in ("ir_processing", "depth_processing", "disparity_processing", "disparity_registered_processing", "scan_processing"):
            self.assertEqual(passed.get(name), "$(arg camera_extras)", name)

    def test_what_rtabmap_reads_is_not_switched_off(self):
        passed = {a.get("name") for a in self.camera_include().findall("arg")}
        self.assertNotIn("rgb_processing", passed)               # /camera/rgb/image_rect_color
        self.assertNotIn("depth_registered_processing", passed)
        self.assertNotIn("depth_registration", passed)           # /camera/depth_registered/image_raw


if __name__ == "__main__":
    unittest.main()
