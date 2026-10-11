"""Regression coverage for lost tracking, frame pairing, and reacquisition."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from climbcv.contracts import Frame, PoseFrame
from climbcv import rendering


def pose(t=1_000_000_000, seq=1, visibility=1.0, position=0.2, mirrored=False):
    points = np.full((33, 4), position, dtype=np.float32)
    points[:, 0] = visibility
    return PoseFrame(seq, t, "mediapipe.pose.33", mirrored, points.copy(), points, False)


def frame(t=1_000_000_000, seq=1, mirrored=False):
    return Frame(seq, t, np.zeros((48, 64, 3), np.uint8), "bgr", mirrored, "test")


def load_plugin(path):
    spec = importlib.util.spec_from_file_location("display_regression_" + path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expiry_uses_capture_time_including_boundary():
    p = pose()
    assert rendering.is_pose_fresh(p, now_ns=p.t_capture_ns + 250_000_000)
    assert not rendering.is_pose_fresh(p, now_ns=p.t_capture_ns + 250_000_001)
    assert not rendering.is_pose_fresh(p, now_ns=p.t_capture_ns - 1)
    assert not rendering.is_pose_fresh(None)


@pytest.mark.parametrize("cached", [None, frame(t=999), frame(mirrored=True)])
def test_overlay_rejects_missing_or_mismatched_source_frame(cached):
    p, latest = pose(), frame(seq=2)
    frames = {} if cached is None else {1: cached}
    selected, overlay = rendering.match_pose_frame(latest, frames, p, now_ns=p.t_capture_ns)
    assert selected is latest
    assert overlay is None


def test_overlay_returns_to_live_video_when_pose_expires():
    p, source, latest = pose(), frame(), frame(seq=2)
    selected, overlay = rendering.match_pose_frame(latest, {1: source}, p, now_ns=p.t_capture_ns)
    assert selected is source and overlay is p
    selected, overlay = rendering.match_pose_frame(
        latest, {1: source}, p, now_ns=p.t_capture_ns + 251_000_000)
    assert selected is latest and overlay is None


def axes():
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure()
    FigureCanvasAgg(fig)
    return fig, fig.add_subplot(projection="3d")


def test_occlusion_breaks_limbs_and_clear_reuses_artists():
    fig, ax = axes()
    ax.view_init(elev=12, azim=34)
    points = pose().world.copy()
    points[[0, 13], 0] = 0.1
    original = points.copy()
    assert rendering.plot_world_landmarks(ax, points)
    artists = ax._climbcv_artists
    assert np.isnan(artists["lines"][0].get_data_3d()[0][1])
    assert len(artists["scatter"]._offsets3d[0]) == 10
    np.testing.assert_array_equal(points, original)
    fig.canvas.draw()
    assert not rendering.plot_world_landmarks(ax, None)
    assert all(len(line.get_data_3d()[0]) == 0 for line in artists["lines"])
    assert len(artists["scatter"]._offsets3d[0]) == 0
    fig.canvas.draw()
    assert rendering.plot_world_landmarks(ax, pose().world)
    assert ax._climbcv_artists is artists
    assert len(ax.lines) == 6 and len(ax.collections) == 1
    assert (ax.elev, ax.azim) == (12, 34)
    fig.canvas.draw()


def test_invisible_pose_draws_nothing():
    fig, ax = axes()
    p = pose(visibility=0.4)
    assert not rendering.plot_world_landmarks(ax, p.world)
    canvas = np.zeros((48, 64, 3), np.uint8)
    assert not rendering.draw_pose_overlay(canvas, p)
    assert rendering.draw_body_tilt(canvas, p) is None
    assert not canvas.any()
    fig.canvas.draw()


def test_plot_expires_without_new_messages_and_recovers(monkeypatch):
    module = load_plugin(ROOT / "examples/plugins/pose_plot/plugin.py")
    plugin = module.PosePlot()
    plugin._fig, plugin._ax = axes()
    plugin._fig.canvas.draw_idle = Mock()
    plugin._plt = SimpleNamespace(pause=Mock())
    plugin._limit, plugin._min_visibility, plugin._max_pose_age_ms = 1, 0.5, 250
    plugin._last_drawn_pose, plugin._drawn = None, 0
    p = pose()
    monkeypatch.setattr(rendering.time, "monotonic_ns", lambda: p.t_capture_ns)
    plugin.on_pose(p, None)
    plugin.redraw()
    assert plugin._drawn == 1
    monkeypatch.setattr(rendering.time, "monotonic_ns", lambda: p.t_capture_ns + 251_000_000)
    plugin.redraw()
    assert plugin._ax.get_title() == "no recent pose"
    assert all(len(line.get_data_3d()[0]) == 0 for line in plugin._ax.lines)
    plugin.redraw()
    assert plugin._fig.canvas.draw_idle.call_count == 2
    plugin.on_pose(pose(t=p.t_capture_ns + 251_000_000), None)
    plugin.redraw()
    assert plugin._drawn == 2
    assert plugin._plt.pause.call_count == 4


@pytest.mark.parametrize("next_time,mirrored", [(1_300_000_000, False), (1, False), (1_020_000_000, True)])
def test_smoothing_resets_on_tracking_gap_restart_or_mirror(next_time, mirrored):
    module = load_plugin(ROOT / "src/plugins/core.smooth_oneeuro/plugin.py")
    plugin = module.SmoothOneEuro()
    plugin._rt = SimpleNamespace(config={}, publish=Mock())
    plugin.setup()
    plugin.on_pose(pose(), None)
    p = pose(t=next_time, position=0.8, mirrored=mirrored)
    plugin.on_pose(p, None)
    output = plugin._rt.publish.call_args.args[1]
    np.testing.assert_array_equal(output.world, p.world)
    np.testing.assert_array_equal(output.image, p.image)


def test_smoothing_does_not_delay_visibility_loss():
    module = load_plugin(ROOT / "src/plugins/core.smooth_oneeuro/plugin.py")
    plugin = module.SmoothOneEuro()
    plugin._rt = SimpleNamespace(config={}, publish=Mock())
    plugin.setup()
    plugin.on_pose(pose(), None)
    plugin.on_pose(pose(t=1_020_000_000, visibility=0.1, position=0.8), None)
    output = plugin._rt.publish.call_args.args[1]
    np.testing.assert_allclose(output.world[:, 0], 0.1)
    np.testing.assert_allclose(output.image[:, 0], 0.1)
    assert np.all((output.world[:, 1:] > 0.2) & (output.world[:, 1:] < 0.8))


def test_overlay_redraws_same_video_frame_to_remove_expired_pose(monkeypatch):
    module = load_plugin(ROOT / "src/plugins/core.exo_live/plugin.py")
    plugin = module.ExoLive()
    p, f = pose(), frame()
    plugin._rt = SimpleNamespace(latest=lambda topic: p if topic == "pose.raw" else None)
    plugin._max_pose_age_ms, plugin._scale = 250, 1
    plugin._window, plugin._show_fps = "test", False
    plugin._drawn, plugin._fps, plugin._last_draw = 0, 0, 0
    plugin.on_frame(f, None)
    overlay = Mock(return_value=True)
    monkeypatch.setattr(module, "draw_pose_overlay", overlay)
    monkeypatch.setattr(module, "draw_body_tilt", Mock())
    monkeypatch.setattr(module.cv2, "imshow", Mock())
    monkeypatch.setattr(module.cv2, "waitKey", lambda _: -1)
    monkeypatch.setattr(module.cv2, "getWindowProperty", lambda *args: 1)
    monkeypatch.setattr(rendering.time, "monotonic_ns", lambda: p.t_capture_ns)
    plugin.draw()
    assert overlay.call_args.args[1] is p
    monkeypatch.setattr(rendering.time, "monotonic_ns", lambda: p.t_capture_ns + 251_000_000)
    plugin.draw()
    assert overlay.call_args.args[1] is None
    assert plugin._drawn == 2
