"""scripts/robot_link_tunnel.sh: the ssh command is the one the link is documented to use. Nothing here opens a connection."""

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "robot_link_tunnel.sh")


class TunnelScriptTest(unittest.TestCase):
    def test_script_is_executable_and_valid_bash(self):
        self.assertTrue(os.stat(SCRIPT).st_mode & stat.S_IXUSR)
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)

    def test_runs_ssh_with_the_documented_options_and_restarts_it(self):
        """A fake ssh that records its arguments and exits at once: the script starts it again, and stops on SIGTERM."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.txt")
            fake = os.path.join(tmp, "ssh")
            with open(fake, "w") as f:
                f.write(textwrap.dedent(f"""\
                    #!/bin/bash
                    echo "$*" >> {log}
                    exit 255
                    """))
            os.chmod(fake, 0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], ROBOT_LINK_TUNNEL_RETRY_S="0.1")
            proc = subprocess.Popen(["bash", SCRIPT], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(100):
                    if os.path.exists(log) and len(open(log).read().splitlines()) >= 2:
                        break
                    proc.poll()
                    import time
                    time.sleep(0.05)
            finally:
                proc.terminate()
                out, _ = proc.communicate(timeout=10)
            calls = open(log).read().splitlines()
            self.assertGreaterEqual(len(calls), 2, out)
            for opt in ("-N", "ServerAliveInterval=2", "ServerAliveCountMax=2", "ExitOnForwardFailure=yes", "-L 8010:127.0.0.1:8010"):
                self.assertIn(opt, calls[0])
            self.assertTrue(calls[0].endswith(" dell"), calls[0])
            self.assertIn("tunnel: stopped", out)


if __name__ == "__main__":
    unittest.main()
