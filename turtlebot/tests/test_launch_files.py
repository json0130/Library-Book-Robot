"""The launch files: vslam.launch's camera does only what RTAB-Map needs; amcl_nav.launch and lidar_mapping.launch localise and map
with the LiDAR only, from the same filtered scan; amcl_nav.launch adds the depth camera as an obstacle sensor (depth only, small,
slow) and the Kobuki safety controller."""

import os
import re
import unittest
import xml.etree.ElementTree as ET

import yaml

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
    """Both LiDAR launch files: no camera for localising, the Hokuyo on /hokuyo_scan, the same LiDAR settings as vslam.launch."""
    FILE = ""

    def setUp(self):
        self.root = ET.parse(os.path.join(CONFIG, self.FILE)).getroot()

    def test_no_camera_is_started_for_localising(self):
        for include in self.root.iter("include"):
            self.assertNotIn("3dsensor", include.get("file", ""))
        self.assertFalse([n for n in self.root.iter("node") if n.get("pkg") in ("rtabmap_slam", "rtabmap_ros", "astra_camera")])
        includes = [i.get("file", "") for i in self.root.iter("include")]
        for include in includes:                                   # the only camera: the obstacle one, and only if asked for
            if "depth_obstacles.launch" in include:
                self.assertEqual([i.get("if") for i in self.root.iter("include") if i.get("file") == include],
                                 ["$(arg depth_obstacles)"])

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
        move_base = node_named(self.root, "move_base")
        self.assertEqual(resolved_scan(self.root, move_base), "/hokuyo_scan_filtered")
        self.assertEqual({r.get("from"): r.get("to") for r in move_base.findall("remap")}["cmd_vel"],
                         "navigation_velocity_smoother/raw_cmd_vel")

    def move_base_files(self):
        return [(r.get("file"), r.get("ns"), r.get("if")) for r in node_named(self.root, "move_base").findall("rosparam")]

    def test_move_base_gets_the_stock_turtlebot_files_then_ours_in_order(self):
        files = [f for f, _, _ in self.move_base_files()]
        stock = [f for f in files if f.startswith("$(find turtlebot_navigation)")]
        self.assertEqual([os.path.basename(f) for f in stock],
                         ["costmap_common_params.yaml", "costmap_common_params.yaml", "local_costmap_params.yaml",
                          "global_costmap_params.yaml", "dwa_local_planner_params.yaml", "move_base_params.yaml",
                          "global_planner_params.yaml", "navfn_global_planner_params.yaml"])
        self.assertEqual(files[len(stock):], ["$(arg costmap_params)", "$(arg shape_params)", "$(arg depth_params)"])
        self.assertEqual(self.move_base_files()[-1][2], "$(arg depth_obstacles)")
        args = args_of(self.root)
        for name, file in (("costmap_params", "hokuyo_costmap.yaml"), ("shape_params", "robot_shape.yaml"),
                           ("depth_params", "depth_costmap.yaml")):
            self.assertTrue(args[name].endswith(file), name)
            self.assertTrue(os.path.isfile(os.path.join(CONFIG, file)), file)

    def test_the_velocity_smoother_and_the_safety_controller_are_started_as_with_the_stock_move_base(self):
        files = [i.get("file") for i in self.root.iter("include")]
        self.assertIn("$(find turtlebot_navigation)/launch/includes/velocity_smoother.launch.xml", files)
        self.assertIn("$(find turtlebot_navigation)/launch/includes/safety_controller.launch.xml", files)

    def test_the_obstacle_heights_follow_robot_height(self):
        args = args_of(self.root)
        self.assertEqual((args["robot_height"], args["depth_obstacles"]), ("1.2", "true"))
        self.assertEqual(args["max_obstacle_height"], "$(eval arg('robot_height') + 0.05)")
        p = {q.get("name"): (q.get("value"), q.get("if")) for q in node_named(self.root, "move_base").findall("param")}
        for costmap in ("local_costmap", "global_costmap"):
            for key in ("obstacle_layer/max_obstacle_height", "obstacle_layer/depth_mark/max_obstacle_height",
                        "obstacle_layer/depth_clear/max_obstacle_height"):
                self.assertEqual(p["%s/%s" % (costmap, key)], ("$(arg max_obstacle_height)", "$(arg depth_obstacles)"))
            self.assertEqual(p[costmap + "/obstacle_layer/z_resolution"],
                             ("$(eval (arg('robot_height') + 0.05) / 16.0)", "$(arg depth_obstacles)"))

    def test_the_camera_mount_is_passed_on_and_defaults_to_the_measured_one(self):
        args = args_of(self.root)
        include = [i for i in self.root.iter("include") if "depth_obstacles.launch" in i.get("file", "")][0]
        passed = {a.get("name"): a.get("value") for a in include.findall("arg")}
        depth = args_of(ET.parse(os.path.join(CONFIG, "depth_obstacles.launch")).getroot())
        for name in ("camera_x", "camera_y", "camera_z", "camera_pitch", "camera_yaw"):
            self.assertEqual(passed[name], "$(arg %s)" % name)
            self.assertEqual(args[name], depth[name], name)
        self.assertEqual((float(args["camera_x"]), float(args["camera_z"]), float(args["camera_pitch"])), (-0.08, 0.33, 0.0))

    def test_amcl_is_set_for_this_lidar_and_these_frames(self):
        p = params_of(node_named(self.root, "amcl"))
        self.assertLessEqual(float(p["laser_max_range"]), 5.6)                  # URG-04LX, not the TurtleBot default 12 m
        self.assertEqual((p["odom_frame_id"], p["base_frame_id"], p["global_frame_id"]), ("odom", "base_footprint", "map"))
        self.assertEqual(p["use_map_topic"], "false")
        self.assertLess(float(p["update_min_d"]), 0.25)                          # /amcl_pose about once a second while driving

    def test_amcl_shrugs_off_what_is_not_on_the_map(self):
        p = params_of(node_named(self.root, "amcl"))
        self.assertEqual(p["laser_model_type"], "likelihood_field_prob")
        self.assertEqual(p["do_beamskip"], "true")
        self.assertGreaterEqual(float(p["laser_z_rand"]), 0.3)                  # the default 0.05 punishes every beam on a chair
        self.assertAlmostEqual(float(p["laser_z_hit"]) + float(p["laser_z_rand"]), 1.0)
        self.assertTrue(30 <= int(p["laser_max_beams"]) <= 120)


class DepthObstaclesLaunchTest(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(os.path.join(CONFIG, "depth_obstacles.launch")).getroot()

    def nodelet(self, plugin):
        found = [n for n in self.root.iter("node") if n.get("pkg") == "nodelet" and plugin in (n.get("args") or "")]
        self.assertEqual(len(found), 1, plugin)
        return found[0]

    def test_the_astra_driver_runs_depth_only_small_and_slow(self):
        driver = self.nodelet("astra_camera/AstraDriverNodelet")
        p = params_of(driver)
        args = args_of(self.root)
        self.assertEqual((p["depth_mode"], args["depth_mode"]), ("$(arg depth_mode)", "8"))   # QVGA_30Hz: 320 x 240
        self.assertEqual((p["data_skip"], args["data_skip"]), ("$(arg data_skip)", "4"))      # 1 frame in 5: 6 Hz
        self.assertEqual(p["depth_registration"], "false")
        plugins = {n.get("args").split()[1] for n in self.root.iter("node") if (n.get("args") or "").startswith("load ")}
        self.assertEqual(plugins, {"astra_camera/AstraDriverNodelet", "depth_image_proc/point_cloud_xyz", "pcl/VoxelGrid"})
        # nothing asks for colour: no image_proc/rectify or debayer, no depth_image_proc/register or point_cloud_xyzrgb
        self.assertFalse(list(self.root.iter("include")))                        # not 3dsensor.launch: its chain starts colour

    def test_depth_becomes_a_5_cm_cloud_in_one_nodelet_manager(self):
        manager = [n for n in self.root.iter("node") if n.get("args") == "manager"]
        self.assertEqual(len(manager), 1)
        name = manager[0].get("name")
        cloud = self.nodelet("depth_image_proc/point_cloud_xyz")
        voxels = self.nodelet("pcl/VoxelGrid")
        for n in (self.nodelet("astra_camera/AstraDriverNodelet"), cloud, voxels):
            self.assertTrue(n.get("args").endswith(" " + name))
        remaps = {r.get("from"): r.get("to") for r in cloud.findall("remap")}
        self.assertEqual(remaps["image_rect"], "depth/image_raw")
        v = {r.get("from"): r.get("to") for r in voxels.findall("remap")}
        self.assertEqual((v["~input"], remaps["points"]), ("depth/points", "depth/points"))
        self.assertEqual(v["~output"], "depth/points_obstacles")
        self.assertEqual(args_of(self.root)["voxel_leaf"], "0.05")
        self.assertEqual(params_of(voxels)["leaf_size"], "$(arg voxel_leaf)")

    def test_the_cloud_reaches_the_topic_the_costmap_reads(self):
        with open(os.path.join(CONFIG, "depth_costmap.yaml")) as f:
            costmap = yaml.safe_load(f)
        ns = [g for g in self.root.iter("group") if g.get("ns")][0].get("ns")
        self.assertEqual(ns, "$(arg camera)")
        topic = "/%s/depth/points_obstacles" % args_of(self.root)["camera"]
        for side in ("local_costmap", "global_costmap"):
            layer = costmap[side]["obstacle_layer"]
            self.assertEqual(layer["depth_mark"]["topic"], topic)
            self.assertEqual(layer["depth_clear"]["topic"], topic)

    def test_its_frames_come_from_its_own_mount_arguments(self):
        tfs = {n.get("name"): re.findall(r"\$\(arg \w+\)|\S+", n.get("args")) for n in self.root.iter("node") if n.get("pkg") == "tf2_ros"}
        mount = tfs["depth_obstacle_camera_tf"]
        self.assertEqual(mount[:5], ["$(arg camera_x)", "$(arg camera_y)", "$(arg camera_z)", "$(arg camera_yaw)", "$(arg camera_pitch)"])
        self.assertEqual(mount[6:], ["base_footprint", "depth_obstacle_camera"])
        self.assertEqual(tfs["depth_obstacle_optical_tf"][6:], ["depth_obstacle_camera", "depth_obstacle_optical"])
        p = params_of(self.nodelet("astra_camera/AstraDriverNodelet"))
        self.assertEqual(p["depth_frame_id"], "depth_obstacle_optical")


class CostmapFilesTest(unittest.TestCase):
    def load(self, name):
        with open(os.path.join(CONFIG, name)) as f:
            return yaml.safe_load(f)

    def test_depth_marks_from_5_cm_and_clears_by_raytracing_on_both_costmaps(self):
        costmap = self.load("depth_costmap.yaml")
        for side in ("local_costmap", "global_costmap"):
            layer = costmap[side]["obstacle_layer"]
            self.assertEqual(layer["observation_sources"].split(), ["scan", "bump", "depth_mark", "depth_clear"])
            self.assertEqual(layer["z_voxels"], 16)
            mark, clear = layer["depth_mark"], layer["depth_clear"]
            self.assertEqual((mark["marking"], mark["clearing"], mark["min_obstacle_height"]), (True, False, 0.05))
            self.assertEqual((clear["marking"], clear["clearing"]), (False, True))
            self.assertLess(clear["min_obstacle_height"], 0.0)                   # floor points clear the air before them
            for source in (mark, clear):
                self.assertEqual(source["data_type"], "PointCloud2")
                self.assertLessEqual(source["expected_update_rate"], 1.0)         # no cloud for 1 s: move_base stops

    def test_the_shape_covers_the_measured_robot_on_both_costmaps(self):
        shape = self.load("robot_shape.yaml")
        for side in ("local_costmap", "global_costmap"):
            radius = shape[side]["robot_radius"]
            self.assertGreaterEqual(radius, 0.18)                                # measured 0.18 m all round
            self.assertGreaterEqual(shape[side]["inflation_layer"]["inflation_radius"], radius + 0.15)


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
