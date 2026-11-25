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


class StatesCommandTest(unittest.TestCase):
    def run_states(self, *flags):
        done = subprocess.run([sys.executable, "-m", "robot", "states", *flags], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_mermaid_prints_the_diagram_from_the_table(self):
        out = self.run_states("--mermaid")
        self.assertTrue(out.startswith("stateDiagram-v2"))
        for state in ("IDLE", "ENGAGED", "RETURNING", "ANNOUNCE", "ANYTHING_ELSE"):
            self.assertIn(state, out)

    def test_markdown_is_the_document_in_docs(self):
        self.assertEqual(self.run_states("--markdown"), (ROOT / "docs" / "state_machine.md").read_text())

    def test_the_plain_listing_names_every_state_with_its_path(self):
        out = self.run_states()
        self.assertIn("ENGAGED.OFFER", out)
        self.assertIn("RETURNING.NAVIGATING", out)


class ScriptedSimTest(unittest.TestCase):
    def test_a_scripted_visit_prints_the_state_path_of_every_transition(self):
        script = "zone common\nengage\npick return\nbook\ndropped\npick done\n"
        done = subprocess.run([sys.executable, "-m", "robot", "sim", "--no-log", "--no-persist", "--settle", "0"], cwd=ROOT,
                              input=script, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        for path in ("IDLE -> ENGAGED.GREET", "ENGAGED.GREET -> ENGAGED.OFFER", "ENGAGED.OFFER -> ENGAGED.AWAIT_BOOK",
                     "ENGAGED.AWAIT_BOOK -> ENGAGED.SCAN", "ENGAGED.SCAN -> ENGAGED.STORE",
                     "ENGAGED.STORE -> ENGAGED.ANYTHING_ELSE", "ENGAGED.ANYTHING_ELSE -> ENGAGED.FAREWELL"):
            self.assertIn(path, done.stdout)


if __name__ == "__main__":
    unittest.main()
