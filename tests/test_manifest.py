"""Tests for climbcv.manifest and climbcv.topics — design/loader.md §3, broker.md §2–§3.

Bias here is toward the *hostile and careless* cases rather than the happy path: this parser
is the gate every other check depends on, and its error text is the primary documentation a
third-party author will ever read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    ManifestError,
    parse_manifest,
)
from climbcv.topics import (  # noqa: E402
    STANDARD_TOPICS,
    Exclusivity,
    Kind,
    validate_topic_name,
)

GOOD = """
[plugin]
id          = "yolo_holds"
version     = "1.2.0"
api_version = "1.0"
entry       = "plugin:HoldDetector"
name        = "YOLO hold detection"
description = "Detects climbing holds."
author      = "Aaron"

[[publishes]]
topic = "holds.boxes"

[[subscribes]]
topic = "frame"
"""


def write(tmp_path: Path, body: str, dirname: str = "yolo_holds") -> Path:
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / MANIFEST_FILENAME
    p.write_text(body, encoding="utf-8")
    return p


def parse(tmp_path: Path, body: str, dirname: str = "yolo_holds"):
    return parse_manifest(write(tmp_path, body, dirname), root="user")


# ------------------------------------------------------------------- happy path


def test_minimal_valid_manifest(tmp_path):
    m = parse(tmp_path, GOOD)
    assert m.id == "yolo_holds"
    assert m.api_version == (1, 0)
    assert m.module == "plugin" and m.class_name == "HoldDetector"
    assert [p.topic for p in m.publishes] == ["holds.boxes"]
    assert m.subscribes[0].required is True, "required defaults to true"
    assert m.subscribes[0].conflate is True, "conflate defaults to true"
    assert m.subscribes[0].mode == "handler"
    assert m.teardown_timeout_s == 1.0 and m.heartbeat_warn_s == 5.0
    assert m.warnings == ()


# ------------------------------------------------------------------- ids


@pytest.mark.parametrize("bad", ["Yolo", "1holds", "yolo-holds", "yolo..holds", "_x"])
def test_invalid_ids_rejected(tmp_path, bad):
    with pytest.raises(ManifestError, match="not a valid id"):
        parse(tmp_path, GOOD.replace('id          = "yolo_holds"', f'id = "{bad}"'))


@pytest.mark.parametrize("reserved", ["core.capture", "climbcv.internal"])
def test_reserved_id_prefixes_rejected(tmp_path, reserved):
    with pytest.raises(ManifestError, match="reserved prefix"):
        parse(tmp_path, GOOD.replace('id          = "yolo_holds"', f'id = "{reserved}"'))


def test_dotted_ids_are_legal(tmp_path):
    assert parse(tmp_path, GOOD.replace('"yolo_holds"', '"acme.grip"')).id == "acme.grip"


def test_id_directory_mismatch_warns_but_loads(tmp_path):
    m = parse(tmp_path, GOOD, dirname="some_other_folder")
    assert m.id == "yolo_holds"
    assert any("does not match the directory name" in w for w in m.warnings)


# ------------------------------------------------------------------- versions


@pytest.mark.parametrize("bad", ["v1.2", "1.2", "2024-06-01", "1.2.0.1", "1.2 beta"])
def test_version_must_be_semver(tmp_path, bad):
    """Validated now because it is free now — authors write semver anyway, and any future
    registry or update check must compare these. Tightening later would be breaking."""
    with pytest.raises(ManifestError, match="not semver"):
        parse(tmp_path, GOOD.replace('version     = "1.2.0"', f'version = "{bad}"'))


@pytest.mark.parametrize("ok", ["1.2.0", "0.1.0", "2.0.0-beta.1", "1.0.0+build5"])
def test_semver_variants_accepted(tmp_path, ok):
    assert parse(tmp_path, GOOD.replace('version     = "1.2.0"', f'version = "{ok}"'))


@pytest.mark.parametrize("bad", ["1", "1.0.0", "abc", ""])
def test_api_version_must_be_major_minor(tmp_path, bad):
    with pytest.raises(ManifestError):
        parse(tmp_path, GOOD.replace('api_version = "1.0"', f'api_version = "{bad}"'))


# ------------------------------------------------------------------- entry


@pytest.mark.parametrize("bad", ["plugin", "plugin:", ":Thing", "a:b:c"])
def test_entry_must_be_module_colon_class(tmp_path, bad):
    with pytest.raises(ManifestError, match="<module>:<ClassName>"):
        parse(tmp_path, GOOD.replace('entry       = "plugin:HoldDetector"', f'entry = "{bad}"'))


# ------------------------------------------------------------------- required fields


@pytest.mark.parametrize("key", ["id", "version", "api_version", "entry", "name", "description", "author"])
def test_missing_required_field_is_an_error_naming_it(tmp_path, key):
    body = "\n".join(line for line in GOOD.splitlines() if not line.strip().startswith(key))
    with pytest.raises(ManifestError, match=key):
        parse(tmp_path, body)


def test_missing_plugin_table_names_what_is_needed(tmp_path):
    with pytest.raises(ManifestError, match=r"\[plugin\]"):
        parse(tmp_path, "[[publishes]]\ntopic = 'frame'\n")


def test_malformed_toml_says_so(tmp_path):
    with pytest.raises(ManifestError, match="not valid TOML"):
        parse(tmp_path, "[plugin\nid = ")


# ------------------------------- unknown keys: guardian-02 finding 10


def test_unknown_key_warns_with_nearest_match_and_still_loads(tmp_path):
    """Silently ignoring these is what the framework cannot afford: a typo'd `conflat`
    reinstates the silent frame loss F-14 exists to prevent."""
    m = parse(tmp_path, GOOD.replace("topic = \"frame\"", "topic = \"frame\"\nconflat = false"))
    assert any("conflat" in w and "conflate" in w for w in m.warnings)
    assert m.subscribes[0].conflate is True, "the typo must not silently take effect"


def test_unknown_plugin_key_warns_with_hint(tmp_path):
    m = parse(tmp_path, GOOD.replace('author      = "Aaron"', 'author = "Aaron"\nplatform = ["darwin"]'))
    assert any("platform" in w and "platforms" in w for w in m.warnings)
    assert m.platforms == (), "the typo must not silently enable a platform filter"


def test_unknown_key_with_no_close_match_still_warns(tmp_path):
    m = parse(tmp_path, GOOD.replace('author      = "Aaron"', 'author = "Aaron"\nzzz_wat = 1'))
    assert any("zzz_wat" in w for w in m.warnings)


# ---------------------------------- payload / unit / record_kind


def test_scalar_topic_requires_unit(tmp_path):
    """Without it two publishers declare the same topic in newtons and kgf, agree on every
    checked field, and disagree by 9.8x forever."""
    body = GOOD + '\n[[publishes]]\ntopic = "acme.grip"\nkind = "stream"\nexclusivity = "shared"\npayload = "scalar"\ndoc = "x"\n'
    with pytest.raises(ManifestError, match="unit is required"):
        parse(tmp_path, body)


def test_record_topic_requires_record_kind(tmp_path):
    """Guardian-02 blocker 2: schema is 'record/1' for every record topic, so record_kind is
    the only thing that can tell two incompatible data layouts apart."""
    body = GOOD + '\n[[publishes]]\ntopic = "acme.hand"\nkind = "stream"\nexclusivity = "shared"\npayload = "record"\ndoc = "x"\n'
    with pytest.raises(ManifestError, match="record_kind is required"):
        parse(tmp_path, body)


def test_record_kind_accepted_when_declared(tmp_path):
    body = GOOD + '\n[[publishes]]\ntopic = "acme.hand"\nkind = "stream"\nexclusivity = "shared"\npayload = "record"\nrecord_kind = "acme.hand_state/1"\ndoc = "x"\n'
    assert parse(tmp_path, body).publishes[1].record_kind == "acme.hand_state/1"


def test_unit_and_record_kind_are_forbidden_on_other_payloads(tmp_path):
    body = GOOD + '\n[[publishes]]\ntopic = "acme.a"\nkind = "stream"\nexclusivity = "shared"\npayload = "frame"\nunit = "degree"\n'
    with pytest.raises(ManifestError, match="unit applies only"):
        parse(tmp_path, body)


def test_unknown_payload_type_lists_the_known_ones(tmp_path):
    body = GOOD + '\n[[publishes]]\ntopic = "acme.a"\nkind = "stream"\nexclusivity = "shared"\npayload = "MyClass"\n'
    with pytest.raises(ManifestError, match="not a contract type"):
        parse(tmp_path, body)


# ------------------------------------------------------------------- topic names


@pytest.mark.parametrize("bad", ["Frame", "1frame", "frame..x", "frame-x", "climbcv.secret"])
def test_invalid_topic_names_rejected(tmp_path, bad):
    with pytest.raises(ManifestError):
        parse(tmp_path, GOOD.replace('topic = "holds.boxes"', f'topic = "{bad}"'))


def test_reserved_topic_prefix_rejected():
    with pytest.raises(ValueError, match="reserved prefix"):
        validate_topic_name("climbcv.wat", "test")


def test_overlong_topic_name_rejected():
    with pytest.raises(ValueError, match="over the"):
        validate_topic_name("a" + ".a" * 40, "test")


def test_duplicate_topic_in_publishes_rejected(tmp_path):
    with pytest.raises(ManifestError, match="declared twice"):
        parse(tmp_path, GOOD + '\n[[publishes]]\ntopic = "holds.boxes"\n')


# ------------------------------------------------------------------- subscribe sub-keys


def test_subscribe_sub_keys_parsed(tmp_path):
    body = GOOD.replace(
        'topic = "frame"',
        'topic = "frame"\nrequired = false\nconflate = false\ndepth = 128\nmode = "latest"',
    )
    s = parse(tmp_path, body).subscribes[0]
    assert (s.required, s.conflate, s.depth, s.mode) == (False, False, 128, "latest")


def test_bad_mode_rejected(tmp_path):
    with pytest.raises(ManifestError, match="mode must be"):
        parse(tmp_path, GOOD.replace('topic = "frame"', 'topic = "frame"\nmode = "poll"'))


def test_negative_depth_rejected(tmp_path):
    with pytest.raises(ManifestError, match="non-negative"):
        parse(tmp_path, GOOD.replace('topic = "frame"', 'topic = "frame"\ndepth = -1'))


# ------------------------------------------------------------------- timeouts


def test_teardown_timeout_over_cap_is_clamped_with_a_warning(tmp_path):
    m = parse(tmp_path, GOOD.replace('author      = "Aaron"', 'author = "Aaron"\nteardown_timeout_s = 999.0'))
    assert m.teardown_timeout_s == 30.0
    assert any("exceeds the cap" in w for w in m.warnings)


def test_nonpositive_timeout_rejected(tmp_path):
    with pytest.raises(ManifestError, match="positive number"):
        parse(tmp_path, GOOD.replace('author      = "Aaron"', 'author = "Aaron"\nteardown_timeout_s = 0'))


# ------------------------------------------ provides_topology: guardian-02 finding 3


def test_provides_topology_accepts_a_list(tmp_path):
    """A topology-agnostic transform stage has no single honest value to declare. Forcing
    one quarantines core.smooth_oneeuro the moment a third-party pose plugin provides a
    different topology — and it is the exclusive publisher of pose.smoothed, so the app dies."""
    body = GOOD.replace(
        'author      = "Aaron"',
        'author = "Aaron"\nprovides_topology = ["mediapipe.pose.33", "coco.17"]',
    )
    assert parse(tmp_path, body).provides_topology == ("mediapipe.pose.33", "coco.17")


def test_provides_topology_accepts_a_bare_string(tmp_path):
    body = GOOD.replace('author      = "Aaron"', 'author = "Aaron"\nprovides_topology = "coco.17"')
    assert parse(tmp_path, body).provides_topology == ("coco.17",)


# ------------------------------------------------------------------- [config] keys


def test_config_keys_are_informational(tmp_path):
    m = parse(tmp_path, GOOD + '\n[config]\nkeys = ["every_n_frames", "model_path"]\n')
    assert m.config_keys == ("every_n_frames", "model_path")


def test_plugin_with_no_topics_warns(tmp_path):
    body = GOOD.split("[[publishes]]")[0]
    m = parse(tmp_path, body)
    assert any("neither" in w for w in m.warnings)


# ------------------------------------------------------------------- standard topic set


def test_standard_set_matches_the_documented_namespace():
    assert set(STANDARD_TOPICS) == {
        "frame", "pose.raw", "pose.smoothed", "holds.boxes",
        "device.lid_angle", "app.shutdown", "app.status",
    }
    assert all(d.standard and d.declared_by == "core" for d in STANDARD_TOPICS.values())


def test_exclusivity_follows_the_criterion():
    """A topic is exclusive iff two publishers would contradict rather than add."""
    ex = Exclusivity.EXCLUSIVE
    sh = Exclusivity.SHARED
    assert STANDARD_TOPICS["frame"].exclusivity is ex
    assert STANDARD_TOPICS["pose.raw"].exclusivity is ex
    assert STANDARD_TOPICS["pose.smoothed"].exclusivity is ex
    assert STANDARD_TOPICS["device.lid_angle"].exclusivity is ex
    # Set-valued: the union of two detectors' boxes is still a valid set of boxes.
    assert STANDARD_TOPICS["holds.boxes"].exclusivity is sh
    # Anyone may ask to stop.
    assert STANDARD_TOPICS["app.shutdown"].exclusivity is sh
    # Guardian-02 finding 11: exclusive so a plugin cannot forge lifecycle events.
    assert STANDARD_TOPICS["app.status"].exclusivity is ex


def test_scalar_standard_topic_declares_its_unit():
    assert STANDARD_TOPICS["device.lid_angle"].unit == "degree"


def test_app_topics_are_event_kind():
    assert STANDARD_TOPICS["app.shutdown"].kind is Kind.EVENT
    assert STANDARD_TOPICS["app.status"].kind is Kind.EVENT
    assert STANDARD_TOPICS["frame"].kind is Kind.STREAM


def test_contract_fields_surfaces_what_schema_alone_cannot():
    """schema is 'record/1' for every record topic, so a diagnostic showing only schema
    cannot distinguish two incompatible layouts."""
    assert "unit=degree" in STANDARD_TOPICS["device.lid_angle"].contract_fields()
    assert "Frame" in STANDARD_TOPICS["frame"].contract_fields()
