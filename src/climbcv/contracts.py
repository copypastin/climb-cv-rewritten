"""Payload contracts — the types that cross topic boundaries.

Imports: dataclasses, numpy, typing — NOTHING ELSE, EVER. Under `spawn` this module is
re-imported in every child process; a heavy import here multiplies across the process tree
and lands in every plugin author's environment. See design/broker.md §5.3.

Every docstring in this module is a public contract. See design/payloads.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, ClassVar

import numpy as np

__all__ = [
    "Meta",
    "Frame",
    "PoseFrame",
    "HoldBoxes",
    "Scalar",
    "Shutdown",
    "Status",
    "Record",
    "STATUS_STATES",
    "TOPOLOGY_SIZES",
]


# --------------------------------------------------------------------------- immutability
#
# design/payloads.md §2.2.1. This mechanism has been specified wrongly twice, and both
# wrong versions read as correct. Do not simplify it without running
# tests/test_contracts.py, whose pickle-round-trip assertions are the acceptance criterion.


def _own_readonly(arr: np.ndarray) -> np.ndarray:
    """C-contiguous, OWNS its buffer, flagged read-only.

    Ownership is tested with flags["OWNDATA"], NOT `arr.base is not None`:
    np.ascontiguousarray returns the SAME object for a contiguous view, so an ownership
    test built on it never copies and the payload keeps a live view onto a caller-owned
    buffer — which a publisher can then mutate before the feeder thread has serialised it.
    """
    if not arr.flags["OWNDATA"] or not arr.flags["C_CONTIGUOUS"]:
        arr = np.array(arr, copy=True, order="C")
    arr.flags.writeable = False
    return arr


def _own_readonly_tree(obj: Any) -> Any:
    """Recursive form, for Record.data's nested ndarray leaves."""
    if isinstance(obj, np.ndarray):
        return _own_readonly(obj)
    if isinstance(obj, dict):
        return {k: _own_readonly_tree(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return tuple(_own_readonly_tree(v) for v in obj)
    return obj


class _Arrays:
    """Mixin. Declares which fields hold arrays and provides the adopt step.

    It must NOT define __setstate__ — see @payload below.
    """

    _ARRAY_FIELDS: ClassVar[tuple[str, ...]] = ()  # top-level ndarray fields
    _TREE_FIELDS: ClassVar[tuple[str, ...]] = ()  # fields whose contents are walked

    def _adopt_arrays(self) -> None:
        for name in self._ARRAY_FIELDS:
            arr = getattr(self, name)
            if arr is not None:
                object.__setattr__(self, name, _own_readonly(arr))
        for name in self._TREE_FIELDS:
            obj = getattr(self, name)
            if obj is not None:
                object.__setattr__(self, name, _own_readonly_tree(obj))


def payload(cls):
    """Apply OUTSIDE @dataclass:  @payload over @dataclass(frozen=True, slots=True).

    @dataclass(frozen=True, slots=True) installs its own __setstate__ into the class
    __dict__, which SHADOWS an inherited one: CPython's dataclasses._add_slots guards on
    `if '__setstate__' not in cls_dict`, and an inherited method is not in cls_dict. So the
    mixin cannot carry __setstate__. This wraps whatever dataclasses installed rather than
    reimplementing it, so it cannot drift with the CPython version.
    """
    inner = cls.__setstate__

    def __setstate__(self, state):
        inner(self, state)
        self._adopt_arrays()

    cls.__setstate__ = __setstate__
    return cls


# --------------------------------------------------------------------------- topologies

TOPOLOGY_SIZES: dict[str, int] = {
    "mediapipe.pose.33": 33,
    "coco.17": 17,
}
"""Landmark count per topology id. A PoseFrame's world/image row count must match.

design/payloads.md §4. TOPOLOGY_EDGES (guardian F-5) is not yet populated — the skeleton
edge list must be transcribed from an authoritative source rather than recalled, or every
visualiser inherits a wrong skeleton from us instead of hardcoding its own.
"""

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*/[0-9]+$")


# --------------------------------------------------------------------------- envelope


@dataclass(frozen=True, slots=True)
class Meta:
    """Provenance the author never constructs — the runtime wraps every published payload.

    topic         which topic this arrived on (a handler may serve several).
    source        publishing plugin id. FRAMEWORK-INJECTED, not author-supplied: a plugin
                  cannot forge it. This is what makes shared topics workable (filter or
                  colour by detector) and every log line attributable.
    seq           per (publisher, topic), monotonic from 0. Gives subscribers free drop
                  detection: a gap means the framework conflated, which is normal and now
                  observable. Because it is per (publisher, topic), drop detection MUST be
                  keyed by meta.source — comparing seq across sources is meaningless, and a
                  decrease means "that source restarted", not reordering.
    t_publish_ns  time.monotonic_ns() at publish.
    """

    topic: str
    source: str
    seq: int
    t_publish_ns: int


# --------------------------------------------------------------------------- payloads


@payload
@dataclass(frozen=True, slots=True)
class Frame(_Arrays):
    """One captured video frame. Topic `frame`, schema `frame/1`.

    seq           monotonically increasing from 0 for the run. The canonical frame identity;
                  every downstream payload derived from this frame carries frame_seq == this.
    t_capture_ns  time.monotonic_ns() sampled immediately after the frame was read from the
                  device. Monotonic — comparable across processes on the same machine, NOT
                  wall-clock, NOT comparable across runs.
    pixels        np.uint8, shape (H, W, 3), C-contiguous, READ-ONLY.
                  Indexed pixels[row, col, channel] — ORIGIN TOP-LEFT, row-major, so
                  pixels[y, x] is the pixel at image coordinate (x, y). H, W are whatever
                  the source produced; subscribers must not assume a fixed size and must not
                  assume it is constant across a run.
                  To modify, take a writable copy — as_bgr()/as_rgb() already return one.
    color         "bgr" | "rgb" | "gray" — declared, never assumed. "gray" still carries
                  shape (H, W, 3) with the channel replicated, so every subscriber has one
                  code path.
    mirrored      True if horizontally flipped relative to the physical scene. Left/right
                  body semantics depend on this; a subscriber computing "which hand" MUST
                  read it. Copied onto PoseFrame by any pose publisher, so a pose-only
                  subscriber does not have to subscribe to `frame` to get it.
    source        informational, e.g. "camera:0", "file:/path/to.mp4". Never parsed for
                  behaviour.
    """

    seq: int
    t_capture_ns: int
    pixels: np.ndarray
    color: str
    mirrored: bool
    source: str

    _ARRAY_FIELDS = ("pixels",)

    def __post_init__(self) -> None:
        if self.color not in ("bgr", "rgb", "gray"):
            raise ValueError(
                f"Frame.color must be 'bgr', 'rgb' or 'gray', got {self.color!r}"
            )
        px = np.asarray(self.pixels)
        if px.dtype != np.uint8:
            raise ValueError(f"Frame.pixels must be np.uint8, got {px.dtype}")
        if px.ndim != 3 or px.shape[2] != 3:
            raise ValueError(
                f"Frame.pixels must have shape (H, W, 3), got {px.shape}. "
                "'gray' frames replicate the channel so every subscriber has one code path."
            )
        object.__setattr__(self, "pixels", px)
        self._adopt_arrays()

    def as_rgb(self) -> np.ndarray:
        """A NEW, writable, C-contiguous np.uint8 array of shape (H, W, 3), in RGB order.

        Never returns self.pixels and never returns a view — including when self.color is
        already "rgb", where the implementation is a copy. The caller owns the result.
        """
        if self.color == "rgb" or self.color == "gray":
            return self.pixels.copy()
        return np.ascontiguousarray(self.pixels[..., ::-1])

    def as_bgr(self) -> np.ndarray:
        """A NEW, writable, C-contiguous np.uint8 array of shape (H, W, 3), in BGR order.

        Same contract as as_rgb().
        """
        if self.color == "bgr" or self.color == "gray":
            return self.pixels.copy()
        return np.ascontiguousarray(self.pixels[..., ::-1])


@payload
@dataclass(frozen=True, slots=True)
class PoseFrame(_Arrays):
    """One skeleton estimate for one frame. Topics `pose.raw`/`pose.smoothed`, schema `pose/1`.

    frame_seq     the Frame.seq this was computed from. -1 if not derived from a live frame
                  (e.g. a replay source publishing recorded poses).
    t_capture_ns  copied from the originating Frame, so end-to-end latency is measurable.
    topology      names the index semantics of both arrays, e.g. "mediapipe.pose.33". This
                  is what makes a different landmark count safe instead of silent.
    mirrored      copied from the originating Frame.mirrored. True if the image this was
                  estimated from was horizontally flipped relative to the physical scene, so
                  the subject's anatomical LEFT appears on the image-right. A subscriber
                  computing "which hand" or "which foot" MUST read it. It lives on this type
                  so a pose-only subscriber need not also subscribe to `frame`.
    world         np.float32, shape (L, 4), columns (visibility, x, y, z). READ-ONLY.
                  L == the landmark count implied by `topology`.
                  METRES. Origin at the midpoint between the hips. Axes: +x IMAGE-right,
                  +y DOWN, +z toward the camera. "Image-right" is a frame direction, not an
                  anatomical one: combine with `mirrored` for anatomical left/right.
                  z is weakly calibrated — usable for relative motion, not absolute depth.
                  visibility in [0, 1].
    image         np.float32, shape (L, 4), columns (visibility, x, y, z), or None. READ-ONLY.
                  NORMALISED to the source frame, ORIGIN TOP-LEFT: x in [0, 1] as a fraction
                  of width measured rightward, y in [0, 1] as a fraction of height measured
                  DOWNWARD. Multiply by (W, H) for pixel coordinates in the same convention
                  Frame.pixels uses.
    smoothed      True if a smoothing stage has processed this.
    """

    frame_seq: int
    t_capture_ns: int
    topology: str
    mirrored: bool
    world: np.ndarray
    image: np.ndarray | None
    smoothed: bool

    _ARRAY_FIELDS = ("world", "image")

    def __post_init__(self) -> None:
        expected = TOPOLOGY_SIZES.get(self.topology)
        if expected is None:
            raise ValueError(
                f"PoseFrame.topology {self.topology!r} is not a known topology. "
                f"Known: {sorted(TOPOLOGY_SIZES)}"
            )
        for name in ("world", "image"):
            arr = getattr(self, name)
            if arr is None:
                continue
            arr = np.asarray(arr)
            if arr.dtype != np.float32:
                raise ValueError(
                    f"PoseFrame.{name} must be np.float32, got {arr.dtype}"
                )
            if arr.shape != (expected, 4):
                raise ValueError(
                    f"PoseFrame.{name} must have shape ({expected}, 4) for topology "
                    f"{self.topology!r}, got {arr.shape}"
                )
            if not np.isfinite(arr).all():
                raise ValueError(
                    f"PoseFrame.{name} contains NaN or inf. A pose stage that has no "
                    "estimate must not publish, rather than publishing non-finite values "
                    "that propagate silently through every subscriber."
                )
            object.__setattr__(self, name, arr)
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class HoldBoxes(_Arrays):
    """Detected climbing holds for one frame. Topic `holds.boxes` (SHARED), schema `holds.boxes/1`.

    Additive: several detectors may publish for the same frame_seq. Use Meta.source to tell
    them apart — that is what makes this topic safe to share.

    frame_seq  the Frame.seq these were detected on. Detectors running every Nth frame report
               the frame they actually ran on, not the current one. Also how a consumer
               expires a stale source from latest_by_source().
    boxes      np.float32, shape (N, 4), rows (x1, y1, x2, y2). READ-ONLY.
               NORMALISED to the frame, ORIGIN TOP-LEFT — the same convention as
               Frame.pixels and PoseFrame.image.
               Values are CLIPPED to [0, 1] at construction, silently. x1 <= x2 and
               y1 <= y2 are ENFORCED and raise.
               N may be 0; shape is then (0, 4), never None.
    labels     tuple[str, ...], length N. Free-form class names from the detector's own
               vocabulary. NOT an enum — do not switch on these across plugins.
    scores     np.float32, shape (N,), each in [0, 1]. Comparable only within one detector.
    """

    frame_seq: int
    boxes: np.ndarray
    labels: tuple[str, ...]
    scores: np.ndarray

    _ARRAY_FIELDS = ("boxes", "scores")

    def __post_init__(self) -> None:
        boxes = np.asarray(self.boxes, dtype=np.float32)
        if boxes.ndim != 2 or boxes.shape[1] != 4:
            raise ValueError(
                f"HoldBoxes.boxes must have shape (N, 4), got {boxes.shape}"
            )
        # Clip the range, raise on the ordering: a box a pixel outside the frame is a normal
        # detector artifact and the framework handles it; x1 > x2 is a genuine bug and must
        # not be silently repaired.
        bad = np.nonzero((boxes[:, 0] > boxes[:, 2]) | (boxes[:, 1] > boxes[:, 3]))[0]
        if bad.size:
            raise ValueError(
                f"HoldBoxes.boxes requires x1 <= x2 and y1 <= y2; row(s) {bad.tolist()} "
                "violate that. Out-of-range values are clipped silently, but reversed "
                "corners indicate a coordinate-order bug in the detector."
            )
        boxes = np.clip(boxes, 0.0, 1.0)

        scores = np.asarray(self.scores, dtype=np.float32)
        n = boxes.shape[0]
        if scores.shape != (n,):
            raise ValueError(
                f"HoldBoxes.scores must have shape ({n},) to match boxes, got {scores.shape}"
            )
        if n and (scores.min() < 0.0 or scores.max() > 1.0):
            raise ValueError("HoldBoxes.scores must each be in [0, 1]")
        labels = tuple(self.labels)
        if len(labels) != n:
            raise ValueError(
                f"HoldBoxes.labels must have length {n} to match boxes, got {len(labels)}"
            )

        object.__setattr__(self, "boxes", boxes)
        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "labels", labels)
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Scalar(_Arrays):
    """A timestamped numeric reading whose cadence is unrelated to the frame rate.

    Topic `device.lid_angle` and any author-declared scalar topic. Schema `scalar/1`.

    value  float. UNIT IS FIXED BY THE TOPIC, not by this payload. Every scalar topic
           declares `unit` in its descriptor (a required manifest key), it is shown by
           `climbcv topics`, and two publishers declaring the same topic with different
           units is a startup error. For device.lid_angle: unit = "degree", 0 == closed.
    t_ns   time.monotonic_ns() at the instant of MEASUREMENT, not at publish. For a sensor
           read through a subprocess this is meaningfully earlier than publish, and
           consumers must be able to reason about the staleness.
    """

    value: float
    t_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", float(self.value))
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Shutdown(_Arrays):
    """A request to stop the run in an orderly way. Topic `app.shutdown`, EVENT kind.

    Any plugin may publish it; the supervisor acts on it. `reason` is shown to the user
    verbatim, e.g. "ESC pressed". Publishing this is a request, not a guarantee — do not
    rely on being killed.
    """

    reason: str

    def __post_init__(self) -> None:
        self._adopt_arrays()


STATUS_STATES = frozenset(
    {
        "ready",  # setup() returned; handlers are running
        "restarted",  # a new process was spawned after a crash
        "crashed",  # the process exited unexpectedly; a restart may follow
        "quarantined",  # no further restarts will be attempted
        "stalled",  # a handler has not returned within heartbeat_warn_s
        "unavailable",  # the plugin declared itself not applicable here
        "finished",  # the plugin declared its work complete
        "shutdown",  # orderly shutdown has begun for this plugin
    }
)
"""Closed set, validated at construction. Adding a state is an additive framework minor;
renaming one is breaking. Subscribers MUST ignore an unrecognised state rather than raising
— that is the subscriber's half of the forward-compatibility bargain."""


@payload
@dataclass(frozen=True, slots=True)
class Status(_Arrays):
    """Framework lifecycle notification. Topic `app.status`, EVENT kind, schema `status/1`.

    Emitted ONLY by the framework; plugins subscribe to build a status UI.

    plugin_id  the plugin this statement is ABOUT — a SUBJECT, not a sender. Because it is
               an ordinary payload field it is author-supplied and therefore forgeable: a
               plugin could publish Status(plugin_id="core.capture", state="crashed").
               Only Meta.source names the sender, and it is framework-injected.
               Framework-emitted Status always has Meta.source == "<host>", so a status UI
               that cares MUST check it. Not a security boundary — a correctness one.
    state      one of STATUS_STATES, validated at construction.
    detail     free-form human-readable context; may be "".
    """

    plugin_id: str
    state: str
    detail: str

    def __post_init__(self) -> None:
        if self.state not in STATUS_STATES:
            raise ValueError(
                f"Status.state {self.state!r} is not a known state. "
                f"Known: {sorted(STATUS_STATES)}"
            )
        self._adopt_arrays()


_RECORD_MAX_DEPTH = 8
_RECORD_MAX_LEAVES = 10_000


def _validate_record_data(obj: Any, depth: int, leaves: list[int], path: str) -> None:
    if depth > _RECORD_MAX_DEPTH:
        raise ValueError(
            f"Record.data nests deeper than {_RECORD_MAX_DEPTH} at {path}"
        )
    if isinstance(obj, np.ndarray):
        leaves[0] += 1  # an array counts as one leaf regardless of size
        return
    if isinstance(obj, (str, int, float, bool, type(None))):
        leaves[0] += 1
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"Record.data keys must be str; {path} has a {type(k).__name__} key"
                )
            _validate_record_data(v, depth + 1, leaves, f"{path}[{k!r}]")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _validate_record_data(v, depth + 1, leaves, f"{path}[{i}]")
        return
    raise ValueError(
        f"Record.data may contain only str, int, float, bool, None, list/tuple, dict with "
        f"str keys, and np.ndarray. {path} is a {type(obj).__name__}. Your own dataclasses, "
        "enums, Paths, datetimes and numpy scalars are not allowed: a plugin-defined class "
        "pickles under the publishing plugin's module name and would resolve against a "
        "DIFFERENT plugin's module in the subscriber — silently, into the wrong class."
    )


@payload
@dataclass(frozen=True, slots=True)
class Record(_Arrays):
    """An observation the framework has no type for, carried as plain data. Schema `record/1`.

    The escape hatch that makes the topic vocabulary open. Publish a Record on a topic you
    declare yourself when none of the typed contracts fits — a route graph, a move
    classification, a per-hand grip estimate, a depth map.

    kind   a versioned id the publishing plugin owns, "<vendor>.<thing>/<n>", e.g.
           "acme.route_graph/1". REQUIRED, and it must match the topic's `record_kind`
           manifest key: `schema` is "record/1" for every record topic in the ecosystem, so
           `kind` is the only thing that can distinguish two incompatible data layouts. The
           framework compares it at wiring time and at publish.
    t_ns   time.monotonic_ns() at the instant of observation.
    data   a dict with str keys, containing ONLY: str, int, float, bool, None; list or tuple
           of the above (returned as tuple); dict with str keys of the above; np.ndarray
           (any dtype, normalised C-contiguous and READ-ONLY, recursively, wherever it
           appears). Anything else raises. Validated recursively at construction: max
           nesting depth 8, max 10_000 non-array leaves.
    """

    kind: str
    t_ns: int
    data: dict

    _TREE_FIELDS = ("data",)

    def __post_init__(self) -> None:
        if not _KIND_RE.match(self.kind):
            raise ValueError(
                f"Record.kind must be '<name>/<major>', e.g. 'acme.route_graph/1'; "
                f"got {self.kind!r}"
            )
        if not isinstance(self.data, dict):
            raise ValueError(
                f"Record.data must be a dict, got {type(self.data).__name__}"
            )
        leaves = [0]
        _validate_record_data(self.data, 0, leaves, "data")
        if leaves[0] > _RECORD_MAX_LEAVES:
            raise ValueError(
                f"Record.data has {leaves[0]} leaves, over the {_RECORD_MAX_LEAVES} limit"
            )
        self._adopt_arrays()


CONTRACT_TYPES: tuple[type, ...] = (
    Frame,
    PoseFrame,
    HoldBoxes,
    Scalar,
    Shutdown,
    Status,
    Record,
)
"""Every payload type, for the reflection test in tests/test_contracts.py."""


def _declared_array_fields_are_complete() -> list[str]:
    """Names of ndarray-annotated fields missing from their type's _ARRAY_FIELDS.

    The check that catches the next forgotten declaration. Used by the test suite.
    """
    missing = []
    for cls in CONTRACT_TYPES:
        declared = set(cls._ARRAY_FIELDS) | set(cls._TREE_FIELDS)
        for f in fields(cls):
            ann = str(f.type).replace("np.", "").replace("numpy.", "")
            if "ndarray" in ann and f.name not in declared:
                missing.append(f"{cls.__name__}.{f.name}")
    return missing
