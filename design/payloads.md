# Design: Topic Payload Contracts

Owner: `framework-core` · Status: **proposed, awaiting review** · Addresses the open item in
`BRAINSTORM.md` §6 ("highest-risk unversioned surface")

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
conflated, which is normal and now *observable*.

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
    pixels        np.uint8, shape (H, W, 3), C-contiguous. H, W are whatever the source
                  produced; subscribers must not assume a fixed size and must not assume it
                  is constant across a run.
    color         "bgr" | "rgb" | "gray"  -- declared, never assumed.
                  "gray" still carries shape (H, W, 3) with the channel replicated, so that
                  every subscriber has one code path.
    mirrored      True if horizontally flipped relative to the physical scene. Left/right
                  body semantics depend on this; a subscriber computing "which hand" MUST
                  read it.
    source        informational, e.g. "camera:0", "file:/path/to.mp4". Never parsed for
                  behaviour.
    """
    seq: int
    t_capture_ns: int
    pixels: np.ndarray
    color: str
    mirrored: bool
    source: str

    def as_rgb(self) -> np.ndarray: ...   # converts per self.color; caches nothing
    def as_bgr(self) -> np.ndarray: ...
```

`color` and `mirrored` are the two fields that close real silent-failure paths in the existing code.
Today `climbcv.start()` does `cv2.flip(frame, 1)` then hands **BGR** to YOLO and **RGBA** to
MediaPipe, converting inline — the conventions are correct only because one author wrote both ends.
A third-party capture plugin publishing RGB against a BGR-assuming overlay inverts every colour with
no error. Making it a required declared field with a converter turns that into either correct
behaviour or an explicit `ValueError` naming both plugins.

Validation: dtype, `ndim == 3`, `shape[2] == 3`, `color in {"bgr","rgb","gray"}`. **No content scan
of the pixel buffer** — that is the one payload large enough for per-message content validation to
cost real time.

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
    world       np.float32, shape (L, 4), columns (visibility, x, y, z).
                L == the landmark count implied by `topology`.
                METRES. Origin at the midpoint between the hips. Axes: +x subject's
                image-right, +y DOWN, +z toward the camera (MediaPipe world convention).
                z is weakly calibrated by the model -- usable for relative motion, not for
                absolute depth measurement.
                visibility in [0, 1].
    image       np.float32, shape (L, 4), columns (visibility, x, y, z), or None.
                NORMALISED to the source frame: x,y in [0, 1] as fractions of frame width and
                height respectively. z is depth relative to the hip midpoint, in roughly the
                same scale as x -- it is NOT normalised to [0,1] and may be negative.
                None means the publisher produced no image-space estimate; a subscriber that
                needs image space (any overlay) must handle None.
    smoothed    True if a filter has been applied. On `pose.smoothed` from
                core.smooth_oneeuro this is True and ALL FOUR columns are filtered,
                including visibility -- a subscriber thresholding visibility is thresholding
                a filtered value. Stated because it is surprising, and it is existing
                behaviour we are keeping, not introducing.
    """
    frame_seq: int
    t_capture_ns: int
    topology: str
    world: np.ndarray
    image: np.ndarray | None
    smoothed: bool
```

Carrying **both** `world` and `image` is a finding from reading the baseline: `_update_landmarks`
consumes `result.pose_world_landmarks` (metres, hip origin → smoothing, plotting, `.npy`) while
`exo_live` consumes `result.pose_landmarks` (normalised image → skeleton draw, body-tilt line
endpoints). One MediaPipe inference produces both. Publishing only one would make either the overlay
or the plotter impossible; splitting them into two exclusive topics would let a swap replace one and
not the other, silently desynchronising them. One payload, both representations, `image` nullable.

Validation: dtype `float32`; `ndim == 2`; `shape[1] == 4`; `shape[0] ==
TOPOLOGY_SIZES[topology]`; `topology` is a known id; `world` and `image` agree on `shape[0]`;
`np.isfinite(world).all()`; `0 <= visibility <= 1`. Cost on a (33,4) array: order of microseconds.
Cheap enough to leave always on, and always-on is worth it because a plugin author's first bug is
then met by an exception with the units written in it.

### 3.3 `HoldBoxes` — topic `holds.boxes` (shared), schema `holds.boxes/1`

```python
@dataclass(frozen=True, slots=True)
class HoldBoxes:
    """A set of detected climbing holds for one frame. Additive: several detectors may
    publish for the same frame_seq; use Meta.source to tell them apart.

    frame_seq  the Frame.seq these were detected on. Detectors that run every Nth frame
               report the frame they actually ran on, not the current one.
    boxes      np.float32, shape (N, 4), rows (x1, y1, x2, y2).
               NORMALISED to the frame: all values in [0, 1], x against width, y against
               height, origin top-left, x1 <= x2 and y1 <= y2 enforced.
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
```

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

    value      float. Unit is fixed by the topic, not by this payload; consult the topic
               descriptor. For device.lid_angle: DEGREES, 0 == closed.
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

### 3.5 `Shutdown` / `Status` — topics `app.shutdown`, `app.status`, EVENT kind

```python
@dataclass(frozen=True, slots=True)
class Shutdown:
    """A request to stop the run in an orderly way. Any plugin may publish it; the
    supervisor acts on it. reason is shown to the user verbatim, e.g. "ESC pressed".
    Publishing this is a request, not a guarantee -- do not rely on being killed."""
    reason: str

@dataclass(frozen=True, slots=True)
class Status:
    """Framework and plugin lifecycle notification: ready, crashed, restarted, quarantined,
    stalled. Emitted by the framework; plugins may subscribe to build a status UI. See
    isolation.md for the event vocabulary."""
    plugin_id: str
    state: str
    detail: str
```

`app.shutdown` exists because full isolation moves the `cv2` window (and therefore
`cv2.waitKey`/ESC) into the overlay plugin's process. Today ESC is read by the host loop. There must
be a way for a plugin to ask the app to stop, or the quit path breaks.

---

## 4. `topology`: making a different landmark count safe

This is the mechanism for the specific scenario in the task — a third-party pose plugin publishing a
different landmark count.

A topology id names a **fixed joint vocabulary**: how many landmarks, and what index *i* means.
Registry in `climbcv.contracts`:

```python
TOPOLOGY_SIZES = {
    "mediapipe.pose.33": 33,   # BlazePose full body; the current model's output
    "coco.17":           17,   # common alternative (YOLO-pose, HRNet, OpenPose subset)
}
```

Both halves declare themselves in their manifests:

```toml
# a pose publisher
provides_topology = "mediapipe.pose.33"

# a subscriber that indexes specific joints
requires_topology = ["mediapipe.pose.33"]
```

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

- `requires_topology` is **omitted** by subscribers that are topology-agnostic (anything treating
  landmarks as an opaque point cloud — a raw recorder, a bounding-box computer, a
  centre-of-mass estimator that weights all points equally). Omitted means "any", and no check runs.
  This keeps the declaration burden on exactly the plugins that index joints by number.
- Declared statically because wiring happens before any code runs. **Spot-checked once at runtime:**
  the child runtime asserts the first `PoseFrame` a plugin publishes has `topology ==
  provides_topology`. A mismatch means the plugin misdescribed itself — logged attributably and the
  plugin is stopped. Once per run, not per message.
- Adding a topology id is a framework minor-version change (additive, non-breaking). A plugin using
  an unknown id fails at manifest load with the list of known ids — so the ecosystem cannot
  fragment silently, only loudly.

### 4.1 Why not a per-landmark name mapping instead?

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
| Add a `topology` id | additive → **minor** bump |
| Tighten a validation rule that previously passed | **treated as breaking.** Silently rejecting a payload that used to be accepted is exactly the class of failure this document exists to prevent. |

That last row is the one that will be tempting to fudge later, so it is written down now.

---

## 6. Handoffs and open items

**Ready for `plugin-api-guardian`:** all of §3 (every docstring is a public contract), §2.3 (`Meta`),
§4 (the `topology` mechanism and its error text), §5 (change rules). These are the surfaces where a
wrong word costs a real third-party author a real afternoon.

**To `plugins-and-config`:** the four first-party conversions are the first test of these contracts.
Specifically — the YOLO plugin must emit **normalised** boxes (a behaviour change from the current
pixel output); the overlay must handle `PoseFrame.image is None` and `HoldBoxes` from more than one
source; the lid plugin publishes `Scalar` with measurement-time `t_ns`. **If any conversion cannot be
expressed in these types, that is a contract defect — report it rather than working around it.**

**To `docs-and-testing`:** the contracts module is where property-based testing pays off (construct
from arbitrary arrays, assert the validator's accept/reject boundary matches the docstring). And the
docstrings should be the *source* of the authoring guide's payload reference, not paraphrased into
it — paraphrase is how the two drift.

**Open:**
- Named-landmark accessor (§4.1) — deferred to v1.x, recorded so it is not reinvented.
- `TOPOLOGY_SIZES` ships with two entries. Whether `coco.17` belongs in v1 when nothing publishes it
  is a genuine question; including it costs one dict entry and demonstrates the mechanism is real
  rather than theoretical.
