"""core.capture — frames from a webcam or a video file.

The reference source plugin. Two idioms here are worth copying:

* Everything expensive is created in `setup()`, in this process. The camera handle is never
  pickled and never crosses a boundary.
* `@every(0)` plus `while not self.stopping` is how you write a source. `stopping` flips
  *while* your handler is running, so a blocking device read still notices shutdown.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from climbcv import Plugin, every
from climbcv.contracts import Frame


class Capture(Plugin):
    def setup(self) -> None:
        source = self.config.get("source")
        device = self.config.get("device", 0)
        self._loop = bool(self.config.get("loop", False))
        self._mirror = bool(self.config.get("mirror", True))
        self._is_file = bool(source)
        self._seq = 0
        # The fps cap PACES a file, which reads instantly. It must NOT pace a camera: a camera
        # already blocks in read() until its next frame, so sleeping first and then blocking
        # again halves the rate -- a 30fps device gated at 30fps delivers about 15. For a
        # camera we ask the DEVICE for the rate instead and let it do the pacing.
        # A rate for BOTH kinds of source. For a file it paces playback; for a camera it
        # stops the loop spinning on duplicate frames when the device cannot keep up with the
        # rate being asked of it. Defaults to the device's own reported rate, set in setup().
        self._fps_cap = float(self.config.get("fps", 0) or 0)
        self._label = str(source) if source else f"camera:{device}"

        target = str(source) if source else int(device)
        # Per-OS backend, as the pre-plugin version did: AVFoundation on macOS, V4L2 on
        # Linux, DSHOW on Windows. Passing CAP_ANY for a file lets OpenCV pick the demuxer.
        if source:
            self._cap = cv2.VideoCapture(target)
        else:
            import sys

            backend = {
                "darwin": cv2.CAP_AVFOUNDATION,
                "linux": cv2.CAP_V4L2,
                "win32": cv2.CAP_DSHOW,
            }.get(sys.platform, cv2.CAP_ANY)
            self._cap = cv2.VideoCapture(target, backend)

        if not self._cap.isOpened():
            # Not a crash: the machine may simply have no camera, or the user may have denied
            # camera permission. "unavailable" says that, where a traceback would say
            # "core.capture crashed twice and has been disabled".
            self.unavailable(
                f"cannot open {self._label}. If this is a webcam, check that no other "
                "application is using it and that camera permission is granted."
            )

        for key, prop in (("width", cv2.CAP_PROP_FRAME_WIDTH),
                          ("height", cv2.CAP_PROP_FRAME_HEIGHT)):
            if key in self.config:
                self._cap.set(prop, float(self.config[key]))

        if not self._is_file:
            requested = float(self.config.get("fps", 0) or 0)
            if requested:
                self._cap.set(cv2.CAP_PROP_FPS, requested)
            # Keep the driver's queue at one frame. Otherwise read() hands back whatever was
            # buffered while a downstream stage was busy, so the overlay shows the past: the
            # latency grows with the buffer and never recovers.
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Deliberately NOT capping at the device's reported rate. Cameras under-report:
            # this one says 15 fps and delivers about 30, so trusting the report halves the
            # frame rate. `fps` remains available as an explicit cap when you want one.

        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        device_fps = self._cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.log.info(
            "capturing %s at %dx%d%s", self._label, w, h,
            f", device reports {device_fps:.0f} fps" if device_fps else "",
        )
        self._t0 = time.monotonic()
        self._reported = 0

    @every(0)
    def pump(self) -> None:
        """A source: run flat out, and check `stopping` between reads.

        The loop is inside the handler rather than relying on repeated calls because a device
        read blocks — and `self.stopping` is a property over the framework's shutdown event
        precisely so this loop can see a shutdown that began while it was blocked.
        """
        interval = (1.0 / self._fps_cap) if self._fps_cap else 0.0
        next_at = time.monotonic()
        while not self.stopping:
            ok, frame = self._cap.read()
            if not ok:
                if self._loop and self.config.get("source"):
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                # End of a file is an orderly completion, not a failure. Note that if
                # anything REQUIRES `frame`, finishing here ends the run — with a message
                # saying so. For a video file that is the correct behaviour.
                self.finish(f"end of {self._label}")
                return

            t_capture_ns = time.monotonic_ns()
            if self._mirror:
                frame = cv2.flip(frame, 1)

            # Pace AFTER the read, never before it. A camera whose read() blocks needs no
            # pacing and gets none, because the deadline has already passed by the time we
            # arrive here. A camera that returns duplicates immediately -- which is what a
            # 15 fps device does when asked for frames faster than it has them -- would
            # otherwise spin flat out, burning a core and flooding every subscriber's queue
            # with redundant frames. Sleeping BEFORE the read was the earlier bug: it added
            # its wait to the device's own, halving the rate.
            if interval:
                now = time.monotonic()
                if now < next_at:
                    remaining = next_at - now
                    while remaining > 0 and not self.stopping:
                        time.sleep(min(remaining, 0.005))
                        remaining = next_at - time.monotonic()
                    if self.stopping:
                        return
                    next_at += interval
                else:
                    # Behind schedule: reset rather than accumulating debt, or a slow patch
                    # makes the source sprint to catch up.
                    next_at = now + interval

            self._seq += 1
            if self._seq - self._reported >= 150:
                elapsed = time.monotonic() - self._t0
                if elapsed > 0:
                    self.log.info("published %d frames, %.1f fps average",
                                  self._seq, self._seq / elapsed)
                self._reported = self._seq

            self.publish("frame", Frame(
                seq=self._seq,
                t_capture_ns=t_capture_ns,
                pixels=np.ascontiguousarray(frame),
                color="bgr",
                mirrored=self._mirror,
                source=self._label,
            ))

    def teardown(self) -> None:
        cap = getattr(self, "_cap", None)
        if cap is not None:
            cap.release()
