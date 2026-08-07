"""Tests for the authoring interface — design/plugin-api.md.

These are the contract checks an author hits on their first attempt, so the assertions are as
much about the *message* as the failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.plugin import (  # noqa: E402
    RESERVED_NAMES,
    Finished,
    Plugin,
    PluginContractError,
    Unavailable,
    check_contract,
    collect_handlers,
    every,
    subscribe,
)


# ---------------------------------------------------------------- decorators


def test_subscribe_records_topics():
    class P(Plugin):
        @subscribe("frame")
        def on_frame(self, payload, meta): ...

    by_topic, timers = collect_handlers(P)
    assert list(by_topic) == ["frame"]
    assert timers == []


def test_one_handler_can_serve_several_topics():
    class P(Plugin):
        @subscribe("frame", "pose.smoothed")
        def on_any(self, payload, meta): ...

    by_topic, _ = collect_handlers(P)
    assert set(by_topic) == {"frame", "pose.smoothed"}


def test_stacked_subscribe_decorators_accumulate():
    class P(Plugin):
        @subscribe("frame")
        @subscribe("holds.boxes")
        def on_any(self, payload, meta): ...

    by_topic, _ = collect_handlers(P)
    assert set(by_topic) == {"frame", "holds.boxes"}


def test_subscribe_with_no_topic_is_an_error():
    with pytest.raises(PluginContractError):
        subscribe()


def test_every_records_the_interval():
    class P(Plugin):
        @every(0.5)
        def tick(self): ...

    _, timers = collect_handlers(P)
    assert timers == [(P.tick, 0.5)]


def test_every_zero_is_legal_because_that_is_how_a_source_is_written():
    """@every(0) composes to 'as fast as possible'. @tick(0) would read as 'never'."""
    class P(Plugin):
        @every(0)
        def pump(self): ...

    _, timers = collect_handlers(P)
    assert timers[0][1] == 0.0


def test_negative_interval_rejected():
    with pytest.raises(PluginContractError):
        every(-1)


# ---------------------------------------------------------------- contract checks


def test_defining_init_is_rejected_with_the_true_reason():
    """The reason is ordering, NOT pickling: the framework binds config/log/publish AFTER
    constructing the plugin. An earlier draft of this message claimed objects could not
    'come along' across the process boundary, which taught authors a false model of what
    crosses one — and would have made them wrong about payloads too."""
    class P(Plugin):
        def __init__(self):
            super().__init__()

    with pytest.raises(PluginContractError) as exc:
        check_contract(P)
    msg = str(exc.value)
    assert "setup()" in msg
    assert "AFTER" in msg or "after" in msg
    assert "pickl" not in msg.lower(), "must not repeat the false pickling rationale"


def test_non_plugin_class_is_rejected_with_the_fix():
    class NotAPlugin:
        pass

    with pytest.raises(PluginContractError, match="Plugin"):
        check_contract(NotAPlugin)


def test_class_body_override_of_a_reserved_name_is_rejected():
    """Quieter than the setup() case: because the framework binds onto the instance, the
    author's method is shadowed and never runs, so their calls silently reach framework code."""
    class P(Plugin):
        def latest(self, topic):
            return "mine"

    with pytest.raises(PluginContractError) as exc:
        check_contract(P)
    assert "latest" in str(exc.value)
    assert "never be called" in str(exc.value)


def test_setup_and_teardown_are_not_treated_as_overrides():
    class P(Plugin):
        def setup(self): ...
        def teardown(self): ...

    check_contract(P)  # must not raise


def test_a_plain_plugin_passes():
    class P(Plugin):
        @subscribe("frame")
        def on_frame(self, payload, meta): ...

    check_contract(P)


def test_reserved_names_cover_the_documented_surface():
    """If the base class grows, this is the list that keeps additions additive."""
    assert {"config", "log", "publish", "latest", "stopping", "data_dir"} <= RESERVED_NAMES
    assert "setup" in RESERVED_NAMES and "teardown" in RESERVED_NAMES


# ---------------------------------------------------------------- control flow


def test_finish_raises_the_internal_signal():
    p = Plugin()
    with pytest.raises(Finished) as exc:
        p.finish("end of file")
    assert exc.value.reason == "end of file"


def test_unavailable_raises_the_internal_signal():
    p = Plugin()
    with pytest.raises(Unavailable) as exc:
        p.unavailable("no lid sensor on this Mac")
    assert "lid sensor" in exc.value.reason


def test_stopping_is_a_property_not_a_plain_attribute():
    """Guardian-02 finding 4. A plain bool could only be reassigned between handler calls,
    and the loop that would reassign it is the one blocked inside the handler asking. The
    blessed @every(0) source idiom depends on this flipping mid-handler."""
    assert isinstance(Plugin.__dict__["stopping"], property)
    assert isinstance(Plugin.__dict__["config"], property)
    assert isinstance(Plugin.__dict__["data_dir"], property)


def test_data_members_are_read_only():
    class Fake:
        stopping = False

    p = Plugin()
    object.__setattr__(p, "_rt", Fake())
    with pytest.raises(AttributeError):
        p.stopping = True  # type: ignore[misc]
