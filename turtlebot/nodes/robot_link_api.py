#!/usr/bin/env python3
# The Dell's side of the Jetson <-> Dell link: a small HTTP API (port 8010) in front of the robot's ROS stack.
# It implements docs/robot_link.md (api_version 1): the Jetson sends goals, reads the pose, the drive and the noise, and keeps a
# heartbeat going; if the heartbeat stops while a goal is active the base is stopped within 3 s.
#
# All the decisions are in robot_link/ (plain Python, tested without ROS). This file only connects them to ROS:
#   reads   tf map->base_footprint, /rtabmap/localization_pose (or /amcl_pose), /odom, /move_base/NavfnROS/plan, /noise_reading,
#           /mobile_base/sensors/core (battery), /move_base/status, /map_clean (or /map),
#           /mobile_base/events/bumper, /mobile_base/events/cliff, /mobile_base/events/wheel_drop (a goal then fails: "bumper")
#   writes  move_base goals (action client), /robot_speaking, zero velocity, DWAPlannerROS max_vel_x (dynamic_reconfigure)
#
# Needs the environment variable ROBOT_LINK_TOKEN (same value on the Jetson) - it refuses to start without it.
# Starts without ROS too (/api/health then says ros_ok false, goals are refused with 503) and connects when ROS comes up.
#
# Run (normally started by run_robot_link.sh):
#   python3 nodes/robot_link_api.py                      binds to the address of the wlp2s0 interface, port 8010
# Options (ROS private-parameter style, they work with or without a ROS master):
#   _iface:=wlp2s0     network interface whose IPv4 address is used (the Wi-Fi address comes from DHCP, so it is looked up at start)
#   _host:=1.2.3.4     bind to exactly this address instead (127.0.0.1 for tests; 0.0.0.0 = every interface - not recommended)
#   _port:=8010
#   _localiser:=amcl   what finds the robot on the map: rtabmap (default) or amcl (sets the next three, see LOCALISERS below)
#   _localization_topic:=/amcl_pose   _localiser_node:=/amcl   _loc_max_covariance:=0.1
#   _max_speed_limit:=0.25   _loc_max_age_s:=5.0   _tf_max_age_s:=1.0   ...any field of robot_link.core.Settings
#   _calibration_file:=~/noise_calibration.json   _skip_clear_costmaps:=true
#   --print-address    print the address it would bind to and exit

import logging
import os
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robot_link import calibration, netaddr, rules                      # noqa: E402
from robot_link.core import Backend, LinkCore, Settings, Unavailable, Unsupported       # noqa: E402
from robot_link.server import ApiServer                                 # noqa: E402

log = logging.getLogger("robot_link")

DEFAULTS = {
    "iface": "wlp2s0", "host": "", "port": 8010, "map_frame": "map", "base_frame": "base_footprint",
    "plan_topic": "/move_base/NavfnROS/plan", "odom_topic": "/odom",
    "noise_topic": "/noise_reading", "speaking_topic": "/robot_speaking", "battery_topic": "/mobile_base/sensors/core",
    "map_topics": "/map_clean,/map", "dwa_name": "/move_base/DWAPlannerROS",
    "zero_topics": "/navigation_velocity_smoother/raw_cmd_vel,/cmd_vel_mux/input/navi",
    "localiser": "rtabmap", "localization_topic": "", "localiser_node": "", "calibration_file": calibration.DEFAULT_PATH,
    "bumper_topic": "/mobile_base/events/bumper", "cliff_topic": "/mobile_base/events/cliff",
    "wheel_drop_topic": "/mobile_base/events/wheel_drop",
    "battery_empty_v": 13.2, "battery_full_v": 16.5, "enable_max_speed": True, "skip_clear_costmaps": False,
    "wait_for_iface_s": 30.0, "log_every_s": 10.0,
}

# What finds the robot on the map. `_localiser:=amcl` picks the topic, the node the health check looks for and the covariance
# limit for "localised"; `_localization_topic:=`, `_localiser_node:=` and `_loc_max_covariance:=` still override each one.
# RTAB-Map sends a real fix with a small covariance and a lost one with 9999, so its limit only has to tell those apart. AMCL's
# covariance is the spread of its particles and grows as it loses track: 0.1 (about 0.3 m, 18 degrees) is a first guess, to be
# tuned in the comparison runs. It must stay above what the start pose is sent with (set_initial_pose.py: 0.15 m, 8 degrees).
# AMCL publishes /amcl_pose only after the robot has moved update_min_d (0.10 m) or turned update_min_a (0.15 rad, 8.6 degrees)
# (config/amcl_nav.launch), and nothing at all while it stands still. The "stood still" test of the localisation rule must be wider
# than that, or a robot that stopped 0.05 - 0.10 m (or 5 - 8.6 degrees) after the last update is "not localised" until it moves on:
# a goal would be refused as not_localised after every short stop. So its limits sit just above AMCL's.
LOCALISERS = {
    "rtabmap": {"localization_topic": "/rtabmap/localization_pose", "localiser_node": "/rtabmap/rtabmap", "loc_max_covariance": 1.0},
    "amcl": {"localization_topic": "/amcl_pose", "localiser_node": "/amcl", "loc_max_covariance": 0.1,
             "still_dist_m": 0.12, "still_turn_deg": 10.0},
}


def convert(text, like):
    if isinstance(like, bool):
        return str(text).lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(text)
    if isinstance(like, float):
        return float(text)
    return str(text)


def parse_private_args(argv, defaults=DEFAULTS, settings_fields=None):
    """{name: value} from ROS-style `_name:=value` arguments, converted to the type of the default."""
    out = {}
    fields = settings_fields if settings_fields is not None else Settings().__dict__
    for arg in argv:
        if arg.startswith("_") and not arg.startswith("__") and ":=" in arg:          # __name:= and friends are ROS remaps
            name, value = arg[1:].split(":=", 1)
            like = defaults.get(name, fields.get(name))
            if like is None:
                raise SystemExit("unknown parameter _%s (see the top of nodes/robot_link_api.py)" % name)
            try:
                out[name] = convert(value, like)
            except ValueError:
                raise SystemExit("_%s:=%s is not a valid %s" % (name, value, type(like).__name__))
    return out


def run_bounded(fn, timeout_s):
    """Call fn() in a thread and wait at most timeout_s for it: an API request must not wait for ROS. Raises Unavailable on timeout."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:                                     # noqa: B902 - reported to the caller below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise Unavailable("ROS did not answer within %g s" % timeout_s)
    if "error" in box:
        if isinstance(box["error"], (Unavailable, Unsupported)):
            raise box["error"]
        raise Unavailable(str(box["error"]))
    return box["value"]


class RosBackend(Backend):
    """Everything that touches ROS. Created before ROS is up; `start()` waits for the master in a thread."""

    def __init__(self, params, core):
        self.p, self.core = params, core
        self.ready = False
        self.stopping = threading.Event()
        self.restart_requested = False
        self._ros_ok = False
        self.status_at = self.odom_at = self.battery_at = 0.0
        self.volts = None
        self.grid = None
        self.grid_seq = 0
        self.map_rank, self.map_at = 99, 0.0            # which map topic the grid came from (0 = the first in _map_topics) and when
        self.epoch = 0
        self.epoch_lock = threading.Lock()
        self.jobs = queue.Queue()
        self.client = self.dyn = self.dwa_original = None
        self.zero_pubs = []
        self.rospy = None

    # ------------------------------------------------------------------ starting
    def start(self):
        threading.Thread(target=self._run, name="ros_connect", daemon=True).start()

    def _run(self):
        try:
            import rosgraph
        except ImportError:
            log.error("the ROS Python modules are not on the path: source /opt/ros/noetic/setup.bash and ~/turtlebot2/devel/setup.bash. "
                      "The API runs without ROS (ros_ok false).")
            return
        warned = False
        while not self.stopping.is_set() and not rosgraph.is_master_online():
            if not warned:
                log.info("no ROS master yet: the API answers, ros_ok is false until ROS is started")
                warned = True
            self.stopping.wait(1.0)
        if self.stopping.is_set():
            return
        try:
            self._connect()
        except Exception:
            log.exception("could not connect to ROS")
            return
        self.ready = True
        log.info("connected to ROS")
        lost_at = None
        while not self.stopping.wait(1.0):
            online = rosgraph.is_master_online()
            self._ros_ok = self._check()
            if not online:
                lost_at = lost_at or time.time()
            elif lost_at is not None:
                log.warning("the ROS master went away for %.0f s and is back: restarting to connect again", time.time() - lost_at)
                self.restart_requested = True
                return

    def _connect(self):
        import rospy
        import actionlib
        import tf2_ros
        from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
        from kobuki_msgs.msg import BumperEvent, CliffEvent, SensorState, WheelDropEvent
        from move_base_msgs.msg import MoveBaseAction
        from nav_msgs.msg import OccupancyGrid, Odometry, Path
        from actionlib_msgs.msg import GoalStatusArray
        from std_msgs.msg import Bool, Float32MultiArray
        self.rospy = rospy
        rospy.init_node("robot_link_api", disable_signals=True)
        # settings from the parameter server (the command line already won)
        values = {k: rospy.get_param("~" + k) for k in Settings().__dict__ if rospy.has_param("~" + k) and k not in self.p.cli}
        if values:
            self.core.configure(Settings.from_mapping(dict(self.p.settings.__dict__, **values)))
        p = self.p
        rospy.Subscriber(p["odom_topic"], Odometry, self._odom, queue_size=5)
        rospy.Subscriber(p["localization_topic"], PoseWithCovarianceStamped, self._localization, queue_size=5)
        rospy.Subscriber(p["plan_topic"], Path, lambda m: self.core.on_plan([(s.pose.position.x, s.pose.position.y) for s in m.poses]),
                         queue_size=2)
        rospy.Subscriber(p["noise_topic"], Float32MultiArray, self._noise, queue_size=5)
        rospy.Subscriber(p["battery_topic"], SensorState, self._battery, queue_size=2)
        rospy.Subscriber("/move_base/status", GoalStatusArray, lambda m: setattr(self, "status_at", time.time()), queue_size=2)
        # the Kobuki's safety sensors: only the moment one fires counts (pressed, a cliff, a wheel off the floor), not the release
        rospy.Subscriber(p["bumper_topic"], BumperEvent,
                         lambda m: m.state == BumperEvent.PRESSED and self.core.on_safety_event("bumper"), queue_size=5)
        rospy.Subscriber(p["cliff_topic"], CliffEvent,
                         lambda m: m.state == CliffEvent.CLIFF and self.core.on_safety_event("cliff"), queue_size=5)
        rospy.Subscriber(p["wheel_drop_topic"], WheelDropEvent,
                         lambda m: m.state == WheelDropEvent.DROPPED and self.core.on_safety_event("wheel_drop"), queue_size=5)
        for rank, topic in enumerate(p["map_topics"].split(",")):
            rospy.Subscriber(topic.strip(), OccupancyGrid, self._map, callback_args=rank, queue_size=1)
        self.speaking_pub = rospy.Publisher(p["speaking_topic"], Bool, queue_size=1, latch=True)
        self.speaking_pub.publish(Bool(data=False))
        self.zero_pubs = [rospy.Publisher(t.strip(), Twist, queue_size=1) for t in p["zero_topics"].split(",") if t.strip()]
        self.Twist, self.Bool = Twist, Bool
        self.client = actionlib.SimpleActionClient("move_base", MoveBaseAction)
        self.tf_buffer = tf2_ros.Buffer()
        tf2_ros.TransformListener(self.tf_buffer)
        threading.Thread(target=self._tf_loop, name="tf", daemon=True).start()
        threading.Thread(target=self._goal_worker, name="goals", daemon=True).start()
        rospy.on_shutdown(self.restore_speed)

    # ------------------------------------------------------------------ what ROS says
    def _localization(self, m):
        trusted = rules.pose_is_trusted(m.pose.covariance, self.core.s.loc_max_covariance)
        if trusted != getattr(self, "_loc_trusted", None):
            log.info("%s %s (covariance x %.4g, y %.4g, yaw %.4g; limit %g)", self.p["localiser"],
                     "has found the robot on the map" if trusted else "has lost track of the robot: the base reports not localised",
                     m.pose.covariance[0], m.pose.covariance[7], m.pose.covariance[35], self.core.s.loc_max_covariance)
            self._loc_trusted = trusted
        self.core.on_localization_pose(trusted)

    def _odom(self, m):
        self.odom_at = time.time()
        o = m.pose.pose.orientation
        self.core.on_odom(m.pose.pose.position.x, m.pose.pose.position.y, rules.yaw_from_quaternion(o.x, o.y, o.z, o.w),
                          m.twist.twist.linear.x, m.twist.twist.angular.z)

    def _noise(self, m):
        if len(m.data) >= 3:                                              # [overall, left, right, balance] in dBFS
            self.core.on_noise(float(m.data[0]), float(m.data[1]), float(m.data[2]))

    def _battery(self, m):
        self.volts, self.battery_at = m.battery / 10.0, time.time()        # Kobuki: tenths of a volt

    def _map(self, m, rank=0):
        now = time.time()
        if rank > self.map_rank and now - self.map_at < 30.0:           # the cleaned map is alive: do not flip back to the raw one
            return
        self.map_rank, self.map_at = rank, now
        i = m.info
        self.grid = rules.GridView(i.width, i.height, i.resolution, i.origin.position.x, i.origin.position.y, m.data)
        self.grid_seq += 1

    def _tf_loop(self):
        rospy = self.rospy
        while not self.stopping.wait(0.1):
            try:
                t = self.tf_buffer.lookup_transform(self.p["map_frame"], self.p["base_frame"], rospy.Time(0))
            except Exception:                                             # no transform yet / extrapolation: the pose just ages
                continue
            q, v = t.transform.rotation, t.transform.translation
            self.core.on_tf(v.x, v.y, rules.yaw_from_quaternion(q.x, q.y, q.z, q.w), t.header.stamp.to_sec())

    def _check(self):
        """ROS is usable: the localiser (RTAB-Map or AMCL) is running, move_base is publishing its status and the base is
        publishing odometry."""
        try:
            import rosnode
            localiser = self.p["localiser_node"] in rosnode.get_node_names()
        except Exception:
            return False
        now = time.time()
        return localiser and now - self.status_at < 3.0 and now - self.odom_at < 2.0

    # ------------------------------------------------------------------ Backend
    def ros_ok(self):
        return self.ready and self._ros_ok

    def battery(self):
        if self.volts is None or time.time() - self.battery_at > 30:
            return None
        return rules.battery_fraction(self.volts, self.p["battery_empty_v"], self.p["battery_full_v"])

    def map_grid(self):
        grid = self.grid
        return None if grid is None else (self.grid_seq, grid)

    def outside_map(self, x, y):
        return self.grid is not None and self.grid.refuses(x, y)

    def send_goal(self, goal_id, x, y, yaw):
        if not self.ready:
            raise Unavailable("ROS is not connected")
        with self.epoch_lock:
            self.epoch += 1
            epoch = self.epoch
        self.jobs.put((epoch, goal_id, x, y, yaw))

    def cancel_goal(self):
        with self.epoch_lock:
            self.epoch += 1                                               # a goal still waiting to be sent is dropped
        if self.ready and self.client is not None:
            self.client.cancel_goal()                                     # ours only: the click-to-go page's goal is not touched

    def stop_base(self):
        if not self.ready:
            return

        def burst():
            msg = self.Twist()
            for _ in range(10):
                for pub in self.zero_pubs:
                    pub.publish(msg)
                time.sleep(0.05)
        threading.Thread(target=burst, daemon=True).start()

    def set_speaking(self, speaking):
        if self.ready:
            self.speaking_pub.publish(self.Bool(data=bool(speaking)))

    def set_max_speed(self, m_s):
        if not self.p["enable_max_speed"]:
            raise Unsupported()
        if not self.ready:
            raise Unavailable("ROS is not connected")
        if not self._ros_ok:
            raise Unavailable("move_base is not running")
        if "DWAPlannerROS" not in str(self.rospy.get_param("/move_base/base_local_planner", "")):
            raise Unsupported()
        return run_bounded(lambda: self._reconfigure(m_s), 0.6)

    def _reconfigure(self, m_s):
        if self.dyn is None:
            import dynamic_reconfigure.client
            self.dyn = dynamic_reconfigure.client.Client(self.p["dwa_name"], timeout=0.5)
            self.dwa_original = self.dyn.get_configuration(timeout=0.5)["max_vel_x"]
            log.info("DWA max_vel_x is %.2f m/s at start", self.dwa_original)
        applied = self.dyn.update_configuration({"max_vel_x": float(m_s)})["max_vel_x"]
        log.info("DWA max_vel_x set to %.2f m/s", applied)
        return applied

    def restore_speed(self):
        if self.dyn is not None and self.dwa_original is not None:
            try:
                self.dyn.update_configuration({"max_vel_x": self.dwa_original})
            except Exception:
                pass

    # ------------------------------------------------------------------ goals (sent from a worker so no request waits for ROS)
    def _goal_worker(self):
        from actionlib_msgs.msg import GoalStatus
        from move_base_msgs.msg import MoveBaseGoal
        from std_srvs.srv import Empty
        rospy = self.rospy
        outcomes = {GoalStatus.SUCCEEDED: "succeeded", GoalStatus.ABORTED: "aborted", GoalStatus.REJECTED: "rejected",
                    GoalStatus.PREEMPTED: "preempted", GoalStatus.RECALLED: "preempted", GoalStatus.LOST: "aborted"}
        clear = rospy.ServiceProxy("/move_base/clear_costmaps", Empty)
        while not self.stopping.is_set():
            try:
                epoch, goal_id, x, y, yaw = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if epoch != self.epoch:
                continue                                                  # cancelled or replaced while it waited
            if not self.p["skip_clear_costmaps"]:
                try:                                                      # forget old obstacles before a new goal, as the web page does
                    rospy.wait_for_service("/move_base/clear_costmaps", 1.0)
                    clear()
                except Exception as exc:
                    log.warning("clear_costmaps failed: %s", exc)
                if epoch != self.epoch:
                    continue
            if not self.client.wait_for_server(rospy.Duration(1.0)):
                self.core.on_goal_result(goal_id, "aborted", "move_base is not available")
                continue
            goal = MoveBaseGoal()
            goal.target_pose.header.frame_id = self.p["map_frame"]
            goal.target_pose.header.stamp = rospy.Time.now()
            goal.target_pose.pose.position.x, goal.target_pose.pose.position.y = x, y
            _, _, goal.target_pose.pose.orientation.z, goal.target_pose.pose.orientation.w = rules.quaternion_from_yaw(yaw)

            def done(state, result, goal_id=goal_id):
                self.core.on_goal_result(goal_id, outcomes.get(state, "aborted"), self.client.get_goal_status_text())
            self.client.send_goal(goal, done_cb=done)
            if epoch != self.epoch:                                       # cancelled / replaced just as it was being sent
                self.client.cancel_goal()


# ---------------------------------------------------------------------------------------------------- main
class Params(dict):
    def __init__(self, argv):
        super().__init__(DEFAULTS)
        self.cli = parse_private_args(argv)
        self.update({k: v for k, v in self.cli.items() if k in DEFAULTS})
        profile = LOCALISERS.get(self["localiser"])
        if profile is None:
            raise SystemExit("_localiser:=%s is not one of %s" % (self["localiser"], ", ".join(sorted(LOCALISERS))))
        for key in ("localization_topic", "localiser_node"):
            self[key] = self[key] or profile[key]
        values = {k: v for k, v in self.cli.items() if k in Settings().__dict__}
        for key in ("loc_max_covariance", "still_dist_m", "still_turn_deg"):
            if key in profile:
                values.setdefault(key, profile[key])
        self.settings = Settings.from_mapping(values)


def bind_address(params):
    return params["host"] or netaddr.iface_ipv4(params["iface"])


def interface_names():
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    params = Params(argv)
    if "--print-address" in argv:
        address = bind_address(params)
        print(address or "")
        return 0 if address else 3
    token = os.environ.get("ROBOT_LINK_TOKEN", "").strip()
    if not token:
        print("ROBOT_LINK_TOKEN is not set: the API will not start without the shared secret (the same value as on the Jetson).\n"
              "Put it in ~/lbr-dell/.robot_link_env (mode 600) or export it, then start again.", file=sys.stderr)
        return 2
    if len(token) < 12:
        log.warning("ROBOT_LINK_TOKEN is short (%d characters): use something like `openssl rand -hex 16`", len(token))
    deadline = time.time() + params["wait_for_iface_s"]
    address = bind_address(params)
    while address is None and time.time() < deadline:
        time.sleep(1.0)
        address = bind_address(params)
    if address is None:
        print("interface %r has no IPv4 address (interfaces here: %s). Is the Wi-Fi connected? Use _iface:=<name> or _host:=<ip>."
              % (params["iface"], ", ".join(interface_names())), file=sys.stderr)
        return 3
    if address == "0.0.0.0":
        log.warning("binding to every interface (0.0.0.0): only the token protects the API")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: stop.set())
    core = LinkCore(None, params.settings)
    backend = RosBackend(params, core)
    core.b = backend
    reloader = calibration.Reloader(os.path.expanduser(params["calibration_file"]))
    server = ApiServer((address, params["port"]), core, token)
    server.start()
    log.info("robot_link_api listening on http://%s:%d  (turtlebot.base_url on the Jetson)", address, params["port"])
    backend.start()

    last_cal = last_addr = last_log = 0.0
    while not stop.wait(0.1):
        now = time.time()
        core.tick()
        if now - last_cal >= 5.0:
            last_cal = now
            previous = core.calibration
            core.set_calibration(reloader.poll())
            if reloader.error and reloader.error != getattr(main, "_cal_error", ""):
                main._cal_error = reloader.error
                log.warning("noise calibration ignored: %s", reloader.error)
            if core.calibration is not previous:
                log.info("noise calibration %s", "loaded (readings are in dB)" if core.calibration else "removed (readings are in dBFS)")
        if not params["host"] and now - last_addr >= 10.0:
            last_addr = now
            current = netaddr.iface_ipv4(params["iface"])
            if current and current != address:
                log.warning("the address of %s changed from %s to %s: the API moves to it. Update turtlebot.base_url on the Jetson.",
                            params["iface"], address, current)
                try:
                    new = ApiServer((current, params["port"]), core, token)
                except OSError as exc:
                    log.error("cannot bind to %s: %s", current, exc)
                else:
                    server.stop()
                    server, address = new, current
                    server.start()
        if now - last_log >= params["log_every_s"]:
            last_log = now
            log.info("status %s", core.debug())
        if backend.restart_requested:
            log.warning("restarting")
            server.stop()
            os.execv(sys.executable, [sys.executable] + sys.argv)

    log.info("stopping")
    core.handle_cancel("*")                                              # never leave the base driving a goal nobody watches
    backend.stopping.set()
    time.sleep(0.6)                                                      # let the zero-velocity burst go out
    backend.restore_speed()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
