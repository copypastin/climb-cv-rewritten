"""Pose rendering, following the original climb-cv `utils/rendering/` implementation.

Two entry points, one per window the original had:

* `draw_pose_overlay` — the 2D skeleton on the camera image, drawn with MediaPipe's own
  `drawing_utils.draw_landmarks` and `PoseLandmarksConnections.POSE_LANDMARKS`, exactly as
  `exo_live.py` did, rather than a per-edge Python loop.
* `plot_world_landmarks` — the 3D view, grouped into seven polylines (face, both arms, both
  body sides, shoulders, waist) exactly as `plot_pose_live.py` did, rather than one
  `ax.plot` per skeleton edge.

The grouping is the point. Thirty-five separate `ax.plot` calls per redraw is what made the
3D window stutter; seven is what the original used and it is ~5x less matplotlib work.

Deliberately NOT imported by `contracts.py`, which stays at dataclasses/numpy/typing and is
re-imported in every child process. cv2, matplotlib and mediapipe are imported lazily here, so
a plugin that never draws never pays for them.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "draw_pose_overlay",
    "draw_body_tilt",
    "plot_world_landmarks",
]

VISIBILITY, X, Y, Z = 0, 1, 2, 3

L_SHOULDER, R_SHOULDER = 11, 12
L_HIP, R_HIP = 23, 24

# The original's DrawingSpecs: white points, red connections, thickness 1.
_POINT_COLOR = (255, 255, 255)
_LINE_COLOR = (0, 0, 255)
_TILT_COLOR = (0, 255, 0)

_cache: dict = {}


def _mp():
    """Lazily import MediaPipe's drawing utilities, or None if unavailable.

    Returns None rather than raising: a visualiser without MediaPipe should fall back to
    drawing points, not take the run down.
    """
    if "mp" not in _cache:
        try:
            from mediapipe.tasks.python.components.containers.landmark import (
                NormalizedLandmark,
            )
            from mediapipe.tasks.python.vision import drawing_utils as mpd
            from mediapipe.tasks.python.vision.pose_landmarker import (
                PoseLandmarksConnections,
            )

            _cache["mp"] = (
                mpd,
                NormalizedLandmark,
                PoseLandmarksConnections.POSE_LANDMARKS,
                mpd.DrawingSpec(color=_POINT_COLOR, thickness=1, circle_radius=1),
                mpd.DrawingSpec(color=_LINE_COLOR, thickness=1),
            )
        except Exception:
            _cache["mp"] = None
    return _cache["mp"]


def draw_pose_overlay(image_bgr: np.ndarray, pose) -> bool:
    """Draw the skeleton onto `image_bgr` in place. Returns False if nothing was drawn.

    `pose.image` holds normalised landmarks with origin top-left, columns
    (visibility, x, y, z) — which is the layout MediaPipe's NormalizedLandmark expects once
    the columns are unpacked.
    """
    if pose is None or pose.image is None:
        return False

    parts = _mp()
    pts = pose.image
    if parts is None:
        return _draw_points_fallback(image_bgr, pts)

    mpd, NormalizedLandmark, connections, point_spec, line_spec = parts
    landmarks = [
        NormalizedLandmark(
            x=float(p[X]), y=float(p[Y]), z=float(p[Z]),
            visibility=float(p[VISIBILITY]), presence=float(p[VISIBILITY]),
        )
        for p in pts
    ]
    mpd.draw_landmarks(image_bgr, landmarks, connections, point_spec, line_spec)
    return True


def _draw_points_fallback(image_bgr: np.ndarray, pts: np.ndarray) -> bool:
    """Points only, when MediaPipe's drawing utils are not importable."""
    import cv2

    h, w = image_bgr.shape[:2]
    for p in pts:
        if p[VISIBILITY] < 0.3:
            continue
        cv2.circle(image_bgr, (int(p[X] * w), int(p[Y] * h)), 2, _POINT_COLOR, -1)
    return True


def draw_body_tilt(
    image_bgr: np.ndarray,
    pose,
    lid_angle_deg: float | None = None,
    origin: tuple[int, int] = (10, 90),
) -> float | None:
    """Draw the torso vector and its angle, the way the original did.

    Without a lid angle the reference is straight up. With one, the reference is rotated about
    the x axis by (lid - 90) degrees, which is what makes the number mean "lean relative to the
    wall" rather than "lean relative to a camera that is itself tilted".

    Returns the angle in degrees, or None when the torso is not visible enough to be meaningful.
    """
    import cv2

    if pose is None or pose.image is None:
        return None
    pts = pose.image
    if min(float(pts[i, VISIBILITY]) for i in (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)) < 0.3:
        return None

    def xyz(i: int) -> np.ndarray:
        return np.array([pts[i, X], pts[i, Y], pts[i, Z]], dtype=np.float64)

    mid_shoulder = (xyz(L_SHOULDER) + xyz(R_SHOULDER)) / 2.0
    mid_hip = (xyz(L_HIP) + xyz(R_HIP)) / 2.0

    theta = np.radians((lid_angle_deg - 90.0) if lid_angle_deg is not None else 0.0)
    # MediaPipe's y points DOWN, so "up" is -1. Rotate that reference about x by the lid angle.
    reference = np.array([0.0, -np.cos(theta), -np.sin(theta)])

    body = mid_shoulder - mid_hip
    mag, mag_ref = np.linalg.norm(body), np.linalg.norm(reference)
    if mag == 0 or mag_ref == 0:
        angle = 0.0
    else:
        angle = float(
            np.degrees(np.arccos(np.clip(np.dot(body, reference) / (mag * mag_ref), -1.0, 1.0)))
        )

    h, w = image_bgr.shape[:2]
    cv2.line(
        image_bgr,
        (int(mid_shoulder[0] * w), int(mid_shoulder[1] * h)),
        (int(mid_hip[0] * w), int(mid_hip[1] * h)),
        _TILT_COLOR, 2,
    )
    cv2.putText(image_bgr, f"Body Tilt: {int(angle)} deg", origin,
                cv2.FONT_HERSHEY_SIMPLEX, 1, _TILT_COLOR, 2)
    return angle


# The original's seven groups. Face is scattered; the rest are drawn as polylines, which is
# what keeps a redraw to seven matplotlib calls instead of one per skeleton edge.
_FACE = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
_GROUPS = (
    [11, 13, 15, 17, 19, 21],   # right arm
    [12, 14, 16, 18, 20, 22],   # left arm
    [11, 23, 25, 27, 29, 31],   # right body side
    [12, 24, 26, 28, 30, 32],   # left body side
    [11, 12],                   # shoulders
    [23, 24],                   # waist
)


def plot_world_landmarks(ax, world: np.ndarray | None, limit: float = 1.0) -> bool:
    """Redraw `ax` with the 3D skeleton. Returns False if there was nothing to draw.

    Plotted as (x, z, -y): matplotlib's third axis is up on screen, and +y is DOWN in the pose
    convention, so negating y puts the head at the top. Same mapping the original used.
    """
    if world is None or world.ndim != 2 or world.shape[0] < 33:
        return False

    xs, ys, zs = world[:, X], world[:, Y], world[:, Z]

    ax.cla()
    ax.set_xlim3d(-limit, limit)
    ax.set_ylim3d(-limit, limit)
    ax.set_zlim3d(-limit, limit)

    ax.scatter([xs[i] for i in _FACE], [zs[i] for i in _FACE], [-ys[i] for i in _FACE])
    for group in _GROUPS:
        ax.plot([xs[i] for i in group], [zs[i] for i in group], [-ys[i] for i in group],
                alpha=0.5)
    return True
