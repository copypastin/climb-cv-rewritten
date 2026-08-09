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
from climbcv import Plugin, every, subscribe
from climbcv.contracts import Shutdown
from climbcv.rendering import draw_body_tilt, draw_pose_overlay, pair_pose_with_frame

_SKELETON = (66, 245, 158)   # BGR
_JOINT = (255, 210, 60)
_TEXT = (240, 240, 240)


class ExoLive(Plugin):
    def setup(self) -> None:
        if not bool(self.config.get("enabled", True)):
            self.unavailable("host-owned overlay is enabled in the example instead")
            return

        self._window = str(self.config.get("window_name", "climb-cv — exo skeleton"))
        self._scale = float(self.config.get("scale", 1.0))   # 1.0 = no resize per draw
        self._radius = int(self.config.get("point_radius", 3))
        self._thickness = int(self.config.get("line_width", 2))
        self._show_fps = bool(self.config.get("show_fps", True))

        from collections import OrderedDict

        self._frames: OrderedDict = OrderedDict()   # seq -> Frame, for pairing
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

        self.set_interval(self.draw, 1.0 / float(self.config.get("draw_hz", 30)))
        self.log.info("overlay window open — press ESC in it to stop the run")

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        # Stash only. Never draw here: see the module docstring. Keeping a short history (not
        # just the newest) is what lets draw() put the skeleton on the frame it belongs to.
        self._frames[frame.seq] = frame
        while len(self._frames) > 12:
            self._frames.popitem(last=False)

    @every(1 / 60)
    def draw(self) -> None:
        pose = self.latest("pose.smoothed")
        # Pair the pose with the frame it was computed from. Drawing the newest pose onto the
        # newest frame makes the skeleton trail the body by however far the pose stage is
        # behind -- which is small on a file and very visible on a live camera.
        frame = pair_pose_with_frame(self._frames, pose)
        if frame is None:
            if not self._frames:
                return
            frame = next(reversed(self._frames.values()))
            pose = None

        # as_bgr() always returns a fresh writable C-contiguous array, so drawing into it is
        # safe. frame.pixels itself is read-only and shared with nothing.
        canvas = frame.as_bgr()
        if self._scale != 1.0:
            canvas = cv2.resize(canvas, None, fx=self._scale, fy=self._scale)

        # One MediaPipe call, as the original did, rather than a Python loop over edges.
        if not draw_pose_overlay(canvas, pose):
            cv2.putText(canvas, "no pose", (12, canvas.shape[0] - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _TEXT, 1, cv2.LINE_AA)
        lid = self.latest("device.lid_angle")
        draw_body_tilt(canvas, pose, lid.value if lid is not None else None)

        self._draw_hud(canvas, frame, pose, canvas.shape[1])

        cv2.imshow(self._window, canvas)
        # waitKey IS the GUI pump. Without it the window never repaints and the OS marks the
        # app unresponsive, however healthy the pipeline is.
        if (cv2.waitKey(1) & 0xFF) == 27:
            self.log.info("ESC pressed — asking the run to stop")
            self.publish("app.shutdown", Shutdown("ESC pressed in the overlay window"))

        self._drawn += 1
        now = time.monotonic()
        dt = now - self._last_draw
        if dt > 0:
            self._fps = 0.9 * self._fps + 0.1 * (1.0 / dt) if self._fps else 1.0 / dt
        self._last_draw = now

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
