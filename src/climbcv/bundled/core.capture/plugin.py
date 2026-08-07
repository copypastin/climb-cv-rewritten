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
        self._fps_cap = float(self.config.get("fps", 0) or 0)
        self._seq = 0
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

        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.log.info("capturing %s at %dx%d", self._label, w, h)

    @every(0)
    def pump(self) -> None:
        """A source: run flat out, and check `stopping` between reads.

        The loop is inside the handler rather than relying on repeated calls because a device
        read blocks — and `self.stopping` is a property over the framework's shutdown event
        precisely so this loop can see a shutdown that began while it was blocked.
        """
        next_at = 0.0
        while not self.stopping:
            if self._fps_cap:
                now = time.monotonic()
                if now < next_at:
                    time.sleep(min(next_at - now, 0.005))
                    continue
                next_at = now + 1.0 / self._fps_cap

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

            self._seq += 1
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
