"""A plugin that consumes pose data — and has to care which topology it is indexing.

The interesting part of this example is `requires_topology` in the manifest. This file indexes
landmarks 11, 12, 23 and 24 by number, and those numbers only mean "shoulders and hips" under
MediaPipe's 33-point topology. Under COCO's 17-point topology, index 11 is a hip. Declaring
the topology you were written against means swapping in a different pose plugin produces a
startup error naming both sides, instead of a plausible-looking wrong skeleton forever.

Note also `mirrored`. Left/right body semantics depend on whether the source image was
flipped, and that flag travels on the pose payload precisely so a pose-only subscriber does not
have to also subscribe to `frame` to find out.
"""

from __future__ import annotations

import math
import time

from climbcv import Plugin, subscribe
from climbcv.contracts import Scalar

# MediaPipe 33-point topology. Safe to index by number *because* the manifest declares the
# topology this file was written against.
L_SHOULDER, R_SHOULDER = 11, 12
L_HIP, R_HIP = 23, 24

VISIBILITY, X, Y, Z = 0, 1, 2, 3


class BodyTilt(Plugin):
    def setup(self) -> None:
        self._min_visibility = float(self.config.get("min_visibility", 0.5))
        self._published = 0

    @subscribe("pose.smoothed")
    def on_pose(self, pose, meta) -> None:
        w = pose.world  # (33, 4) float32, columns (visibility, x, y, z), metres, READ-ONLY

        joints = (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)
        if min(float(w[j, VISIBILITY]) for j in joints) < self._min_visibility:
            return  # a partly-occluded torso gives a meaningless angle; publish nothing

        shoulder_mid_x = (float(w[L_SHOULDER, X]) + float(w[R_SHOULDER, X])) / 2
        shoulder_mid_y = (float(w[L_SHOULDER, Y]) + float(w[R_SHOULDER, Y])) / 2
        hip_mid_x = (float(w[L_HIP, X]) + float(w[R_HIP, X])) / 2
        hip_mid_y = (float(w[L_HIP, Y]) + float(w[R_HIP, Y])) / 2

        # +y is DOWN in this coordinate system, so the torso vector points from hips upward.
        dx = shoulder_mid_x - hip_mid_x
        dy = shoulder_mid_y - hip_mid_y
        if dx == 0.0 and dy == 0.0:
            return

        # Angle from vertical. Sign is relative to *image* direction, so mirroring flips it —
        # which is why the payload carries `mirrored` rather than leaving it to be looked up.
        tilt = math.degrees(math.atan2(dx, -dy))
        if pose.mirrored:
            tilt = -tilt

        self.publish("example.body_tilt", Scalar(value=tilt, t_ns=time.monotonic_ns()))
        self._published += 1

    def teardown(self) -> None:
        self.log.info("published %d tilt readings", self._published)
