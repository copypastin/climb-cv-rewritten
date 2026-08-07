"""Tests for climbcv.wiring — design/broker.md §4.

Table-driven: manifests plus config in, a plan or a specific error out. The assertions check
the error *content* as well as the failure, because for a drop-in ecosystem with no install
step the message is the only documentation a stuck author will read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.config import load_config  # noqa: E402
from climbcv.loader import Discovery  # noqa: E402
from climbcv.manifest import Manifest, Publish, Subscribe  # noqa: E402
from climbcv.topics import Exclusivity, Kind  # noqa: E402
from climbcv.wiring import HOST, Subscription, WiringError, resolve  # noqa: E402


def mf(pid, *, publishes=(), subscribes=(), provides=(), requires=None, root="user") -> Manifest:
    return Manifest(
        id=pid, version="1.0.0", api_version=(1, 0), entry="plugin:P",
        name=f"{pid} name", description="d", author="a",
        directory=Path("plugins") / pid, root=root,
        provides_topology=provides, requires_topology=requires,
        publishes=tuple(publishes), subscribes=tuple(subscribes),
    )


def disc(*manifests, skipped=()) -> Discovery:
    return Discovery(plugins=tuple(manifests), skipped=tuple(skipped), warnings=())


def cfg(**tables):
    return load_config(tables or None)


PUB_FRAME = (Publish("frame"),)
PUB_POSE = (Publish("pose.smoothed"),)
SUB_FRAME = (Subscribe("frame"),)
POSE33 = ("mediapipe.pose.33",)


# ---------------------------------------------------------------- happy paths


def test_single_publisher_wins_silently():
    plan = resolve(disc(mf("cap", publishes=PUB_FRAME), mf("eye", subscribes=SUB_FRAME)), cfg())
    assert plan.publishers["frame"] == ("cap",)
    assert plan.warnings == ()


def test_shared_topic_admits_every_publisher():
    plan = resolve(
        disc(
            mf("yolo", publishes=(Publish("holds.boxes"),)),
            mf("route", publishes=(Publish("holds.boxes"),)),
        ),
        cfg(),
    )
    assert set(plan.publishers["holds.boxes"]) == {"yolo", "route"}


def test_absent_topic_with_optional_subscriber_is_fine():
    """What makes device.lid_angle work: absent on Linux, and the overlay says so."""
    plan = resolve(
        disc(mf("overlay", subscribes=(Subscribe("device.lid_angle", required=False),))),
        cfg(),
    )
    assert "device.lid_angle" in plan.absent
    assert plan.publishers["device.lid_angle"] == ()


# ---------------------------------------------------------------- exclusivity


def test_two_exclusive_publishers_is_fatal_with_paste_ready_toml():
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("a", publishes=PUB_POSE, provides=POSE33),
                     mf("b", publishes=PUB_POSE, provides=POSE33)), cfg())
    msg = str(exc.value)
    assert "exclusive topic 'pose.smoothed'" in msg
    assert '[topics."pose.smoothed"]' in msg and "publisher =" in msg
    assert "enabled = false" in msg, "the other way out must be offered too"
    assert "contradict" in msg, "say WHY it is exclusive, not just that it is"


def test_config_pin_resolves_contention():
    plan = resolve(
        disc(mf("a", publishes=PUB_POSE, provides=POSE33),
             mf("b", publishes=PUB_POSE, provides=POSE33)),
        cfg(topics={"pose.smoothed": {"publisher": "b"}}),
    )
    assert plan.publishers["pose.smoothed"] == ("b",)


def test_pin_naming_an_unavailable_plugin_falls_back_with_a_warning():
    """A config written on another machine must not stop the app (broker.md §4.4)."""
    plan = resolve(
        disc(mf("a", publishes=PUB_POSE, provides=POSE33)),
        cfg(topics={"pose.smoothed": {"publisher": "mac_only"}}),
    )
    assert plan.publishers["pose.smoothed"] == ("a",)
    assert any("Falling back" in w for w in plan.warnings)


def test_pin_naming_a_plugin_that_does_not_publish_it_warns():
    plan = resolve(
        disc(mf("a", publishes=PUB_POSE, provides=POSE33),
             mf("b", subscribes=(Subscribe("pose.smoothed", required=False),), requires="any")),
        cfg(topics={"pose.smoothed": {"publisher": "b"}}),
    )
    assert plan.publishers["pose.smoothed"] == ("a",)
    assert any("does not publish" in w for w in plan.warnings)


# ---------------------------------------------------------------- starved subscriptions


def test_required_subscription_with_no_publisher_is_fatal_naming_the_plugins():
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("overlay", subscribes=SUB_FRAME)), cfg())
    msg = str(exc.value)
    assert "overlay" in msg and "'frame'" in msg
    assert "required = false" in msg, "offer the user-side override"


def test_starved_message_points_at_a_skipped_plugin_when_there_is_one():
    from climbcv.loader import Skipped

    with pytest.raises(WiringError) as exc:
        resolve(
            disc(
                mf("overlay", subscribes=(Subscribe("device.lid_angle"),)),
                skipped=(Skipped("mac_lid", "declares platforms ['darwin']", "INFO"),),
            ),
            cfg(),
        )
    assert "mac_lid" in str(exc.value)


# ------------------------------- unknown topic name: always fatal


def test_unknown_topic_name_is_fatal_even_when_optional():
    """`required = false` means 'this feature may be absent', not 'I may have misspelled it'.
    Splitting those two jobs is what lets a known-but-absent topic be fine."""
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("p", subscribes=(Subscribe("pose.smoothd", required=False),))), cfg())
    assert "pose.smoothed" in str(exc.value), "must suggest the nearest real name"


def test_known_but_absent_topic_is_not_an_unknown_topic():
    plan = resolve(disc(mf("p", subscribes=(Subscribe("holds.boxes", required=False),))), cfg())
    assert "holds.boxes" in plan.absent


# ---------------------------------------------------------------- descriptor merge


def test_redeclaring_a_standard_topic_differently_is_fatal():
    bad = (Publish("frame", kind=Kind.EVENT, exclusivity=Exclusivity.SHARED, payload="frame"),)
    with pytest.raises(WiringError, match="re-declares the standard topic"):
        resolve(disc(mf("p", publishes=bad)), cfg())


def test_two_plugins_disagreeing_on_an_author_topic_is_fatal_with_a_local_override():
    a = (Publish("acme.x", kind=Kind.STREAM, exclusivity=Exclusivity.SHARED, payload="scalar", unit="newton"),)
    b = (Publish("acme.x", kind=Kind.STREAM, exclusivity=Exclusivity.SHARED, payload="scalar", unit="kgf"),)
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("a", publishes=a), mf("b", publishes=b)), cfg())
    msg = str(exc.value)
    assert "unit" in msg and "newton" in msg and "kgf" in msg
    assert "[topics." in msg, "the user needs a way out that does not require an upstream release"


def test_record_kind_disagreement_is_caught_by_the_merge():
    """Guardian-02 blocker 2: schema is 'record/1' for both, so without record_kind in the
    merge these wire cleanly and fail in the subscriber's handler."""
    a = (Publish("acme.hand", kind=Kind.STREAM, exclusivity=Exclusivity.SHARED, payload="record", record_kind="acme.hand/1"),)
    b = (Publish("acme.hand", kind=Kind.STREAM, exclusivity=Exclusivity.SHARED, payload="record", record_kind="acme.hand/2"),)
    with pytest.raises(WiringError, match="record_kind"):
        resolve(disc(mf("a", publishes=a), mf("b", publishes=b)), cfg())


def test_undescribed_nonstandard_topic_is_fatal_with_a_template():
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("p", publishes=(Publish("acme.x"),))), cfg())
    assert "does not describe it" in str(exc.value)
    assert "exclusivity" in str(exc.value)


# ---------------------------------------------------------------- topology


def test_pose_publisher_must_declare_provides_topology():
    with pytest.raises(WiringError, match="does not declare provides_topology"):
        resolve(disc(mf("pose", publishes=PUB_POSE)), cfg())


def test_pose_subscriber_must_declare_requires_topology():
    with pytest.raises(WiringError, match="does not declare requires_topology"):
        resolve(
            disc(
                mf("pose", publishes=PUB_POSE, provides=POSE33),
                mf("overlay", subscribes=(Subscribe("pose.smoothed"),)),
            ),
            cfg(),
        )


def test_topology_mismatch_is_fatal_naming_both_sides():
    with pytest.raises(WiringError) as exc:
        resolve(
            disc(
                mf("fast_pose", publishes=PUB_POSE, provides=("coco.17",)),
                mf("overlay", subscribes=(Subscribe("pose.smoothed"),), requires="mediapipe.pose.33"),
            ),
            cfg(),
        )
    msg = str(exc.value)
    assert "coco.17" in msg and "mediapipe.pose.33" in msg
    assert "different body part" in msg


def test_requires_any_opts_out():
    plan = resolve(
        disc(
            mf("fast_pose", publishes=PUB_POSE, provides=("coco.17",)),
            mf("recorder", subscribes=(Subscribe("pose.smoothed"),), requires="any"),
        ),
        cfg(),
    )
    assert plan.publishers["pose.smoothed"] == ("fast_pose",)


def test_multi_valued_provides_topology_wires_against_either():
    """Guardian-02 finding 3, resolution half. A topology-agnostic transform stage — a One
    Euro filter filters columns and indexes no joints — must be able to sit between a
    third-party pose plugin and a subscriber without being quarantined for declaring the
    'wrong' single topology."""
    plan = resolve(
        disc(
            mf("smooth", publishes=PUB_POSE, provides=("mediapipe.pose.33", "coco.17"), requires="any",
               subscribes=(Subscribe("pose.raw"),)),
            mf("fast_pose", publishes=(Publish("pose.raw"),), provides=("coco.17",)),
            mf("overlay", subscribes=(Subscribe("pose.smoothed"),), requires="coco.17"),
        ),
        cfg(),
    )
    assert plan.publishers["pose.smoothed"] == ("smooth",)
    assert plan.plugins["smooth"].provides_topology is None, "multi-valued stays unresolved"
    assert plan.plugins["fast_pose"].provides_topology == "coco.17"


# ---------------------------------------------------------------- warnings


def test_multi_publisher_shared_topic_warns_its_subscribers():
    """Guardian B1 / F-1: latest() collapses across publishers and the author may not know a
    second one exists."""
    plan = resolve(
        disc(
            mf("yolo", publishes=(Publish("holds.boxes"),)),
            mf("route", publishes=(Publish("holds.boxes"),)),
            mf("overlay", subscribes=(Subscribe("holds.boxes"),)),
        ),
        cfg(),
    )
    assert any("latest_by_source" in w for w in plan.warnings)


def test_shared_topic_latest_mode_warns_even_with_one_publisher():
    """Guardian-02 finding 9: the warning above fires on deployment shape, so an author
    testing with one publisher never sees it and ships anyway."""
    plan = resolve(
        disc(
            mf("yolo", publishes=(Publish("holds.boxes"),)),
            mf("overlay", subscribes=(Subscribe("holds.boxes", mode="latest"),)),
        ),
        cfg(),
    )
    assert any("collapses across publishers" in w for w in plan.warnings)


def test_widening_exclusivity_via_config_warns_about_the_delivery_contract():
    a = (Publish("acme.x", kind=Kind.STREAM, exclusivity=Exclusivity.EXCLUSIVE, payload="scalar", unit="n"),)
    plan = resolve(disc(mf("a", publishes=a)), cfg(topics={"acme.x": {"exclusivity": "shared"}}))
    assert any("different delivery contract" in w for w in plan.warnings)


# ---------------------------------------------------------------- queue depths


def test_depth_scales_with_subscription_count_not_a_constant():
    """A constant depth lets a 30fps frame burst evict another topic's pending message
    before its handler ever runs."""
    many = resolve(
        disc(
            mf("cap", publishes=PUB_FRAME),
            mf("pose", publishes=(Publish("pose.raw"),), provides=POSE33),
            mf("yolo", publishes=(Publish("holds.boxes"),)),
            mf("busy", requires="any", subscribes=(
                Subscribe("frame"), Subscribe("pose.raw"), Subscribe("holds.boxes"),
            )),
        ),
        cfg(),
    )
    assert many.plugins["busy"].stream_depth == 6
    few = resolve(disc(mf("cap", publishes=PUB_FRAME), mf("one", subscribes=SUB_FRAME)), cfg())
    assert few.plugins["one"].stream_depth == 4, "floor of 4"


def test_nonconflating_subscription_gets_its_own_depth():
    """F-14: a recorder cannot record every frame if conflation is unconditional."""
    plan = resolve(
        disc(
            mf("cap", publishes=PUB_FRAME),
            mf("rec", subscribes=(Subscribe("frame", conflate=False),)),
        ),
        cfg(),
    )
    assert plan.plugins["rec"].subscribes[0].conflate is False
    assert plan.plugins["rec"].subscribes[0].depth == 64, "0 means the 64 default"


def test_configured_stream_depth_overrides_and_is_capped():
    plan = resolve(
        disc(mf("cap", publishes=PUB_FRAME), mf("one", subscribes=SUB_FRAME)),
        cfg(framework={"stream_depth": 9999, "max_stream_depth": 32}),
    )
    assert plan.plugins["one"].stream_depth == 32


# ---------------------------------------------------------------- host participation


def test_host_can_subscribe_and_appears_in_the_plan():
    plan = resolve(
        disc(mf("cap", publishes=PUB_FRAME)),
        cfg(),
        host_subscribes=(Subscription("frame", True, 4, "handler", False),),
    )
    assert HOST in plan.plugins
    assert HOST in plan.subscribers("frame")


def test_host_publisher_enters_exclusive_contention_like_any_other():
    with pytest.raises(WiringError) as exc:
        resolve(disc(mf("cap", publishes=PUB_FRAME)), cfg(), host_publishes=("frame",))
    assert "the embedding application" in str(exc.value)


def test_host_can_be_the_sole_publisher():
    plan = resolve(
        disc(mf("eye", subscribes=SUB_FRAME)), cfg(), host_publishes=("frame",)
    )
    assert plan.publishers["frame"] == (HOST,)
