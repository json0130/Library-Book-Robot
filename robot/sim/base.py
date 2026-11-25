"""The base for the simulator and the scenario harness without the real TurtleBot: tools/fake_base.py in this process.

    fake = start_fake_base(cfg, speed=1.0)          # a FakeBaseServer on a free local port, with a random token
    link = fake.make_link(cfg)                      # a TurtleBotLink pointed at it
    ...
    fake.stop()
"""

from __future__ import annotations

import secrets

from robot.config import Config


class FakeBaseHandle:
    def __init__(self, server, token: str):
        self.server, self.token = server, token
        self.model = server.model

    @property
    def url(self) -> str:
        return self.server.url

    def make_link(self, cfg: Config):
        from robot.app import build_link
        return build_link(cfg, {"ROBOT_LINK_TOKEN": self.token}, self.url)

    def stop(self) -> None:
        self.server.stop()


def load_replay(path) -> list:
    """The tracks of a trajectory file made by tools/video/trajectory.py: [[[seconds, x, y, yaw], ...], ...]."""
    import json
    data = json.load(open(path, encoding="utf-8"))
    return [t["points"] for t in data["tracks"]]


def start_fake_base(cfg: Config, speed: float = 1.0, time_scale: float = 1.0, **options) -> FakeBaseHandle:
    """Start the stand-in for the Dell on a free local port. `speed` is its driving speed in m/s, `time_scale` multiplies
    it (scenario runs); `options` are FakeBase's (fail_rate, seed, unlocalised_for, uncalibrated...)."""
    from tools.fake_base import FakeBase, FakeBaseServer, read_sections
    token = secrets.token_hex(8)
    plan, zones = read_sections(cfg.path(cfg.books["floorplan_path"]), cfg.path(cfg.books["shelves_path"]))
    model = FakeBase(token, plan, zones, speed=speed, time_scale=time_scale, **options)
    return FakeBaseHandle(FakeBaseServer(model, "127.0.0.1", 0).start(), token)
