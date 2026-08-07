"""The host — spawns children, wires queues, and keeps the app alive when a plugin does not.

design/isolation.md §4–§7. The host owns no pipeline work: it allocates queues from the
`WiringPlan`, hands endpoints to children at spawn, then supervises. Messages go
publisher-process -> subscriber-process directly, with no forwarding hop in the data path.

`spawn` is forced on every platform. Fork would inherit the parent's threads, GPU contexts and
signal handlers into a child that never asked for them, and the resulting failures are
platform-specific and awful to debug.
"""

from __future__ import annotations

import logging
import logging.handlers
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .child import (
    CONTRACT_ERROR,
    CRASHED,
    FINISHED,
    HEARTBEAT,
    READY,
    UNAVAILABLE,
    ChildSpec,
    child_main,
)
from .wiring import HOST, WiringPlan

__all__ = ["Supervisor", "PluginState"]

log = logging.getLogger("climbcv")

_EVENT_DEPTH = 256


@dataclass
class PluginState:
    plugin_id: str
    state: str = "starting"
    detail: str = ""
    crashes: tuple[float, ...] = ()
    restarts: int = 0
    last_beat: float = field(default_factory=time.monotonic)
    stall_warned: bool = False
    process: Any = None


class Supervisor:
    """Owns the process tree for one run.

    One concurrently running instance per process (Decision #24): two would each spawn
    `core.capture` and fight over camera 0, write the same per-plugin log files, hand the same
    state_dir to different children, both publish as `<host>` so nothing downstream could
    attribute a host publish, and both install signal handlers so Ctrl-C would stop only one.
    The guard is on *running*, not construction, so `stop()` then a fresh instance is fine —
    which is what a notebook user does twenty times an afternoon.
    """

    _running_lock = threading.Lock()
    _running: Supervisor | None = None

    def __init__(self, plan: WiringPlan, framework: dict[str, Any]) -> None:
        self.plan = plan
        self.framework = framework
        self.states: dict[str, PluginState] = {}
        self._ctx = mp.get_context("spawn")
        self._control: Any = None
        self._log_queue: Any = None
        self._shutdown: Any = None
        self._queues: dict[tuple[str, str], Any] = {}
        self._host_inbox: dict[str, Any] = {}
        self._host_callbacks: dict[str, list[Callable]] = {}
        self._log_thread: threading.Thread | None = None
        self._has_run = False
        self._stop_reason = ""

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with Supervisor._running_lock:
            if Supervisor._running is not None:
                raise RuntimeError(
                    "A ClimbCV is already running in this process. Two cannot run at once: "
                    "they would both open the camera, write the same log files, and install "
                    "competing signal handlers. Call stop() on the first one, then start "
                    "this one."
                )
            if self._has_run:
                raise RuntimeError(
                    "This ClimbCV has already run; construct a new one. Declarations are "
                    "frozen at start(), the queues are consumed and the child processes are "
                    "gone, so a second run would report nothing rather than working."
                )
            Supervisor._running = self
            self._has_run = True

        self._control = self._ctx.Queue()
        self._log_queue = self._ctx.Queue()
        self._shutdown = self._ctx.Event()

        self._log_thread = threading.Thread(
            target=self._pump_logs, name="climbcv-logs", daemon=True
        )
        self._log_thread.start()

        # The host must RETAIN every queue for the whole run. Under `spawn` a child rebuilds
        # the queue's semaphore from the pickled state *after* start() has returned, so if the
        # parent's last reference goes out of scope the resource tracker unlinks the semaphore
        # first and the child dies with `FileNotFoundError` from SemLock._rebuild — an error
        # that names nothing in this codebase and points nowhere near the cause.
        self._queues = self._allocate_queues()
        for pid, plan in self.plan.plugins.items():
            if pid == HOST:
                continue
            self.states[pid] = PluginState(pid)
            self._spawn(pid, self._queues)

    def stop(self, reason: str = "stop() called") -> None:
        """Signal shutdown and reap children. Idempotent."""
        if self._shutdown is None:
            return
        self._stop_reason = self._stop_reason or reason
        self._shutdown.set()

        grace = float(self.framework.get("grace_s", 2.0))
        for state in self.states.values():
            proc = state.process
            if proc is None:
                continue
            timeout = grace
            manifest_plan = self.plan.plugins.get(state.plugin_id)
            if manifest_plan is not None and manifest_plan.manifest is not None:
                timeout = max(grace, manifest_plan.manifest.teardown_timeout_s)
            proc.join(timeout=timeout)
            if proc.is_alive():
                log.warning(
                    "%s did not exit within %.1fs; terminating. Its teardown() may not have "
                    "completed — if it writes a file at the end, raise its "
                    "teardown_timeout_s.",
                    state.plugin_id, timeout,
                )
                proc.terminate()
                proc.join(timeout=1.0)

        with Supervisor._running_lock:
            if Supervisor._running is self:
                Supervisor._running = None

    def __enter__(self) -> Supervisor:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---------------------------------------------------------------- queues

    def _allocate_queues(self) -> dict[tuple[str, str], Any]:
        """One queue per (topic, subscriber). The host computes the whole graph up front, so
        exclusivity is settled before a process exists rather than raced for at runtime."""
        out: dict[tuple[str, str], Any] = {}
        for pid, plan in self.plan.plugins.items():
            for sub in plan.subscribes:
                if not self.plan.publishers.get(sub.topic):
                    continue  # absent topic, optional subscription: wire nothing
                depth = sub.depth if not sub.conflate else max(sub.depth, 1)
                if self.plan.topics[sub.topic].kind.value == "event":
                    depth = max(depth, _EVENT_DEPTH)
                q = self._ctx.Queue(maxsize=depth)
                out[(sub.topic, pid)] = q
                if pid == HOST:
                    self._host_inbox[sub.topic] = q
        return out

    def _spawn(self, plugin_id: str, queues: dict[tuple[str, str], Any]) -> None:
        plan = self.plan.plugins[plugin_id]
        manifest = plan.manifest
        assert manifest is not None

        in_queues = {
            topic: q for (topic, pid), q in queues.items() if pid == plugin_id
        }
        out_queues: dict[str, list[Any]] = {}
        for topic in plan.publishes:
            targets = [
                q for (t, pid), q in queues.items() if t == topic and pid != plugin_id
            ]
            out_queues[topic] = targets

        state_dir = Path(self.framework.get("state_dir", ".climbcv"))
        spec = ChildSpec(
            plugin_id=plugin_id,
            directory=str(manifest.directory),
            module=manifest.module,
            class_name=manifest.class_name,
            config=dict(plan.config),
            config_dir=None,
            data_dir=str(state_dir / plugin_id),
            log_level=str(self.framework.get("log_level", "INFO")),
            subscriptions=tuple(
                (s.topic, s.conflate, s.depth, s.mode) for s in plan.subscribes
            ),
            publishes=tuple(plan.publishes),
            heartbeat_warn_s=manifest.heartbeat_warn_s,
            teardown_timeout_s=manifest.teardown_timeout_s,
        )

        proc = self._ctx.Process(
            target=child_main,
            args=(spec, in_queues, out_queues, self._control, self._log_queue, self._shutdown),
            name=f"climbcv-{plugin_id}",
            daemon=True,
        )
        proc.start()
        self.states[plugin_id].process = proc
        self.states[plugin_id].last_beat = time.monotonic()

    # ---------------------------------------------------------------- supervision

    def poll(self, timeout: float = 0.0) -> None:
        """Process control messages and host deliveries. Call from the host's own loop.

        Exists so an embedder owning a GUI can drain on *their* thread rather than being
        handed callbacks from a framework thread they know nothing about.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                pid, kind, detail = self._control.get(timeout=max(0.0, deadline - time.monotonic()))
            except (queue.Empty, OSError):
                break
            self._handle_control(pid, kind, detail)
            if time.monotonic() >= deadline:
                break

        for topic, q in self._host_inbox.items():
            while True:
                try:
                    meta, payload = q.get_nowait()
                except (queue.Empty, OSError):
                    break
                for fn in self._host_callbacks.get(topic, ()):
                    fn(payload, meta)

        for pid in self.due_restarts():
            state = self.states[pid]
            state.state, state.detail = "starting", ""
            log.info("Restarting %s now.", pid)
            self._spawn(pid, self._queues)

        self._check_stalls()

    def _handle_control(self, plugin_id: str, kind: str, detail: str) -> None:
        state = self.states.get(plugin_id)
        if state is None:
            return
        now = time.monotonic()
        state.last_beat = now

        if kind == HEARTBEAT:
            if state.stall_warned:
                log.info("%s is responsive again.", plugin_id)
                state.stall_warned = False
            return

        if kind == READY:
            state.state, state.detail = "ready", ""
            log.info("%s is ready.", plugin_id)
            return

        if kind == UNAVAILABLE:
            state.state, state.detail = "unavailable", detail
            log.info(
                "%s is not available on this machine: %s. Its topics will be absent; "
                "plugins that declared those subscriptions optional carry on.",
                plugin_id, detail,
            )
            return

        if kind == FINISHED:
            state.state, state.detail = "finished", detail
            log.info("%s finished (%s). This is a normal end of run, not an error.",
                     plugin_id, detail or "no reason given")
            self._maybe_shutdown_for_absent_publisher(plugin_id, "finished")
            return

        if kind == CONTRACT_ERROR:
            # Deterministic: retrying produces the same failure with the useful message
            # buried under a restart log, and it never escalates to app shutdown.
            state.state, state.detail = "quarantined", detail
            log.error("%s has an authoring error and will not be retried:\n%s",
                      plugin_id, detail)
            return

        if kind == CRASHED:
            state.crashes = state.crashes + (now,)
            window = float(self.framework.get("quarantine_window_s", 60.0))
            recent = tuple(t for t in state.crashes if now - t <= window)
            state.crashes = recent
            limit = int(self.framework.get("quarantine_crashes", 5))
            log.error("%s crashed:\n%s", plugin_id, detail)
            if len(recent) >= limit:
                state.state, state.detail = "quarantined", detail
                log.error(
                    "%s crashed %d times in %.0fs and will not be restarted again. %s",
                    plugin_id, len(recent), window, self._what_is_lost(plugin_id),
                )
                self._maybe_shutdown_for_absent_publisher(plugin_id, "quarantined")
                return
            state.state = "crashed"
            state.restarts += 1
            backoff = min(
                float(self.framework.get("restart_backoff_min_s", 0.5)) * (2 ** (len(recent) - 1)),
                float(self.framework.get("restart_backoff_max_s", 30.0)),
            )
            log.info("Restarting %s in %.1fs (attempt %d).", plugin_id, backoff, state.restarts)
            # Restart is scheduled rather than immediate; the caller's next poll() picks it up.
            state.detail = f"restart_at:{now + backoff}"

    def _check_stalls(self) -> None:
        now = time.monotonic()
        for pid, state in self.states.items():
            if state.state not in ("ready", "crashed"):
                continue
            plan = self.plan.plugins.get(pid)
            limit = (
                plan.manifest.heartbeat_warn_s
                if plan is not None and plan.manifest is not None
                else 5.0
            )
            if not state.stall_warned and now - state.last_beat > limit:
                state.stall_warned = True
                log.warning(
                    "%s has not checked in for %.0fs. If it does a long blocking read this "
                    "is expected — raise its heartbeat_warn_s to silence this. climb-cv is "
                    "not going to intervene: killing a process mid-native-call risks leaving "
                    "a device in a bad state, and the rest of the app is unaffected.",
                    pid, now - state.last_beat,
                )

    def _what_is_lost(self, plugin_id: str) -> str:
        plan = self.plan.plugins.get(plugin_id)
        if plan is None or not plan.publishes:
            return ""
        orphaned = [
            t for t in plan.publishes
            if self.plan.publishers.get(t, ()) == (plugin_id,)
        ]
        if not orphaned:
            return ""
        return (
            f"{', '.join(repr(t) for t in orphaned)} now has no publisher, so whatever "
            "depended on it will stop appearing."
        )

    def _maybe_shutdown_for_absent_publisher(self, plugin_id: str, why: str) -> None:
        plan = self.plan.plugins.get(plugin_id)
        if plan is None:
            return
        for topic in plan.publishes:
            if self.plan.publishers.get(topic, ()) != (plugin_id,):
                continue
            requirers = [
                pid for pid, p in self.plan.plugins.items()
                if any(s.topic == topic and s.required for s in p.subscribes)
            ]
            if requirers:
                log.error(
                    "%s %s; it is the only publisher of %r, which %d plugin(s) require "
                    "(%s) — shutting down.",
                    plugin_id, why, topic, len(requirers), ", ".join(sorted(requirers)),
                )
                self.stop(f"{plugin_id} {why} and {topic!r} is required")
                return

    def due_restarts(self) -> list[str]:
        """Plugin ids whose backoff has elapsed. The caller respawns them."""
        now = time.monotonic()
        out = []
        for pid, state in self.states.items():
            if state.state == "crashed" and state.detail.startswith("restart_at:"):
                if now >= float(state.detail.split(":", 1)[1]):
                    out.append(pid)
        return out

    # ---------------------------------------------------------------- host callbacks

    def on(self, topic: str, fn: Callable) -> None:
        self._host_callbacks.setdefault(topic, []).append(fn)

    def _pump_logs(self) -> None:
        while True:
            try:
                record = self._log_queue.get()
            except (OSError, ValueError):
                return
            if record is None:
                return
            logging.getLogger(record.name).handle(record)
