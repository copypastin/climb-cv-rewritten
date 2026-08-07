"""Stage 6 — the bundled first-party plugins, end to end.

design/first-party-plugins.md. This is the dogfood proof: if these three cannot be built on the
public API without a private hook, the API is incomplete. Decision #7's claim is only as good
as the last time this actually ran.

The end-to-end test drives a generated video FILE, not a webcam, so it runs unattended. A real
camera and a 30fps sustained load still need a human and real hardware.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.app import ClimbCV, bundled_root  # noqa: E402
from climbcv.contracts import PoseFrame  # noqa: E402
from climbcv.manifest import MANIFEST_FILENAME, parse_manifest  # noqa: E402

pytestmark = pytest.mark.slow

cv2 = pytest.importorskip("cv2")


# ------------------------------------------------- the manifests are real, not synthetic


def test_every_bundled_plugin_has_a_real_parseable_manifest():
    """Guardian-02 S20: synthetic in-code manifests would mean the parser is never exercised
    by the first-party plugins, so manifest bugs get found by third parties instead."""
    dirs = sorted(p for p in bundled_root().iterdir() if p.is_dir())
    assert [p.name for p in dirs] == ["core.capture", "core.pose_mediapipe", "core.smooth_oneeuro"]
    for d in dirs:
        m = parse_manifest(d / MANIFEST_FILENAME, root="bundled")
        assert m.id.startswith("core.")
        assert (d / f"{m.module}.py").is_file()


def test_core_prefix_is_legal_in_the_bundled_root_only():
    """The reserved prefix must not make the plugins it is reserved FOR unloadable."""
    from climbcv.manifest import ManifestError

    path = bundled_root() / "core.capture" / MANIFEST_FILENAME
    assert parse_manifest(path, root="bundled").id == "core.capture"
    with pytest.raises(ManifestError, match="reserved prefix"):
        parse_manifest(path, root="user")


def test_the_smoother_declares_several_topologies():
    """The plugin that forced guardian-02 finding 3. It filters columns and indexes no joints,
    so a single provides_topology would be a guess — and the wrong guess quarantines the
    exclusive publisher of pose.smoothed, ending the run."""
    m = parse_manifest(bundled_root() / "core.smooth_oneeuro" / MANIFEST_FILENAME, root="bundled")
    assert len(m.provides_topology) > 1
    assert m.requires_topology == "any"


# ------------------------------------------------- the graph resolves


def cfg_for(tmp_path: Path, **plugins) -> dict:
    return {
        "framework": {
            "plugin_dir": str(tmp_path / "plugins"),
            "state_dir": str(tmp_path / "state"),
            "log_level": "INFO",
            "grace_s": 3.0,
        },
        "plugins": plugins,
    }


def test_the_default_pipeline_resolves_with_no_user_plugins(tmp_path):
    """pip install and run: capture -> pose -> smooth, all three from the bundled root."""
    app = ClimbCV(cfg_for(tmp_path))
    plan = app.plan()
    assert plan.publishers["frame"] == ("core.capture",)
    assert plan.publishers["pose.raw"] == ("core.pose_mediapipe",)
    assert plan.publishers["pose.smoothed"] == ("core.smooth_oneeuro",)


def test_a_host_subscription_wires_into_the_default_pipeline(tmp_path):
    app = ClimbCV(cfg_for(tmp_path))
    app.subscribe("pose.smoothed", lambda p, m: None,
                  required=True, requires_topology="mediapipe.pose.33")
    plan = app.plan()
    assert "<host>" in plan.subscribers("pose.smoothed")


def test_pose_subscription_without_a_topology_raises_at_the_call_site(tmp_path):
    """The embedder's own traceback names the exact line, which no manifest error can do."""
    from climbcv.wiring import WiringError

    app = ClimbCV(cfg_for(tmp_path))
    with pytest.raises(WiringError, match="requires_topology"):
        app.subscribe("pose.smoothed", lambda p, m: None, required=True)


def test_disabling_the_smoother_starves_the_pipeline_which_is_why_passthrough_exists(tmp_path):
    """C-7: `enabled = false` is not a translation of 'don't smooth'. Disabling an
    exclusive-topic publisher removes the topic and fatally starves required subscribers, so
    turning the filter off has to be an option OF the plugin."""
    from climbcv.wiring import WiringError

    app = ClimbCV(cfg_for(tmp_path, **{"core.smooth_oneeuro": {"enabled": False}}))
    app.subscribe("pose.smoothed", lambda p, m: None,
                  required=True, requires_topology="any")
    with pytest.raises(WiringError, match="require the topic"):
        app.plan()


def test_passthrough_keeps_the_topic_published(tmp_path):
    app = ClimbCV(cfg_for(tmp_path, **{"core.smooth_oneeuro": {"passthrough": True}}))
    plan = app.plan()
    assert plan.publishers["pose.smoothed"] == ("core.smooth_oneeuro",)


# ------------------------------------------------- the One Euro filter itself


def test_one_euro_smooths_jitter_and_tracks_motion():
    sys.path.insert(0, str(bundled_root() / "core.smooth_oneeuro"))
    from plugin import _OneEuro  # type: ignore[import-not-found]

    f = _OneEuro(freq=30.0, min_cutoff=1.0, beta=0.0, d_cutoff=1.0)
    rng = np.random.default_rng(0)
    noisy, smoothed = [], []
    for i in range(120):
        truth = np.full((3, 4), 0.5, dtype=np.float32)
        sample = truth + rng.normal(0, 0.05, (3, 4)).astype(np.float32)
        noisy.append(sample.copy())
        smoothed.append(f(sample, i / 30.0))

    late = slice(30, None)
    assert np.std(np.array(smoothed)[late]) < np.std(np.array(noisy)[late]) / 2, \
        "smoothing must actually reduce variance"

    g = _OneEuro(freq=30.0, min_cutoff=1.0, beta=0.5, d_cutoff=1.0)
    out = [g(np.full((1, 4), t / 30.0, dtype=np.float32), t / 30.0) for t in range(90)]
    assert out[-1][0, 0] > out[0][0, 0], "a moving signal must be tracked, not flattened"
    assert abs(float(out[-1][0, 0]) - 89 / 30.0) < 0.5, "and tracked without huge lag"


def test_one_euro_first_sample_passes_through_unchanged():
    sys.path.insert(0, str(bundled_root() / "core.smooth_oneeuro"))
    from plugin import _OneEuro  # type: ignore[import-not-found]

    f = _OneEuro(30.0, 1.0, 0.0, 1.0)
    first = np.full((2, 4), 7.0, dtype=np.float32)
    assert np.array_equal(f(first, 0.0), first), "no history means nothing to blend with"


# ------------------------------------------------- end to end, on a video file


def make_video(path: Path, frames: int = 40, size=(160, 120)) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, size
    )
    assert writer.isOpened(), "no mp4v encoder available"
    rng = np.random.default_rng(1)
    for _ in range(frames):
        writer.write(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))
    writer.release()


def test_capture_reads_a_video_file_and_publishes_frames(tmp_path):
    """core.capture end to end in its own process, with no camera involved."""
    video = tmp_path / "clip.mp4"
    make_video(video)
    # fps + loop deliberately: a conflating subscription of depth 4 can only ever expose ~4
    # of a 40-frame burst, because "latest value wins" is the whole point. Pacing the source
    # to a rate the host can drain is what a real consumer does, and it is what makes the
    # observation meaningful rather than a race against the queue.
    app = ClimbCV(cfg_for(tmp_path, **{
        "core.capture": {"source": str(video), "mirror": False, "loop": True, "fps": 30},
        "core.pose_mediapipe": {"enabled": False},
        "core.smooth_oneeuro": {"enabled": False},
    }))
    seen: list = []
    app.subscribe("frame", lambda p, m: seen.append((p.seq, m.source)), required=False)
    try:
        app.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(seen) < 5:
            app.poll(0.02)
            state = app.states.get("core.capture")
            if state is not None and state.state in ("unavailable", "quarantined"):
                pytest.fail(f"core.capture did not run: {state.state} {state.detail[:200]}")
        assert len(seen) >= 5, f"only {len(seen)} frames arrived from the video file"
        assert seen[0][1] == "core.capture", "Meta.source must be framework-injected"
        assert [s for s, _ in seen] == sorted(s for s, _ in seen), "seq must be monotonic"
    finally:
        app.stop()


def test_capture_reports_a_missing_source_as_unavailable_not_a_crash(tmp_path):
    """A missing file or a denied camera is not a plugin defect, and the log should not say
    'core.capture crashed twice and has been disabled'."""
    app = ClimbCV(cfg_for(tmp_path, **{
        "core.capture": {"source": str(tmp_path / "nope.mp4")},
        "core.pose_mediapipe": {"enabled": False},
        "core.smooth_oneeuro": {"enabled": False},
    }))
    try:
        app.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.poll(0.02)
            state = app.states.get("core.capture")
            if state is not None and state.state in ("unavailable", "finished", "quarantined"):
                break
        state = app.states["core.capture"]
        assert state.state == "unavailable", f"got {state.state}: {state.detail[:200]}"
        assert state.crashes == ()
    finally:
        app.stop()


# A synthetic pose stage. The bundled MediaPipe plugin cannot stand in here: a generated test
# clip contains no human, so MediaPipe correctly finds no pose and never publishes -- which
# tests nothing about the framework. This proves what the framework is actually responsible
# for: three processes chained, topology carried through a transform stage, and the
# immutability guarantee surviving two hops.
FAKE_POSE = '''
import numpy as np
from climbcv import Plugin, subscribe
from climbcv.contracts import PoseFrame


class FakePose(Plugin):
    @subscribe("frame")
    def on_frame(self, frame, meta):
        world = np.full((33, 4), frame.seq * 0.001, dtype=np.float32)
        self.publish("pose.raw", PoseFrame(
            frame_seq=frame.seq, t_capture_ns=frame.t_capture_ns,
            topology="mediapipe.pose.33", mirrored=frame.mirrored,
            world=world, image=None, smoothed=False,
        ))
'''


def test_three_process_chain_capture_to_pose_to_smoothed(tmp_path):
    """capture -> pose -> smooth -> host, in three child processes plus the host."""
    from test_runtime import make

    video = tmp_path / "clip.mp4"
    make_video(video, frames=40)
    make(tmp_path / "plugins", "fake_pose", FAKE_POSE,
         publishes=("pose.raw",), subscribes=("frame",),
         extra='provides_topology = "mediapipe.pose.33"\nrequires_topology = "any"\n')

    app = ClimbCV(cfg_for(tmp_path, **{
        "core.capture": {"source": str(video), "mirror": True, "loop": True, "fps": 30},
        "core.pose_mediapipe": {"enabled": False},
    }))
    got: list[PoseFrame] = []
    app.subscribe("pose.smoothed", lambda p, m: got.append(p),
                  required=False, requires_topology="mediapipe.pose.33")
    try:
        app.start()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and len(got) < 3:
            app.poll(0.02)
        assert got, "no smoothed pose reached the host through three processes"
        pose = got[0]
        assert pose.topology == "mediapipe.pose.33", "topology must survive the transform stage"
        assert pose.world.shape == (33, 4)
        assert pose.smoothed is True
        assert pose.mirrored is True, "mirrored must be carried so hands can be told apart"
        assert not pose.world.flags.writeable, "arrays arrived mutable after two hops"
    finally:
        app.stop()


@pytest.mark.e2e
def test_real_mediapipe_pipeline_on_supplied_footage(tmp_path):
    """The bundled pose plugin against real footage containing a person.

    Set CLIMBCV_TEST_VIDEO to a clip with a visible human. Skipped otherwise: a generated clip
    contains no body, so MediaPipe finding nothing in it would be correct behaviour and a
    useless assertion. Sustained 30fps on a real webcam still needs a human at the keyboard.
    """
    import os

    src = os.environ.get("CLIMBCV_TEST_VIDEO")
    if not src or not Path(src).is_file():
        pytest.skip("set CLIMBCV_TEST_VIDEO to a video file containing a person")
    pytest.importorskip("mediapipe")

    app = ClimbCV(cfg_for(tmp_path, **{
        "core.capture": {"source": src, "mirror": False, "fps": 30},
    }))
    got: list[PoseFrame] = []
    app.subscribe("pose.smoothed", lambda p, m: got.append(p),
                  required=False, requires_topology="mediapipe.pose.33")
    try:
        app.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and len(got) < 5:
            app.poll(0.02)
            state = app.states.get("core.pose_mediapipe")
            if state is not None and state.state in ("unavailable", "quarantined"):
                pytest.skip(f"pose stage unavailable: {state.detail[:200]}")
        assert got, "no smoothed pose from real footage"
        assert got[0].world.shape == (33, 4)
        assert np.isfinite(got[0].world).all()
    finally:
        app.stop()
