"""Live 3D pose plot in matplotlib — the second GUI-owning plugin.

Same stash-then-tick idiom as `exo_live`, and worth having both: this is the case where doing
it wrong is most tempting, because a matplotlib redraw is slow enough (tens of milliseconds)
that calling it from the frame handler visibly throttles the whole subscription.

Note what makes this plugin possible at all: it runs in its own OS process, so it owns a main
thread that matplotlib is happy to draw on. In the pre-plugin version of climb-cv this needed a
hand-written `multiprocessing.Process` plus a queue of landmark frames. Here it is a manifest
and a handler, and the framework's `setup()`-in-the-child rule means the figure is created in
the process that draws it rather than being pickled across a boundary.
"""

from __future__ import annotations

import time

import numpy as np

from climbcv import Plugin, every, subscribe
from climbcv.rendering import plot_world_landmarks

VISIBILITY, X, Y, Z = 0, 1, 2, 3


class PosePlot(Plugin):
    def setup(self) -> None:
        try:
            import matplotlib

            # An interactive backend is required; the default may be Agg (file-only) when no
            # display is attached. Fail over to unavailable() rather than drawing into the void.
            if matplotlib.get_backend().lower() in ("agg", "pdf", "ps", "svg", "template"):
                for candidate in ("macosx", "qtagg", "tkagg"):
                    try:
                        matplotlib.use(candidate, force=True)
                        break
                    except Exception:
                        continue
            import matplotlib.pyplot as plt
        except Exception as exc:
            self.unavailable(f"matplotlib is not usable here: {exc}")
            return

        backend = matplotlib.get_backend()
        if backend.lower() in ("agg", "pdf", "ps", "svg", "template"):
            self.unavailable(
                f"matplotlib has no interactive backend available (got {backend}); "
                "there is nothing to display into."
            )
            return

        self._plt = plt
        self._limit = float(self.config.get("limit_m", 1.0))
        self._pose = None
        self._drawn = 0

        plt.ion()
        self._fig = plt.figure("climb-cv — 3D pose", figsize=(5, 5))
        self._ax = self._fig.add_subplot(111, projection="3d")
        self._ax.view_init(
            elev=float(self.config.get("elev", -75)),
            azim=float(self.config.get("azim", -90)),
        )
        self._fig.tight_layout()
        self._fig.show()

        self.set_interval(self.redraw, 1.0 / float(self.config.get("redraw_hz", 15)))
        self.log.info("3D plot window open (backend %s)", backend)

    @subscribe("pose.smoothed")
    def on_pose(self, pose, meta) -> None:
        # Stash only — a matplotlib redraw here would throttle this subscription and, worse,
        # leave the window unpumped between messages.
        self._pose = pose

    @every(1 / 15)
    def redraw(self) -> None:
        pose = self._pose
        if pose is None:
            # Still pump the event loop, or the window is unresponsive before the first pose.
            self._plt.pause(0.001)
            return

        # The original's grouping: one scatter for the face and six polylines for the limbs,
        # shoulders and waist. The previous version issued one ax.plot per skeleton edge --
        # thirty-five calls a redraw -- which is what made this window stutter. matplotlib 3D
        # is the expensive thing on screen, so the call count is the whole game.
        if plot_world_landmarks(self._ax, pose.world, self._limit):
            self._ax.set_title(f"frame {pose.frame_seq}   {pose.topology}")
            self._fig.canvas.draw_idle()
            self._drawn += 1

        # pause() both flushes the draw and services the GUI event loop -- matplotlib's
        # equivalent of cv2.waitKey, and leaving it out is the same bug.
        self._plt.pause(0.001)

    def teardown(self) -> None:
        self.log.info("rendered %d 3D frames", self._drawn)
        try:
            self._plt.close(self._fig)
        except Exception:
            pass
