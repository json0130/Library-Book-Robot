"""The launch files: vslam.launch's camera does only what RTAB-Map needs; amcl_nav.launch and lidar_mapping.launch use the LiDAR only,
and the map is made from the same filtered scan AMCL later reads."""

import os
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "config")
LAUNCH = os.path.join(CONFIG, "vslam.launch")


def args_of(root):
    return {a.get("name"): a.get("default", a.get("value")) for a in root.findall("arg")}


def params_of(node):
    return {p.get("name"): p.get("value") for p in node.findall("param")}


def node_named(root, name):
    found = [n for n in root.iter("node") if n.get("name") == name]
    if len(found) != 1:
        raise AssertionError("expected one node %r, found %d" % (name, len(found)))
    return found[0]


def resolved_scan(root, node):
    """The topic a node's `scan` is remapped to, with $(arg ...) looked up."""
    target = {r.get("from"): r.get("to") for r in node.findall("remap")}["scan"]
    if target.startswith("$(arg "):
        target = args_of(root)[target[6:-1]]
    return target


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


class LidarOnly:
    """Both LiDAR launch files: no camera, the Hokuyo on /hokuyo_scan, the same LiDAR settings as vslam.launch."""
    FILE = ""

    def setUp(self):
        self.root = ET.parse(os.path.join(CONFIG, self.FILE)).getroot()

    def test_no_camera_is_started(self):
        for include in self.root.iter("include"):
            self.assertNotIn("3dsensor", include.get("file", ""))
        self.assertFalse([n for n in self.root.iter("node") if n.get("pkg") in ("rtabmap_slam", "rtabmap_ros", "astra_camera")])

    def test_the_lidar_is_set_up_like_in_vslam_launch(self):
        vslam = args_of(ET.parse(LAUNCH).getroot())
        mine = args_of(self.root)
        for name in ("port", "laser_x", "laser_y", "laser_z", "laser_yaw"):
            self.assertEqual(mine.get(name), vslam.get(name), name)
        hokuyo = node_named(self.root, "hokuyo")
        self.assertEqual(hokuyo.get("pkg"), "urg_node")
        self.assertEqual({r.get("from"): r.get("to") for r in hokuyo.findall("remap")}, {"scan": "hokuyo_scan"})

    def test_it_reads_the_filtered_scan(self):
        self.assertEqual(args_of(self.root)["scan_topic"], "/hokuyo_scan_filtered")


class AmclLaunchTest(LidarOnly, unittest.TestCase):
    FILE = "amcl_nav.launch"

    def test_the_saved_map_amcl_and_move_base_are_started(self):
        self.assertIn("$(arg map_file)", node_named(self.root, "map_server").get("args"))
        self.assertTrue(args_of(self.root)["map_file"].endswith("maps/lidar_map.yaml"))
        self.assertEqual(resolved_scan(self.root, node_named(self.root, "amcl")), "/hokuyo_scan_filtered")
        move_base = [i for i in self.root.iter("include") if "move_base.launch.xml" in i.get("file", "")]
        self.assertEqual(len(move_base), 1)
        passed = {a.get("name"): a.get("value") for a in move_base[0].findall("arg")}
        self.assertEqual(passed["laser_topic"], "$(arg scan_topic)")
        self.assertEqual(passed["custom_param_file"], "$(arg costmap_params)")
        self.assertTrue(args_of(self.root)["costmap_params"].endswith("hokuyo_costmap.yaml"))

    def test_amcl_is_set_for_this_lidar_and_these_frames(self):
        p = params_of(node_named(self.root, "amcl"))
        self.assertLessEqual(float(p["laser_max_range"]), 5.6)                  # URG-04LX, not the TurtleBot default 12 m
        self.assertEqual((p["odom_frame_id"], p["base_frame_id"], p["global_frame_id"]), ("odom", "base_footprint", "map"))
        self.assertEqual(p["use_map_topic"], "false")
        self.assertLess(float(p["update_min_d"]), 0.25)                          # /amcl_pose about once a second while driving


class LidarMappingLaunchTest(LidarOnly, unittest.TestCase):
    FILE = "lidar_mapping.launch"

    def test_gmapping_makes_a_5_cm_map_from_the_filtered_scan(self):
        gmapping = node_named(self.root, "slam_gmapping")
        self.assertEqual(resolved_scan(self.root, gmapping), "/hokuyo_scan_filtered")
        p = params_of(gmapping)
        self.assertEqual(float(p["delta"]), 0.05)
        self.assertEqual((p["base_frame"], p["odom_frame"], p["map_frame"]), ("base_footprint", "odom", "map"))


if __name__ == "__main__":
    unittest.main()
