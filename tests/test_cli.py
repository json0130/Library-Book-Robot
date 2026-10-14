import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CtrlCTest(unittest.TestCase):
    def test_ctrl_c_quits_the_simulator_while_it_waits_for_typing(self):
        # stdin stays open and silent, like a keyboard nobody is typing on
        proc = subprocess.Popen([sys.executable, "-m", "robot", "sim", "--no-log"], cwd=ROOT,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(2)
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            proc.kill()
            proc.communicate()


if __name__ == "__main__":
    unittest.main()
