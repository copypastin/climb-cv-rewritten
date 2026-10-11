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

from climbcv import Plugin, every, subscribe
from climbcv.rendering import is_pose_fresh, plot_world_landmarks

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
        self._max_pose_age_ms = float(self.config.get("max_pose_age_ms", 250))
        self._min_visibility = float(self.config.get("min_visibility", 0.5))
        if not 0 < self._max_pose_age_ms < float("inf"):
            raise ValueError("max_pose_age_ms must be positive and finite")
        if not 0 <= self._min_visibility <= 1:
            raise ValueError("min_visibility must be between 0 and 1")
        self._pose = None
        self._last_drawn_pose = None
        self._drawn = 0

        plt.ion()
        self._fig = plt.figure("climb-cv — 3D pose", figsize=(5, 5))
        self._ax = self._fig.add_subplot(111, projection="3d")
        self._ax.view_init(
            elev=float(self.config.get("elev", -75)),
            azim=float(self.config.get("azim", 90)),
        )
        self._fig.tight_layout()
        self._fig.show()

        self.set_interval(self.redraw, 1.0 / float(self.config.get("redraw_hz", 60)))
        self.log.info("3D plot window open (backend %s)", backend)

    @subscribe("pose.smoothed")
    def on_pose(self, pose, meta) -> None:
        # Stash only — a matplotlib redraw here would throttle this subscription and, worse,
        # leave the window unpumped between messages.
        self._pose = pose

    @every(0)
    def redraw(self) -> None:
        pose = self._pose
        if not is_pose_fresh(pose, self._max_pose_age_ms):
            pose = None
            self._pose = None

        # Check expiry before this shortcut, even when the source has stopped entirely.
        if self._last_drawn_pose is pose:
            self._plt.pause(0.001)
            return

        self._last_drawn_pose = pose
        visible = plot_world_landmarks(
            self._ax, pose.world if pose is not None else None,
            self._limit, self._min_visibility,
        )
        if visible:
            self._ax.set_title(f"frame {pose.frame_seq}   {pose.topology}")
            self._drawn += 1
        else:
            self._ax.set_title("no recent pose" if pose is None else "no visible landmarks")
        self._fig.canvas.draw_idle()

        # pause() both flushes the draw and services the GUI event loop -- matplotlib's
        # equivalent of cv2.waitKey, and leaving it out is the same bug.
        self._plt.pause(0.001)

    def teardown(self) -> None:
        """Close the matplotlib figure and log how many frames were rendered."""
        self.log.info("rendered %d 3D frames", self._drawn)
        # We explicitly DO NOT call self._plt.close(self._fig) here. When using the 'macosx'
        # backend in a multiprocessing child process, calling close() during shutdown often
        # triggers a native segfault because the Cocoa event loop is in an unstable state.
        # The OS will reap the window cleanly when the process exits.
