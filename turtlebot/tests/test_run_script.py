"""run_robot_link.sh: the checks that happen before it starts anything. None of these starts ROS or moves the robot."""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "run_robot_link.sh")


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

    def test_the_script_has_no_sudo_in_it(self):
        with open(SCRIPT) as f:
            code = [line for line in f if not line.lstrip().startswith("#")]
        offenders = [line for line in code if "sudo" in line and "echo" not in line and "warn" not in line]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
