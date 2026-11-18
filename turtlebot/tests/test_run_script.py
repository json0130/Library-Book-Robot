"""run_robot_link.sh and run_lidar_mapping.sh: the checks that happen before they start anything. None of these starts ROS or
moves the robot."""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "run_robot_link.sh")
MAPPING = os.path.join(os.path.dirname(HERE), "run_lidar_mapping.sh")


def run(script, *args, env=None, timeout=30):
    e = {k: v for k, v in os.environ.items() if k != "ROBOT_LINK_TOKEN"}
    e.update(env or {})
    return subprocess.run(["bash", script, *args], capture_output=True, text=True, timeout=timeout, env=e)


class ScriptTest(unittest.TestCase):
    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)

    def test_help(self):
        r = run(SCRIPT, "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("run_robot_link.sh", r.stdout)

    def test_unknown_option(self):
        self.assertEqual(run(SCRIPT, "--nonsense").returncode, 2)

    def test_check_passes_when_nothing_else_runs(self):
        r = run(SCRIPT, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("No other ROS stack is running", r.stdout)

    def refuses_because_of(self, name):
        proc = subprocess.Popen(["bash", "-c", "exec -a %s sleep 60" % name])
        try:
            time.sleep(0.3)
            r = run(SCRIPT, "--check")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("Another ROS stack is already running", r.stdout + r.stderr)
            self.assertIn(name, r.stdout + r.stderr)
            self.assertIsNone(proc.poll(), "the script must never stop what it did not start")
        finally:
            proc.kill()
            proc.wait()

    def test_refuses_next_to_a_roscore(self):
        self.refuses_because_of("roscore")

    def test_refuses_next_to_the_kobuki_node(self):
        self.refuses_because_of("kobuki_node")

    def test_refuses_next_to_the_teammates_script(self):
        self.refuses_because_of("run_vslam_noise_nav.sh")
        self.refuses_because_of("run_vslam_mapping.sh")

    def test_a_secret_file_others_can_read_is_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            script = shutil.copy(SCRIPT, tmp)
            env_file = os.path.join(tmp, ".robot_link_env")
            with open(env_file, "w") as f:
                f.write("export ROBOT_LINK_TOKEN=abcdefabcdefabcdef\n")
            os.chmod(env_file, 0o644)
            r = run(script)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("chmod 600", r.stdout + r.stderr)
            self.assertFalse(os.path.exists(os.path.join(tmp, "run_data", "run_robot_link.pid")), "the pid file must be removed on exit")
        finally:
            shutil.rmtree(tmp)

    def test_no_secret_is_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            r = run(shutil.copy(SCRIPT, tmp))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("ROBOT_LINK_TOKEN is not set", r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_a_second_copy_is_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            script = shutil.copy(SCRIPT, tmp)
            os.makedirs(os.path.join(tmp, "run_data"))
            sleeper = subprocess.Popen(["sleep", "30"])
            with open(os.path.join(tmp, "run_data", "run_robot_link.pid"), "w") as f:
                f.write(str(sleeper.pid))
            try:
                r = run(script, "--check")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("Another ROS stack", r.stdout + r.stderr)
            finally:
                sleeper.kill()
                sleeper.wait()
        finally:
            shutil.rmtree(tmp)

    def test_dry_run_shows_the_whole_plan_and_starts_nothing(self):
        """Whatever is missing on this computer (no ROS, no LiDAR, no map), --dry-run reports it and carries on to the end."""
        home = tempfile.mkdtemp()
        try:
            with open(os.path.join(home, "vslam_map.db"), "w") as f:
                f.write("not a real map")
            r = run(SCRIPT, "--dry-run", env={"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"})
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            for name in ("minimal", "vslam", "scan_filter", "map_cleaner", "noise_logger", "noise_web", "robot_link_api"):
                self.assertIn("would start %s:" % name, out)
            for helper in ("warn", "say", "bail", "start", "others"):
                self.assertNotIn("%s: command not found" % helper, out)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(SCRIPT), "run_data", "vslam_map.db")), "dry run copies nothing")
        finally:
            shutil.rmtree(home)

    def test_local_option_binds_the_api_to_loopback_only(self):
        home = tempfile.mkdtemp()
        try:
            with open(os.path.join(home, "vslam_map.db"), "w") as f:
                f.write("not a real map")
            env = {"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"}
            local = run(SCRIPT, "--dry-run", "--local", env=env)
            plain = run(SCRIPT, "--dry-run", env=env)
            self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
            api_local = [l for l in local.stdout.splitlines() if "would start robot_link_api:" in l][0]
            api_plain = [l for l in plain.stdout.splitlines() if "would start robot_link_api:" in l][0]
            self.assertIn("_host:=127.0.0.1", api_local)
            self.assertNotIn("_host", api_plain)
            self.assertIn("robot_link_tunnel.sh", local.stdout)
        finally:
            shutil.rmtree(home)

    def test_arguments_are_cleared_before_sourcing_ros(self):
        """`source` passes the script's arguments to catkin's setup.sh, where --local means "ignore the rest of the environment"."""
        with open(SCRIPT) as f:
            lines = [l.strip() for l in f if not l.lstrip().startswith("#")]
        first_source = next(i for i, l in enumerate(lines) if "source /opt/ros/noetic/setup.bash" in l)
        self.assertIn("set --", lines[:first_source])

    def test_topic_waits_retry_and_cannot_hang(self):
        """`rostopic echo -n 1 T/header` fails at once if T is not advertised yet, so a start-up wait must poll; and rostopic can
        ignore the polite stop `timeout` sends (a start once hung on it for an hour), so it must be killed outright after that."""
        for script in (SCRIPT, MAPPING):
            with open(script) as f:
                code = [l for l in f if not l.lstrip().startswith("#")]
            unsafe = [l.strip() for l in code if "rostopic echo -n 1" in l and "timeout -k 2 4" not in l]
            self.assertEqual(unsafe, [], script)

    def test_the_camera_type_is_set_not_defaulted(self):
        for script in (SCRIPT, MAPPING):
            with open(script) as f:
                text = f.read()
            self.assertIn("export TURTLEBOT_BASE=kobuki TURTLEBOT_3D_SENSOR=astra", text)
            self.assertNotIn("TURTLEBOT_3D_SENSOR:-", text)

    def test_the_scripts_have_no_sudo_in_them(self):
        for script in (SCRIPT, MAPPING):
            with open(script) as f:
                code = [line for line in f if not line.lstrip().startswith("#")]
            offenders = [line for line in code if "sudo" in line and "echo" not in line and "warn" not in line]
            self.assertEqual(offenders, [], script)

    def test_refuses_next_to_the_lidar_mapping(self):
        self.refuses_because_of("run_lidar_mapping.sh")


def lidar_map(folder, image="lidar_map.pgm", with_picture=True):
    os.makedirs(folder, exist_ok=True)
    yaml_file = os.path.join(folder, "lidar_map.yaml")
    with open(yaml_file, "w") as f:
        f.write("image: %s\nresolution: 0.050000\norigin: [-15.0, -15.0, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n" % image)
    if with_picture:
        with open(os.path.join(folder, image), "wb") as f:
            f.write(b"P5\n2 2\n255\n\xfe\xfe\xfe\x00")
    return yaml_file


class AmclStartTest(unittest.TestCase):
    """run_robot_link.sh --amcl, in dry runs only: nothing is started."""

    def dry(self, *args):
        home = tempfile.mkdtemp()
        try:
            r = run(SCRIPT, "--dry-run", "--amcl", *args, env={"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"})
        finally:
            shutil.rmtree(home)
        return r, r.stdout + r.stderr

    def test_the_lidar_stack_replaces_the_camera_one(self):
        tmp = tempfile.mkdtemp()
        try:
            r, out = self.dry("--map", lidar_map(tmp))
            self.assertEqual(r.returncode, 0, out)
            for name in ("minimal", "amcl_nav", "scan_filter", "map_cleaner", "noise_logger", "noise_web", "robot_link_api"):
                self.assertIn("would start %s:" % name, out)
            self.assertNotIn("would start vslam:", out)
            self.assertNotIn("Map not found", out)                        # no RTAB-Map database is wanted
            self.assertNotIn("LiDAR map not found", out)
            nav = [l for l in r.stdout.splitlines() if "would start amcl_nav:" in l][0]
            self.assertIn("config/amcl_nav.launch", nav)
            self.assertIn("map_file:=%s" % os.path.join(tmp, "lidar_map.yaml"), nav)
        finally:
            shutil.rmtree(tmp)

    def test_the_link_and_the_page_are_told_it_is_amcl_and_the_station_pose_is_sent(self):
        r, out = self.dry()
        lines = r.stdout.splitlines()
        self.assertIn("_localiser:=amcl", [l for l in lines if "would start robot_link_api:" in l][0])
        self.assertIn("_localiser:=amcl", [l for l in lines if "would start noise_web:" in l][0])
        start_pose = [l for l in lines if "would run set_initial_pose:" in l]
        self.assertEqual(len(start_pose), 1, out)
        self.assertIn("--floorplan %s" % os.path.normpath(os.path.join(os.path.dirname(SCRIPT), "..", "data", "floorplan.yaml")),
                      start_pose[0])
        self.assertLess(lines.index(start_pose[0]), [i for i, l in enumerate(lines) if "would start noise_logger:" in l][0],
                        "the start pose is sent once move_base is up, before the rest")

    def test_the_default_map_is_the_one_the_mapping_script_writes(self):
        r, out = self.dry()
        self.assertIn(os.path.join(os.path.dirname(SCRIPT), "maps", "lidar_map.yaml"), out)

    def test_a_missing_map_or_picture_is_reported(self):
        r, out = self.dry("--map", "/nonexistent/lab.yaml")
        self.assertIn("LiDAR map not found: /nonexistent/lab.yaml", out)
        self.assertIn("run_lidar_mapping.sh", out)
        tmp = tempfile.mkdtemp()
        try:
            r, out = self.dry("--map=" + lidar_map(tmp, with_picture=False))
            self.assertIn("The map's picture is missing: %s" % os.path.join(tmp, "lidar_map.pgm"), out)
        finally:
            shutil.rmtree(tmp)

    def test_map_is_only_for_amcl(self):
        r = run(SCRIPT, "--map", "x.yaml", "--check")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--amcl", r.stdout)
        self.assertEqual(run(SCRIPT, "--amcl", "--map").returncode, 2)

    def test_without_amcl_it_is_still_rtabmap(self):
        home = tempfile.mkdtemp()
        try:
            with open(os.path.join(home, "vslam_map.db"), "w") as f:
                f.write("not a real map")
            r = run(SCRIPT, "--dry-run", env={"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"})
        finally:
            shutil.rmtree(home)
        self.assertIn("would start vslam:", r.stdout)
        self.assertNotIn("amcl", r.stdout.replace("--amcl uses the LiDAR instead", ""))


class DepthAndKeepoutOptionTest(unittest.TestCase):
    """--amcl's depth camera (obstacles only) and keep-out zones: option handling, in dry runs and --check only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.map = lidar_map(os.path.join(self.tmp, "maps"))

    def dry(self, *args):
        home = tempfile.mkdtemp()
        try:
            r = run(SCRIPT, "--dry-run", *args, env={"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"})
        finally:
            shutil.rmtree(home)
        return r, r.stdout + r.stderr

    def line(self, out, name):
        return [l for l in out.splitlines() if "would start %s:" % name in l][0]

    def test_the_depth_camera_is_on_by_default_with_amcl(self):
        r, out = self.dry("--amcl", "--map", self.map)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("depth_obstacles:=true", self.line(out, "amcl_nav"))
        self.assertIn("depth camera for obstacles", out)
        self.assertNotIn("robot_height:=", self.line(out, "amcl_nav"))           # the launch file's default (1.2 m)
        r, out = self.dry("--amcl", "--depth-obstacles", "--map", self.map)
        self.assertIn("depth_obstacles:=true", self.line(out, "amcl_nav"))

    def test_no_depth_turns_it_off_and_says_what_that_means(self):
        r, out = self.dry("--amcl", "--no-depth", "--map", self.map)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("depth_obstacles:=false", self.line(out, "amcl_nav"))
        self.assertIn("ONLY keep-out zones", out)
        self.assertNotIn("Astra camera is not on USB", out)                      # no camera is looked for

    def test_robot_height_is_passed_on_and_checked(self):
        r, out = self.dry("--amcl", "--robot-height", "1.15", "--map", self.map)
        self.assertIn("robot_height:=1.15", self.line(out, "amcl_nav"))
        r, out = self.dry("--amcl", "--robot-height=1.3", "--map", self.map)
        self.assertIn("robot_height:=1.3", self.line(out, "amcl_nav"))
        for bad in ("0.2", "3", "tall", "1.2m", "-1"):
            self.assertEqual(run(SCRIPT, "--amcl", "--robot-height", bad, "--check").returncode, 2, bad)
        self.assertEqual(run(SCRIPT, "--amcl", "--robot-height").returncode, 2)

    def test_these_options_are_for_amcl_only(self):
        for args in (["--no-depth"], ["--depth-obstacles"], ["--robot-height", "1.2"], ["--keepout", "k.yaml"]):
            r = run(SCRIPT, *args, "--check")
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("are for --amcl", r.stdout)
        self.assertEqual(run(SCRIPT, "--amcl", "--depth-obstacles", "--no-depth", "--check").returncode, 2)

    def test_the_map_cleaner_paints_the_zones_with_amcl_only(self):
        r, out = self.dry("--amcl", "--map", self.map)
        cleaner = self.line(out, "map_cleaner")
        here = os.path.dirname(SCRIPT)
        self.assertIn("_keepout:=%s" % os.path.join(here, "config", "keepout.yaml"), cleaner)
        self.assertIn("_floorplan:=%s" % os.path.normpath(os.path.join(here, "..", "data", "floorplan.yaml")), cleaner)
        self.assertIn("_shelves:=%s" % os.path.normpath(os.path.join(here, "..", "data", "shelves.csv")), cleaner)
        empty = os.path.join(self.tmp, "keepout.yaml")                           # the repo's file holds the real zones, which belong to the real map
        with open(empty, "w") as f:
            f.write("zones: []\n")
        r, out = self.dry("--amcl", "--map", self.map, "--keepout", empty)
        self.assertIn("_keepout:=%s" % empty, self.line(out, "map_cleaner"))
        self.assertIn("keepout: 0 zone(s) OK", out)
        home = tempfile.mkdtemp()
        try:
            with open(os.path.join(home, "vslam_map.db"), "w") as f:
                f.write("not a real map")
            plain = run(SCRIPT, "--dry-run", env={"HOME": home, "ROBOT_LINK_TOKEN": "dry-run-token-1234"})
        finally:
            shutil.rmtree(home)
        self.assertNotIn("_keepout", self.line(plain.stdout, "map_cleaner"))     # RTAB-Map's map frame is another one

    def big_map(self):
        """30 x 30 m round (0, 0), so the floor plan's station (0, 0) is on it."""
        folder = os.path.join(self.tmp, "big")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "lab.pgm"), "wb") as f:
            f.write(b"P5\n300 300\n255\n" + b"\xfe" * 90000)
        path = os.path.join(folder, "lab.yaml")
        with open(path, "w") as f:
            f.write("image: lab.pgm\nresolution: 0.1\norigin: [-15.0, -15.0, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
        return path

    def test_a_zone_over_the_station_is_refused_before_anything_starts(self):
        zones = os.path.join(self.tmp, "keepout.yaml")
        with open(zones, "w") as f:
            f.write("zones:\n  - {name: desk, rect: [-0.5, -0.5, 0.5, 0.5]}\n")
        r, out = self.dry("--amcl", "--map", self.big_map(), "--keepout", zones)
        self.assertIn("The keep-out zones cannot be used", out)
        self.assertIn("desk covers the station", out)
        self.assertIn("keepout_preview.py", out)
        self.assertIn("_keepout:=%s" % zones, self.line(out, "map_cleaner"))

    def test_a_good_zone_and_a_missing_file(self):
        zones = os.path.join(self.tmp, "keepout.yaml")
        with open(zones, "w") as f:
            f.write("zones:\n  - {name: desk, rect: [1.0, 1.0, 2.0, 2.0]}\n")
        r, out = self.dry("--amcl", "--map", self.big_map(), "--keepout=" + zones)
        self.assertIn("keepout: 1 zone(s) OK: desk", out)
        r, out = self.dry("--amcl", "--map", self.big_map(), "--keepout", os.path.join(self.tmp, "none.yaml"))
        self.assertIn("No keep-out file", out)

    def test_the_help_lists_the_options(self):
        out = run(SCRIPT, "--help").stdout
        for option in ("--no-depth", "--robot-height", "--keepout", "--amcl"):
            self.assertIn(option, out)
        self.assertIn("Press Ctrl-C", out)                                       # the whole header, not cut short


class MappingScriptTest(unittest.TestCase):
    """run_lidar_mapping.sh: only --help, --check and dry runs. It drives the robot for real, so it is never started here."""

    def test_syntax_help_and_bad_arguments(self):
        self.assertEqual(subprocess.run(["bash", "-n", MAPPING]).returncode, 0)
        r = run(MAPPING, "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("THE ROBOT DRIVES", r.stdout)
        self.assertEqual(run(MAPPING, "--nonsense").returncode, 2)
        self.assertEqual(run(MAPPING, "../home").returncode, 2)               # a map name, not a path

    def test_check_passes_when_nothing_else_runs_and_refuses_next_to_the_link(self):
        r = run(MAPPING, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        proc = subprocess.Popen(["bash", "-c", "exec -a run_robot_link.sh sleep 60"])
        try:
            time.sleep(0.3)
            r = run(MAPPING, "--check")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("run_robot_link.sh", r.stdout)
            self.assertIsNone(proc.poll(), "the script must never stop what it did not start")
        finally:
            proc.kill()
            proc.wait()

    def copy(self):
        """The script on its own in a temporary folder, so its maps/ is a temporary one."""
        tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(os.path.dirname(SCRIPT), "config"), os.path.join(tmp, "config"))
        shutil.copytree(os.path.join(os.path.dirname(SCRIPT), "nodes"), os.path.join(tmp, "nodes"))
        return tmp, shutil.copy(MAPPING, tmp)

    def test_dry_run_shows_the_plan_and_where_the_map_goes(self):
        tmp, script = self.copy()
        try:
            r = run(script, "--dry-run", "lab2")
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            for name in ("minimal", "mapping", "scan_filter"):
                self.assertIn("would start %s:" % name, out)
            self.assertIn("config/lidar_mapping.launch", out)
            self.assertIn("keyboard_teleop.launch", out)
            self.assertIn("map_saver -f %s" % os.path.join(tmp, "maps", "lab2"), out)
            self.assertFalse(os.path.exists(os.path.join(tmp, "maps")), "a dry run makes nothing")
        finally:
            shutil.rmtree(tmp)

    def test_an_existing_map_is_not_replaced_unless_asked(self):
        tmp, script = self.copy()
        try:
            lidar_map(os.path.join(tmp, "maps"))
            out = run(script, "--dry-run").stdout
            self.assertIn("exists already", out)
            out = run(script, "--dry-run", "--overwrite").stdout
            self.assertNotIn("exists already", out)
        finally:
            shutil.rmtree(tmp)

    def test_it_never_writes_to_the_home_folder(self):
        with open(MAPPING) as f:
            code = [l for l in f if not l.lstrip().startswith("#")]
        self.assertEqual([l.strip() for l in code if "~/" in l and "devel/setup.bash" not in l], [])

    def test_arguments_are_cleared_before_sourcing_ros(self):
        with open(MAPPING) as f:
            lines = [l.strip() for l in f if not l.lstrip().startswith("#")]
        first_source = next(i for i, l in enumerate(lines) if "source /opt/ros/noetic/setup.bash" in l)
        self.assertIn("set --", [l.split("#")[0].strip() for l in lines[:first_source]])


if __name__ == "__main__":
    unittest.main()
