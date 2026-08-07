"""The plugin authoring interface — everything a third party writes against.

design/plugin-api.md. One uniform model for every plugin type: subscribe to topics, publish
to topics. An observer, a new stage, a stage replacement and a new sink are all the same
thing to write; what a plugin *is* comes from which topics it declares, not from which class
it subclasses or which flag it sets.

Nothing in here requires knowing about processes. The framework runs each plugin in its own
child process, and no author writes a Process, Queue, Lock, Event or pickle call.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

__all__ = [
    "Plugin",
    "PluginContractError",
    "subscribe",
    "every",
    "RESERVED_NAMES",
    "Finished",
    "Unavailable",
]


class PluginContractError(Exception):
    """A deterministic authoring mistake — defining __init__, overriding a reserved name,
    a handler with no matching manifest entry.

    Non-retryable, deliberately: retrying a deterministic mistake produces the same failure
    with the useful message buried under a restart log. Quarantined on first occurrence, one
    message, and it never escalates to shutting the whole app down.
    """


class Finished(Exception):
    """Internal: raised by finish() to unwind the child loop. Not for authors to catch."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class Unavailable(Exception):
    """Internal: raised by unavailable() to unwind the child loop. Not for authors."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


RESERVED_NAMES = frozenset({
    "config", "config_dir", "data_dir", "log", "stopping",
    "publish", "latest", "latest_by_source", "finish", "unavailable", "set_interval",
    "setup", "teardown",
})
"""Names the framework binds. Assigning to any of them in setup() shadows framework
machinery and produces a cryptic failure later — `self.latest = deque(...)` turns every
subsequent `self.latest(topic)` into "'deque' object is not callable" from inside a handler.

Any future framework member will be prefixed `cv_`, so an author who avoids that prefix is
safe from collisions when the base class grows. That reservation is what makes adding a
member in a minor version genuinely additive rather than a coin flip.
"""


def subscribe(*topics: str) -> Callable:
    """Bind a handler to one or more topics. Signature is always `(self, payload, meta)`.

        @subscribe("frame")
        def on_frame(self, frame, meta): ...

    Every topic named here must also appear in the manifest's [[subscribes]]. The manifest is
    what the framework reads to build the graph — it never imports your code to find out.
    """
    if not topics:
        raise PluginContractError("@subscribe() needs at least one topic name")

    def decorate(fn: Callable) -> Callable:
        existing = getattr(fn, "_climbcv_topics", ())
        fn._climbcv_topics = tuple(existing) + topics  # type: ignore[attr-defined]
        return fn

    return decorate


def every(seconds: float) -> Callable:
    """Run this handler on a timer. Signature is `(self)`.

        @every(1.0)        # about once a second
        def tick(self): ...

        @every(0)          # as fast as possible -- how you write a source
        def pump(self): ...

    Interval is measured from when your handler RETURNS, and missed ticks are dropped rather
    than queued: a handler that overruns its interval falls behind rather than accumulating
    an unbounded backlog it can never work off.

    `@every(0)` composes to "as fast as possible", which is exactly what a capture source
    needs. A blocking read inside one is fine and expected — see `self.stopping`.
    """
    if seconds < 0:
        raise PluginContractError(f"@every() interval must be >= 0, got {seconds}")

    def decorate(fn: Callable) -> Callable:
        fn._climbcv_interval = float(seconds)  # type: ignore[attr-defined]
        return fn

    return decorate


class Plugin:
    """Subclass this. Do NOT define __init__.

    `__init__` is reserved because the framework binds `config`, `log`, `publish` and the
    rest *after* constructing you — so inside `__init__` none of them exist yet, and the
    first thing an author reaches for there (`self.config.get(...)`) fails. Put your setup in
    `setup()`, which runs in this plugin's own process with everything available.

    That is also why no author ever pickles a model: your heavy objects are created in the
    process that uses them, not handed across a boundary.
    """

    # Populated by the child runtime before setup() is called. Never touch it directly.
    _rt: Any = None

    # ---------------------------------------------------------------- lifecycle

    def setup(self) -> None:
        """Called ONCE, in this plugin's own process, before any handler runs.

        Open devices, load models, build windows here. Note the difference from
        pytest/unittest, where `setup` runs per test — this runs once per process lifetime.

        Raising here means the plugin cannot start. It is retried once, then quarantined:
        a plugin that fails on first setup rarely succeeds on the fifth attempt. If the
        reason is "this machine does not have the thing I need", call `self.unavailable()`
        instead, which is not a failure.
        """

    def teardown(self) -> None:
        """Called once when the run is ending. Close files, release devices.

        NOT for finishing work. It may run while your inputs are still publishing and while
        your outputs are already gone, and it has a time budget (1 second by default,
        raisable per plugin with `teardown_timeout_s`). There is deliberately no ordering
        guarantee between plugins — inventing one would be a promise the framework cannot
        keep across independent processes.
        """

    # ---------------------------------------------------------------- data members

    @property
    def config(self) -> Mapping[str, Any]:
        """Your section of `climbcv.toml`, as a plain dict. Absent section -> empty dict.

        Deliberately unvalidated (Decision #8): use `self.config.get(key, default)`. TOML has
        no null, so "explicitly unset" is expressed by omitting the key, not by "" or 0.
        """
        return self._rt.config

    @property
    def config_dir(self) -> str | None:
        """The directory containing `climbcv.toml`, or None if there is no file.

        Framework path keys resolve against this; your own path values do not, because the
        framework cannot know which of your config values are paths without the typed schema
        Decision #8 excludes. If you want a path relative to the config file rather than the
        working directory, join it yourself.
        """
        return self._rt.config_dir

    @property
    def data_dir(self) -> str:
        """A writable directory that belongs to you, created before setup() runs.

        For compiled helpers, downloaded weights, caches. Do not write inside your own plugin
        directory: it may be read-only, and the bundled root always is.
        """
        return self._rt.data_dir

    @property
    def log(self) -> logging.Logger:
        """Your logger. Everything you log is attributed to you and routed to the host."""
        return self._rt.log

    @property
    def stopping(self) -> bool:
        """True once shutdown has begun — and it becomes True *while your handler is running*.

        That is the whole point of this member, and it is why it is a property over a shared
        event rather than a plain attribute: a plain bool could only be reassigned between
        handler calls, and the loop that would do the reassigning is the loop currently
        blocked inside your handler. A blocking source must be able to notice.

            @every(0)
            def pump(self):
                while not self.stopping:
                    chunk = self.socket.recv(65536)   # may block for seconds
                    self.publish("acme.chunk", ...)
        """
        return self._rt.stopping

    # ---------------------------------------------------------------- methods

    def publish(self, topic: str, payload: Any) -> None:
        """Send a payload on a topic you declared in [[publishes]].

        Fire-and-forget and it cannot block: if a subscriber is behind, the framework drops
        according to the topic's kind rather than making you wait. Publishing an undeclared
        topic raises, because a typo that silently goes nowhere forever is the worst failure
        this system can produce.

        The payload is yours until you publish it and the framework's afterwards; arrays
        inside it are read-only, so build a fresh one rather than mutating the last.
        """
        self._rt.publish(topic, payload)

    def latest(self, topic: str) -> Any | None:
        """The newest payload seen on `topic`, or None if none has arrived.

        For a topic with one publisher this is exactly what you want. On a SHARED topic it
        collapses across publishers — you get whichever published most recently, and two
        detectors will alternate. Use `latest_by_source()` there.
        """
        return self._rt.latest(topic)

    def latest_by_source(self, topic: str) -> Mapping[str, Any]:
        """`{publisher_id: newest payload}` for `topic`. Empty mapping, never None.

        This is the accessor that makes shared topics usable: colour by source, filter to
        one source, or draw everything. `latest()` cannot express any of those.
        """
        return self._rt.latest_by_source(topic)

    def set_interval(self, handler: Callable | str, seconds: float) -> None:
        """Change a timer's interval at runtime. Call from setup() to make it configurable.

        `@every(seconds)` fixes its interval at class-definition time, so without this a
        plugin cannot honour a configured rate at all:

            def setup(self):
                self.set_interval(self.poll, self.config.get("poll_interval_s", 0.5))
        """
        self._rt.set_interval(handler, seconds)

    def finish(self, reason: str = "") -> None:
        """Declare your work complete. An orderly end for this plugin, not an error.

        Careful: if you are the resolved publisher of a topic that other plugins REQUIRE,
        your finishing removes their input, and the framework will shut the run down — with a
        message saying so. A batch plugin that exhausts its input list and happens to publish
        `frame` has just ended the session. That is usually correct; know that it happens.
        """
        raise Finished(reason)

    def unavailable(self, reason: str) -> None:
        """Declare that you cannot run *on this machine* — no GPU, no sensor, no binary.

        Not a crash and not a failure: one INFO line, no retry, no quarantine, and the
        framework treats your topics as absent so optional subscribers carry on. Use this
        rather than raising, so the log says "this Mac has no lid sensor" instead of
        "mac_lid crashed twice and has been disabled".

        `platforms` in the manifest covers the static case; this is for what you can only
        discover by looking.
        """
        raise Unavailable(reason)


def collect_handlers(cls: type) -> tuple[dict[str, list[Callable]], list[tuple[Callable, float]]]:
    """(topic -> handlers, [(timer_handler, interval)]) declared on a Plugin subclass."""
    by_topic: dict[str, list[Callable]] = {}
    timers: list[tuple[Callable, float]] = []
    for name in dir(cls):
        if name.startswith("__"):
            continue
        attr = getattr(cls, name, None)
        if not callable(attr):
            continue
        for topic in getattr(attr, "_climbcv_topics", ()):
            by_topic.setdefault(topic, []).append(attr)
        interval = getattr(attr, "_climbcv_interval", None)
        if interval is not None:
            timers.append((attr, interval))
    return by_topic, timers


def check_contract(cls: type) -> None:
    """Deterministic authoring mistakes, raised before setup() so the message is not buried.

    Runs in the child, because the host never imports plugin code.
    """
    if not isinstance(cls, type) or not issubclass(cls, Plugin):
        raise PluginContractError(
            f"{cls!r} is not a climbcv.Plugin subclass. Your entry class must be:\n\n"
            "    from climbcv import Plugin\n\n"
            "    class MyPlugin(Plugin):\n        ...\n"
        )
    if "__init__" in cls.__dict__:
        raise PluginContractError(
            f"{cls.__name__} defines __init__, which climb-cv reserves.\n\n"
            "climb-cv binds self.config, self.log, self.publish and the rest AFTER "
            "constructing your plugin, so inside __init__ none of them exist yet — "
            "self.config.get(...) there fails, which is what everyone tries first.\n\n"
            "Move that code to setup(). It runs in this plugin's own process, once, with "
            "everything available, and it is where your model or device should be created "
            "anyway."
        )
    # A class-body override is quieter than the setup() case: because the framework binds its
    # members onto the INSTANCE, an instance attribute shadows the author's method, so their
    # override never runs and their calls silently reach framework code instead.
    shadowed = sorted(RESERVED_NAMES & set(cls.__dict__) - {"setup", "teardown"})
    if shadowed:
        raise PluginContractError(
            f"{cls.__name__} defines {shadowed}, which climb-cv reserves and binds onto "
            "each instance. Your definition would never be called — the framework's binding "
            "shadows it — so this fails silently rather than loudly.\n\n"
            f"Reserved: {sorted(RESERVED_NAMES)}. Any future framework member will be "
            "prefixed 'cv_', so avoiding that prefix keeps you safe as the base class grows."
        )
