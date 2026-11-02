"""The plain helpers in nodes/robot_link_api.py (importable without ROS) and the address lookup."""

import importlib.util
import os
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
