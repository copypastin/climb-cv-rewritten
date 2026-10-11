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
from climbcv.rendering import draw_body_tilt, draw_pose_overlay, match_pose_frame

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
        self._max_pose_age_ms = float(self.config.get("max_pose_age_ms", 250))
        if not 0 < self._max_pose_age_ms < float("inf"):
            raise ValueError("max_pose_age_ms must be positive and finite")

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

        self.set_interval(self.draw, 1.0 / float(self.config.get("draw_hz", 30)))
        self.log.info("overlay window open — press ESC in it to stop the run")

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        # Stash in a small ring buffer so we can synchronize video to the pose
        if not hasattr(self, "_frames"):
            self._frames = {}
        self._frames[frame.seq] = frame
        if len(self._frames) > 30:
            del self._frames[min(self._frames.keys())]
        self._latest_frame = frame

    @every(1 / 60)
    def draw(self) -> None:
        if not hasattr(self, "_latest_frame"):
            # Still pump the window to keep the OS happy
            if (cv2.waitKey(1) & 0xFF) == 27:
                self.publish("app.shutdown", Shutdown("ESC pressed in the overlay window"))
            return

        frame, pose = match_pose_frame(
            self._latest_frame, self._frames, self.latest("pose.raw"),
            self._max_pose_age_ms,
        )

        lid = self.latest("device.lid_angle")

        # Only redraw if the visual inputs have actually changed
        pose_seq = pose.frame_seq if pose else -1
        lid_time = lid.t_ns if lid else -1
        draw_state = (frame.seq, frame.t_capture_ns, pose_seq, lid_time)
        if getattr(self, "_last_drawn_state", None) == draw_state:
            # Same inputs. Just pump the GUI and return.
            if (cv2.waitKey(1) & 0xFF) == 27:
                self.publish("app.shutdown", Shutdown("ESC pressed in the overlay window"))
            return

        self._last_drawn_state = draw_state

        # Fast OpenCV color conversion into a persistent buffer rather than numpy slicing
        if not hasattr(self, "_canvas") or self._canvas.shape != frame.pixels.shape:
            self._canvas = np.empty_like(frame.pixels)

        if frame.color == "rgb":
            cv2.cvtColor(frame.pixels, cv2.COLOR_RGB2BGR, dst=self._canvas)
        else:
            np.copyto(self._canvas, frame.pixels)

        canvas = self._canvas

        if self._scale != 1.0:
            # If scaling, we can't reuse the buffer for the destination easily, so let it allocate
            canvas = cv2.resize(canvas, None, fx=self._scale, fy=self._scale)

        # One MediaPipe call, as the original did, rather than a Python loop over edges.
        if not draw_pose_overlay(canvas, pose):
            cv2.putText(canvas, "no pose", (12, canvas.shape[0] - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _TEXT, 1, cv2.LINE_AA)

        draw_body_tilt(canvas, pose, lid.value if lid is not None else None)

        self._draw_hud(canvas, frame, pose, canvas.shape[1])

        cv2.imshow(self._window, canvas)
        # waitKey IS the GUI pump. Without it the window never repaints and the OS marks the
        # app unresponsive, however healthy the pipeline is.
        if (cv2.waitKey(1) & 0xFF) == 27:
            self.log.info("ESC pressed — asking the run to stop")
            self.publish("app.shutdown", Shutdown("ESC pressed in the overlay window"))
        try:
            if cv2.getWindowProperty(self._window, cv2.WND_PROP_VISIBLE) < 1:
                self.publish("app.shutdown", Shutdown("Overlay window closed by user"))
        except cv2.error:
            self.publish("app.shutdown", Shutdown("Overlay window closed by user"))

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
