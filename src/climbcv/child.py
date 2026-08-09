"""The child process — one per plugin.

design/isolation.md §3. One thread, no locks: the plugin author never writes a concurrency
primitive because there is nothing here to coordinate. The loop drains inbound queues,
dispatches handlers, fires timers, and sends a heartbeat.

Everything that crosses the boundary into this process is package-level (this module's
`child_main`) plus dataclasses and plain dicts, so `spawn` works everywhere — including from
a Jupyter notebook, where `__main__` has no `__file__`.
"""

from __future__ import annotations

import importlib
import logging
import os
import logging.handlers
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import Meta
from .plugin import (
    Finished,
    PluginContractError,
    Unavailable,
    check_contract,
    collect_handlers,
)

__all__ = ["ChildSpec", "child_main", "READY", "CRASHED", "FINISHED", "UNAVAILABLE", "HEARTBEAT"]

READY = "ready"
CRASHED = "crashed"
FINISHED = "finished"
UNAVAILABLE = "unavailable"
HEARTBEAT = "heartbeat"
CONTRACT_ERROR = "contract_error"

_HEARTBEAT_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class ChildSpec:
    """Everything a child needs, all of it picklable by construction.

    No plugin instance and no plugin class crosses the boundary — only the module name and
    class name, imported on the far side. That is what keeps `spawn` viable and what makes
    "no author ever pickles a model" true rather than aspirational.
    """

    plugin_id: str
    directory: str
    module: str
    class_name: str
    config: dict
    config_dir: str | None
    data_dir: str
    log_level: str
    subscriptions: tuple[tuple[str, bool, int, str], ...]  # topic, conflate, depth, mode
    publishes: tuple[str, ...]
    heartbeat_warn_s: float
    teardown_timeout_s: float


class _Runtime:
    """The object behind every `self.x` on a Plugin. Lives only in the child."""

    def __init__(
        self,
        spec: ChildSpec,
        out_queues: dict[str, list[Any]],
        control: Any,
        shutdown_event: Any,
        log: logging.Logger,
    ) -> None:
        self.spec = spec
        self.config = dict(spec.config)
        self.config_dir = spec.config_dir
        self.data_dir = spec.data_dir
        self.log = log
        self._out = out_queues
        self._control = control
        self._shutdown = shutdown_event
        self._seq: dict[str, int] = {t: 0 for t in spec.publishes}
        self._latest: dict[str, Any] = {}
        self._latest_by_source: dict[str, dict[str, Any]] = {}
        self._intervals: dict[str, float] = {}
        self._declared = set(spec.publishes)
        self._subscribed = {s[0] for s in spec.subscriptions}

    # -- the property that has to be a property (guardian-02 finding 4) ----------------
    @property
    def stopping(self) -> bool:
        """Backed by the shared shutdown Event, so it flips DURING a blocking handler.

        A plain bool cannot: this process is single-threaded, so the only code that could
        reassign it is the loop that is currently inside the handler asking.
        """
        return self._shutdown.is_set()

    def publish(self, topic: str, payload: Any) -> None:
        if topic not in self._declared:
            raise PluginContractError(
                f"{self.spec.plugin_id} published {topic!r}, which it does not declare.\n\n"
                "Add it to the manifest so the framework can wire it — it builds the graph "
                "from manifests and never imports your code to find out:\n\n"
                "    [[publishes]]\n"
                f'    topic = "{topic}"\n\n'
                f"Declared: {sorted(self._declared) or 'nothing'}"
            )
        meta = Meta(
            topic=topic,
            source=self.spec.plugin_id,
            seq=self._seq[topic],
            t_publish_ns=time.monotonic_ns(),
        )
        self._seq[topic] += 1
        for q in self._out.get(topic, ()):
            try:
                q.put_nowait((meta, payload))
            except queue.Full:
                # Drop the OLDEST, not the newest. A conflating stream means "the latest value
                # wins", so discarding the message we are holding would keep stale frames and
                # throw away fresh ones -- the exact opposite of the intent, and invisible
                # except as mysterious latency. Never block the publisher either way.
                try:
                    q.get_nowait()
                    q.put_nowait((meta, payload))
                except (queue.Empty, queue.Full):
                    pass

    def latest(self, topic: str) -> Any | None:
        self._check_subscribed(topic, "latest")
        return self._latest.get(topic)

    def latest_by_source(self, topic: str) -> dict[str, Any]:
        self._check_subscribed(topic, "latest_by_source")
        return dict(self._latest_by_source.get(topic, {}))

    def _check_subscribed(self, topic: str, what: str) -> None:
        if topic not in self._subscribed:
            raise PluginContractError(
                f"{self.spec.plugin_id} called {what}({topic!r}) but does not subscribe to "
                f"it, so nothing is ever delivered and this would return empty forever.\n\n"
                "    [[subscribes]]\n"
                f'    topic = "{topic}"\n'
                '    mode  = "latest"     # "I read this with latest(), not a handler"\n\n'
                f"Subscribed: {sorted(self._subscribed) or 'nothing'}"
            )

    def set_interval(self, handler: Callable | str, seconds: float) -> None:
        name = handler if isinstance(handler, str) else handler.__name__
        if seconds < 0:
            raise PluginContractError(f"set_interval({name!r}, {seconds}) must be >= 0")
        self._intervals[name] = float(seconds)


def _send(control: Any, plugin_id: str, kind: str, detail: str = "") -> None:
    try:
        control.put_nowait((plugin_id, kind, detail))
    except Exception:  # the host is gone; nothing useful left to do
        pass


_SETUP_STACK_DUMP_S = float(os.environ.get("CLIMBCV_SETUP_STACK_DUMP_S", "20") or 20)
"""Seconds before a still-running setup() dumps its stack. Diagnostic only, never fatal.

Env-tunable because the child is spawned: a value set in the parent process does not reach it,
and the whole point is to be able to shorten the wait while chasing a hang.
"""


def child_main(
    spec: ChildSpec,
    in_queues: dict[str, Any],
    out_queues: dict[str, list[Any]],
    control: Any,
    log_queue: Any,
    shutdown_event: Any,
) -> None:
    """Child entry point. Must be importable by name for `spawn`."""
    root = logging.getLogger()
    root.handlers[:] = [logging.handlers.QueueHandler(log_queue)]
    root.setLevel(spec.log_level)
    log = logging.getLogger(spec.plugin_id)

    # The plugin's own directory goes first so `entry = "plugin:Thing"` resolves, and its
    # vendor/ dir after, so a vendored dependency is importable without touching site-packages.
    # Never block our own exit flushing messages nobody will read. Without this, a child whose
    # subscriber has already exited hangs in Queue's feeder-thread join at interpreter shutdown;
    # the supervisor then has to terminate it, and terminating a process that holds a queue lock
    # wedges that queue for the host too -- turning one slow plugin into a frozen app.
    for _targets in out_queues.values():
        for _q in _targets:
            _q.cancel_join_thread()
    # NOT the control or log queues. They are unbounded and the host drains them, so their
    # feeder cannot block -- and they carry the one message that must never be lost: this
    # plugin's own exit report. Cancelling their join discards it, which makes a plugin that
    # cleanly declared itself unavailable look like it is still starting, forever.

    sys.path.insert(0, spec.directory)
    vendor = Path(spec.directory) / "vendor"
    if vendor.is_dir():
        sys.path.insert(1, str(vendor))

    try:
        module = importlib.import_module(spec.module)
        cls = getattr(module, spec.class_name, None)
        if cls is None:
            raise PluginContractError(
                f"{spec.module}.py has no class named {spec.class_name!r}. "
                f"entry says \"{spec.module}:{spec.class_name}\"; defined here: "
                f"{[n for n in vars(module) if not n.startswith('_')]}"
            )
        check_contract(cls)
        handlers_by_topic, timers = collect_handlers(cls)

        # A handler bound to a topic the manifest does not declare would never fire.
        undeclared = sorted(set(handlers_by_topic) - {s[0] for s in spec.subscriptions})
        if undeclared:
            raise PluginContractError(
                f"{spec.class_name} has @subscribe handlers for {undeclared}, which the "
                "manifest does not list in [[subscribes]] — so they would never fire. The "
                "manifest is what the framework wires from."
            )

        instance = cls()
        rt = _Runtime(spec, out_queues, control, shutdown_event, log)
        object.__setattr__(instance, "_rt", rt)

        Path(spec.data_dir).mkdir(parents=True, exist_ok=True)

        before = set(vars(instance))

        # If setup() overruns, dump this child's stack to stderr and keep waiting.
        #
        # A plugin that hangs in setup() is otherwise completely opaque: it never reports
        # READY, it is still alive so the supervisor's dead-child reap does not fire, and it
        # has produced no log line because it never got far enough to log. The supervisor can
        # say "it is still starting" and nothing more. Only the child itself knows where it is
        # blocked, and this is the one cheap way to ask it.
        #
        # Deliberately does NOT exit: a slow setup is legitimate (a model download, a device
        # warming up), and F-16 is right that no threshold can separate slow from hung. So this
        # diagnoses without intervening -- print the stack, carry on, let the operator decide.
        _watchdog = None
        try:
            import faulthandler

            timeout = float(getattr(spec, "setup_warn_s", 0) or 0) or _SETUP_STACK_DUMP_S
            faulthandler.dump_traceback_later(timeout, exit=False, file=sys.stderr)
            _watchdog = faulthandler
        except Exception:  # noqa: BLE001 — diagnostics must never break the run
            _watchdog = None

        try:
            instance.setup()
        finally:
            if _watchdog is not None:
                _watchdog.cancel_dump_traceback_later()
        # S22: catch a member rebound during setup() while the message can still be clear.
        from .plugin import RESERVED_NAMES

        rebound = sorted(RESERVED_NAMES & (set(vars(instance)) - before))
        if rebound:
            raise PluginContractError(
                f"{spec.class_name}.setup() assigned to {rebound}, which climb-cv reserves. "
                f"Every later use would hit your value instead of the framework's — "
                f"self.latest = ... turns self.latest(topic) into "
                "\"'dict' object is not callable\" from inside a handler. Rename yours; a "
                "'cv_' prefix is reserved for the framework, everything else is yours."
            )

    except Unavailable as exc:
        _send(control, spec.plugin_id, UNAVAILABLE, exc.reason)
        return
    except PluginContractError as exc:
        _send(control, spec.plugin_id, CONTRACT_ERROR, str(exc))
        return
    except Finished as exc:
        _send(control, spec.plugin_id, FINISHED, exc.reason or "finished during setup")
        return
    except BaseException:
        _send(control, spec.plugin_id, CRASHED, traceback.format_exc())
        return

    _send(control, spec.plugin_id, READY)

    # The heartbeat runs on a framework-owned daemon thread, NOT from the dispatch loop.
    # A source is written as `@every(0)` with a blocking read inside -- the design explicitly
    # blesses that shape -- so a loop-driven heartbeat would never fire while such a plugin was
    # doing exactly what it is supposed to, and the supervisor would report it stalled while it
    # was perfectly healthy. Verified: core.capture tripped the stall warning on its first run.
    # This does not leak concurrency into the authoring model: no author writes or sees it.
    def _beat() -> None:
        # POLL is_set(), never wait(). A child that calls Event.wait() registers itself as a
        # sleeper on the shared multiprocessing Condition; if the supervisor then has to
        # terminate that child, the host's own Event.set() blocks forever inside notify_all(),
        # waiting for an acknowledgement from a process that no longer exists. That deadlocks
        # the supervisor -- the one process whose survival everything depends on -- and it was
        # reproducible here: `stop()` hung in synchronize.py notify() with four plugins gone.
        # is_set() takes the lock only momentarily and never registers as a waiter.
        while not shutdown_event.is_set():
            time.sleep(_HEARTBEAT_INTERVAL_S)
            if shutdown_event.is_set():
                return
            _send(control, spec.plugin_id, HEARTBEAT)

    threading.Thread(target=_beat, name=f"beat-{spec.plugin_id}", daemon=True).start()

    intervals = {fn.__name__: sec for fn, sec in timers}
    intervals.update(rt._intervals)
    next_due = {name: time.monotonic() for name in intervals}
    exit_kind, exit_detail = FINISHED, "shutdown"

    try:
        while not shutdown_event.is_set():
            now = time.monotonic()
            # A zero-interval timer is a source: run it flat out and do not sleep.
            hot = any(intervals[n] == 0.0 for n in intervals)
            timeout = 0.0 if hot else _timeout_until(next_due, intervals, now)

            drained = _drain(in_queues, handlers_by_topic, instance, rt, timeout)

            now = time.monotonic()
            for name, interval in list(intervals.items()):
                if now < next_due.get(name, 0.0):
                    continue
                fn = getattr(instance, name)
                fn()
                # Measured from RETURN, and missed ticks are dropped rather than queued: a
                # handler that overruns its interval falls behind instead of accumulating a
                # backlog it can never work off.
                next_due[name] = time.monotonic() + interval

            if not hot and not intervals and not drained and not in_queues:
                # No inputs and no timers: nothing will ever wake us. Sleep rather than spin.
                time.sleep(0.05)
    except Finished as exc:
        exit_kind, exit_detail = FINISHED, exc.reason or "work complete"
    except Unavailable as exc:
        exit_kind, exit_detail = UNAVAILABLE, exc.reason
    except PluginContractError as exc:
        _send(control, spec.plugin_id, CONTRACT_ERROR, str(exc))
        return
    except BaseException:
        _send(control, spec.plugin_id, CRASHED, traceback.format_exc())
        return

    try:
        instance.teardown()
    except BaseException:
        log.warning("teardown() raised; continuing shutdown\n%s", traceback.format_exc())
    _send(control, spec.plugin_id, exit_kind, exit_detail)


def _timeout_until(next_due: dict[str, float], intervals: dict[str, float], now: float) -> float:
    if not next_due:
        # A plugin with inputs but no timers must still wake to send its heartbeat, or the
        # supervisor reports it stalled while it is perfectly healthy and merely idle.
        return _HEARTBEAT_INTERVAL_S / 2
    soonest = min(next_due.values())
    return max(0.0, min(soonest - now, _HEARTBEAT_INTERVAL_S / 2))


def _drain(
    in_queues: dict[str, Any],
    handlers_by_topic: dict[str, list[Callable]],
    instance: Any,
    rt: _Runtime,
    timeout: float,
) -> bool:
    """One pass over the inbound queues. Returns whether anything was handled.

    latest() is populated for everything drained BEFORE any handler runs, so a handler
    reading latest() of another topic sees this cycle's value rather than one a frame stale.
    """
    batch: list[tuple[str, Meta, Any]] = []
    first = True
    for topic, q in in_queues.items():
        while True:
            try:
                item = q.get(timeout=timeout) if first and timeout > 0 else q.get_nowait()
            except (queue.Empty, OSError):
                break
            first = False
            meta, payload = item
            batch.append((topic, meta, payload))
        first = False

    for topic, meta, payload in batch:
        rt._latest[topic] = payload
        rt._latest_by_source.setdefault(topic, {})[meta.source] = payload

    for topic, meta, payload in batch:
        for fn in handlers_by_topic.get(topic, ()):
            fn(instance, payload, meta)
    return bool(batch)
