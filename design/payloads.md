# Design: Topic Payload Contracts

Owner: `framework-core` · Status: **revised 2026-08-07 (revision 01)** · Addresses the open item in
`BRAINSTORM.md` §6 ("highest-risk unversioned surface")

Revision 01 actions guardian B1, B2, B3, B5, S12, S13, S21, the `Frame`-origin and grayscale notes,
and F-5, F-9, F-11, F-12. Changelog: [`revision-01.md`](revision-01.md).

---

## 1. The problem, stated precisely

Decision #9 makes **topics the swap mechanism**: replacing the pose model means pointing an exclusive
topic at a different plugin. But a topic name is only half a contract. If a third-party pose plugin
publishes 17 landmarks instead of 33, or `float64` instead of `float32`, or normalised image
coordinates where the previous publisher sent metres, or `+y` up where the previous publisher sent
`+y` down — then every subscriber keeps running and produces wrong answers. Body tilt is off by an
axis. The saved `.npy` has a different shape than the replay tool expects. Nothing raises.

That is worse than a crash, because a crash is attributable and this is not. **The swap mechanism is
worth nothing unless subscribers can rely on payload shape.** This document specifies how.

Three failure modes to close, in order of how quietly they fail:

1. **Semantic mismatch that is invisible in the bytes** — 33 float32s that mean COCO joints instead
   of BlazePose joints; metres vs. normalised; `+y` up vs. down; RGB vs. BGR; mirrored vs. not.
2. **Structural mismatch** — wrong shape, wrong dtype, wrong column count.
3. **Version skew** — a plugin built against an older payload definition.

---

## 2. Chosen approach: framework-shipped typed payloads, validated at construction, with
declared semantic variants checked at wiring time

Three mechanisms, each aimed at a different failure mode.

| Failure mode | Mechanism | When it fires |
|---|---|---|
| Structural (2) | frozen dataclass with `__post_init__` checks, in `climbcv.contracts` | at construction, in the publishing plugin's own process |
| Semantic (1) — enumerable variants | declared in the manifest, cross-checked while wiring | **at startup, before any frame is processed** |
| Semantic (1) — non-enumerable conventions | eliminated by *fixing one convention* in the type | never — cannot be expressed wrongly |
| Version skew (3) | `api_version` (single resolution mechanism) + a per-topic `schema` id for diagnostics | at plugin load |

### 2.1 Alternatives rejected

**P1 — documentation only.** Write "pose.smoothed is float32 (33,4) in metres" in the authoring
guide. Rejected: the failure mode we are trying to prevent is *exactly* the one where nobody read
the doc, and the doc lives in a different repository from the third-party plugin. Zero enforcement
against the highest-risk surface in the project is not a trade-off, it is the status quo.

**P2 — a schema language with per-message validation** (pydantic, jsonschema, protobuf). Rejected on
three counts: it adds a dependency, and every framework dependency is **imposed on every plugin
author** in a system with no `pip install` step; it costs CPU on a 30 fps hot path for a check whose
answer never changes after the first message; and Decision #8 already set this project's posture
("no config schema validation in v1") — a heavyweight payload validator sitting next to that would
be incoherent.

**P4 — version field in every message, subscribers branch on it.** Rejected: that is versioning for
a distributed system with independently deployed components. climb-cv is one process tree started
from one installation, so there is exactly one framework version in play at runtime. Per-message
versioning would let us support mixed versions we can never actually have, at the cost of branching
in every handler.

**P3 — chosen, as detailed below.**

### 2.2 Where the types live

`climbcv/contracts.py`. Imports: `dataclasses`, `numpy`, `typing` — **nothing else, ever**. Under
`spawn` this module is re-imported in every child process; a heavy import here multiplies across the
process tree and lands in every plugin author's environment (see `broker.md` §5.3).

Frozen, `slots=True`. Frozen matters beyond hygiene: a payload is delivered to N subscribers in N
processes, and a mutable payload invites authors to build code that only works when they happen to
be the only subscriber.

#### 2.2.1 Arrays are read-only, and `frozen=True` does not achieve that (guardian B3)

`frozen=True` freezes *references*, not the numpy buffers that are the entire content of these types.
Every array-bearing payload therefore normalises its arrays at construction. **Each type declares its
array fields once**, in a class variable, and both the construction path and the unpickling path
iterate that one list:

```python
def _own_readonly(arr: np.ndarray) -> np.ndarray:
    """C-contiguous, OWNS its buffer, flagged read-only.

    Ownership is tested with flags["OWNDATA"], NOT `arr.base is not None`:
    np.ascontiguousarray returns the SAME object for a contiguous view, so an
    ownership test built on it never copies and the payload keeps a live view
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
    """Mixin. Declares which fields hold arrays and provides the adopt step.
    It must NOT define __setstate__ -- see @payload below."""
    _ARRAY_FIELDS: ClassVar[tuple[str, ...]] = ()   # top-level ndarray fields
    _TREE_FIELDS:  ClassVar[tuple[str, ...]] = ()   # fields whose contents are walked

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
    __dict__, which SHADOWS an inherited one: CPython's dataclasses._add_slots guards
    on `if '__setstate__' not in cls_dict`, and an inherited method is not in cls_dict.
    So the mixin cannot carry __setstate__. This wraps whatever dataclasses installed
    rather than reimplementing it, so it cannot drift with the CPython version.
    """
    inner = cls.__setstate__

    def __setstate__(self, state):
        inner(self, state)
        self._adopt_arrays()

    cls.__setstate__ = __setstate__
    return cls
```

Each type is then `@payload` over `@dataclass(frozen=True, slots=True)`, declares its
`_ARRAY_FIELDS` (and `_TREE_FIELDS`, which only `Record` uses), and calls `_adopt_arrays()` from
`__post_init__` after the structural checks.

**Why the guarantee needs all three parts, each found by measurement rather than reading.** This
mechanism has been wrong twice; both fixes were real and neither was sufficient alone.

1. **Guardian B3's `writeable = False` in `__post_init__` is not enough.** A frozen `slots=True`
   dataclass pickles via `__reduce_ex__` → `__newobj__` + state, which **bypasses `__init__` and
   therefore `__post_init__`** — and `numpy` does not preserve the `writeable` flag across a pickle
   round-trip, so a read-only array unpickles **writeable**. B3 alone freezes the array in the
   *publisher*, which has no reason to touch it again, and leaves it writeable in *every subscriber* —
   exactly where B3's own `stabilize` and `latest()`-cache-corruption cases happen.
2. **`__setstate__` on the `_Arrays` mixin is silently shadowed** (guardian-02 finding 1, verified by
   execution). `_add_slots` installs its own `__setstate__` into the subclass `__dict__` and its guard
   only checks `cls_dict`, where an inherited method never appears. So the mixin's version never runs,
   and the observable behaviour is identical to having no fix at all. Hence `@payload` *outside*
   `@dataclass`, wrapping rather than replacing.
3. **`arr.base is not None` is the wrong ownership test** (same finding). `np.ascontiguousarray`
   returns the same object for a contiguous view, so the copy branch never fired and consequence 1
   below was false: a publisher preallocating `ring`, publishing `ring[:33]`, then writing `ring`
   mutated what the subscriber received, because `publish()` is asynchronous and the feeder thread had
   not serialised yet. `flags["OWNDATA"]` is the correct test.

Cost: 0.23 µs per array per delivery, i.e. 2 % of the 10.5 µs it costs to pickle one 320×240 frame.

**Acceptance criterion — `design/verify/payload_immutability.py`.** The mechanism above is
transcribed there exactly as a reader would implement it, and asserts: `__setstate__` resolves to the
wrapped one for all seven types; arrays are read-only in the publisher; **read-only survives
`pickle.loads(pickle.dumps(x))`**; a subscriber's `frame.pixels[:] = 9` raises; the `OWNDATA` test
copies an aliasing view and survives a round-trip; `Record.data`'s nested arrays are read-only at
every depth while non-array leaves are untouched; every `ndarray`-annotated field is declared; and
non-contiguous input is copied rather than rejected. Verified passing on Python 3.13.11 / numpy 2.4.1.
**Run it before treating this section as true** — two plausible-reading mechanisms have already failed
here, and the round-trip assertion is the one that catches the next `_ARRAY_FIELDS` omission.

**Three consequences to state, because each is author-visible:**

1. **Constructing a payload from an array makes *your* array read-only too**, in your own process, since
   the flag is set on the object you passed (unless it was a view, in which case a copy is taken).
   This is deliberate: the natural construction idiom already produces a fresh array
   (`(xyxy / [w, h, w, h]).astype("float32")`), and it closes a race — `publish()` is asynchronous, so
   an author mutating an array after publishing it would be mutating a buffer the feeder thread may not
   have serialised yet.
2. **Consumers must `.copy()`** anything they intend to write to. `frame.pixels[:] = ...` raises
   `ValueError: assignment destination is read-only`, which is the whole point: B3's `stabilize`
   plugin becomes a loud failure instead of a silent no-op.
3. **Every array in a payload is C-contiguous and owns its buffer.** This is a promise, not an
   accident, and it is what lets `as_bgr()` (§3.1), the `Record` recursion (§3.6), and a future
   shared-memory transport (`broker.md` §5.3) all assume a flat buffer. Consequence 1's race-closing
   claim depends on the `OWNDATA` test specifically; with the earlier `base is not None` test the
   promise was stated and false.

### 2.3 The envelope — provenance the author never constructs

```python
@dataclass(frozen=True, slots=True)
class Meta:
    topic: str            # which topic this arrived on (a handler may serve several)
    source: str           # publishing plugin id — framework-injected, not author-supplied
    seq: int              # per (publisher, topic), monotonic from 0
    t_publish_ns: int     # time.monotonic_ns() at publish
```

The author writes `self.publish("holds.boxes", HoldBoxes(...))`; the runtime wraps it. `source` being
framework-injected rather than a payload field is what makes shared topics workable (filter or colour
by detector — see `broker.md` §3.1) and what makes every log line attributable. A plugin cannot
forge it.

`seq` on the envelope also gives subscribers free drop detection: a gap means the framework
conflated, which is normal and now *observable*. **`seq` is per `(publisher, topic)`, so drop
detection must be keyed by `meta.source`** — see `plugin-api.md` §4.2, corrected per guardian S12.

### 2.4 Every payload is self-timestamping — an invariant, not a coincidence

**Rule for this and every future contract type: a payload carries either a `frame_seq` field or a
`t_ns`-family field, so that the age of a payload is computable from the payload alone, without its
`Meta`.**

| Type | Self-timestamp |
|---|---|
| `Frame` | `seq`, `t_capture_ns` |
| `PoseFrame` | `frame_seq`, `t_capture_ns` |
| `HoldBoxes` | `frame_seq` |
| `Scalar` | `t_ns` |
| `Record` | `t_ns` |
| `Shutdown`, `Status` | exempt: EVENT-kind, never retained |

This exists to earn something specific. `latest_by_source()` (`plugin-api.md` §3.5, added for guardian
B1 / F-1) hands back a mapping of source id → payload with no envelope, and the second-order problem
`first-party-plugins.md` §5.3 raises is that **a stale source never expires**: a crashed detector's
last boxes sit in the mapping forever. With this invariant, expiry is a field read on the payload the
author already has — `frame.seq - holds.frame_seq > max_age`, or
`time.monotonic_ns() - scalar.t_ns > max_age_ns` — and no third accessor is needed to expose `Meta`.

It is also why `Record` (§3.6) carries a mandatory `t_ns` that its author might otherwise have put
inside `data`.

---

## 3. The standard payload types

Each type's docstring **is** the contract. It must state, without exception: dtype, exact array
shape, column meaning, coordinate frame and origin, axis directions, units, value ranges, index
semantics, and what "absent" looks like. A payload type whose docstring omits any of these is
incomplete; that is the review criterion for `plugin-api-guardian`.

### 3.1 `Frame` — topic `frame`, schema `frame/1`

```python
@dataclass(frozen=True, slots=True)
class Frame:
    """One captured video frame.

    seq           monotonically increasing from 0 for the run. The canonical frame identity;
                  every downstream payload derived from this frame carries frame_seq == this.
    t_capture_ns  time.monotonic_ns() sampled immediately after the frame was read from the
                  device. Monotonic clock — comparable across processes on the same machine,
                  NOT wall-clock, NOT comparable across runs.
    pixels        np.uint8, shape (H, W, 3), C-contiguous, READ-ONLY (§2.2.1).
                  Indexed pixels[row, col, channel] -- ORIGIN TOP-LEFT, row-major, so
                  pixels[y, x] is the pixel at image coordinate (x, y). H, W are whatever
                  the source produced; subscribers must not assume a fixed size and must
                  not assume it is constant across a run.
                  To modify pixels, take a writable copy -- as_bgr()/as_rgb() below already
                  return one.
    color         "bgr" | "rgb" | "gray"  -- declared, never assumed.
                  "gray" still carries shape (H, W, 3) with the channel replicated, so that
                  every subscriber has one code path. See the cost note below.
    mirrored      True if horizontally flipped relative to the physical scene. Left/right
                  body semantics depend on this; a subscriber computing "which hand" MUST
                  read it. Copied onto PoseFrame by any pose publisher (see §3.2), so a
                  pose-only subscriber does not have to subscribe to `frame` to get it.
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

    def as_rgb(self) -> np.ndarray: ...
    def as_bgr(self) -> np.ndarray: ...
```

**`as_rgb()` / `as_bgr()` — return contract, specified (guardian B3, F-11).** Previously unstated, and
§2.1's two examples implied opposite things (the YOLO example passed the result straight into
`model.predict()`; the `ExoLive` example called `.copy()` on it).

> Both **always** return a **new, writable, C-contiguous `np.uint8` array of shape (H, W, 3)**. They
> never return `self.pixels`, and they never return a view — including when `self.color` already
> matches the requested order, where the implementation is `self.pixels.copy()`. The caller owns the
> result and may write to it freely.

Always-a-copy rather than a-view-when-no-conversion-is-needed, for three reasons: a return type that is
sometimes writable and sometimes not is the worst possible contract (it works in testing on a BGR
camera and raises on an RGB one); `pixels[..., ::-1]` — the obvious implementation — is a
negative-stride non-contiguous view that many C extensions silently mishandle; and a view would alias
shared memory under a future `shm_ring` transport. The cost is one 5.6 µs memcpy per call at 320×240,
measured, which is half the cost of pickling the same frame. `ExoLive`'s `.copy()` is dropped in
`plugin-api.md` §2.1 so the two examples now agree.

`color` and `mirrored` are the two fields that close real silent-failure paths in the existing code.
Today `climbcv.start()` does `cv2.flip(frame, 1)` then hands **BGR** to YOLO and **RGBA** to
MediaPipe, converting inline — the conventions are correct only because one author wrote both ends.
A third-party capture plugin publishing RGB against a BGR-assuming overlay inverts every colour with
no error. Making it a required declared field with a converter turns that into either correct
behaviour or an explicit `ValueError` naming both plugins.

Validation: dtype, `ndim == 3`, `shape[2] == 3`, `color in {"bgr","rgb","gray"}`. **No content scan
of the pixel buffer** — that is the one payload large enough for per-message content validation to
cost real time.

**Grayscale cost, on the record (guardian note).** `color = "gray"` carrying a replicated `(H, W, 3)`
buffer triples serialisation cost for a grayscale source, on the exact path that has the documented
resolution ceiling: a 640×480 gray source pays 922 KB/frame per subscriber instead of 307 KB. The
one-code-path argument still wins — a `(H, W)` variant would fork every `pixels.shape[:2]` and every
`cv2` call in every subscriber, and grayscale sources are rare in this application — but the cost
belongs in `broker.md` §5.3's budget table rather than only in this paragraph, and it is now there.

### 3.2 `PoseFrame` — topics `pose.raw`, `pose.smoothed`, schema `pose/1`

```python
@dataclass(frozen=True, slots=True)
class PoseFrame:
    """One skeleton estimate for one frame.

    frame_seq   the Frame.seq this was computed from. -1 if not derived from a live frame
                (e.g. a replay source publishing recorded poses).
    t_capture_ns  copied from the originating Frame, so end-to-end latency is measurable.
    topology    names the index semantics of both arrays, e.g. "mediapipe.pose.33".
                See §4 -- this is what makes a different landmark count safe instead of silent.
    mirrored    copied from the originating Frame.mirrored. True if the image this was
                estimated from was horizontally flipped relative to the physical scene, so
                the subject's anatomical LEFT appears on the image-right and vice versa.
                A subscriber computing "which hand" or "which foot" MUST read it. See the
                note below on why this field exists on this type.
    world       np.float32, shape (L, 4), columns (visibility, x, y, z). READ-ONLY (§2.2.1).
                L == the landmark count implied by `topology`.
                METRES. Origin at the midpoint between the hips. Axes: +x IMAGE-right,
                +y DOWN, +z toward the camera (MediaPipe world convention). "Image-right"
                is a frame direction, not an anatomical one: combine with `mirrored` to
                get anatomical left/right.
                z is weakly calibrated by the model -- usable for relative motion, not for
                absolute depth measurement.
                visibility in [0, 1].
    image       np.float32, shape (L, 4), columns (visibility, x, y, z), or None.
                READ-ONLY (§2.2.1).
                NORMALISED to the source frame, ORIGIN TOP-LEFT: x in [0, 1] as a fraction
                of frame width measured rightward, y in [0, 1] as a fraction of frame
                height measured DOWNWARD. Multiply by (W, H) to get pixel coordinates in
                the same convention Frame.pixels uses.
                z is depth relative to the hip midpoint, in roughly the same scale as x --
                it is NOT normalised to [0,1] and may be negative.
                None means the publisher produced no image-space estimate; a subscriber that
                needs image space (any overlay) must handle None.
    smoothed    True if a temporal filter has been applied. When True, EVERY COLUMN of
                BOTH arrays has been filtered, including visibility -- so a subscriber
                thresholding visibility is thresholding a filtered value. When False,
                neither array has been filtered. There is no state in which one array is
                filtered and the other is not; see F-12 below.
    """
    frame_seq: int
    t_capture_ns: int
    topology: str
    mirrored: bool
    world: np.ndarray
    image: np.ndarray | None
    smoothed: bool

    _ARRAY_FIELDS = ("world", "image")
```

**Why `mirrored` is on this type (guardian B2).** It was omitted, and the omission was
unrecoverable: `Frame.mirrored` is documented as mandatory reading for anyone computing "which hand",
but under full isolation `pose.smoothed` arrives on a different topic from `frame` and a pose
subscriber has no reason to subscribe to `frame` at all — it needs no pixels. A `hand_sequence` plugin
subscribing only to `pose.smoothed` would report left/right swapped whenever capture mirrors, which is
the default in the existing code (`cv2.flip(frame, 1)`), with nothing raising anywhere. The same hole
existed downstream of persistence, where a saved `.npy` carried no mirror flag and every offline
analysis was a coin flip.

The design had already made the right call once and then not applied it twice: it decided `topology`
must **travel with the payload** rather than be looked up, precisely because a lookup on another topic
is not available to every subscriber. `mirrored` is the other semantics-determining flag and gets the
same treatment. It is one `bool`, and adding it later would have been a required field — a **major**
bump and a `pose/1` → `pose/2` increment by this document's own §5 table, invalidating every
third-party pose plugin written against v1.

A pose publisher that is not derived from a live `Frame` (a replay source) must still state it. That is
why `core.persist_npy` writes a sidecar carrying `mirrored` and `topology` alongside the `.npy` —
`isolation.md` §8.2.

**F-12 answered: on `pose.smoothed`, both arrays are filtered.** `plugins-and-config` asked whether
`world`-only filtering (today's behaviour: `_update_landmarks` smooths `pose_world_landmarks`, while
`exo_live` reads the unsmoothed `pose_landmarks`) carries over, and noted both readings were
defensible and its overlay's behaviour depended on the answer. The answer is **filter both**, for three
reasons: a topic named `pose.smoothed` whose second array is not smoothed is a name that lies, and the
whole of this document is about not doing that; `smoothed: bool` is one flag and cannot honestly
describe two different states, so the alternative is a per-array caveat every subscriber must carry;
and the extra cost is 132 more `OneEuroFilter` states and one more filter pass over a `(33, 4)` array,
which is microseconds. The visible consequence is an improvement — the overlay's skeleton stops
jittering — and it is a v1 first definition, not a change to a shipped contract.

**What the contract does *not* fix, deliberately:** today's `_update_landmarks` holds the previous
landmark value when `visibility < threshold`. That is *publisher* behaviour, not contract behaviour —
a different smoother may reasonably not do it — so it is not in this docstring. `core.smooth_oneeuro`
applies it identically to both arrays, exposes it as `visibility_threshold` (0.0 disables), and
documents it in its own config section. Stated because F-12 asked and the answer is "the publisher
decides, and here is what ours does."

Carrying **both** `world` and `image` is a finding from reading the baseline: `_update_landmarks`
consumes `result.pose_world_landmarks` (metres, hip origin → smoothing, plotting, `.npy`) while
`exo_live` consumes `result.pose_landmarks` (normalised image → skeleton draw, body-tilt line
endpoints). One MediaPipe inference produces both. Publishing only one would make either the overlay
or the plotter impossible; splitting them into two exclusive topics would let a swap replace one and
not the other, silently desynchronising them. One payload, both representations, `image` nullable.

Validation: dtype `float32`; `ndim == 2`; `shape[1] == 4`; `shape[0] ==
TOPOLOGY_SIZES[topology]`; `topology` is a known id; `world` and `image` agree on `shape[0]`;
`np.isfinite(world).all()`; `0 <= visibility <= 1`. Measured at 2.2 µs on a (33,4) array.
Cheap enough to leave always on, and always-on is worth it because a plugin author's first bug is
then met by an exception with the units written in it.

### 3.3 `HoldBoxes` — topic `holds.boxes` (shared), schema `holds.boxes/1`

```python
@dataclass(frozen=True, slots=True)
class HoldBoxes:
    """A set of detected climbing holds for one frame. Additive: several detectors may
    publish for the same frame_seq; use Meta.source to tell them apart.

    frame_seq  the Frame.seq these were detected on. Detectors that run every Nth frame
               report the frame they actually ran on, not the current one. Also how a
               consumer expires a stale source from latest_by_source() (§2.4).
    boxes      np.float32, shape (N, 4), rows (x1, y1, x2, y2). READ-ONLY (§2.2.1).
               NORMALISED to the frame, ORIGIN TOP-LEFT: x against width measured
               rightward, y against height measured downward -- the same convention as
               Frame.pixels and PoseFrame.image.
               Values are CLIPPED to [0, 1] at construction, silently. x1 <= x2 and
               y1 <= y2 are ENFORCED and raise.
               N may be 0; shape is then (0, 4), never None.
    labels     tuple[str, ...], length N. Free-form class names from the detector's own
               vocabulary. NOT an enum -- do not switch on these across plugins.
    scores     np.float32, shape (N,), each in [0, 1]. Detector confidence; comparable
               only within one detector, not across detectors.
    """
    frame_seq: int
    boxes: np.ndarray
    labels: tuple[str, ...]
    scores: np.ndarray

    _ARRAY_FIELDS = ("boxes", "scores")
```

**Clip the range, raise on the ordering (F-9).** The original specification raised on any value outside
`[0, 1]`. `plugins-and-config` reported that real detectors routinely emit boxes a pixel outside the
frame, so **every** detector author would need an `np.clip` or their plugin would raise mid-run and
eventually quarantine — the framework demanding defensive noise from every author for a condition it
can handle itself in one line.

The split is by whether the input is a normal artifact or a bug: a box extending slightly past the
frame edge is what an object detector does, and clipping it is what every consumer would do anyway;
`x1 > x2` is a coordinate-order mistake with no correct interpretation, and silently "fixing" it by
swapping would hide the bug this document exists to surface. Clipping happens before the ordering
check, and may produce a zero-area box (both edges outside on the same side), which is legal.

`__post_init__` already owns the array by §2.2.1, so the clip is a free in-place `np.clip(arr, 0, 1,
out=arr)` before the read-only flag is set. This is a *loosening* of a validation rule, which §5's
change table permits at any time; the reverse would not be.

**Normalised coordinates are enforced, with no pixel option.** The existing `yolo_boxes_worker`
returns integer pixel coordinates and carries `scale_x`/`scale_y` through the worker to rescale from
the 192-px-wide inference frame back to full resolution — a rescale that only works because one
author controlled both ends and knew both sizes.

Offering both conventions with a `coords` field would reintroduce the silent-failure problem one
level up: every subscriber would have to handle both, and the one that forgets is wrong without
error. Fixing one convention removes the degree of freedom. A detector working in pixels divides by
its own frame size — one line, and it forces the author to think about *which* frame size, which is
exactly the thing that goes wrong. An overlay multiplies by its own frame size, and is then correct
even if the detector ran at a different resolution.

Note the deliberate asymmetry with `Frame.color`, which is *declared* rather than fixed: normalising
an `(N,4)` box array is free, while converting an `(H,W,3)` colour buffer costs real per-frame time
and capture backends genuinely differ. **Fix the convention where conversion is free; declare it
where conversion is expensive.**

### 3.4 `Scalar` — topic `device.lid_angle`, schema `scalar/1`

```python
@dataclass(frozen=True, slots=True)
class Scalar:
    """A single timestamped numeric reading from a sensor whose cadence is unrelated to
    the frame rate.

    value      float. Unit is fixed by the TOPIC, not by this payload. Every topic whose
               payload is `scalar` declares `unit` in its descriptor (a required manifest
               key -- see below), it is shown by `climbcv topics`, and two publishers
               declaring the same topic with different units is a startup error.
               For device.lid_angle: unit = "degree", 0 == closed.
    t_ns       time.monotonic_ns() at the instant of measurement -- not at publish. For a
               sensor read through a subprocess this is meaningfully earlier than publish,
               and consumers must be able to reason about the staleness.
    """
    value: float
    t_ns: int
```

Replaces the current `Manager().Value('d')` + `Value('f')` timestamp pair. The existing `exo_live`
computes and displays `(curr_time - lid_timestamp.value) * 1000` ms of staleness, so
measurement-time-not-publish-time is a requirement carried over from real use, not an invention.

**`unit` becomes a checked descriptor field (guardian S13).** Previously the unit lived in prose
("consult the topic descriptor") and the descriptor's only unit information was a free-form `doc`
string — while §4.2's descriptor-merge check compared `kind`, `exclusivity` and `schema` only. So two
plugins declaring `acme.grip_force` in newtons and in kgf would agree on every checked field, wire
successfully, and disagree by a factor of 9.8 for the rest of time. `Scalar` is also the type most
third-party topics will use, so this is the space where most future topics live.

Therefore: `unit` is a **required** `[[publishes]]` key whenever `payload = "scalar"`, it is a field on
`TopicDescriptor`, and it joins the merge-equality check — at which point the existing
"descriptor contradiction" error (`broker.md` §4.3) produces the message for free.

Unit strings are free-form and **not** validated against a list: a framework-owned unit enumeration is
the structural ceiling Decision #12 rejected, one level down. The guide recommends lowercase singular
SI-ish names (`degree`, `newton`, `metre`, `celsius`, `ratio`, `count`) and `""` for dimensionless.
Standardisation is the ecosystem's job; the framework's job is to make disagreement loud.

### 3.5 `Shutdown` / `Status` — topics `app.shutdown`, `app.status`, EVENT kind

```python
@dataclass(frozen=True, slots=True)
class Shutdown:
    """A request to stop the run in an orderly way. Any plugin may publish it; the
    supervisor acts on it. reason is shown to the user verbatim, e.g. "ESC pressed".
    Publishing this is a request, not a guarantee -- do not rely on being killed."""
    reason: str

STATUS_STATES = frozenset({
    "ready",         # setup() returned; handlers are running
    "restarted",     # a new process was spawned after a crash
    "crashed",       # the process exited unexpectedly; a restart may follow
    "quarantined",   # no further restarts will be attempted
    "stalled",       # a handler has not returned within heartbeat_warn_s
    "unavailable",   # the plugin declared itself not applicable here (plugin-api.md §3.7)
    "finished",      # the plugin declared its work complete
    "shutdown",      # orderly shutdown has begun for this plugin
})


@dataclass(frozen=True, slots=True)
class Status:
    """Framework lifecycle notification. Emitted ONLY by the framework; plugins subscribe
    to build a status UI. See isolation.md for when each state is emitted.

    plugin_id  the plugin this statement is ABOUT -- a subject, not a sender. Because it
               is an ordinary payload field it is author-supplied and therefore forgeable:
               a plugin could publish Status(plugin_id="core.capture", state="crashed").
               Only Meta.source names the sender, and it is framework-injected (§2.3).
               Framework-emitted Status always has Meta.source == "<host>", so a status UI
               that cares MUST check it. Not a security boundary (Decision #6) -- a
               correctness one.
    state      one of STATUS_STATES, validated at construction. Subscribers MUST ignore a
               state they do not recognise rather than raising: adding a state is an
               additive framework minor (§5), so forward-compatibility is the subscriber's
               half of that bargain.
    detail     human-readable, for display and logs. Never parsed.
    """
    plugin_id: str
    state: str
    detail: str
```

`app.shutdown` exists because full isolation moves the `cv2` window (and therefore
`cv2.waitKey`/ESC) into the overlay plugin's process. Today ESC is read by the host loop. There must
be a way for a plugin to ask the app to stop, or the quit path breaks.

**`state` is now a closed, validated, versioned set (guardian S21).** It was a free-form string whose
vocabulary was already inconsistent across two documents — this docstring listed
ready/crashed/restarted/quarantined/stalled while `isolation.md`'s messages said "has been disabled"
and "is shutting down" — so a dashboard matching `state == "quarantined"` was matching an
unenumerated, unversioned, already-divergent vocabulary. The literal set lives in `contracts.py` next
to the type, `__post_init__` rejects anything else, and `isolation.md` §6.3 maps every lifecycle
transition onto exactly one of these. Human message prose stays human; the machine-readable field is
now one of eight strings.

### 3.6 `Record` — the author-defined payload, schema `record/1`

```python
@dataclass(frozen=True, slots=True)
class Record:
    """An observation the framework has no type for, carried as plain data.

    This is the escape hatch that makes the topic vocabulary open. Publish a Record on a
    topic you declare yourself when none of the typed contracts fits -- a route graph, a
    move classification, a per-hand grip estimate, a depth map.

    kind    a versioned id the publishing plugin owns, "<vendor>.<thing>/<n>", e.g.
            "acme.route_graph/1". Grammar as for topic names, plus the "/<n>" suffix.
            Required, because a Record's shape is documented by its author and a consumer
            needs to know which version of that documentation it is looking at.
    t_ns    time.monotonic_ns() at the instant of observation (§2.4).
    data    a dict with str keys, containing ONLY:
              - str, int, float, bool, None
              - list or tuple of the above (returned as tuple)
              - dict with str keys, of the above
              - np.ndarray  (any dtype; normalised C-contiguous and READ-ONLY per §2.2.1,
                             recursively, wherever it appears)
            NOT ALLOWED, and raised on: any other class, including your own dataclasses,
            enums, Paths, datetimes, and numpy scalars.
            Validated recursively at construction: max nesting depth 8, max 10_000
            non-array leaves. An array counts as one leaf regardless of size.
    """
    kind: str
    t_ns: int
    data: dict
```

Manifest: `payload = "record"` **and `record_kind = "<name>/<major>"`, required** (`loader.md` §3.1).
`unit` does not apply (a `Record` may carry many quantities; units belong in its author's
documentation, keyed by `kind`).

**`kind` is declared and checked, not merely carried (guardian-02 blocker 2).** Three places, and all
three are needed:

1. **Manifest** — `record_kind` is required whenever `payload = "record"`, so the value exists before
   any process starts.
2. **Wiring time** — `record_kind` is a `TopicDescriptor` field and joins §4.2's merge-equality check
   alongside `kind`/`exclusivity`/`schema`/`unit`, so two plugins declaring the same record topic with
   different versions are a startup error. `broker.md` §4.3's descriptor-contradiction message then
   covers it with no new error text.
3. **`publish()`** — asserts `payload.kind == descriptor.record_kind` next to the `isinstance` check
   already added for B5, in the publisher's process, where the bug is.

Without this, `schema` is `record/1` for **every** record topic in the ecosystem, so it cannot
distinguish two incompatible `data` layouts. The concrete failure: `grip_viz` subscribes
`acme.hand_state` written against `/1`, where `data["fingers"]` is five floats; a second plugin
publishes the same topic at `/2`, where `fingers` became a dict. Every checked field agrees, the topic
wires, and the subscriber raises `KeyError` inside its handler — where `isolation.md` §6.2's ladder
logs "handler `on_hand` has raised 148 times" and attributes the bug to the innocent plugin. That is
precisely the outcome B5's `isinstance` check was added to eliminate, reachable through the type B5
added. It also bites within one plugin across versions: bump `kind` to `/2` and every existing
subscriber breaks silently, with no mechanism that could have warned them.

This is guardian S13's fix for `Scalar`'s `unit` applied one level down, and for the same reason S13
gave — `Record` will carry most third-party topics, so it is the highest-traffic corner of the payload
surface. v1 is the only opportunity: making a manifest key required later is a tightening, which §5's
own table calls **breaking**, and §4.0 already recorded what happens when the safe direction is
deferred — "the unsafe default would have persisted and the mechanism would have protected only
careful authors, who were never the risk."

**Why this exists (guardian B5).** `loader.md` §3 advertises author-invented topics, but `payload`
was constrained to a `climbcv.contracts` type, so the six framework types were **the entire expressible
payload vocabulary** — a framework-owned enumeration in exactly the shape Decision #12 rejected for
exclusivity. And the obvious workaround was mechanically broken rather than merely inelegant:
`isolation.md` §3.1 sets `sys.path[0]` to the plugin's own directory and `entry` is conventionally
`"plugin:Class"`, so **every** plugin's classes pickle under the qualified name `plugin.<Class>`. A
subscriber unpickling `plugin.RouteGraph` resolves it against *its own* `plugin.py` — raising
`AttributeError` if it has no such class, and silently unpickling into the **wrong class** if it
happens to have one. `Record` sidesteps the whole question: no plugin-defined class is ever in the
pickle stream.

**The rule this generalises to, stated once (guardian B5):**

> **Nothing but a `climbcv.contracts` type crosses the data plane. Nothing but strings and
> primitives crosses the control plane.** A plugin-defined class in either stream is a defect.

The control-plane half matters because `isolation.md` §4.2 sends a crash traceback to the host: if that
were an exception *object* rather than `traceback.format_exc()` output, a plugin-defined exception class
would break the crash-reporting path in the same way, and it would break it precisely when a plugin has
already crashed. `isolation.md` §4.2 now says `format_exc()` explicitly. (The stdlib `QueueHandler`
already gets this right for log records — its `prepare()` formats the message and clears
`exc_info`/`args` — which is a good sign the rule is the conventional one.)

**Enforcement, in the publisher's process (guardian B5, second half).** Nothing previously type-checked
a payload against its topic, so `self.publish("holds.boxes", PoseFrame(...))` enqueued fine and the
`AttributeError` surfaced in the *subscriber's* handler, where `isolation.md` §6.2's ladder would log
"handler `on_holds` has raised 148 times" and attribute the bug to the innocent plugin. So `publish()`
asserts `isinstance(payload, descriptor.payload)` — one `isinstance` per publish, in the process that
made the mistake (`plugin-api.md` §3.4 carries the error text). This also converts the broken
custom-class workaround above into a loud, immediate, correctly-attributed failure: a `RouteGraph` is
not a contracts type, so it never reaches a queue.

**Cost, honestly.** `Record` validation is O(size of `data`) on **every** construction, unlike the fixed
few-microsecond cost of the typed contracts. The caps bound the worst case, but a 5,000-key `Record` on
a 30 Hz topic is a bad idea and the guide should say so. Arrays are exempt from the leaf count
specifically so that carrying a depth map stays cheap.

---

## 4. `topology`: making a different landmark count safe

This is the mechanism for the specific scenario in the task — a third-party pose plugin publishing a
different landmark count.

A topology id names a **fixed joint vocabulary**: how many landmarks, and what index *i* means.
Registry in `climbcv.contracts`:

```python
TOPOLOGY_SIZES: dict[str, int] = {
    "mediapipe.pose.33": 33,   # BlazePose full body; the current model's output
    "coco.17":           17,   # common alternative (YOLO-pose, HRNet, OpenPose subset)
}

TOPOLOGY_EDGES: dict[str, tuple[tuple[int, int], ...]] = {
    "mediapipe.pose.33": ((11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
                          (11, 23), (12, 24), (23, 24),
                          (23, 25), (25, 27), (24, 26), (26, 28), ...),
    "coco.17":           (...),
}
```

**`TOPOLOGY_EDGES` added (F-5).** `plugins-and-config` reported that with only a landmark *count*
shipped, every visualiser plugin must either hardcode its own copy of the MediaPipe skeleton — its
overlay, `pose_plot`'s six polylines, and every third-party one, with no way to keep the copies honest
— or `import mediapipe` into its own process purely for `PoseLandmarksConnections`, which throws away
one of the specific wins `isolation.md` §2.3 claims for full isolation ("the pose process imports
mediapipe but not torch"). The request is granted because it is **pure data with no new mechanism**: a
topology id already claims to name "what index *i* means", and which joints connect to which is part of
that claim, not an addition to it. It respects the stdlib+numpy rule and it is the natural companion to
`TOPOLOGY_SIZES`.

Edges are undirected pairs, `i < j`, sorted, with indices valid under the same topology. Adding a
topology's edges is covered by the existing "add a topology id → minor bump" rule. A *joint-name* table
is the obvious next companion and stays deferred with the named-landmark accessor in §4.2 — it is what
that accessor would need, and shipping the names without the accessor invites the per-frame name lookups
§4.2 rejects.

Both halves declare themselves in their manifests:

```toml
# a pose publisher
provides_topology = "mediapipe.pose.33"

# a subscriber: EITHER the topologies it can handle...
requires_topology = ["mediapipe.pose.33"]
# ...OR an explicit opt-out, if it treats landmarks as an opaque point cloud:
requires_topology = "any"
```

### 4.0 The declaration is mandatory, not optional (guardian B4)

Originally `requires_topology` was omitted by topology-agnostic subscribers, and **omitted meant
"any", so no check ran.** That made the highest-value check in this document opt-in — and the failure
mode §1 defines is "nobody read the doc", while an omittable manifest key is a doc-read-dependent
declaration. Guardian B4's walkthrough: an author copies `plugin-api.md` §2.1's `ExoLive` (which shows
no manifest) and `templates/detector/`'s manifest (a detector, so there is no `requires_topology` line
to notice), indexes joint 11 for a shoulder, the user swaps in a `coco.17` pose plugin, index 11 is a
hip under COCO, and the overlay draws a wrong skeleton every frame with no error. Indices above 16
would `IndexError`, so the *silent* cases are exactly the low-index joints overlays actually draw —
face, shoulders, hips.

**Rule:** for any plugin that publishes or subscribes to a topic **whose descriptor's `payload` is
`PoseFrame`**, the corresponding declaration is required:

- publishes → `provides_topology` required.
- subscribes → `requires_topology` required, as either a list of topology ids or the literal string
  `"any"`.

Omission is a manifest error that states both choices:

```
Plugin 'hand_sequence' (plugins/hand_sequence/climbcv-plugin.toml) subscribes to
'pose.smoothed', which carries pose landmarks, but does not say which landmark
topologies it can read.

Landmark INDICES mean different joints under different topologies -- index 11 is a
left shoulder under mediapipe.pose.33 and a left hip under coco.17 -- so climb-cv
needs this before it will wire you to a pose publisher.

Add ONE of these to [plugin]:

    requires_topology = ["mediapipe.pose.33"]   # I index joints by number
    requires_topology = "any"                   # I treat landmarks as opaque points
```

Three notes on the shape of this:

- **It keys off the payload type, not off the topic name.** "Any plugin subscribing to `pose.*`" would
  be a name-prefix rule that misses a third-party topic carrying `PoseFrame` and false-positives on a
  topic named `pose.confidence` carrying a `Scalar`. The descriptor already knows the payload type, so
  the trigger is exact and it extends to author-declared topics for free.
- **It is the same trick, in the same direction, as `subscribes.required = true`.** `broker.md` §4.2
  defaults `required` to true with the reasoning that "a mistyped topic name that silently delivers
  nothing forever is one of the worst authoring experiences pub/sub has." The safe direction was chosen
  there and the unsafe one here; they are now consistent. Reversing this default later would have been
  a breaking manifest change for every plugin that omitted the field, so in practice the unsafe default
  would have persisted and the mechanism would have protected only careful authors — who were never the
  risk.
- **The declaration is required of the embedding host too**, on the same terms, which is what closes
  guardian S19's sharpest point. See `plugin-api.md` §7.3.

**Wiring-time check.** When the resolver wires a pose publisher to a subscriber whose
`requires_topology` does not include the publisher's `provides_topology`, startup fails:

```
climb-cv cannot start: pose topology mismatch on 'pose.smoothed'.

  publisher   fast_pose 0.3.0  plugins/fast_pose/   provides  "coco.17"
  subscriber  exo_live  1.0.0  plugins/exo_live/    requires  "mediapipe.pose.33"

exo_live indexes specific joints (e.g. 11 = left shoulder) and those indices mean
different joints under coco.17, so it would draw a wrong skeleton rather than fail.

Use a pose plugin that provides "mediapipe.pose.33", or ask exo_live's author to
support "coco.17".
```

That single check converts the project's worst silent-failure mode into a startup error naming both
plugins and both topologies. It is the highest-value item in this document.

Design notes:

- `requires_topology = "any"` is the declaration for subscribers that are genuinely
  topology-agnostic — anything treating landmarks as an opaque point cloud: a raw recorder, a
  bounding-box computer, a centre-of-mass estimator that weights all points equally. It is spelled out
  rather than omitted (§4.0), so the concept is taught by the manifest error rather than by the guide.
- Adding a topology id is a framework minor-version change (additive, non-breaking). A plugin using
  an unknown id fails at manifest load with the list of known ids — so the ecosystem cannot
  fragment silently, only loudly.

### 4.1 Enforcement at run time: every delivery, in the subscriber (guardian S11)

The wiring-time check above compares *declarations*. Something must also check that the declarations
stayed true, and the original design only spot-checked the **first** `PoseFrame` a publisher emitted.

That is not enough, and the counter-example is already in this codebase: the existing code has a
MediaPipe **GPU → CPU delegate fallback** because creating the GPU delegate can fail hard, so
multi-path pose publishers are real rather than hypothetical. A `multi_pose` plugin publishes
`mediapipe.pose.33`, its GPU delegate dies at t = 60 s, it falls back to a bundled 17-point model, and
every payload after that is *internally* consistent — `shape[0]` matches its own `topology` field, so
every construction-time check passes — while contradicting the contract it was wired under. Subscribers
indexing 0–16 then read different joints, silently, for the rest of an hour-long session.

So, in addition to the publisher-side first-payload spot check (kept — it is earlier and catches a
plugin that simply misdescribed itself):

> **Every delivery of a `PoseFrame` to a subscriber with a concrete `requires_topology` is checked
> against the wired expectation.** One string comparison — cheaper than the `np.isfinite(world).all()`
> already run once per construction.

On mismatch, the attribution goes to the publisher, because the publisher is the one that broke its
word:

- The **subscriber** logs one ERROR naming both plugins and both topologies, stops delivering that
  topic to itself, and keeps running. It did nothing wrong, and killing it neither fixes anything nor
  tells anyone more.
- The **host** quarantines the **publisher** as a non-retryable contract violation
  (`isolation.md` §4.5), because a publisher that cannot honour its declaration will not start being
  able to on restart. The normal absent-publisher consequences then follow — including, for
  `pose.smoothed`, the §5.3 critical-shutdown escalation.

That last consequence is deliberate and worth defending: it means a GPU delegate failure now ends the
run instead of degrading it. The alternative is indexing the wrong joints into a saved session for
another fifty minutes, which is the failure class this whole document exists to prevent. The correct fix
for such a plugin is to declare both topologies and publish the one it is actually using — which the
per-delivery check then validates rather than punishes.

### 4.2 Why not a per-landmark name mapping instead?

Considered: publish landmark **names** and let subscribers look up by name, so topologies interoperate
automatically where they overlap. Rejected for v1: it costs a dict lookup per joint per frame on the
hot path, it does not actually solve the interesting cases (COCO has no `left_pinky`; "hip" means the
joint in one topology and the midpoint in another), and it converts a startup error into a runtime
`KeyError` in whichever handler happens to ask first. A named-landmark **accessor** over a declared
topology (`pose.joint("left_shoulder")`, resolved from the topology id at setup) gives most of the
ergonomic benefit with none of the ambiguity, and is listed as a v1.x addition.

---

## 5. Versioning

**One resolution mechanism, one diagnostic aid.**

- **`api_version` resolves.** The framework's plugin API version covers the base class, manifest
  schema, lifecycle, *and the payload types collectively*. A plugin declares the minimum it needs
  (`loader.md` §4). Incompatible → the plugin does not load, with a clear message. Payload types are
  not versioned independently, because they cannot skew independently: one installation, one
  `climbcv.contracts`.
- **Per-topic `schema` ids diagnose.** `pose/1`, `frame/1`, `holds.boxes/1` appear in the topic
  descriptor, in `climbcv topics` output, and in error messages. Documentation-grade, so an author
  debugging a mismatch reads `pose/1 -> pose/2` rather than a generic version number. Nothing
  resolves on them.

Change rules for payload types:

| Change | Verdict |
|---|---|
| Add an **optional** field with a default | additive → framework **minor** bump, `schema` unchanged |
| Add a **required** field | breaking → **major** bump, `schema` id incremented |
| Change dtype, shape, units, axis direction, or column meaning | breaking → **major** bump, `schema` id incremented |
| Add a `topology` id, or edges for one | additive → **minor** bump |
| Add a `Status.state` value | additive → **minor** bump; subscribers are required by §3.5 to ignore unknown states |
| **Loosen** a validation rule (F-9's clip) | additive → **minor** bump |
| Tighten a validation rule that previously passed | **treated as breaking.** Silently rejecting a payload that used to be accepted is exactly the class of failure this document exists to prevent. |

That last row is the one that will be tempting to fudge later, so it is written down now. It is also
the row that decided three of guardian review 01's five blocking findings — `mirrored` (B2), read-only
arrays (B3) and mandatory `requires_topology` (B4) are each cheap now and a major bump later — which is
the rule doing its job rather than the rule being inconvenient.

**Two rules that bind every future contract type, gathered here so they are not rediscovered:**

- **Self-timestamping (§2.4).** A new type carries a `frame_seq` or a `t_ns`-family field unless it is
  EVENT-kind and never retained.
- **One array list per type (§2.2.1).** A new array field must be added to `_ARRAY_FIELDS`, or it will be
  writeable in every subscriber and the read-only guarantee will be true of some fields and not others.
  Worth a test that reflects over each type's array-typed annotations and asserts they all appear.

---

## 6. Handoffs and open items

**Ready for `plugin-api-guardian`:** all of §3 (every docstring is a public contract), §2.2.1
(read-only arrays and the `as_bgr()` contract), §2.3 (`Meta`), §2.4 (the self-timestamping invariant),
§3.6 (`Record` and the data-plane/control-plane rule), §4–§4.2 (the `topology` mechanism, its mandatory
declaration, and every error text), §5 (change rules). These are the surfaces where a wrong word costs
a real third-party author a real afternoon.

**To `plugins-and-config`:** the four first-party conversions are the first test of these contracts.
Specifically — the YOLO plugin must emit **normalised** boxes (a behaviour change from the current
pixel output); the overlay must handle `PoseFrame.image is None` and `HoldBoxes` from more than one
source; the lid plugin publishes `Scalar` with measurement-time `t_ns`. **If any conversion cannot be
expressed in these types, that is a contract defect — report it rather than working around it.**

Revision-01 changes that touch your conversions directly:

- `PoseFrame` gains `mirrored` (B2) — your overlay can now compute anatomical left/right without
  subscribing to `frame` for the flag, and your `.npy` sidecar carries it.
- `pose.smoothed` filters **both** arrays (F-12 answered). Your overlay's skeleton is drawn from
  smoothed `image` landmarks, not raw ones. `visibility_threshold` hold-last applies to both and is
  `core.smooth_oneeuro`'s config, not the contract's.
- `HoldBoxes` clips to `[0, 1]` itself (F-9), so the `np.clip` in your `yolo_holds` port can go.
- Arrays are read-only (B3/F-11). Your overlay's `frame.as_bgr().copy()` becomes `frame.as_bgr()` —
  the copy is now the contract. Anything that writes into a payload array must copy first.
- `TOPOLOGY_EDGES` exists (F-5), so neither `exo_live` nor `pose_plot` hardcodes a skeleton and neither
  imports `mediapipe`.
- `requires_topology` is now **mandatory** for pose subscribers (B4). All three of your pose-consuming
  manifests already declare it; a plugin that did not would now fail at load rather than silently.
- `Scalar` topics need `unit` in the manifest (S13): `device.lid_angle` declares `unit = "degree"`.
- `Record` exists (B5) if any conversion needs a payload these six types cannot carry.

**To `docs-and-testing`:** the contracts module is where property-based testing pays off (construct
from arbitrary arrays, assert the validator's accept/reject boundary matches the docstring). And the
docstrings should be the *source* of the authoring guide's payload reference, not paraphrased into
it — paraphrase is how the two drift.

**Open:**
- Named-landmark accessor (§4.2) — deferred to v1.x, recorded so it is not reinvented.
- `TOPOLOGY_SIZES` ships with two entries. Whether `coco.17` belongs in v1 when nothing publishes it
  is a genuine question; including it costs one dict entry and demonstrates the mechanism is real
  rather than theoretical.
