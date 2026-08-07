"""`ClimbCV` — the host, and the embedding API.

design/plugin-api.md §7. The host is a *participant*, not an exemption: it subscribes and
publishes through the same resolution rules as any plugin, appears as `<host>` in
`climbcv topics`, and enters exclusive contention like anything else. Making it a participant
is what stops "the host is exempt from every rule" being true.

    app = ClimbCV()
    app.subscribe("pose.smoothed", on_pose, requires_topology="mediapipe.pose.33",
                  required=True)
    app.run()
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any, Callable

from .config import LoadedConfig, check_against_plugins, load_config
from .loader import discover
from .supervisor import Supervisor
from .wiring import Subscription, WiringError, resolve

__all__ = ["ClimbCV", "bundled_root"]

log = logging.getLogger("climbcv")


def bundled_root() -> Path:
    """The plugin root shipped with the distribution.

    Without this, `pip install climb-cv` and run gives no capture, no pose and no smoothing,
    because those are plugins now — four previously-default features would silently become
    opt-in. The bundled root also means the manifest parser is exercised on real manifests at
    every startup rather than being bypassed by synthetic in-code descriptors.
    """
    return Path(__file__).resolve().parent / "bundled"


class ClimbCV:
    """One run. Not reusable after `stop()`; construct a new one (Decision #24)."""

    def __init__(self, config: str | Path | dict | None = "climbcv.toml") -> None:
        self._cfg: LoadedConfig = load_config(config)
        self._host_subs: list[Subscription] = []
        self._host_pubs: list[str] = []
        self._callbacks: dict[str, list[Callable]] = {}
        self._topologies: dict[str, str] = {}
        self._sup: Supervisor | None = None
        self._frozen = False

        logging.basicConfig(
            level=self._cfg.framework["log_level"],
            format="%(levelname)s %(name)s: %(message)s",
        )
        for warning in self._cfg.warnings:
            log.warning("%s", warning)

    # ---------------------------------------------------------------- declarations

    def subscribe(
        self,
        topic: str,
        callback: Callable[[Any, Any], None],
        *,
        required: bool,
        requires_topology: str | None = None,
    ) -> None:
        """Receive a topic in the host process.

        `required` has no default deliberately. For a plugin it defaults to true, to catch a
        typo in an unvalidated manifest — but a host subscription is a string literal in code
        you can see, so that reasoning does not transfer, while the cost of guessing wrong is
        higher: default-false silently produces no callbacks forever, and default-true lets an
        optional telemetry callback end the run. Saying which you mean is one word.

        `requires_topology` raises here, at the call site, rather than at `run()` — your own
        traceback names the exact line, which no manifest error can do.
        """
        self._check_open()
        if requires_topology is not None:
            self._topologies[topic] = requires_topology
        elif topic.startswith("pose."):
            raise WiringError(
                f"ClimbCV.subscribe({topic!r}) needs requires_topology, because a pose "
                "payload's landmark indices are meaningless without it — index 11 is a "
                "shoulder under mediapipe.pose.33 and a hip under coco.17.\n\n"
                '    app.subscribe("pose.smoothed", on_pose, required=True,\n'
                '                  requires_topology="mediapipe.pose.33")\n\n'
                'If you do not index individual joints, pass requires_topology="any".'
            )
        self._host_subs.append(
            Subscription(topic=topic, conflate=True, depth=4, mode="handler",
                         required=required)
        )
        self._callbacks.setdefault(topic, []).append(callback)

    def publishes(self, topic: str) -> None:
        """Declare that the host will publish `topic` — e.g. supplying frames itself."""
        self._check_open()
        self._host_pubs.append(topic)

    def publish(self, topic: str, payload: Any) -> None:
        raise NotImplementedError(
            "Host publishing is declared but not yet implemented in this build. Declare it "
            "with publishes() and supply frames from a plugin for now."
        )

    def _check_open(self) -> None:
        if self._frozen:
            raise RuntimeError(
                "Declarations are frozen once the run starts — the topic graph is computed "
                "before any process is spawned, which is what lets exclusivity be enforced "
                "with a clear error instead of raced for at runtime."
            )

    # ---------------------------------------------------------------- running

    def plan(self):
        """Resolve the graph without starting anything. What `climbcv topics` prints."""
        discovery = discover(self._cfg, bundled_root=bundled_root())
        for warning in discovery.warnings:
            log.warning("%s", warning)
        for skip in discovery.skipped:
            log.log(
                logging.INFO if skip.level == "INFO" else logging.ERROR,
                "%s was skipped: %s", skip.identifier, skip.reason,
            )
        wiring = resolve(
            discovery, self._cfg,
            host_publishes=tuple(self._host_pubs),
            host_subscribes=tuple(self._host_subs),
        )
        # Skipped ids count as *found*: a plugin the user deliberately disabled, or one
        # skipped for its platform, is not a typo, and warning "configures a plugin that was
        # not found" about a section the user just wrote is worse than saying nothing.
        known_ids = {m.id for m in discovery.plugins} | {s.identifier for s in discovery.skipped}
        for warning in check_against_plugins(self._cfg, known_ids, set(wiring.topics)):
            log.warning("%s", warning)
        for warning in wiring.warnings:
            log.warning("%s", warning)
        return wiring

    def start(self) -> None:
        """Spawn everything and return. Callbacks arrive via `poll()`."""
        self._frozen = True
        wiring = self.plan()
        self._sup = Supervisor(wiring, self._cfg.framework)
        for topic, fns in self._callbacks.items():
            for fn in fns:
                self._sup.on(topic, fn)
        self._sup.start()

    def poll(self, timeout: float = 0.0) -> None:
        """Drain pending callbacks and supervision on the CALLER's thread.

        This is the method a Qt or Tk host wants: your callbacks run on the thread that calls
        this, so you can touch your own widgets from them. Being handed callbacks from a
        framework-owned thread is how GUI hosts crash.
        """
        if self._sup is not None:
            self._sup.poll(timeout)

    def run(self) -> None:
        """Start, then poll until stopped. Ctrl-C stops cleanly."""
        self.start()
        stopping = {"flag": False}

        def handle(_sig: int, _frame: Any) -> None:
            stopping["flag"] = True

        try:
            signal.signal(signal.SIGINT, handle)
            signal.signal(signal.SIGTERM, handle)
        except ValueError:
            pass  # not on the main thread; the embedder owns signals

        try:
            while not stopping["flag"]:
                self.poll(0.05)
                if self._sup is not None and not any(
                    s.process is not None and s.process.is_alive()
                    for s in self._sup.states.values()
                ):
                    log.info("every plugin has exited; shutting down.")
                    break
                time.sleep(0.005)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._sup is not None:
            self._sup.stop()

    @property
    def states(self) -> dict:
        return {} if self._sup is None else dict(self._sup.states)
