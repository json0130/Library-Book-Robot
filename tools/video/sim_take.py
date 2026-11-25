"""A simulated take: the robot program on the fake base, a scripted visit, and its screens and voice recorded. Nothing drives.

    python tools/video/sim_take.py common --zone common --sources face,audio
    python tools/video/sim_take.py quiet --zone quiet --sources face,audio

It starts `film.py record --take <take>` (the face page and Stuart's speaker), waits until it records, then runs
`python -m robot sim --base fake --hold-zone <zone> --film <take> --speak --llm` with video/takes/<script>.txt as its input (lines starting
with # are left out; `wait N` lets the robot talk), and stops the recording when the visit is over. Use --take to name the take
differently from the script (e.g. a retake: --take common2).

The AI must be reachable: the local one (`scripts/start_local_ai.sh` in the local-ai worktree, docs/local_ai.md) at AI_HOST/AI_PORT,
or the AI server. The display server must be running on --port (by default a separate one on 8775, so a real robot's page on 8765 is
never touched: `python -m display.server --port 8775 --host 127.0.0.1`). The book is "scanned" with the fake camera
(`book <isbn>`): the video marks it as simulated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAKES = ROOT / "video" / "takes"


def script_lines(name: str) -> str:
    path = Path(name) if Path(name).is_file() else TAKES / f"{name}.txt"
    lines = [l.rstrip() for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return "\n".join(lines + ["quit"]) + "\n"


FACE_ONLY = ["--config-set", "styles.common_alone.screen_content=false", "--config-set", "styles.common_group.screen_content=false"]


def sim_command(take: str, zone: str, port: int, speak: bool, llm: bool, face_only: bool = True, replay: str = "") -> list:
    """face_only: in the common area the screen shows only the face (the paper's design, `screen_content` in config/styles.yaml)."""
    return [sys.executable, "-m", "robot", "sim", "--base", "fake", "--hold-zone", zone, "--film", take, "--no-persist", "--no-log",
            "--settle", "0", "--config-set", f"display.port={port}", *(FACE_ONLY if face_only else []),
            *(["--base-replay", replay] if replay else []), *(["--speak"] if speak else []), *(["--llm"] if llm else [])]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sim_take.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("script", help="video/takes/<script>.txt, or a path")
    p.add_argument("--take", help="the take's name (default: the script's name + 1, e.g. common1)")
    p.add_argument("--zone", choices=("quiet", "common"), required=True)
    p.add_argument("--sources", default="face,audio")
    p.add_argument("--port", type=int, default=8775, help="the display server the robot and the recording use")
    p.add_argument("--no-speak", action="store_true")
    p.add_argument("--no-llm", action="store_true", help="answer from the catalogue text only")
    p.add_argument("--replay", default="", help="a trajectory file (tools/video/trajectory.py): each `drive` replays a track recorded on the real robot")
    p.add_argument("--common-cards", action="store_true",
                   help="let the common area's screen show book cards too (the robot's default today); without it only the face")
    p.add_argument("--force", action="store_true", help="passed to film.py record (little free memory)")
    args = p.parse_args(argv)
    take = args.take or f"{Path(args.script).stem}1"
    stdin = script_lines(args.script)
    env = {k: v for k, v in os.environ.items() if k != "ROBOT_LINK_TOKEN"}     # a simulated take never reaches the real base
    rec = subprocess.Popen([sys.executable, str(ROOT / "tools" / "video" / "film.py"), "record", "--take", take, "--sources", args.sources,
                            "--port", str(args.port), *(["--force"] if args.force else [])],
                           cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    started = threading.Event()
    lines = []

    def read():
        for line in rec.stdout:
            lines.append(line)
            print("  [film] " + line.rstrip(), flush=True)
            if "Press Enter" in line or "Nothing is recording" in line:
                started.set()
        started.set()

    threading.Thread(target=read, daemon=True).start()
    if not started.wait(120) or rec.poll() is not None:
        print("film.py did not start recording", file=sys.stderr)
        rec.kill()
        return 1
    time.sleep(1.0)
    print(f"  [sim] {' '.join(sim_command(take, args.zone, args.port, not args.no_speak, not args.no_llm, not args.common_cards, args.replay))}", flush=True)
    sim = subprocess.run(sim_command(take, args.zone, args.port, not args.no_speak, not args.no_llm, not args.common_cards, args.replay), cwd=ROOT, env=env, input=stdin,
                         text=True, capture_output=True)
    (ROOT / "recordings" / take / "sim.log").write_text(sim.stdout + sim.stderr)
    time.sleep(1.5)                                            # the last words leave the speaker
    try:
        rec.stdin.write("\n")
        rec.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    rec.wait(timeout=60)
    time.sleep(0.5)
    print(f"  sim exit {sim.returncode}; its output is in recordings/{take}/sim.log")
    return 0 if sim.returncode == 0 and rec.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
