"""Live exo-skeleton overlay — the reference GUI-owning plugin.

Two idioms here are the whole point of this example.

**Stash, then draw on a tick.** The handler does not touch the window; it stores the newest
frame and returns. A separate `@every` timer draws and pumps the GUI event loop. If drawing
happened in the handler instead, the window would freeze whenever data paused — because the
framework's loop is then blocked waiting for the next message, and a window that is not pumped
is a window the OS reports as "not responding". This is the single most common way a GUI plugin
goes wrong, and it looks correct on the page.

**Combine inputs with latest(), not with a second handler.** Frames arrive at 30 Hz, poses
arrive whenever the pose stage finishes, and the lid angle arrives about once a second. Trying
to synchronise them in handlers means keeping state and guessing; `self.latest(topic)` just
gives you the newest of each at the moment you draw.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from climbcv import Plugin, every, subscribe
from climbcv.contracts import Shutdown
from climbcv.topology import edges_for

_SKELETON = (66, 245, 158)   # BGR
_JOINT = (255, 210, 60)
_TEXT = (240, 240, 240)


class ExoLive(Plugin):
    def setup(self) -> None:
        self._window = str(self.config.get("window_name", "climb-cv — exo skeleton"))
        self._scale = float(self.config.get("scale", 1.0))
        self._radius = int(self.config.get("point_radius", 3))
        self._thickness = int(self.config.get("line_width", 2))
        self._show_fps = bool(self.config.get("show_fps", True))

        self._frame = None
        self._drawn = 0
        self._t0 = time.monotonic()
        self._fps = 0.0
        self._last_draw = self._t0

        try:
            cv2.namedWindow(self._window, cv2.WINDOW_NORMAL)
        except cv2.error as exc:
            # No display (headless CI, ssh without X). Not a crash and not this plugin's
            # fault: "unavailable" gets one INFO line, no retry, and the rest of the run
            # continues without an overlay.
            self.unavailable(f"cannot open a window: {exc}")

        self.set_interval(self.draw, 1.0 / float(self.config.get("draw_hz", 60)))
        self.log.info("overlay window open — press ESC in it to stop the run")

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        # Stash only. Never draw here: see the module docstring.
        self._frame = frame

    @every(1 / 60)
    def draw(self) -> None:
        frame = self._frame
        if frame is None:
            return

        # as_bgr() always returns a fresh writable C-contiguous array, so drawing into it is
        # safe. frame.pixels itself is read-only and shared with nothing.
        canvas = frame.as_bgr()
        if self._scale != 1.0:
            canvas = cv2.resize(canvas, None, fx=self._scale, fy=self._scale)
        h, w = canvas.shape[:2]

        pose = self.latest("pose.smoothed")
        if pose is not None and pose.image is not None:
            self._draw_skeleton(canvas, pose, w, h)
        else:
            cv2.putText(canvas, "no pose", (12, h - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _TEXT, 1, cv2.LINE_AA)

        self._draw_hud(canvas, frame, pose, w)

        cv2.imshow(self._window, canvas)
        # waitKey IS the GUI pump. Without it the window never repaints and the OS marks the
        # app unresponsive, however healthy the pipeline is.
        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            self.log.info("ESC pressed — asking the run to stop")
            self.publish("app.shutdown", Shutdown("ESC pressed in the overlay window"))

        self._drawn += 1
        now = time.monotonic()
        dt = now - self._last_draw
        if dt > 0:
            self._fps = 0.9 * self._fps + 0.1 * (1.0 / dt) if self._fps else 1.0 / dt
        self._last_draw = now

    def _draw_skeleton(self, canvas, pose, w: int, h: int) -> None:
        """pose.image is normalised to the frame with origin top-left, so pixels are (x*w, y*h).

        Column order is (visibility, x, y, z) — visibility first, which is easy to get wrong.
        """
        pts = pose.image
        vis, xs, ys = pts[:, 0], pts[:, 1], pts[:, 2]
        px = (xs * w).astype(np.int32)
        py = (ys * h).astype(np.int32)

        # Edges come from the framework, sourced from mediapipe itself rather than transcribed
        # here — so every visualiser draws the same skeleton and none of them invents one.
        for a, b in edges_for(pose.topology):
            if a >= len(pts) or b >= len(pts):
                continue
            if vis[a] < 0.3 or vis[b] < 0.3:
                continue
            cv2.line(canvas, (px[a], py[a]), (px[b], py[b]),
                     _SKELETON, self._thickness, cv2.LINE_AA)

        for i in range(len(pts)):
            if vis[i] < 0.3:
                continue
            cv2.circle(canvas, (px[i], py[i]), self._radius, _JOINT, -1, cv2.LINE_AA)

    def _draw_hud(self, canvas, frame, pose, w: int) -> None:
        lines = [f"frame {frame.seq}" + ("  mirrored" if frame.mirrored else "")]
        if pose is not None:
            # frame_seq tells you how stale the pose is relative to what you are looking at —
            # the pose stage runs slower than capture, so they are usually not the same frame.
            lag = frame.seq - pose.frame_seq
            lines.append(f"pose {pose.topology}  lag {lag} frame(s)")
        if self._show_fps and self._fps:
            lines.append(f"draw {self._fps:4.1f} fps")

        lid = self.latest("device.lid_angle")
        if lid is not None:
            age_ms = (time.monotonic_ns() - lid.t_ns) / 1e6
            lines.append(f"lid {lid.value:.0f}deg ({age_ms:.0f} ms old)")

        for i, text in enumerate(lines):
            cv2.putText(canvas, text, (12, 24 + i * 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _TEXT, 1, cv2.LINE_AA)

    def teardown(self) -> None:
        self.log.info("drew %d frames in %.1fs", self._drawn, time.monotonic() - self._t0)
        try:
            cv2.destroyWindow(self._window)
            cv2.waitKey(1)   # macOS needs one more pump to actually close the window
        except cv2.error:
            pass
