"""The plain helpers in nodes/robot_link_api.py (importable without ROS) and the address lookup."""

import importlib.util
import math
import os
import re
import threading
import time
import unittest

from support import FakeBackend  # noqa: F401  (puts turtlebot/ on the path)
from robot_link import netaddr
from robot_link.core import Unavailable, Unsupported

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("robot_link_api", os.path.join(os.path.dirname(HERE), "nodes", "robot_link_api.py"))
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class ArgsTest(unittest.TestCase):
    def test_private_arguments_are_typed_by_their_defaults(self):
        got = node.parse_private_args(["_iface:=wlan9", "_port:=8123", "_max_speed_limit:=0.3", "_skip_clear_costmaps:=true", "ignored", "__name:=x"])
        self.assertEqual(got, {"iface": "wlan9", "port": 8123, "max_speed_limit": 0.3, "skip_clear_costmaps": True})

    def test_unknown_or_bad_values_stop_the_start_with_a_message(self):
        with self.assertRaises(SystemExit) as e:
            node.parse_private_args(["_nonsense:=1"])
        self.assertIn("unknown parameter", str(e.exception))
        with self.assertRaises(SystemExit):
            node.parse_private_args(["_port:=abc"])

    def test_params_start_from_the_defaults(self):
        p = node.Params([])
        self.assertEqual((p["iface"], p["host"], p["port"]), ("wlp2s0", "", 8010))
        p = node.Params(["_host:=10.1.2.3", "_loc_max_age_s:=9"])
        self.assertEqual(p["host"], "10.1.2.3")
        self.assertEqual(p.settings.loc_max_age_s, 9.0)
        self.assertEqual(p.settings.heartbeat_timeout_s, 3.0)

    def test_the_bind_address_comes_from_the_interface_unless_host_is_given(self):
        self.assertEqual(node.bind_address(node.Params(["_iface:=lo"])), "127.0.0.1")
        self.assertIsNone(node.bind_address(node.Params(["_iface:=nope0"])))
        self.assertEqual(node.bind_address(node.Params(["_iface:=nope0", "_host:=127.0.0.2"])), "127.0.0.2")


class LocaliserTest(unittest.TestCase):
    def test_rtabmap_is_the_default_and_keeps_its_old_topic_node_and_limit(self):
        p = node.Params([])
        self.assertEqual((p["localiser"], p["localization_topic"], p["localiser_node"]),
                         ("rtabmap", "/rtabmap/localization_pose", "/rtabmap/rtabmap"))
        self.assertEqual(p.settings.loc_max_covariance, 1.0)

    def test_amcl_brings_its_topic_its_node_for_the_health_check_and_a_tighter_limit(self):
        p = node.Params(["_localiser:=amcl"])
        self.assertEqual((p["localization_topic"], p["localiser_node"]), ("/amcl_pose", "/amcl"))
        self.assertEqual(p.settings.loc_max_covariance, 0.1)

    def test_each_part_can_still_be_given_on_its_own(self):
        p = node.Params(["_localiser:=amcl", "_loc_max_covariance:=0.25", "_localization_topic:=/other_pose", "_localiser_node:=/amcl2"])
        self.assertEqual((p["localization_topic"], p["localiser_node"]), ("/other_pose", "/amcl2"))
        self.assertEqual(p.settings.loc_max_covariance, 0.25)

    def test_an_unknown_localiser_stops_the_start(self):
        with self.assertRaises(SystemExit) as e:
            node.Params(["_localiser:=gps"])
        self.assertIn("amcl", str(e.exception))

    def test_the_start_pose_and_a_click_on_the_page_count_as_localised_under_amcl(self):
        """AMCL cannot check either until the robot moves, and a robot that is not localised is sent no goal: if the pose it
        starts from counted as lost, it could never drive to get a better one."""
        from robot_link import rules
        spec = importlib.util.spec_from_file_location("set_initial_pose", os.path.join(os.path.dirname(HERE), "nodes", "set_initial_pose.py"))
        start = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(start)
        limit = node.Params(["_localiser:=amcl"]).settings.loc_max_covariance
        self.assertTrue(rules.pose_is_trusted(start.covariance(0.15, math.radians(8.0)), limit))
        with open(os.path.join(os.path.dirname(HERE), "nodes", "noise_nav_web.py")) as f:
            page = f.read()
        xy = float(re.search(r"covariance\[0\] = p\.pose\.covariance\[7\] = ([0-9.]+)", page).group(1))
        yaw = float(re.search(r"covariance\[35\] = ([0-9.]+)", page).group(1))
        click = [0.0] * 36
        click[0], click[7], click[35] = xy, xy, yaw
        self.assertTrue(rules.pose_is_trusted(click, limit))


class BoundedTest(unittest.TestCase):
    def test_a_quick_call_returns(self):
        self.assertEqual(node.run_bounded(lambda: 7, 1.0), 7)

    def test_a_slow_call_is_unavailable_and_does_not_hold_the_caller(self):
        t = time.time()
        with self.assertRaises(Unavailable):
            node.run_bounded(lambda: time.sleep(2), 0.2)
        self.assertLess(time.time() - t, 0.6)

    def test_errors_become_unavailable_but_unsupported_stays(self):
        def boom():
            raise RuntimeError("no server")
        with self.assertRaises(Unavailable) as e:
            node.run_bounded(boom, 1.0)
        self.assertIn("no server", str(e.exception))

        def nope():
            raise Unsupported()
        with self.assertRaises(Unsupported):
            node.run_bounded(nope, 1.0)


class AddressTest(unittest.TestCase):
    def test_loopback(self):
        self.assertEqual(netaddr.iface_ipv4("lo"), "127.0.0.1")

    def test_missing_or_silly_names(self):
        for name in ("nope0", "", "x" * 40, None):
            self.assertIsNone(netaddr.iface_ipv4(name))


if __name__ == "__main__":
    unittest.main()
