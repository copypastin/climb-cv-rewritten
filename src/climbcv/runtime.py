from __future__ import annotations

import logging
import queue
import time
from collections.abc import Callable
from typing import Any

from .contracts import Meta
from .plugin import PluginContractError

_HEARTBEAT_INTERVAL_S = 1.0


class _Runtime:
    """The object behind every `self.x` on a Plugin. Lives only in the child."""

    def __init__(
        self,
        spec: Any,  # Typed as Any to avoid circular import, or use TYPE_CHECKING
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

    @property
    def stopping(self) -> bool:
        return self._shutdown.is_set()

    def publish(self, topic: str, payload: Any) -> None:
        if topic not in self._declared:
            raise PluginContractError(f"{self.spec.plugin_id} published {topic!r} but did not declare it.")

        from .contracts import Frame, FrameRef
        if isinstance(payload, Frame):
            slot = self._seq[topic] % 4
            shm_name = f"climbcv_{self.spec.plugin_id}_frame_{slot}"
            try:
                from multiprocessing.shared_memory import SharedMemory
                import numpy as np
                shm = SharedMemory(name=shm_name)
                # Write to shared memory
                ndarray = np.ndarray(payload.pixels.shape, dtype=payload.pixels.dtype, buffer=shm.buf)
                np.copyto(ndarray, payload.pixels)
                shm.close()
                # Substitute the payload
                payload = FrameRef(
                    seq=payload.seq,
                    t_capture_ns=payload.t_capture_ns,
                    color=payload.color,
                    mirrored=payload.mirrored,
                    source=payload.source,
                    shape=payload.pixels.shape,
                    shm_name=shm_name
                )
            except Exception as e:
                # Fallback to standard queue if shared memory fails
                self.log.warning("Shared memory transport failed for frame: %s", e)

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
                pass

    def latest(self, topic: str) -> Any | None:
        self._check_subscribed(topic, "latest")
        return self._latest.get(topic)

    def latest_by_source(self, topic: str) -> dict[str, Any]:
        self._check_subscribed(topic, "latest_by_source")
        return dict(self._latest_by_source.get(topic, {}))

    def _check_subscribed(self, topic: str, what: str) -> None:
        if topic not in self._subscribed:
            raise PluginContractError(f"{self.spec.plugin_id} called {what}({topic!r}) without subscribing.")

    def set_interval(self, handler: Callable | str, seconds: float) -> None:
        name = handler if isinstance(handler, str) else handler.__name__
        if seconds < 0:
            raise PluginContractError(f"set_interval({name!r}, {seconds}) must be >= 0")
        self._intervals[name] = float(seconds)

def _timeout_until(next_due: dict[str, float], intervals: dict[str, float], now: float) -> float:
    if not next_due:
        return _HEARTBEAT_INTERVAL_S / 2
    soonest = min(next_due.values())
    return max(0.0, min(soonest - now, _HEARTBEAT_INTERVAL_S / 2))

def _drain(
    in_queues: dict[str, Any],
    handlers_by_topic: dict[str, list[Callable]],
    instance: Any,
    rt: _Runtime,
    timeout: float,
    track_fn: Callable[[str, Callable], None] | None = None,
) -> bool:
    """One pass over the inbound queues. Returns whether anything was handled.

    latest() is populated for everything drained BEFORE any handler runs, so a handler
    reading latest() of another topic sees this cycle's value rather than one a frame stale.

    Strategy: try all queues non-blocking first to avoid head-of-line stalls.
    If nothing is ready and a timeout is set, do one short blocking get on the first queue
    so the child wakes up immediately when any message arrives rather than spinning or
    sleeping for the full timeout interval.
    """
    batch: list[tuple[str, Meta, Any]] = []
    conflating_topics = {sub[0] for sub in rt.spec.subscriptions if sub[1]}
    
    # Bounded round-robin drain: prevent a continuously replenished queue from starving others
    MAX_DRAIN = 50
    did_work = False

    for topic, q in in_queues.items():
        conflate = topic in conflating_topics
        pulled = []
        for _ in range(MAX_DRAIN):
            try:
                pulled.append(q.get_nowait())
            except (queue.Empty, OSError):
                break
                
        if not pulled:
            continue
            
        if conflate:
            # Keep newest per publisher
            newest = {}
            for meta, payload in pulled:
                newest[meta.source] = (meta, payload)
            for meta, payload in newest.values():
                batch.append((topic, meta, payload))
        else:
            for meta, payload in pulled:
                batch.append((topic, meta, payload))

    # If nothing was ready and a timeout is requested, sleep briefly but check back.
    # We do a tiny bounded sleep rather than a full queue block so that if multiple queues
    # are pending, we remain responsive to whichever arrives first.
    if not batch and timeout > 0 and in_queues:
        import time
        time.sleep(min(timeout, 0.005))
        
        # Check one more time non-blocking after the tiny sleep
        for topic, q in in_queues.items():
            try:
                item = q.get_nowait()
                meta, payload = item
                batch.append((topic, meta, payload))
                # For simplicity after a tiny sleep, we just append the single item found.
            except (queue.Empty, OSError):
                pass

    for topic, meta, payload in batch:
        rt._latest[topic] = payload
        rt._latest_by_source.setdefault(topic, {})[meta.source] = payload

    for topic, meta, payload in batch:
        from .contracts import Frame, FrameRef
        if isinstance(payload, FrameRef):
            try:
                from multiprocessing.shared_memory import SharedMemory
                import numpy as np
                shm = SharedMemory(name=payload.shm_name)
                ndarray = np.ndarray(payload.shape, dtype=np.uint8, buffer=shm.buf)
                pixels = ndarray.copy()
                shm.close()
                payload = Frame(
                    seq=payload.seq,
                    t_capture_ns=payload.t_capture_ns,
                    pixels=pixels,
                    color=payload.color,
                    mirrored=payload.mirrored,
                    source=payload.source
                )
            except Exception as e:
                rt.log.warning("Failed to read frame from shared memory: %s", e)
                continue

        for fn in handlers_by_topic.get(topic, ()):
            if track_fn is not None:
                track_fn(f"handler {topic}", lambda: fn(instance, payload, meta))
            else:
                fn(instance, payload, meta)
    return bool(batch)
