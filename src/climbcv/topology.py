"""Skeleton edges per topology — for anything that draws a pose.

Deliberately NOT in `contracts.py`, which imports dataclasses/numpy/typing only and is
re-imported in every child process. This module lazily imports mediapipe when asked about a
mediapipe topology, so a plugin that never draws never pays for it.

Guardian F-5: without a shared edge list, every visualiser either hardcodes its own copy of the
skeleton or imports mediapipe purely for the connection list — throwing away an isolation win.
The edges here are read from mediapipe itself rather than transcribed, because a wrong skeleton
shipped from the framework would be inherited by every plugin that trusts it.
"""

from __future__ import annotations

from functools import lru_cache

__all__ = ["edges_for", "TORSO_ONLY"]


TORSO_ONLY: tuple[tuple[int, int], ...] = (
    (11, 12), (11, 23), (12, 24), (23, 24),   # shoulders and hips
    (11, 13), (13, 15),                       # left arm
    (12, 14), (14, 16),                       # right arm
    (23, 25), (25, 27),                       # left leg
    (24, 26), (26, 28),                       # right leg
)
"""A minimal fallback: the twelve segments that make a recognisable stick figure under
MediaPipe's 33-point indexing. Used only when mediapipe is not importable, so a visualiser
still draws something rather than nothing."""


@lru_cache(maxsize=8)
def edges_for(topology: str) -> tuple[tuple[int, int], ...]:
    """Landmark index pairs to draw as bones, or () if the topology is unknown.

    Never raises: a visualiser that cannot get edges should degrade to drawing points, not
    crash the run.
    """
    if topology == "mediapipe.pose.33":
        try:
            from mediapipe.tasks.vision import PoseLandmarksConnections as _C

            return tuple(sorted((c.start, c.end) for c in _C.POSE_LANDMARKS))
        except Exception:
            return TORSO_ONLY
    if topology == "coco.17":
        # COCO's 17-point ordering is a different convention entirely; nothing in this project
        # publishes it yet, so returning () is more honest than guessing at index pairs.
        return ()
    return ()
