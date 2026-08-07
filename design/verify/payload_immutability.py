"""Acceptance test for payloads.md §2.2.1, transcribed exactly as a reader would
implement it. Verifies the CORRECTED mechanism after guardian-02 finding 1.

Run: python3 verify_readonly.py
"""
import pickle, sys
from dataclasses import dataclass, fields
from typing import ClassVar
import numpy as np

# ---------------------------------------------------------------- §2.2.1 mechanism

def _own_readonly(arr: np.ndarray) -> np.ndarray:
    """Return a C-contiguous array that OWNS its buffer, flagged read-only.

    Ownership is tested with flags["OWNDATA"], NOT `arr.base is not None`:
    np.ascontiguousarray returns the SAME object for a contiguous view, so an
    ownership test that relies on it never copies and the payload keeps a view
    onto a caller-owned buffer.
    """
    if not arr.flags["OWNDATA"] or not arr.flags["C_CONTIGUOUS"]:
        arr = np.array(arr, copy=True, order="C")
    arr.flags.writeable = False
    return arr


def _own_readonly_tree(obj):
    """Recursive form, for Record.data's nested ndarray leaves."""
    if isinstance(obj, np.ndarray):
        return _own_readonly(obj)
    if isinstance(obj, dict):
        return {k: _own_readonly_tree(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return tuple(_own_readonly_tree(v) for v in obj)
    return obj


class _Arrays:
    """Mixin. Declares which fields hold arrays; provides the adopt step.

    It must NOT define __setstate__ -- see the `payload` decorator below.
    """
    _ARRAY_FIELDS: ClassVar[tuple[str, ...]] = ()   # top-level ndarray fields
    _TREE_FIELDS: ClassVar[tuple[str, ...]] = ()    # fields whose contents are walked

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
    """Apply OUTSIDE @dataclass:  @payload  over  @dataclass(frozen=True, slots=True).

    @dataclass(frozen=True, slots=True) installs its own __setstate__ into the
    class __dict__, which SHADOWS an inherited one -- CPython's
    dataclasses._add_slots guards on `if '__setstate__' not in cls_dict`, and an
    inherited method is not in cls_dict. So the mixin cannot carry __setstate__.
    This decorator wraps whatever dataclasses installed rather than reimplementing
    it, so it cannot drift with the CPython version.
    """
    inner = cls.__setstate__

    def __setstate__(self, state):
        inner(self, state)
        self._adopt_arrays()

    cls.__setstate__ = __setstate__
    return cls


# ---------------------------------------------------------------- the contract types

@payload
@dataclass(frozen=True, slots=True)
class Frame(_Arrays):
    seq: int
    t_capture_ns: int
    pixels: np.ndarray
    color: str
    mirrored: bool
    source: str
    _ARRAY_FIELDS = ("pixels",)

    def __post_init__(self):
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class PoseFrame(_Arrays):
    frame_seq: int
    t_capture_ns: int
    topology: str
    mirrored: bool
    world: np.ndarray
    image: np.ndarray | None
    smoothed: bool
    _ARRAY_FIELDS = ("world", "image")

    def __post_init__(self):
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class HoldBoxes(_Arrays):
    frame_seq: int
    boxes: np.ndarray
    labels: tuple
    scores: np.ndarray
    _ARRAY_FIELDS = ("boxes", "scores")

    def __post_init__(self):
        object.__setattr__(self, "boxes", np.clip(np.asarray(self.boxes), 0.0, 1.0))
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Scalar(_Arrays):
    value: float
    t_ns: int

    def __post_init__(self):
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Record(_Arrays):
    kind: str
    t_ns: int
    data: dict
    _TREE_FIELDS = ("data",)

    def __post_init__(self):
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Shutdown(_Arrays):
    reason: str

    def __post_init__(self):
        self._adopt_arrays()


@payload
@dataclass(frozen=True, slots=True)
class Status(_Arrays):
    plugin_id: str
    state: str
    detail: str

    def __post_init__(self):
        self._adopt_arrays()


ALL_TYPES = [Frame, PoseFrame, HoldBoxes, Scalar, Record, Shutdown, Status]


def sample(cls):
    if cls is Frame:
        return Frame(1, 2, np.zeros((4, 4, 3), np.uint8), "bgr", True, "camera:0")
    if cls is PoseFrame:
        return PoseFrame(1, 2, "mediapipe.pose.33", True,
                         np.zeros((33, 4), np.float32), np.zeros((33, 4), np.float32), True)
    if cls is HoldBoxes:
        return HoldBoxes(1, np.array([[0.1, 0.1, 0.2, 0.2]], np.float32),
                         ("hold",), np.array([0.9], np.float32))
    if cls is Scalar:
        return Scalar(90.0, 123)
    if cls is Record:
        return Record("acme.route_graph/1", 123,
                      {"depth": np.zeros((8, 8), np.float32),
                       "nested": {"heat": np.ones((4,), np.float32)},
                       "listed": [np.zeros((2,), np.float32), 3, "x"],
                       "grade": "V4"})
    if cls is Shutdown:
        return Shutdown("ESC pressed")
    if cls is Status:
        return Status("core.capture", "ready", "")
    raise AssertionError(cls)


def arrays_in(obj):
    """Every ndarray reachable from a payload, for the flag assertions."""
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


# ---------------------------------------------------------------- the assertions

fails = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


print(f"python {sys.version.split()[0]}  numpy {np.__version__}\n")

print("A. __setstate__ is ours, not the one @dataclass installed")
for cls in ALL_TYPES:
    check(f"{cls.__name__}.__setstate__ is the wrapped one",
          cls.__setstate__.__qualname__.startswith("payload"))

print("\nB. read-only in the PUBLISHER (at construction)")
for cls in ALL_TYPES:
    obj = sample(cls)
    arrs = arrays_in(obj)
    check(f"{cls.__name__}: {len(arrs)} array(s), none writeable",
          all(not a.flags.writeable for _, a in arrs))

print("\nC. read-only SURVIVES pickle.loads(pickle.dumps(x))  <-- the blocker")
for cls in ALL_TYPES:
    obj = pickle.loads(pickle.dumps(sample(cls)))
    arrs = arrays_in(obj)
    bad = [n for n, a in arrs if a.flags.writeable]
    check(f"{cls.__name__}: {len(arrs)} array(s) after round-trip, writeable={bad}", not bad)

print("\nD. a subscriber mutating a payload RAISES")
f = pickle.loads(pickle.dumps(sample(Frame)))
try:
    f.pixels[:] = 9
    check("Frame.pixels[:] = 9 raises", False)
except ValueError as e:
    check(f"Frame.pixels[:] = 9 -> ValueError: {e}", True)

p = pickle.loads(pickle.dumps(sample(PoseFrame)))
try:
    p.world[:, 1] *= -1
    check("PoseFrame.world[:,1] *= -1 raises", False)
except ValueError as e:
    check(f"PoseFrame.world[:,1] *= -1 -> ValueError: {e}", True)

print("\nE. OWNDATA test actually copies an aliasing view  <-- the publisher-side race")
ring = np.zeros((64, 4), np.float32)
view = ring[:33]                                  # ordinary preallocation
check("np.ascontiguousarray returns the SAME object for this view (why base-is-None fails)",
      np.ascontiguousarray(view) is view)
pf = PoseFrame(1, 2, "mediapipe.pose.33", True, view, None, True)
ring[:] = 7.0                                     # publisher writes the base after publishing
check(f"payload.world unaffected by writing the base (max={pf.world.max()})",
      pf.world.max() == 0.0)
check("payload.world OWNDATA", pf.world.flags["OWNDATA"])
pf2 = pickle.loads(pickle.dumps(pf))
check(f"...and after a round-trip (max={pf2.world.max()})", pf2.world.max() == 0.0)

print("\nF. Record.data nested arrays are read-only after a round-trip")
r = pickle.loads(pickle.dumps(sample(Record)))
check("data['depth']", not r.data["depth"].flags.writeable)
check("data['nested']['heat']", not r.data["nested"]["heat"].flags.writeable)
check("data['listed'][0]", not r.data["listed"][0].flags.writeable)
check("non-array leaves untouched", r.data["grade"] == "V4" and r.data["listed"][1] == 3)

print("\nG. reflection: every ndarray-annotated field is declared")
for cls in ALL_TYPES:
    ann = [f.name for f in fields(cls)
           if "ndarray" in str(f.type).replace("np.", "").replace("numpy.", "")]
    declared = set(cls._ARRAY_FIELDS)
    check(f"{cls.__name__}: annotated={ann} declared={sorted(declared)}",
          set(ann) <= declared)

print("\nH. non-contiguous input is copied, not rejected")
neg = np.zeros((4, 4, 3), np.uint8)[..., ::-1]
check("negative-stride view is non-contiguous", not neg.flags["C_CONTIGUOUS"])
fr = Frame(1, 2, neg, "rgb", False, "x")
check("payload.pixels is C-contiguous", fr.pixels.flags["C_CONTIGUOUS"])

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
