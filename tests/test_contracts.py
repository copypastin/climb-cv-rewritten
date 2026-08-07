"""Acceptance tests for climbcv.contracts — design/payloads.md §2.2.1 and §3.

The immutability assertions here are the ACCEPTANCE CRITERION for §2.2.1, not ordinary
coverage. That mechanism has been specified wrongly twice, and both wrong versions read as
correct on the page; only execution caught them. The pickle-round-trip cases are the ones
that matter — a fix that protects only the publisher passes every in-process check.

Runnable standalone (`python3 tests/test_contracts.py`) as well as under pytest.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.contracts import (  # noqa: E402
    CONTRACT_TYPES,
    STATUS_STATES,
    TOPOLOGY_SIZES,
    Frame,
    HoldBoxes,
    Meta,
    PoseFrame,
    Record,
    Scalar,
    Shutdown,
    Status,
    _declared_array_fields_are_complete,
)


# --------------------------------------------------------------------------- samples


def sample(cls):
    """A valid instance of every contract type."""
    if cls is Frame:
        return Frame(1, 2, np.zeros((4, 4, 3), np.uint8), "bgr", True, "camera:0")
    if cls is PoseFrame:
        return PoseFrame(
            1, 2, "mediapipe.pose.33", True,
            np.zeros((33, 4), np.float32), np.zeros((33, 4), np.float32), True,
        )
    if cls is HoldBoxes:
        return HoldBoxes(
            1, np.array([[0.1, 0.1, 0.2, 0.2]], np.float32),
            ("hold",), np.array([0.9], np.float32),
        )
    if cls is Scalar:
        return Scalar(90.0, 123)
    if cls is Shutdown:
        return Shutdown("ESC pressed")
    if cls is Status:
        return Status("core.capture", "ready", "")
    if cls is Record:
        return Record(
            "acme.route_graph/1", 123,
            {
                "depth": np.zeros((8, 8), np.float32),
                "nested": {"heat": np.ones((4,), np.float32)},
                "listed": [np.zeros((2,), np.float32), 3, "x"],
                "grade": "V4",
            },
        )
    raise AssertionError(cls)


def arrays_in(obj):
    """Every ndarray reachable from a payload, with a path label."""
    out = []
    for name in getattr(obj, "_ARRAY_FIELDS", ()):
        a = getattr(obj, name)
        if a is not None:
            out.append((name, a))

    def walk(prefix, o):
        if isinstance(o, np.ndarray):
            out.append((prefix, o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(f"{prefix}[{k!r}]", v)
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                walk(f"{prefix}[{i}]", v)

    for name in getattr(obj, "_TREE_FIELDS", ()):
        walk(name, getattr(obj, name))
    return out


# ------------------------------------------------- §2.2.1 the immutability guarantee


@pytest.mark.parametrize("cls", CONTRACT_TYPES, ids=lambda c: c.__name__)
def test_setstate_is_ours_not_the_one_dataclass_installed(cls):
    """@payload must be applied OUTSIDE @dataclass.

    A __setstate__ inherited from a mixin is SILENTLY shadowed: dataclasses._add_slots
    guards on `'__setstate__' not in cls_dict`, and an inherited method is never in
    cls_dict. That failure is invisible in-process and total across a process boundary.
    """
    assert cls.__setstate__.__qualname__.startswith("payload")


@pytest.mark.parametrize("cls", CONTRACT_TYPES, ids=lambda c: c.__name__)
def test_arrays_readonly_in_publisher(cls):
    for name, arr in arrays_in(sample(cls)):
        assert not arr.flags.writeable, f"{cls.__name__}.{name} is writeable"


@pytest.mark.parametrize("cls", CONTRACT_TYPES, ids=lambda c: c.__name__)
def test_arrays_readonly_survive_pickle_round_trip(cls):
    """THE acceptance criterion. numpy does not preserve writeable=False across pickle,
    and a frozen slots dataclass unpickles without running __post_init__ — so a fix that
    lives only in __post_init__ freezes the publisher's copy and leaves every subscriber's
    writeable, which is exactly where the failure cases live."""
    obj = pickle.loads(pickle.dumps(sample(cls)))
    writeable = [n for n, a in arrays_in(obj) if a.flags.writeable]
    assert not writeable, f"{cls.__name__}: writeable after round-trip: {writeable}"


def test_subscriber_mutation_raises():
    f = pickle.loads(pickle.dumps(sample(Frame)))
    with pytest.raises(ValueError, match="read-only"):
        f.pixels[:] = 9
    p = pickle.loads(pickle.dumps(sample(PoseFrame)))
    with pytest.raises(ValueError, match="read-only"):
        p.world[:, 1] *= -1


def test_owndata_test_copies_an_aliasing_view():
    """The publisher-side race. `arr.base is not None` was the wrong ownership test:
    np.ascontiguousarray returns the SAME object for a contiguous view, so the copy branch
    never fired and the payload kept a live view onto the publisher's buffer — which it can
    mutate before the queue feeder thread has serialised it."""
    ring = np.zeros((64, 4), np.float32)
    view = ring[:33]
    assert np.ascontiguousarray(view) is view, "premise of the old, broken test"

    pf = PoseFrame(1, 2, "mediapipe.pose.33", True, view, None, True)
    ring[:] = 7.0
    assert pf.world.max() == 0.0, "payload aliased the publisher's buffer"
    assert pf.world.flags["OWNDATA"]
    assert pickle.loads(pickle.dumps(pf)).world.max() == 0.0


def test_record_nested_arrays_readonly_at_every_depth():
    r = pickle.loads(pickle.dumps(sample(Record)))
    assert not r.data["depth"].flags.writeable
    assert not r.data["nested"]["heat"].flags.writeable
    assert not r.data["listed"][0].flags.writeable
    assert r.data["grade"] == "V4" and r.data["listed"][1] == 3, "non-array leaves altered"


def test_every_ndarray_field_is_declared():
    """Catches the next forgotten _ARRAY_FIELDS entry, which is how a nested array escaped."""
    assert _declared_array_fields_are_complete() == []


def test_non_contiguous_input_is_copied_not_rejected():
    neg = np.zeros((4, 4, 3), np.uint8)[..., ::-1]
    assert not neg.flags["C_CONTIGUOUS"]
    assert Frame(1, 2, neg, "rgb", False, "x").pixels.flags["C_CONTIGUOUS"]


# ------------------------------------------------------------------- §3 type contracts


def test_as_bgr_and_as_rgb_always_return_a_fresh_writable_array():
    """Specified per guardian B3/F-11: previously unstated, and §2.1's two examples implied
    opposite things about whether the result was safe to mutate or already a copy."""
    for color in ("bgr", "rgb", "gray"):
        f = Frame(1, 2, np.zeros((4, 4, 3), np.uint8), color, False, "x")
        for out in (f.as_bgr(), f.as_rgb()):
            assert out.flags.writeable, f"{color}: result must be writable"
            assert out.flags["C_CONTIGUOUS"], f"{color}: result must be contiguous"
            assert out.dtype == np.uint8 and out.shape == (4, 4, 3)
            assert out is not f.pixels, f"{color}: must never return self.pixels"
            out[:] = 1  # must not raise, and must not touch the payload
        assert f.pixels.max() == 0


def test_as_rgb_actually_reverses_channel_order():
    px = np.zeros((1, 1, 3), np.uint8)
    px[0, 0] = (10, 20, 30)  # B, G, R
    f = Frame(1, 2, px, "bgr", False, "x")
    assert tuple(f.as_rgb()[0, 0]) == (30, 20, 10)
    assert tuple(f.as_bgr()[0, 0]) == (10, 20, 30)


def test_frame_rejects_wrong_dtype_and_shape():
    with pytest.raises(ValueError, match="uint8"):
        Frame(1, 2, np.zeros((4, 4, 3), np.float32), "bgr", False, "x")
    with pytest.raises(ValueError, match=r"\(H, W, 3\)"):
        Frame(1, 2, np.zeros((4, 4), np.uint8), "gray", False, "x")
    with pytest.raises(ValueError, match="color"):
        Frame(1, 2, np.zeros((4, 4, 3), np.uint8), "bgra", False, "x")


def test_poseframe_landmark_count_must_match_topology():
    """The silent-failure mode topology exists to convert into a loud one: a 17-point model
    published where subscribers index 33-point landmarks reads different joints entirely."""
    with pytest.raises(ValueError, match=r"\(33, 4\)"):
        PoseFrame(1, 2, "mediapipe.pose.33", False,
                  np.zeros((17, 4), np.float32), None, False)
    ok = PoseFrame(1, 2, "coco.17", False, np.zeros((17, 4), np.float32), None, False)
    assert ok.world.shape == (TOPOLOGY_SIZES["coco.17"], 4)


def test_poseframe_rejects_unknown_topology_and_nonfinite():
    with pytest.raises(ValueError, match="not a known topology"):
        PoseFrame(1, 2, "acme.pose.99", False, np.zeros((33, 4), np.float32), None, False)
    bad = np.zeros((33, 4), np.float32)
    bad[3, 2] = np.nan
    with pytest.raises(ValueError, match="NaN or inf"):
        PoseFrame(1, 2, "mediapipe.pose.33", False, bad, None, False)


def test_poseframe_frame_seq_minus_one_is_legal_for_replay_sources():
    """broker.md §8 recommends building replay() as a plugin publishing pose.smoothed, so
    the not-from-a-live-frame sentinel must be constructible."""
    assert PoseFrame(-1, 0, "mediapipe.pose.33", False,
                     np.zeros((33, 4), np.float32), None, True).frame_seq == -1


def test_holdboxes_clips_range_but_raises_on_reversed_corners():
    """F-9: out-of-frame values are a normal detector artifact the framework handles; a
    reversed corner is a coordinate-order bug that must not be silently repaired."""
    hb = HoldBoxes(1, np.array([[-0.05, 0.1, 1.20, 0.9]], np.float32),
                   ("hold",), np.array([0.5], np.float32))
    assert hb.boxes[0, 0] == 0.0 and hb.boxes[0, 2] == 1.0

    with pytest.raises(ValueError, match="x1 <= x2"):
        HoldBoxes(1, np.array([[0.9, 0.1, 0.2, 0.9]], np.float32),
                  ("hold",), np.array([0.5], np.float32))


def test_holdboxes_empty_is_shape_0_4_not_none():
    hb = HoldBoxes(7, np.zeros((0, 4), np.float32), (), np.zeros((0,), np.float32))
    assert hb.boxes.shape == (0, 4) and hb.labels == ()


def test_holdboxes_labels_and_scores_must_match_box_count():
    with pytest.raises(ValueError, match="labels"):
        HoldBoxes(1, np.zeros((2, 4), np.float32), ("a",), np.zeros((2,), np.float32))
    with pytest.raises(ValueError, match="scores"):
        HoldBoxes(1, np.zeros((2, 4), np.float32), ("a", "b"), np.zeros((1,), np.float32))


def test_status_state_is_a_closed_set():
    with pytest.raises(ValueError, match="not a known state"):
        Status("core.capture", "disabled", "")
    for state in STATUS_STATES:
        assert Status("x", state, "").state == state


def test_record_kind_must_be_versioned():
    for bad in ("acme.route_graph", "AcmeGraph/1", "acme.route_graph/", "/1"):
        with pytest.raises(ValueError, match="Record.kind"):
            Record(bad, 1, {})


def test_record_rejects_plugin_defined_classes():
    """The reason Record exists as data rather than as an author's own class: a
    plugin-defined class pickles under the publishing plugin's module name and resolves
    against a DIFFERENT plugin's module in the subscriber — silently, into the wrong class."""

    class Mine:
        pass

    with pytest.raises(ValueError, match="only str, int, float"):
        Record("acme.thing/1", 1, {"x": Mine()})
    with pytest.raises(ValueError, match="only str, int, float"):
        Record("acme.thing/1", 1, {"x": {"deep": Mine()}})


def test_record_rejects_non_str_keys_and_excessive_nesting():
    with pytest.raises(ValueError, match="keys must be str"):
        Record("acme.thing/1", 1, {1: "x"})
    deep: dict = {"a": "leaf"}
    for _ in range(12):
        deep = {"a": deep}
    with pytest.raises(ValueError, match="nests deeper"):
        Record("acme.thing/1", 1, deep)


def test_record_lists_become_tuples():
    r = Record("acme.thing/1", 1, {"xs": [1, 2, 3]})
    assert r.data["xs"] == (1, 2, 3)


def test_meta_is_constructible_and_frozen():
    m = Meta("holds.boxes", "yolo_holds", 0, 123)
    assert m.source == "yolo_holds"
    with pytest.raises(Exception):
        m.source = "spoofed"  # type: ignore[misc]


def test_shutdown_carries_its_reason_verbatim():
    assert Shutdown("ESC pressed").reason == "ESC pressed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
