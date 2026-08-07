"""A minimal third-party plugin: read frames, publish a number.

This is the whole shape of a climb-cv plugin. Things to notice:

* No Process, Queue, Lock, Event or pickle anywhere. The framework already runs this file in
  its own OS process; you write single-threaded code and it stays single-threaded.
* No `__init__`. The framework binds `self.config`, `self.log` and `self.publish` *after*
  constructing you, so inside `__init__` none of them exist yet. `setup()` runs in this
  plugin's own process with everything available — that is where your model, camera or window
  belongs.
* Every topic used here is declared in climbcv-plugin.toml. Publishing something you did not
  declare raises, because a typo that silently goes nowhere forever is the worst failure a
  pub/sub system can hand you.
"""

from __future__ import annotations

import time

import numpy as np

from climbcv import Plugin, every, subscribe
from climbcv.contracts import Scalar


class Brightness(Plugin):
    def setup(self) -> None:
        # self.config is your section of climbcv.toml as a plain dict. Deliberately
        # unvalidated: use .get() with a default. TOML has no null, so "unset" means the key
        # is simply absent rather than "" or 0.
        self._dark_below = float(self.config.get("dark_below", 40.0))
        self._frames = 0
        self._sum = 0.0
        self._was_dark = False

        # @every fixes its interval at class-definition time, so a *configurable* rate needs
        # set_interval from setup().
        self.set_interval(self.report, float(self.config.get("report_every_s", 2.0)))
        self.log.info("watching brightness; dark below %.0f", self._dark_below)

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        # frame.pixels is read-only, and shared with no one: every subscriber gets its own
        # copy. To modify it, take a copy — as_bgr()/as_rgb() already return a writable one.
        level = float(np.mean(frame.pixels))

        self._frames += 1
        self._sum += level
        self.publish("example.brightness", Scalar(value=level, t_ns=time.monotonic_ns()))

        dark = level < self._dark_below
        if dark != self._was_dark:
            self._was_dark = dark
            self.log.info(
                "scene went %s (%.1f) at frame %d", "dark" if dark else "light",
                level, frame.seq,
            )

    @every(2.0)
    def report(self) -> None:
        if not self._frames:
            self.log.info("no frames yet")
            return
        self.log.info(
            "%d frames, mean brightness %.1f", self._frames, self._sum / self._frames
        )

    def teardown(self) -> None:
        # For closing files and releasing devices — not for finishing work. It has a time
        # budget (1s by default, raisable per plugin with teardown_timeout_s) and there is no
        # ordering guarantee between plugins.
        self.log.info("saw %d frames in total", self._frames)
