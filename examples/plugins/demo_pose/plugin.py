"""A synthetic pose source — a stand-in for a real pose model.

Why this exists: a generated test clip contains no person, so MediaPipe correctly finds no
skeleton in it and publishes nothing. That is right behaviour and useless for demonstrating a
visualiser. This plugin publishes an animated fake skeleton in MediaPipe's 33-point layout so
`exo_live` and `pose_plot` have something to draw immediately.

It also demonstrates a **stage replacement**. It publishes `pose.raw`, exactly like
`core.pose_mediapipe`, and nothing downstream knows or cares which one is running — swapping a
core stage is the same mechanism as adding a plugin, which is the whole premise of the
architecture. Because `pose.raw` is exclusive, running both at once is a startup error rather
than a silent coin flip.
"""

from __future__ import annotations

import math

import numpy as np

from climbcv import Plugin, subscribe
from climbcv.contracts import PoseFrame

# MediaPipe 33-point indices we place deliberately; the rest are filled in around them.
NOSE = 0
L_SHOULDER, R_SHOULDER = 11, 12
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28

_N = 33


class DemoPose(Plugin):
    def setup(self) -> None:
        self._cycle = float(self.config.get("cycle_frames", 45))
        self._amp = float(self.config.get("amplitude", 0.12))
        self.log.info(
            "publishing a synthetic skeleton — this is a stand-in for a real pose model, "
            "so the overlay and 3D plot have something to draw"
        )

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        phase = 2 * math.pi * (frame.seq % self._cycle) / self._cycle
        swing = self._amp * math.sin(phase)
        lift = self._amp * 0.6 * math.cos(phase)

        # Image coordinates: normalised to the frame, origin TOP-LEFT, y measured DOWNWARD.
        img = np.zeros((_N, 4), dtype=np.float32)
        img[:, 0] = 1.0            # visibility

        def place(idx: int, x: float, y: float, z: float = 0.0) -> None:
            img[idx, 1:] = (x, y, z)

        # Lean the upper body from side to side, so a torso-angle consumer sees a signal that
        # actually moves instead of a symmetric figure reading a flat zero.
        lean = 0.10 * math.sin(phase / 2)
        place(NOSE, 0.5 + lean * 1.6, 0.20)
        place(L_SHOULDER, 0.42 + lean, 0.32)
        place(R_SHOULDER, 0.58 + lean, 0.32)
        place(L_ELBOW, 0.36 + lean, 0.44 + swing * 0.4)
        place(R_ELBOW, 0.64 + lean, 0.44 - swing * 0.4)
        place(L_WRIST, 0.32 + lean, 0.56 + swing)
        place(R_WRIST, 0.68 + lean, 0.56 - swing)
        place(L_HIP, 0.45, 0.58)
        place(R_HIP, 0.55, 0.58)
        place(L_KNEE, 0.44, 0.74 - abs(lift))
        place(R_KNEE, 0.56, 0.74 - abs(lift) * 0.3)
        place(L_ANKLE, 0.43, 0.90 - swing * 0.3)
        place(R_ANKLE, 0.57, 0.90 + swing * 0.3)

        # Face and hand/foot detail points: cluster them near their anchors so the drawn
        # skeleton looks complete rather than having floating stray joints at the origin.
        for i in range(1, 11):
            img[i, 1:] = img[NOSE, 1:] + np.float32(
                [0.02 * math.cos(i), 0.015 * math.sin(i), 0.0]
            )
        for i, anchor in ((17, L_WRIST), (18, R_WRIST), (19, L_WRIST),
                          (20, R_WRIST), (21, L_WRIST), (22, R_WRIST)):
            img[i, 1:] = img[anchor, 1:]
        for i, anchor in ((29, L_ANKLE), (30, R_ANKLE), (31, L_ANKLE), (32, R_ANKLE)):
            img[i, 1:] = img[anchor, 1:]

        # World coordinates: METRES, origin at the hip midpoint, +x image-right, +y DOWN.
        # Derive them from the image points so the two views agree, which is what a real pose
        # model gives you and what a subscriber will assume.
        hip_x = (img[L_HIP, 1] + img[R_HIP, 1]) / 2
        hip_y = (img[L_HIP, 2] + img[R_HIP, 2]) / 2
        world = np.zeros((_N, 4), dtype=np.float32)
        world[:, 0] = img[:, 0]
        world[:, 1] = (img[:, 1] - hip_x) * 1.6            # x, metres
        world[:, 2] = (img[:, 2] - hip_y) * 1.8            # y, metres, still +DOWN
        world[:, 3] = np.float32(swing * 0.5)              # a little depth movement

        self.publish("pose.raw", PoseFrame(
            frame_seq=frame.seq,
            t_capture_ns=frame.t_capture_ns,
            topology="mediapipe.pose.33",
            mirrored=frame.mirrored,   # carried through, so consumers can tell hands apart
            world=world,
            image=img,
            smoothed=False,
        ))
