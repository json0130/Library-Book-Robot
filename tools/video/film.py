"""Filming helper for the competition video: what is available, and recording the robot's screens and voice in parallel.

    python tools/video/film.py check
    python tools/video/film.py record --take common1 --sources face,map,audio
    python tools/video/film.py record --take quiet1 --sources face --duration 40
    python tools/video/film.py dell-command --take trip1          # what to run on the Dell if the map is filmed there

`record` starts every source at once into recordings/<take>/ and writes meta.json with the time each one started (time.time(),
the same clock as the robot's recordings/<take>/events.jsonl from `python -m robot sim --film <take>`). Press Enter or Ctrl-C to
stop them all cleanly. A source that fails is reported and the others keep going.

Sources
    face    the face page (http://<display>/?film=1, with the zone badge) in a kiosk browser on a virtual screen (Xvfb, 1024x600:
            the robot's screen), filmed with ffmpeg x11grab -> face.mp4. Headless Chromium through Playwright would also do; it is
            not installed on the Jetson, Firefox and Xvfb are.
    map     the operator page's floor plan with the live robot and the sound map (/operator?film=map) the same way, 1920x1080
            -> map.mp4. The robot's position reaches it from the robot program (`sim --base fake|turtlebot`) or /pose.json.
    screen  the real desktop ($DISPLAY) with x11grab -> screen.mp4, for anything shown in a normal window.
    audio   Stuart's own speaker output from the PulseAudio monitor of its sink (`devices.speaker` in config/robot.yaml) with
            parecord -> audio.wav: its voice without the room.
    mic     the robot's microphone (`devices.mic`, else the system default input) with parecord -> mic.wav: what the visitor says. The
            robot does not keep the sound it hears, so this is the only recording of the visitor's voice. Both audio files are checked
            when the recording stops: a silent one is reported. Record these two beside a screen recording made with another tool
            (GNOME's Ctrl+Shift+Alt+R has no sound), then `session_to_take.py --sound-take <take>` puts them in the picture.

The camera with the faces boxed is recorded by the robot itself (`python -m robot sim --camera --faces --record ...`), so the
webcam is never opened twice. Nothing here moves the robot: it only opens pages and records.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RECORDINGS = ROOT / "recordings"
# snap Firefox can only read and write inside the home folder's snap area, so its throwaway profiles live there
PROFILES = Path.home() / "snap" / "firefox" / "common" / "lbr-film"
SOURCES = ("face", "map", "screen", "audio", "mic")
SIZES = {"face": (1024, 600), "map": (1920, 1080)}
FPS = 30


# ---------------------------------------------------------------------------------------------------- what is available
def run_text(cmd, timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ffmpeg_features(ffmpeg: str = "ffmpeg") -> dict:
    """What this ffmpeg can do that the video needs: libx264, aac, ass/subtitles, loudnorm, x11grab, pulse."""
    if shutil.which(ffmpeg) is None:
        return {"present": False}
    version = (run_text([ffmpeg, "-hide_banner", "-version"]).splitlines() or [""])[0]
    filters = {line.split()[1] for line in run_text([ffmpeg, "-hide_banner", "-filters"]).splitlines() if len(line.split()) > 2}
    encoders = {line.split()[1] for line in run_text([ffmpeg, "-hide_banner", "-encoders"]).splitlines() if len(line.split()) > 2}
    devices = run_text([ffmpeg, "-hide_banner", "-devices"])
    return {"present": True, "version": version,
            "libx264": "libx264" in encoders, "aac": "aac" in encoders,
            "ass": "ass" in filters, "loudnorm": "loudnorm" in filters, "sidechaincompress": "sidechaincompress" in filters,
            "x11grab": "x11grab" in devices, "pulse": " pulse " in devices}


def v4l2_devices() -> list:
    out = []
    for dev in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            name = (dev / "name").read_text().strip()
        except OSError:
            name = "?"
        out.append((f"/dev/{dev.name}", name))
    return out


def pulse_sources() -> list:
    """(name, is_monitor) for every PulseAudio/PipeWire source."""
    out = []
    for line in run_text(["pactl", "list", "short", "sources"]).splitlines():
        parts = line.split("\t")
        if len(parts) > 1:
            out.append((parts[1], parts[1].endswith(".monitor")))
    return out


def speaker_monitor(fragment: Optional[str]) -> Optional[str]:
    """The monitor source of the speaker sink whose name contains `fragment` (devices.speaker), else of the default sink."""
    sinks = [line.split("\t")[1] for line in run_text(["pactl", "list", "short", "sinks"]).splitlines() if "\t" in line]
    if fragment:
        sinks_matching = [s for s in sinks if fragment.lower() in s.lower()]
        if sinks_matching:
            return sinks_matching[0] + ".monitor"
    default = run_text(["pactl", "get-default-sink"]).strip()
    return default + ".monitor" if default else None


def mic_input(fragment: Optional[str]) -> Optional[str]:
    """The input source (not a monitor) whose name contains `fragment` (devices.mic), else the system default input."""
    inputs = [name for name, monitor in pulse_sources() if not monitor]
    if fragment:
        hit = [n for n in inputs if fragment.lower() in n.lower()]
        if hit:
            return hit[0]
    default = run_text(["pactl", "get-default-source"]).strip()
    return default if default and not default.endswith(".monitor") else (inputs[0] if inputs else None)


def sound_level(path: Path) -> Optional[tuple]:
    """(mean dB, peak dB) of a recorded sound file, or None if it cannot be read: a way to tell that the microphone really heard something."""
    import re
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    mean, peak = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", out), re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    if not (mean and peak):
        return None
    return float(mean.group(1)), float(peak.group(1))


def x_display_ok(display: Optional[str]) -> bool:
    if not display:
        return False
    return bool(run_text(["xdpyinfo", "-display", display], timeout=5)) if shutil.which("xdpyinfo") else Path(
        f"/tmp/.X11-unix/X{display.lstrip(':').split('.')[0]}").exists()


def url_ok(url: str, timeout: float = 2.0) -> Optional[str]:
    """None if the URL answers 2xx, else why not."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return None if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except Exception as exc:                               # URLError, timeouts, refused
        return getattr(exc, "reason", None) and str(exc.reason) or exc.__class__.__name__


def display_base(host: Optional[str] = None, port: Optional[int] = None) -> str:
    from robot.config import load_config
    shown = load_config().display
    return f"http://{host or shown['host']}:{port or int(shown['port'])}"


def browsers() -> dict:
    found = {name: shutil.which(name) for name in ("firefox", "chromium", "chromium-browser", "google-chrome", "Xvfb")}
    try:
        import playwright  # noqa: F401
        found["playwright"] = "python package installed"
    except ImportError:
        found["playwright"] = None
    return found


def check(host: Optional[str] = None, port: Optional[int] = None, out=sys.stdout) -> int:
    """Print what is available for filming; returns the number of problems that block a source."""
    say = lambda line="": print(line, file=out)
    problems = 0
    ff = ffmpeg_features()
    say("ffmpeg")
    if not ff["present"]:
        say("  MISSING: sudo apt install ffmpeg (Ubuntu's build: NVIDIA's Jetson build has no libx264/aac/subtitles)")
        problems += 1
    else:
        say(f"  {ff['version']}")
        for key in ("libx264", "aac", "ass", "loudnorm", "sidechaincompress", "x11grab", "pulse"):
            say(f"  {key:18} {'yes' if ff[key] else 'NO'}")
            problems += 0 if ff[key] else 1
    say("cameras (v4l2)")
    for dev, name in v4l2_devices() or [("none", "")]:
        say(f"  {dev:12} {name}")
    say("PulseAudio sources (*.monitor = what a speaker plays)")
    from robot.config import load_config
    speaker = (load_config().robot.get("devices") or {}).get("speaker")
    for name, monitor in pulse_sources():
        say(f"  {'monitor ' if monitor else 'input   '} {name}")
    mon = speaker_monitor(speaker)
    say(f"  -> Stuart's voice would be recorded from: {mon or 'NONE (no sink)'} (devices.speaker = {speaker!r})")
    problems += 0 if mon else 1
    mic_wanted = (load_config().robot.get("devices") or {}).get("mic")
    mic_dev = mic_input(mic_wanted)
    say(f"  -> the visitor's voice would be recorded from: {mic_dev or 'NONE (no input)'} (devices.mic = {mic_wanted!r})")
    problems += 0 if mic_dev else 1
    free = available_mb()
    say(f"memory: {free} MB available (each browser source needs about {BROWSER_MB} MB)")
    display = os.environ.get("DISPLAY")
    say(f"X display: {display or 'not set'} {'reachable' if x_display_ok(display) else '(not reachable: the screen source will not work)'}")
    found = browsers()
    say("browsers: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in found.items()))
    if not (found.get("Xvfb") and found.get("firefox")) and not found.get("playwright"):
        say("  the face and map sources need Xvfb + Firefox (sudo apt install xvfb) or Playwright")
        problems += 1
    base = display_base(host, port)
    for what, path in (("face page", "/?film=1"), ("map UI (operator page)", "/operator?film=map"), ("floor plan", "/plan.json"),
                       ("sound map", "/noise.json"), ("robot position", "/pose.json")):
        why = url_ok(base + path)
        say(f"{what:24} {base + path}: {'ok' if why is None else 'NOT reachable (' + why + ')'}")
    say("  (start the display server with `python -m display.server`; /noise.json needs data/noise/noise_grid.json, "
        "/pose.json the robot link, or run the robot with `sim --base fake` for a simulated position)")
    say(f"clock: time.time() = {time.time():.3f} (the robot's events.jsonl and meta.json use this clock; a recording made on the Dell "
        "must be NTP-synced: `timedatectl` there should say 'System clock synchronized: yes')")
    return problems


# ---------------------------------------------------------------------------------------------------- recording
@dataclass
class Source:
    """One thing being recorded: its processes (started in order, stopped in reverse) and what happened."""
    name: str
    output: Path
    procs: list = field(default_factory=list)
    started: Optional[float] = None
    error: Optional[str] = None
    cmds: list = field(default_factory=list)
    stop_with_q: bool = False                 # ffmpeg: write 'q' on stdin so it finishes the mp4 properly

    def alive(self) -> bool:
        return bool(self.procs) and self.procs[-1].poll() is None


def free_display(start: int = 91) -> int:
    for n in range(start, start + 40):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise RuntimeError("no free X display number for Xvfb")


def firefox_profile(name: str) -> Path:
    """A fresh throwaway profile for a kiosk window: no welcome page, no session restore, no update nags."""
    d = PROFILES / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    prefs = {"browser.aboutwelcome.enabled": False, "browser.shell.checkDefaultBrowser": False,
             "browser.startup.homepage_override.mstone": "ignore", "browser.sessionstore.resume_from_crash": False,
             "datareporting.policy.dataSubmissionEnabled": False, "toolkit.telemetry.reportingpolicy.firstRun": False,
             "trailhead.firstrun.didSeeAboutWelcome": True, "browser.tabs.warnOnClose": False, "app.update.disabledForTesting": True,
             "browser.translations.automaticallyPopup": False, "media.autoplay.default": 0}
    (d / "user.js").write_text("".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in prefs.items()))
    return d


def browser_capture_cmds(name: str, url: str, size: tuple, output: Path, display_no: int, profile: Path, fps: int = FPS) -> list:
    """Xvfb, the kiosk browser, and ffmpeg filming the virtual screen."""
    w, h = size
    xvfb = ["Xvfb", f":{display_no}", "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-ac"]
    firefox = ["firefox", "--no-remote", "--profile", str(profile), "--kiosk", "--width", str(w), "--height", str(h), url]
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", str(fps),
              "-video_size", f"{w}x{h}", "-i", f":{display_no}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
              "-pix_fmt", "yuv420p", str(output)]
    return [xvfb, firefox, ffmpeg]


def screen_cmd(output: Path, display: str, fps: int = FPS) -> list:
    size = None
    for line in run_text(["xdpyinfo", "-display", display]).splitlines():
        if "dimensions:" in line:
            size = line.split()[1]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab", "-framerate", str(fps),
            *(["-video_size", size] if size else []), "-i", display, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(output)]


def audio_cmd(output: Path, monitor: str, channels: int = 2) -> list:
    return ["parecord", f"--device={monitor}", "--rate=48000", f"--channels={channels}", "--format=s16le", "--file-format=wav", str(output)]


def plan_sources(names, take_path: Path, base_url: str, speaker: Optional[str], mic: Optional[str] = None) -> list:
    """The Source objects for the names asked for, with their commands; raises ValueError for an unknown name."""
    sources = []
    display_no = None
    for name in names:
        if name not in SOURCES:
            raise ValueError(f"unknown source {name!r}: use {', '.join(SOURCES)}")
        if name in ("face", "map"):
            display_no = free_display(display_no + 1 if display_no else 91)
            url = base_url + ("/?film=1&preview" if name == "face" else "/operator?film=map")
            src = Source(name, take_path / f"{name}.mp4", stop_with_q=True)
            src.cmds = browser_capture_cmds(name, url, SIZES[name], src.output, display_no, PROFILES / f"{take_path.name}-{name}")
            src.display_no = display_no
        elif name == "screen":
            display = os.environ.get("DISPLAY", ":0")
            src = Source(name, take_path / "screen.mp4", stop_with_q=True)
            src.cmds = [screen_cmd(src.output, display)]
        elif name == "mic":
            device = mic_input(mic)
            src = Source(name, take_path / "mic.wav")
            if device is None:
                src.error = "no microphone to record from"
            src.cmds = [audio_cmd(src.output, device or "none", 1)]
        else:
            monitor = speaker_monitor(speaker)
            src = Source(name, take_path / "audio.wav")
            if monitor is None:
                src.error = "no speaker sink to record from"
            src.cmds = [audio_cmd(src.output, monitor or "none")]
        sources.append(src)
    return sources


BROWSER_MB = 800                      # a snap Firefox showing the face or the map, with Xvfb and ffmpeg: about this much memory


def available_mb() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def memory_check(names) -> Optional[str]:
    """Why the browser sources must not start (the Jetson's OOM killer then kills the desktop, VS Code first), or None."""
    browsers_wanted = sum(1 for n in names if n in ("face", "map"))
    free = available_mb()
    if not browsers_wanted or free is None or free >= browsers_wanted * BROWSER_MB:
        return None
    return (f"only {free} MB of memory is free and {browsers_wanted} browser source(s) need about {browsers_wanted * BROWSER_MB} MB: "
            "close other browsers (headless Firefox left over from screenshots counts), or record face and map in separate takes")


def wait_for_display(display: str, proc=None, timeout: float = 10.0) -> None:
    """Xvfb takes a moment (it compiles a keymap) before it accepts clients: a browser started too soon says 'cannot open display'."""
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("Xvfb exited at once")
        if subprocess.run(["xdpyinfo", "-display", display], capture_output=True).returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError(f"the virtual screen {display} did not come up in {timeout:g} s")


def wait_for_page(display: str, proc=None, timeout: float = 60.0, paint_s: float = 2.0) -> None:
    """Snap Firefox takes about 12 s to start on the Jetson. Its window is titled "<page title> — Mozilla Firefox" once the page has
    loaded (just "Firefox" before): wait for that, then a moment for the first paint, so the recording does not start black."""
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("the browser exited at once")
        tree = subprocess.run(["xwininfo", "-display", display, "-root", "-tree"], capture_output=True, text=True).stdout
        if "Mozilla Firefox" in tree:
            time.sleep(paint_s)
            return
        time.sleep(0.5)
    raise RuntimeError(f"the page did not load in the browser within {timeout:g} s")


def first_to_go() -> None:
    """In the child, before it runs: the highest OOM score, so if memory runs out the kernel kills this recording, not the desktop
    or VS Code (on the Jetson it did, twice, before this)."""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def start_source(src: Source, log_dir: Path, popen=subprocess.Popen, settle_s: float = 6.0) -> None:
    """Start a source's processes in order (Xvfb, browser, then the recorder). Errors are kept on the source, never raised."""
    if src.error:
        return
    try:
        env = dict(os.environ)
        for i, cmd in enumerate(src.cmds):
            if cmd[0] == "firefox":
                firefox_profile(Path(cmd[cmd.index("--profile") + 1]).name)
            if hasattr(src, "display_no") and cmd[0] != "Xvfb":
                env["DISPLAY"] = f":{src.display_no}"
            last = i == len(src.cmds) - 1
            log = open(log_dir / f"{src.name}.{Path(cmd[0]).name}.log", "w")
            proc = popen(cmd, stdin=subprocess.PIPE if (last and src.stop_with_q) else subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True, preexec_fn=first_to_go)
            src.procs.append(proc)
            if not last:                                              # the screen, then the page, must be up before filming starts
                if cmd[0] == "Xvfb":
                    wait_for_display(f":{src.display_no}", proc)
                elif cmd[0] == "firefox":
                    wait_for_page(f":{src.display_no}", proc)
                else:
                    time.sleep(settle_s)
                if proc.poll() is not None:
                    raise RuntimeError(f"{cmd[0]} exited at once (see {log_dir}/{src.name}.{Path(cmd[0]).name}.log)")
        src.started = time.time()
    except Exception as exc:
        src.error = str(exc)
        stop_source(src)


def stop_source(src: Source, timeout: float = 8.0) -> None:
    """Stop the recorder first (cleanly: 'q' for ffmpeg, SIGINT for parecord), then the browser and the screen."""
    for i, proc in enumerate(reversed(src.procs)):
        if proc.poll() is not None:
            continue
        try:
            if i == 0 and src.stop_with_q and proc.stdin:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
            else:
                os.killpg(proc.pid, signal.SIGINT if i == 0 else signal.SIGTERM)
            proc.wait(timeout=timeout)
        except (OSError, subprocess.SubprocessError, ValueError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


def write_meta(take_path: Path, take: str, sources: list, started: float, stopped: Optional[float] = None, extra=None) -> Path:
    meta = {"take": take, "start_time": started, "stop_time": stopped, "clock": "time.time() on " + socket.gethostname(),
            "sources": {s.name: {"file": s.output.name, "started": s.started, "error": s.error,
                                 "commands": [" ".join(c) for c in s.cmds]} for s in sources}}
    meta.update(extra or {})
    path = take_path / "meta.json"
    path.write_text(json.dumps(meta, indent=2))
    return path


def record(take: str, names, base_url: str, speaker: Optional[str], duration: Optional[float] = None,
           wait: Optional[Callable[[], None]] = None, out=sys.stdout, force: bool = False, mic: Optional[str] = None) -> int:
    from robot.film import take_dir
    take_path = take_dir(take, RECORDINGS)
    sources = plan_sources(names, take_path, base_url, speaker, mic)
    short = memory_check(names)
    if short and force:
        print(f"  warning: {short}. Going ahead (--force): the recording is the first thing the kernel would stop.", file=out)
        short = None
    if short:
        for s in sources:
            if s.name in ("face", "map"):
                s.error = short
    started = time.time()
    write_meta(take_path, take, sources, started)
    threads = [threading.Thread(target=start_source, args=(s, take_path)) for s in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    write_meta(take_path, take, sources, started)
    for s in sources:
        print(f"  {s.name:7} {'RECORDING -> ' + str(s.output) if s.error is None else 'FAILED: ' + s.error}", file=out)
    if all(s.error for s in sources):
        print("Nothing is recording.", file=out)
        return 1
    try:
        if duration:
            end = time.time() + duration
            while time.time() < end and any(s.alive() for s in sources):
                time.sleep(0.2)
        else:
            print(f"Recording take {take!r}. Press Enter (or Ctrl-C) to stop.", file=out, flush=True)
            (wait or (lambda: sys.stdin.readline()))()
    except KeyboardInterrupt:
        pass
    finally:
        for s in sources:
            if s.error is None and not s.alive() and s.procs:
                s.error = f"{s.cmds[-1][0]} stopped early (see its .log in {take_path})"
        stopped = time.time()
        for s in sources:
            stop_source(s)
        meta = write_meta(take_path, take, sources, started, stopped)
    for s in sources:
        size = s.output.stat().st_size if s.output.exists() else 0
        print(f"  {s.name:7} {s.output} {size / 1e6:.1f} MB" + (f"  ({s.error})" if s.error else ""), file=out)
    for s in sources:
        if s.name in ("audio", "mic") and s.error is None and s.output.exists():
            level = sound_level(s.output)
            if level is None or level[1] < -50:
                print(f"  WARNING {s.name}: nothing was heard (peak {'?' if level is None else f'{level[1]:.0f}'} dB). Check the device: `film.py check`", file=out)
                s.error = "silent"
            else:
                print(f"  {s.name:7} heard: peak {level[1]:.0f} dB, average {level[0]:.0f} dB", file=out)
    print(f"  meta: {meta}", file=out)
    return 0 if all(s.error is None for s in sources) else 2


def dell_command(take: str) -> str:
    """What to run on the Dell when the map is filmed there instead (its own screen, e.g. noise_nav_web.py in a browser)."""
    return ("# on the Dell (check `timedatectl` says the clock is synchronized), in the folder you want the file in:\n"
            f"python3 -c \"import time,json; json.dump({{'take': '{take}', 'start_time': time.time()}}, open('{take}-map-meta.json','w'))\" && \\\n"
            f"  ffmpeg -f x11grab -framerate 30 -i $DISPLAY -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p {take}-map.mp4\n"
            "# press q to stop; copy both files into recordings/" + take + "/ on the Jetson as map.mp4 and dell-map-meta.json")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="film.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="what is available for filming")
    c.add_argument("--host"), c.add_argument("--port", type=int)
    r = sub.add_parser("record", help="record sources in parallel to recordings/<take>/")
    r.add_argument("--take", required=True)
    r.add_argument("--sources", default="face,map,audio", help=f"comma-separated: {', '.join(SOURCES)} (default %(default)s)")
    r.add_argument("--host", help="the display server's host (default display.host in config/robot.yaml)")
    r.add_argument("--port", type=int)
    r.add_argument("--duration", type=float, help="stop by itself after this many seconds")
    r.add_argument("--mic", help="part of the microphone's name, the one the robot listens with (default devices.mic, else the system default input)")
    r.add_argument("--force", action="store_true",
                   help="record a browser source even with little free memory (it runs with the highest OOM score, so it goes first)")
    d = sub.add_parser("dell-command", help="print the command to film the map on the Dell")
    d.add_argument("--take", required=True)
    args = p.parse_args(argv)
    if args.cmd == "check":
        return 1 if check(args.host, args.port) else 0
    if args.cmd == "dell-command":
        print(dell_command(args.take))
        return 0
    from robot.config import load_config
    speaker = (load_config().robot.get("devices") or {}).get("speaker")
    names = [n.strip() for n in args.sources.split(",") if n.strip()]
    try:
        return record(args.take, names, display_base(args.host, args.port), speaker, args.duration, force=args.force,
                      mic=args.mic or (load_config().robot.get("devices") or {}).get("mic"))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
