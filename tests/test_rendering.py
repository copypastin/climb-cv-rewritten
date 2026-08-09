"""Tests for climbcv.rendering — the pose/frame pairing and the two draw paths.

The pairing tests are the important ones. A skeleton drawn on the wrong frame still *looks*
like a skeleton, so this failure has no crash, no log line and no wrong number anywhere — it
just trails the body, and only a human watching a live camera notices. That makes it exactly
the kind of bug worth pinning down in a test.
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.contracts import Frame, PoseFrame  # noqa: E402
from climbcv.rendering import (  # noqa: E402
    draw_body_tilt,
    draw_pose_overlay,
    pair_pose_with_frame,
    plot_world_landmarks,
)


def frame(seq: int) -> Frame:
    return Frame(seq, seq * 1000, np.zeros((48, 64, 3), np.uint8), "bgr", False, "test")


def pose(frame_seq: int, *, upright: bool = True) -> PoseFrame:
    img = np.zeros((33, 4), np.float32)
    img[:, 0] = 1.0
    img[11, 1:] = (0.45, 0.30, 0.0)   # left shoulder
    img[12, 1:] = (0.55, 0.30, 0.0)   # right shoulder
    img[23, 1:] = (0.45, 0.60, 0.0) if upright else (0.25, 0.60, 0.0)
    img[24, 1:] = (0.55, 0.60, 0.0) if upright else (0.35, 0.60, 0.0)
    return PoseFrame(frame_seq, 0, "mediapipe.pose.33", False, img.copy(), img, True)


# ------------------------------------------------------------------ pairing


def test_pose_is_paired_with_the_frame_it_was_computed_from():
    """The whole fix. The pose stage runs behind capture, so the newest pose belongs to an
    older frame; drawing it on the newest frame is what makes the skeleton trail the body."""
    frames = OrderedDict((i, frame(i)) for i in range(10, 20))
    assert pair_pose_with_frame(frames, pose(13)).seq == 13
    assert pair_pose_with_frame(frames, pose(19)).seq == 19


def test_pairing_returns_none_when_the_frame_has_been_evicted():
    """Better to draw the frame alone than to draw a mismatched pair."""
    frames = OrderedDict((i, frame(i)) for i in range(10, 20))
    assert pair_pose_with_frame(frames, pose(3)) is None


def test_pairing_handles_no_pose_yet():
    assert pair_pose_with_frame(OrderedDict(), None) is None
    assert pair_pose_with_frame(OrderedDict({1: frame(1)}), None) is None


def test_pairing_never_returns_a_newer_frame_than_the_pose():
    """The failure this guards against: silently falling back to 'newest' inside the pairing
    helper would reintroduce exactly the lag it exists to remove."""
    frames = OrderedDict((i, frame(i)) for i in range(0, 30))
    for seq in (0, 7, 15, 29):
        assert pair_pose_with_frame(frames, pose(seq)).seq == seq


# ------------------------------------------------------------------ 2D overlay


def test_draw_pose_overlay_marks_the_image():
    canvas = np.zeros((48, 64, 3), np.uint8)
    assert draw_pose_overlay(canvas, pose(1)) is True
    assert canvas.sum() > 0, "nothing was drawn"


def test_draw_pose_overlay_is_a_noop_without_a_pose():
    canvas = np.zeros((48, 64, 3), np.uint8)
    assert draw_pose_overlay(canvas, None) is False
    assert canvas.sum() == 0


def test_draw_pose_overlay_handles_a_pose_with_no_image_landmarks():
    p = PoseFrame(1, 0, "mediapipe.pose.33", False, np.zeros((33, 4), np.float32), None, True)
    canvas = np.zeros((48, 64, 3), np.uint8)
    assert draw_pose_overlay(canvas, p) is False


# ------------------------------------------------------------------ body tilt


def test_body_tilt_is_near_zero_when_upright():
    canvas = np.zeros((480, 640, 3), np.uint8)
    angle = draw_body_tilt(canvas, pose(1, upright=True))
    assert angle is not None and angle < 5.0


def test_body_tilt_grows_when_leaning():
    canvas = np.zeros((480, 640, 3), np.uint8)
    upright = draw_body_tilt(canvas, pose(1, upright=True))
    leaning = draw_body_tilt(canvas, pose(1, upright=False))
    assert leaning > upright + 10.0


def test_lid_angle_rotates_the_reference():
    """Without the lid angle the reference is straight up; with it, the number means lean
    relative to the wall rather than relative to a camera that is itself tilted."""
    canvas = np.zeros((480, 640, 3), np.uint8)
    plain = draw_body_tilt(canvas, pose(1), None)
    tilted = draw_body_tilt(canvas, pose(1), 45.0)
    assert abs(tilted - plain) > 10.0


def test_body_tilt_returns_none_when_the_torso_is_not_visible():
    img = np.zeros((33, 4), np.float32)   # visibility all 0
    p = PoseFrame(1, 0, "mediapipe.pose.33", False, img.copy(), img, True)
    assert draw_body_tilt(np.zeros((48, 64, 3), np.uint8), p) is None


# ------------------------------------------------------------------ 3D plot


def test_plot_world_landmarks_uses_seven_calls_not_one_per_edge():
    """The reason the 3D window stuttered: 35 ax.plot calls per redraw. The original grouped
    the skeleton into a face scatter plus six polylines, and that is ~5x less matplotlib work."""

    class FakeAx:
        def __init__(self):
            self.plots = 0
            self.scatters = 0

        def cla(self): pass
        def set_xlim3d(self, *a): pass
        def set_ylim3d(self, *a): pass
        def set_zlim3d(self, *a): pass
        def plot(self, *a, **k): self.plots += 1
        def scatter(self, *a, **k): self.scatters += 1

    ax = FakeAx()
    assert plot_world_landmarks(ax, pose(1).world) is True
    assert ax.plots == 6, f"expected 6 polylines, got {ax.plots}"
    assert ax.scatters == 1, "the face should be one scatter"


def test_plot_world_landmarks_rejects_junk_without_raising():
    class FakeAx:
        def cla(self): pass
        def set_xlim3d(self, *a): pass
        def set_ylim3d(self, *a): pass
        def set_zlim3d(self, *a): pass
        def plot(self, *a, **k): pass
        def scatter(self, *a, **k): pass

    assert plot_world_landmarks(FakeAx(), None) is False
    assert plot_world_landmarks(FakeAx(), np.zeros((4, 4), np.float32)) is False


@pytest.mark.parametrize("seq", [0, 1, 999])
def test_pairing_round_trip_matches_what_the_overlay_would_draw(seq):
    frames = OrderedDict({seq: frame(seq)})
    p = pose(seq)
    matched = pair_pose_with_frame(frames, p)
    assert matched is not None and matched.seq == p.frame_seq
