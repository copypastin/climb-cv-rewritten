# Design: The Four First-Party Plugin Conversions

Owner: `plugins-and-config` · Status: **proposed, awaiting review** · Implements Decision #7
(dogfooding) against `framework-core`'s proposed API (`plugin-api.md`, `payloads.md`, `loader.md`,
`isolation.md`, `broker.md`)

Baseline read in full at `copypastin/climb-cv@main`: `src/climbcv/climbcv.py`,
`utils/yolo.py`, `utils/rendering/plot_pose_live.py`, `utils/rendering/exo_live.py`,
`utils/angles/read_swift_lid.py`, `utils/angles/lid_angle.swift`, `utils/smoothing.py`,
`utils/config.py`.

---

## 1. What this document is for

`plugin-api.md` §6 states the terms: *"the four conversions are the acceptance test for this
interface. If any of them needs something not in §2, that is a defect in this layer — report it
rather than reaching around it."* This document is that test, run.

Every conversion below is expressible on the proposed public API. **Nothing here needs a private
hook or a framework-internals import.** But eleven things came out crooked, and §10 ranks them. Three
of them (F-1, F-2, F-3) are places where a *first-party* plugin cannot preserve today's behaviour
without either an API addition or boilerplate that every third-party author will copy wrong, which by
the standing test means the API is incomplete rather than that my ports are clumsy.

Read §10 first if you are reviewing rather than implementing.

---

## 2. Conventions shared by all four

### 2.1 Ids, directories, entry points

| Plugin | id | directory | entry |
|---|---|---|---|
| YOLO hold detection | `yolo_holds` | `plugins/yolo_holds/` | `plugin:YoloHolds` |
| Live 3D pose plot | `pose_plot` | `plugins/pose_plot/` | `plugin:PosePlot` |
| Live overlay | `exo_live` | `plugins/exo_live/` | `plugin:ExoLive` |
| macOS lid-angle sensor | `mac_lid` | `plugins/mac_lid/` | `plugin:MacLid` |

Ids match `broker.md` §7's sample `climbcv topics` output so the two documents describe the same
system. None of them take a `core.` prefix: `loader.md` §3.1 reserves that for the four built-in
stages, and Decision #7's whole point is that these four are **ordinary plugins with no privilege**.

### 2.2 File layout of one plugin

```
plugins/yolo_holds/
  climbcv-plugin.toml     # manifest, per loader.md §3
  plugin.py               # entry module; the Plugin subclass and nothing else
  detect.py               # the pure functions -- no framework imports, unit-testable alone
  hold_detection.pt       # optional: a local model. See §2.4.
```

The split between `plugin.py` (framework contact surface) and a sibling module of pure functions is
deliberate and is the pattern the authoring guide should teach: `detect.py` takes arrays and returns
arrays, imports no `climbcv` symbol, and can be tested with `pytest` and no processes. `plugin.py`
stays short enough to read in one screen. This is also how today's code is already shaped
(`plot_world_landmarks_points` is a pure function; `plotting_process` is the wrapper), so the
conversion is mostly *deleting* the wrapper.

### 2.3 Where the first-party plugins actually ship — unresolved, and it blocks the release

`climb-cv` is a pip-installable package today, and `enable_exo_live` defaults to `True`. After this
refactor, a user who runs `pip install climb-cv` in an empty directory gets **no overlay and no hold
detection**, because `plugins/` is empty. Decision #7 turns four working features into four features
that do not exist until the user locates and copies four folders.

`loader.md` has no mechanism for this, and it needs one. See **F-3** in §10 for the requested loader
change (a bundled plugin root scanned alongside `plugins/`, with `plugins/` shadowing by id). The
conversions below assume it; if it is rejected, `climbcv init` copying the four folders is the
fallback, and it must then be documented as a required first-run step.

### 2.4 Model and asset resolution

`hold_detection.pt` is **44 MB** and the pose models are 5.7–30 MB. That size is what makes §2.3 a
real problem rather than a tidy-up: telling a user to copy `yolo_holds/` out of the package copies
44 MB, and shipping the same file twice on disk is worse.

Resolution order for `yolo_holds`, in the plugin, with a log line naming which one won:

1. `config["model_path"]`, if set — absolute, or relative to the plugin directory.
2. `<plugin_dir>/hold_detection.pt`, if present. A self-contained plugin folder, which is what a
   third-party detector will look like.
3. The packaged asset via `importlib.resources.files("climbcv") / "models" / "hold_detection.pt"`.

Step 3 is the part worth flagging: it is a first-party plugin reaching into the host package for an
asset, and **a third-party plugin has no equivalent**. It is not a private *API* — `importlib.resources`
against an installed package is public Python, and any plugin could do the same against its own
distribution — so it does not fail the "would a third party get this" test outright. But it does mean
the first-party plugin is thinner than a third-party one can be, and step 2 exists precisely so the
example stays honest about the normal case. See **F-7**.

### 2.5 Every plugin's `setup()` reads config exactly once, with literal defaults

```python
def setup(self) -> None:
    self.every_n = max(1, int(self.config.get("every_n_frames", 4)))
```

No config access outside `setup()`. Two reasons worth teaching: `self.config` never changes (no hot
reload, Assumption §3), and a plugin whose every default is visible in one block is a plugin whose
behaviour can be understood without running it. This is also the *only* place a plugin's option names
appear anywhere machine-readable, which is the subject of **F-8**.

---

## 3. `yolo_holds` — YOLO hold detection

### 3.1 What it does today

Two halves. In the host loop (`climbcv._queue_yolo_frame`):

- increments `_exo_live_frame_index` on **every captured frame** and returns unless
  `index % exo_live_yolo_every_n_frames == 0` (default 4);
- resizes the already-mirrored BGR frame to `yolo_input_width = 192` wide, aspect preserved
  (320×240 → 192×144), `INTER_LINEAR`;
- computes `scale_x = width / 192`, `scale_y = height / 144`;
- drop-oldest into a `maxsize=1` queue, `put_nowait`.

In the worker (`utils/yolo.py::yolo_boxes_worker`), spawned once with the model *path*:

- `YOLO(model_path)` in the child;
- `model.predict(source=frame, imgsz=256, verbose=False)`;
- for each box: `astype(int)`, multiply by `scale_x`/`scale_y` back to full-resolution **integer
  pixels**, look up the label from `det.names`, keep `conf`;
- drop-oldest into a `maxsize=1` output queue.

The host drains the output queue keeping only the newest list, and `_draw_yolo_boxes` renders it onto
whatever the current frame is — so boxes are always from an older frame and today that is invisible.

### 3.2 Topics

| Direction | Topic | Kind | Exclusivity | Required |
|---|---|---|---|---|
| subscribes | `frame` | stream | exclusive | yes |
| publishes | `holds.boxes` | stream | **shared** (Decision #12) | — |

Shared means the plugin must set `frame_seq` honestly and must not assume it is the only publisher
(`broker.md` §8). It sets `frame_seq = frame.seq` of the frame it *actually ran on*, per
`payloads.md` §3.3 — which is exactly the "every Nth frame" case that field was written for.

`meta.source` is framework-injected, so `yolo_holds` needs no self-identification of any kind. That
part of Decision #12 lands cleanly.

### 3.3 Manifest

```toml
[plugin]
id          = "yolo_holds"
version     = "1.0.0"
api_version = "1.0"
entry       = "plugin:YoloHolds"
name        = "YOLO Hold Detection"
description = "Detects climbing holds in the live feed with a YOLO model."
author      = "Aaron Nguyen"
license     = "Apache-2.0"
requires    = ["ultralytics>=8.0", "opencv-python"]   # informational only, loader.md §6

[[publishes]]
topic = "holds.boxes"

[[subscribes]]
topic    = "frame"
required = true
```

No `platforms` (works everywhere), no `provides_topology` / `requires_topology` (it never touches
pose data).

### 3.4 Config section

```toml
[plugins.yolo_holds]
every_n_frames = 4      # run inference at most once per N captured frames
input_width    = 192    # downscale width before inference; 0 disables downscaling
imgsz          = 256    # YOLO inference size
min_score      = 0.0    # drop detections below this confidence
# model_path   = "hold_detection.pt"
```

`every_n_frames`, `input_width` and `imgsz` are today's `exo_live_yolo_every_n_frames`,
`yolo_input_width` and `yolo_imgsz` with the same defaults. `min_score` is new and defaults to
today's behaviour (no filtering); it exists because two detectors on a shared topic make a confidence
floor genuinely useful.

### 3.5 The port

```python
import numpy as np
from climbcv.plugin import Plugin, subscribe
from climbcv.contracts import Frame, HoldBoxes, Meta


class YoloHolds(Plugin):
    """Publishes hold detections from a YOLO model, at most once per N frames."""

    def setup(self) -> None:
        import cv2                      # heavy imports live in the child
        from ultralytics import YOLO

        self._cv2 = cv2
        self.every_n   = max(1, int(self.config.get("every_n_frames", 4)))
        self.input_w   = int(self.config.get("input_width", 192))
        self.imgsz     = int(self.config.get("imgsz", 256))
        self.min_score = float(self.config.get("min_score", 0.0))

        model_path = resolve_model_path(self.config.get("model_path"))
        self.log.info("loading hold detector: %s", model_path)
        self.model = YOLO(str(model_path))

        self._next_seq = 0

    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta) -> None:
        # Throttle against the CAPTURE sequence number, never against a count of
        # handler calls. This handler only sees frames that survived conflation,
        # so counting calls would multiply the framework's drops by ours. See F-1.
        if frame.seq < self._next_seq:
            return
        self._next_seq = frame.seq + self.every_n

        image = self._downscale(frame.as_bgr())
        h, w = image.shape[:2]

        result = self.model.predict(source=image, imgsz=self.imgsz, verbose=False)[0]
        boxes, labels, scores = extract_boxes(result, self.min_score)

        # Normalised coordinates (payloads.md §3.3). Dividing by the *inference*
        # frame size is correct and scale-invariant: the whole scale_x/scale_y
        # bookkeeping the old worker carried disappears here.
        boxes = np.clip(boxes / np.array([w, h, w, h], dtype="float32"), 0.0, 1.0)

        self.publish("holds.boxes", HoldBoxes(
            frame_seq=frame.seq,
            boxes=boxes.astype("float32"),
            labels=labels,
            scores=scores.astype("float32"),
        ))

    def _downscale(self, image: np.ndarray) -> np.ndarray:
        if self.input_w <= 0 or image.shape[1] <= self.input_w:
            return image
        h, w = image.shape[:2]
        new_h = max(1, round(h * (self.input_w / w)))
        return self._cv2.resize(image, (self.input_w, new_h),
                                interpolation=self._cv2.INTER_LINEAR)
```

### 3.6 Behaviour and performance, mapped honestly

**Normalised coordinates simplify the port, and this is real evidence the contract is right.**
`scale_x`/`scale_y` are gone. Not moved — gone. Because normalisation is scale-invariant, dividing
boxes by the *downscaled* frame's dimensions yields exactly the numbers you would get from
full-resolution inference, and the overlay multiplies by whatever size it happens to be drawing at.
`payloads.md` §3.3's argument that fixing the convention "forces the author to think about *which*
frame size" is borne out: the question is asked once, at the division, next to the resize that
created the ambiguity.

**The every-Nth-frame throttle survives, but only if it is written against `frame.seq`.** This is the
sharpest single finding in the document and it is worth stating in full, because `plugin-api.md`
§2.1's own YOLO example gets it wrong:

```python
self._i += 1
if self._i % self.every_n:      # <-- degrades hold detection ~4x under conflation
    return
```

Under `isolation.md` §3.3, the child loop drains its stream queue each turn and keeps **only the
newest message per topic**. So `on_frame` is not called once per captured frame; it is called once per
loop turn, at whatever rate the plugin itself can sustain. Worked through with today's numbers
(30 fps capture, ~130 ms YOLO inference):

| | frames reaching the detector | detections published |
|---|---|---|
| today | 7.5/s (host enqueues every 4th) | ~7.5/s |
| naive port (`self._i % 4`) | ~7.7/s (conflation) | **~1.9/s** |
| `frame.seq` port | ~7.7/s | ~7.7/s |

The naive port compounds the framework's conflation with the plugin's own decimation and produces a
4× regression that no test of the plugin in isolation would reveal. The `frame.seq` form is correct in
both regimes: when inference is slow, conflation is already doing the throttling and the guard rarely
fires; when inference is fast, the guard enforces the same duty cycle as today and leaves CPU for
MediaPipe — which is the actual reason the knob exists now that IPC is no longer the thing it saves.

The throttle therefore **keeps its purpose but changes which resource it protects**: today it saves
host-loop time and IPC; under full isolation it saves GPU/CPU contention with the pose process.

**Downscaling keeps its purpose too, but a smaller one.** Today the resize shrinks what crosses the
process boundary (192×144 vs 320×240) *and* what YOLO preprocesses. Under full isolation, capture
publishes the full frame to every subscriber unconditionally, so the resize no longer saves any IPC —
the plugin downscales a frame it has already paid to receive. Kept anyway because the inference-side
saving is real and because removing it would change detection results.

**Full isolation costs the holds path IPC it did not pay before, and `broker.md` §5.3's budget does
not account for it.** Today the YOLO path moves 7.5 × 83 KB ≈ **0.6 MB/s**. Under the new design
capture serialises 30 × 230 KB ≈ **6.9 MB/s** to the holds process, of which the plugin discards
three frames in four. That is an 11× increase on this one edge. It fits inside §5.3's ~20 MB/s total
at 320×240, so the conclusion does not change — but the table reads as though three frame subscribers
are alike, and they are not: two of them consume every frame and one consumes a quarter of them. This
is the strongest concrete argument for the T2 shared-memory transport, and the cheapest v1 mitigation
would be a subscription-level decimation declaration so the *publisher* skips the serialisation
entirely. Recorded as **F-6**; not requested for v1.

**Boxes out of range.** `payloads.md` §3.3 enforces `[0, 1]` and `x1 <= x2` at construction. Real
detectors emit boxes a pixel outside the frame; the `np.clip` above is not defensive noise, it is
required, and **every** detector author will need it or their plugin will raise mid-run and eventually
quarantine. Recommend the contract clip the range itself while still raising on `x1 > x2` (a genuine
bug). See **F-9**.

---

## 4. `pose_plot` — live 3D plotting

This is the port `framework-core` flagged as most likely to be bitten by the process model, and it is.
Two distinct hazards, one of which is severe.

### 4.1 What it does today

`plotting_process(queue)` runs in its own `Process`, fed from a `Manager().Queue(maxsize=2)`:

```python
plt.ion(); fig = plt.figure(); ax = fig.add_subplot(111, projection="3d")
while True:
    raw = queue.get(timeout=1.0)          # loops on timeout
    if raw is None: break
    plot_world_landmarks_points(ax, raw, visibility_th)
    fig.canvas.draw_idle()
    plt.pause(0.001)                      # <-- pumps the GUI event loop
```

`plot_world_landmarks_points` does `ax.cla()`, fixes the limits to ±1, then scatters the face and
plots six polylines over **hardcoded MediaPipe indices** (`[0..10]`, `[11,13,15,17,19,21]`,
`[12,14,16,18,20,22]`, `[11,23,25,27,29,31]`, `[12,24,26,28,30,32]`, `[11,12]`, `[23,24]`). It maps
`(x, y, z) → (x, z, -y)` for display. Input is the **smoothed world** array, `(33, 4)` float32,
columns `(visibility, x, y, z)`. `visibility_th` is accepted and never used.

### 4.2 Hazard 1 — the framework's loop starves the GUI event loop (severe)

`plt.pause(0.001)` is not cosmetic; it is what runs the backend's event loop. Today the plot process
pumps it after every message, and at worst waits 1 s in `queue.get(timeout=1.0)` between pumps — a
window that is visibly sluggish but never wedged.

Under `isolation.md` §3.3, a plugin **with no timers** computes `timeout = None` and blocks in the
stream-queue read until a message arrives. So the handler-only shape shown in `plugin-api.md` §2.1:

```python
@subscribe("pose.smoothed")
def on_pose(self, pose, meta):
    draw(self.ax, pose.world)
    self.fig.canvas.draw_idle()          # queues a repaint that nothing ever services
```

produces a window that repaints only while pose data is flowing and **freezes completely the moment it
stops** — climber steps out of frame, pose plugin quarantines, capture reaches EOF. On macOS a
frozen main-thread window is reported by the OS as "not responding", so the user's conclusion is that
climb-cv hung. Today's version does not do this. It is a regression introduced purely by the process
model, and it is invisible in any test that keeps publishing.

**Fix, entirely within the public API:** give the plugin an `@every` tick and pump from there. The
existence of any timer bounds the child's blocking read, so the loop turns over at the tick rate
whether or not data arrives.

This also cleanly **decouples redraw rate from data rate**, which is an improvement: the handler
becomes a one-line stash and the expensive `ax.cla()`-and-replot happens on the tick.

The fix is available. What is *not* acceptable is that it must be discovered. **Any plugin owning a
GUI must pump its own event loop from an `@every` tick, because the framework's loop blocks in a queue
read** — that sentence is a process-model fact leaking into the authoring interface, it applies to two
of my four plugins (`pose_plot` and `exo_live`), and nothing in `plugin-api.md` says it. See **F-4**.

### 4.3 Hazard 2 — the tick interval cannot come from config

`@every(0.03)` is a decorator argument, evaluated at class-definition time. `self.config` does not
exist yet. So **a timer interval cannot be configured**, and both `pose_plot` (`redraw_hz`) and
`mac_lid` (`poll_interval`, an actual parameter with a default in today's code) want exactly that.

This is **F-2** and it is the API gap I would fix first. The workaround — tick fast and no-op until
the configured period has elapsed — works, but it is per-plugin clock bookkeeping in a framework whose
premise is that authors do none, it burns wakeups, and every author who needs a configurable rate will
write it slightly differently. Requested addition: `self.set_interval(self.redraw, seconds)` callable
from `setup()`. The sketch below shows the ideal form with the fallback in a comment.

### 4.4 Hazard 3 — backend selection under `spawn`

Under `spawn` the child imports matplotlib fresh and resolves a backend from env/rcParams with no
inheritance from the host. On a headless Linux box that is `Agg`: `plt.ion()`, `figure()` and
`draw_idle()` all succeed, no window appears, and nothing warns. Today's code has the same hole, but
today it is one process and the user has other output; a silent plugin in a plugin ecosystem reads as
a broken plugin. Handled in the plugin: a `backend` config key, and a WARNING naming the fix when the
resolved backend is non-interactive.

### 4.5 Topics

| Direction | Topic | Required |
|---|---|---|
| subscribes | `pose.smoothed` | yes |

Nothing published. It is `plugin-api.md`'s "new sink" case and it stays a five-line contact surface.

### 4.6 Manifest

```toml
[plugin]
id                = "pose_plot"
version           = "1.0.0"
api_version       = "1.0"
entry             = "plugin:PosePlot"
name              = "Live 3D Pose Plot"
description       = "Animates smoothed world landmarks in a matplotlib 3D figure."
author            = "Aaron Nguyen"
license           = "Apache-2.0"
requires_topology = ["mediapipe.pose.33"]
requires          = ["matplotlib"]

[[subscribes]]
topic    = "pose.smoothed"
required = true
```

`requires_topology` earns its keep here more clearly than anywhere else in the four: the six polylines
are literal MediaPipe indices, and under `coco.17` they would index a *different* skeleton and draw a
plausible-looking wrong figure. `payloads.md` §4's wiring-time check converts that into a startup
error. This is the mechanism working exactly as advertised.

### 4.7 Config section

```toml
[plugins.pose_plot]
enabled              = false   # matches today's enable_plotting default
redraw_hz            = 30      # 30 == "as fast as the redraw allows", i.e. today's behaviour
visibility_threshold = 0.5     # today's plot_world_landmarks_points default (currently unused)
# backend            = "MacOSX"
```

`enabled = false` preserves `enable_plotting: bool = False`. `redraw_hz = 30` preserves today's
effective rate: a full `ax.cla()` redraw costs tens of milliseconds, so the tick self-paces to the
same place the old `while` loop did.

### 4.8 The port

```python
from climbcv.plugin import Plugin, subscribe, every
from climbcv.contracts import PoseFrame, Meta
from .draw import plot_world_landmarks_points     # unchanged, pure, still CC BY-SA 4.0


class PosePlot(Plugin):
    """Animates smoothed world landmarks in its own matplotlib window."""

    def setup(self) -> None:
        import matplotlib

        backend = self.config.get("backend")
        if backend:
            matplotlib.use(backend, force=True)

        import matplotlib.pyplot as plt

        if matplotlib.get_backend().lower() in _NON_INTERACTIVE:
            self.log.warning(
                "matplotlib resolved the non-interactive backend %r, so no window will "
                "appear. Set a backend explicitly:\n\n"
                '    [plugins.pose_plot]\n    backend = "MacOSX"   # or TkAgg, QtAgg',
                matplotlib.get_backend(),
            )

        self._plt = plt
        plt.ion()
        self.fig = plt.figure()
        self.ax = self.fig.add_subplot(111, projection="3d")

        self._vis_th  = float(self.config.get("visibility_threshold", 0.5))
        self._pending = None

        hz = float(self.config.get("redraw_hz", 30.0))
        self.set_interval(self.redraw, 1.0 / max(hz, 1.0))     # <-- requested; see F-2

    @subscribe("pose.smoothed")
    def on_pose(self, pose: PoseFrame, meta: Meta) -> None:
        self._pending = pose.world          # cheap; the tick does the drawing

    @every(1 / 30)                          # fallback if set_interval is rejected
    def redraw(self) -> None:
        if not self._plt.fignum_exists(self.fig.number):
            self.log.info("plot window was closed")
            self.finish()                   # orderly, not an error -- isolation.md §4.4
            return

        if self._pending is not None:
            plot_world_landmarks_points(self.ax, self._pending, self._vis_th)
            self._pending = None
            self.fig.canvas.draw_idle()

        # Pump the GUI event loop every tick, data or not. Without this the window
        # stops responding whenever pose data pauses. See F-4.
        self.fig.canvas.flush_events()

    def teardown(self) -> None:
        self._plt.close(self.fig)
```

`fignum_exists` → `finish()` is a small win the old code did not have: closing the window today leaves
an orphan process holding a dead figure. `finish()` is right here because `pose.smoothed` has other
subscribers, so per `isolation.md` §4.4 the plugin simply goes away and the app continues.

`flush_events()` rather than `plt.pause(0.001)`: `pause` also sleeps and calls `show()`, and the sleep
is 1 ms of the plugin's own budget for no benefit once the tick governs the rate.

### 4.9 What the process model gets right here

Worth recording, because §4.2–4.4 are complaints and the balance matters. `isolation.md` §2.3 claims
the macOS main-thread GUI constraint is "satisfied by construction," and it is: the child's handler
runs on that child process's main thread, so `plt.figure()` and every draw happen there with no
arrangement by the author. Today matplotlib is *exiled* to a worker precisely because it cannot share
the capture thread; under the new model that exile is just what every plugin gets. The conversion
deletes `plotting_process`'s entire loop, its `None` sentinel, its queue handling and its bare
`except: continue` — about 25 lines of machinery replaced by two decorated methods.

---

## 5. `exo_live` — the live overlay

Four simultaneous inputs, the shared-topic case, and the quit path. `plugin-api.md` §3.5 called this
the plugin `latest()` was designed for; it is also where `latest()` runs out.

### 5.1 What it does today

`exo_live(cv, frame, result, lid_angle, lid_timestamp)`, called synchronously from the host loop after
YOLO boxes are drawn onto the frame by `climbcv._draw_yolo_boxes`:

- if `result.pose_landmarks` is empty, it does nothing but `cv2.imshow` the frame;
- FPS from a module-global `config.prev_time` (`utils/config.py`), drawn top-left;
- skeleton via `mp_drawing.draw_landmarks(frame, landmarks, PoseLandmarksConnections.POSE_LANDMARKS, ...)`
  using **`result.pose_landmarks[0]`** — the *raw, normalised image* landmarks, not the smoothed world
  ones;
- if a lid angle exists: `"Mac Camera Angle: {v} ({latency_ms:.2f}ms ago)"` where latency is
  `(time.time() - lid_timestamp.value) * 1000`;
- body tilt, **nested inside the lid-angle branch**: takes image-space landmarks 11/12/23/24, builds
  a reference "up" vector rotated about X by `radians(lid_angle - 90)`, computes the angle between the
  mid-shoulder→mid-hip vector and it, draws the line in pixel coordinates and the angle as text;
- no lid angle → `"Mac Camera Angle: n/a"` and no tilt.

Boxes are drawn as 1 px green rectangles with `"{label} {conf:.2f}"` above each.

### 5.2 Topics

| Direction | Topic | Required | Why |
|---|---|---|---|
| subscribes | `frame` | **true** | nothing to draw on without it |
| subscribes | `pose.smoothed` | **true** | matches `isolation.md` §5.3's escalation example |
| subscribes | `holds.boxes` | **false** | `broker.md` §4.3 uses exactly this; a run with no detector is normal |
| subscribes | `device.lid_angle` | **false** | absent off macOS by design |
| publishes | `app.shutdown` | — | it owns the window, so it owns ESC |

`required = true` on `pose.smoothed` is a deliberate behaviour change: today the overlay shows a bare
feed when pose is unavailable, and under `isolation.md` §5.3 a quarantined pose publisher will now
shut the app down instead. That is the right call — a climbing-analysis tool showing an unannotated
webcam feed is not degraded, it is broken — and it is the declaration `isolation.md` already assumes.
Stated because it is a change, not because it is in doubt.

### 5.3 `latest()` cannot express a shared topic — the finding

Decision #12 says framework-injected `meta.source` disambiguates overlapping boxes. It does, in a
handler. It does **not** in `latest()`, which returns "the newest payload seen on the topic" with no
`Meta` and no per-source keying. With two detectors running:

- `latest("holds.boxes")` returns whichever detector published most recently, so the overlay draws
  detector A's boxes, then B's, then A's — **flickering between two sets rather than showing both**;
- there is no way to ask for both, and no way to know which one you got.

So the one sugar method that `plugin-api.md` §3.5 justifies by *this exact plugin* fails on the one
shared topic in the standard set. The overlay must instead keep a handler and a dict:

```python
@subscribe("holds.boxes")
def on_holds(self, holds: HoldBoxes, meta: Meta) -> None:
    # holds.boxes is SHARED: several detectors may publish for the same frame.
    # Key by meta.source so a second detector ADDS boxes instead of replacing them.
    self._holds[meta.source] = holds
```

Three lines, correct, and a third-party overlay author who does not know Decision #12 exists will
write `self.latest("holds.boxes")` — because that is what the reference example in `plugin-api.md`
§2.1 shows — and will ship a plugin that flickers the moment anyone adds a second detector, with no
error anywhere. That is a silent-failure path of exactly the class `payloads.md` exists to close.
See **F-1**; it is my highest-severity finding.

A second-order problem the dict creates and the framework should have an opinion on: **stale sources
never expire.** If detector B crashes and quarantines, its last boxes stay in `self._holds` forever.
Handled here with `hold_box_max_age_frames`, but every shared-topic consumer will need the same
bookkeeping.

### 5.4 The skeleton needs an edge list the framework does not ship

`mp_drawing.draw_landmarks` takes MediaPipe's landmark-list protobuf, and `PoseFrame.image` is a
`(33, 4)` float32 array. To keep using it the overlay would have to **reconstruct protobufs from the
array every frame**, which means importing `mediapipe` into the overlay process purely for a drawing
helper and a constant — throwing away one of the specific wins `isolation.md` §2.3 claims ("the pose
process imports mediapipe but not torch; the holds process imports torch but not mediapipe").

Better: draw with `cv2.line`/`cv2.circle` from an edge list. But the edge list is topology data, and
`climbcv.contracts` ships `TOPOLOGY_SIZES` (a count) and nothing else. So every visualiser plugin —
mine, `pose_plot`'s six polylines, and every third-party one — hardcodes its own copy of the MediaPipe
skeleton, which is a shared constant duplicated per plugin with no way to keep the copies honest.

Request: `climbcv.contracts.TOPOLOGY_EDGES: dict[str, tuple[tuple[int, int], ...]]`, keyed by the same
topology ids. Pure data, stdlib-only, respects the "contracts imports nothing but numpy" rule, and it
is the natural companion to `TOPOLOGY_SIZES` — a topology id already claims to name "what index *i*
means", and the connectivity is part of that. See **F-5**.

### 5.5 Manifest

```toml
[plugin]
id                = "exo_live"
version           = "1.0.0"
api_version       = "1.0"
entry             = "plugin:ExoLive"
name              = "Live Overlay (exo_live)"
description       = "Draws the skeleton, hold boxes, FPS, lid angle and body tilt on the live feed."
author            = "Aaron Nguyen"
license           = "Apache-2.0"
requires_topology = ["mediapipe.pose.33"]
requires          = ["opencv-python"]

[[publishes]]
topic = "app.shutdown"

[[subscribes]]
topic    = "frame"
required = true

[[subscribes]]
topic    = "pose.smoothed"
required = true
mode     = "latest"          # <-- requested; see F-10

[[subscribes]]
topic    = "holds.boxes"
required = false

[[subscribes]]
topic    = "device.lid_angle"
required = false
mode     = "latest"          # <-- requested; see F-10
```

`requires_topology` because the body-tilt math indexes 11/12/23/24 by number.

`mode = "latest"` does not exist yet. Without it, `loader.md` §7 emits a WARNING for every
`[[subscribes]]` that has no handler — which is precisely the "subscribe purely to populate
`self.latest()`" pattern that same clause declares legal and that `plugin-api.md` §3.5 recommends. As
written, the flagship first-party overlay logs two warnings at every startup. See **F-10**.

### 5.6 Config section

```toml
[plugins.exo_live]
window_title             = "climb-cv"
show_diagnostics         = false   # end-to-end latency and dropped-frame counts
lid_max_age_ms           = 2000    # older than this reads "n/a (stale)"
hold_box_max_age_frames  = 0       # 0 = never expire, matching today
box_color                = [0, 255, 0]
```

### 5.7 The port

```python
import time
import numpy as np
from climbcv.plugin import Plugin, subscribe, every
from climbcv.contracts import Frame, PoseFrame, HoldBoxes, Scalar, Shutdown, Meta, TOPOLOGY_EDGES
from .draw import draw_skeleton, draw_boxes, body_tilt_degrees


class ExoLive(Plugin):
    """Owns the live cv2 window, and therefore the quit path."""

    def setup(self) -> None:
        import cv2
        self._cv2 = cv2

        self.window   = self.config.get("window_title", "climb-cv")
        self.diag     = bool(self.config.get("show_diagnostics", False))
        self.lid_max  = float(self.config.get("lid_max_age_ms", 2000.0))
        self.box_age  = int(self.config.get("hold_box_max_age_frames", 0))
        self.box_bgr  = tuple(self.config.get("box_color", [0, 255, 0]))

        self._edges = TOPOLOGY_EDGES["mediapipe.pose.33"]     # see F-5
        self._holds: dict[str, HoldBoxes] = {}                # source -> newest; see F-1
        self._prev: tuple[int, int] | None = None             # (seq, t_capture_ns)
        self._fps = 0.0
        self._dropped = 0

    @subscribe("holds.boxes")
    def on_holds(self, holds: HoldBoxes, meta: Meta) -> None:
        self._holds[meta.source] = holds

    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta) -> None:
        img = frame.as_bgr().copy()      # never draw into frame.pixels -- see F-11
        self._track_rate(frame)

        for source, holds in self._holds.items():
            if self.box_age and frame.seq - holds.frame_seq > self.box_age:
                continue
            draw_boxes(self._cv2, img, holds, self.box_bgr)

        pose: PoseFrame | None = self.latest("pose.smoothed")
        lid:  Scalar | None    = self.latest("device.lid_angle")

        self._cv2.putText(img, f"FPS: {int(self._fps)}", (10, 30),
                          self._cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)

        if pose is not None and pose.image is not None:
            draw_skeleton(self._cv2, img, pose.image, self._edges)
            age_ms = (time.monotonic_ns() - lid.t_ns) / 1e6 if lid is not None else None
            if age_ms is not None and age_ms <= self.lid_max:
                self._cv2.putText(img, f"Mac Camera Angle: {lid.value} ({age_ms:.2f}ms ago)",
                                  (10, 60), self._cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
                tilt = body_tilt_degrees(self._cv2, img, pose.image, lid.value)
                self._cv2.putText(img, f"Body Tilt: {int(tilt)} deg", (10, 90),
                                  self._cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            else:
                self._cv2.putText(img, "Mac Camera Angle: n/a", (10, 60),
                                  self._cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)

        if self.diag:
            self._draw_diagnostics(img, frame)

        self._cv2.imshow(self.window, img)
        self._pump()

    @every(0.05)
    def keep_alive(self) -> None:
        # Keeps the window responsive and ESC working even when frames stop.
        # Same GUI-pump requirement as pose_plot -- see F-4.
        self._pump()

    def _pump(self) -> None:
        if self._cv2.waitKey(1) & 0xFF == 27:
            self.publish("app.shutdown", Shutdown(reason="ESC pressed in the climb-cv window"))

    def _track_rate(self, frame: Frame) -> None:
        if self._prev is not None:
            dseq = frame.seq - self._prev[0]
            dt   = frame.t_capture_ns - self._prev[1]
            if dseq <= 0:                       # publisher restarted -- isolation.md §4.3
                self._dropped = 0
            elif dt > 0:
                self._fps = dseq * 1e9 / dt     # CAPTURE rate, not our draw rate
                self._dropped += dseq - 1
        self._prev = (frame.seq, frame.t_capture_ns)

    def teardown(self) -> None:
        self._cv2.destroyWindow(self.window)
```

### 5.8 Behaviour notes and deliberate changes

**FPS now measures capture rate, not the overlay's own rate.** Today the two are the same number
because the overlay runs inside the capture loop. Under isolation, `on_frame` fires at the overlay's
post-conflation rate, so the naive port would display a number lower than the camera is actually
producing. Computing it from `frame.seq` and `frame.t_capture_ns` deltas restores the original meaning
and is more correct than today's `time.time()` difference besides — it also makes drops countable,
which is `plugin-api.md` §4.1's stated goal, for free.

Today's FPS is also only updated when a pose is present (the whole block is inside
`if result.pose_landmarks`). The port updates it every frame. Deliberate: the old coupling was an
accident of nesting, not a feature.

**The skeleton is now drawn from smoothed landmarks, not raw ones.** Today's overlay draws
`result.pose_landmarks` (raw image space) while smoothing is applied only to world landmarks. Under
`payloads.md` §3.2 a single `PoseFrame` carries both arrays, and the overlay subscribes to
`pose.smoothed`. Whether the drawn skeleton actually changes depends on an unanswered question:
**does `core.smooth_oneeuro` filter `image` as well as `world`?** `payloads.md` §3.2 says "ALL FOUR
COLUMNS are filtered" about `world` and says nothing about `image`. If `image` passes through
unfiltered, this port is byte-identical to today. If both are filtered, the overlay skeleton becomes
smoother — a visible improvement, and the body-tilt number shifts slightly. Either is defensible; the
ambiguity is not. See **F-12**.

**Lid staleness now expires.** Today a failed sensor read sets `lid_angle_value.value = None`, so the
overlay falls back to "n/a" immediately. Under the new design a failed read simply does not publish, so
`latest()` keeps returning the last good reading indefinitely — the overlay would show a frozen angle
with an ever-growing staleness figure. `lid_max_age_ms` restores the "n/a" fallback. Worth noting that
`Scalar` has **no representation for "invalid"**, so staleness is the only available mechanism; that is
consistent with `payloads.md` §3.4 putting `t_ns` there for this purpose, but it means every
`Scalar` consumer must implement its own max-age policy.

**Boxes and pose may be a frame or two older than the frame they are drawn on.** True today too
(boxes always were), and now also true of pose. `plugin-api.md` §4.3's declared `join=` would fix the
pose case exactly — but the overlay cannot use it, because a join fires only when *both* have arrived
for a `frame_seq`, and the overlay must keep rendering when pose is briefly absent (climber out of
frame). So the "one item to cut under scope pressure" is one my flagship consumer would not use. I
have no opinion on whether it should ship for others; recorded as data for that decision.

**The overlay no longer slows capture.** Today drawing happens in the capture loop, so overlay cost
directly caps frame rate. Now it does not. A genuine, free improvement from full isolation.

---

## 6. `mac_lid` — the macOS lid-angle sensor

### 6.1 What it does today

`read_swift_lid(lid_angle_value, lid_timestamp, stop_event, poll_interval=0.5)` in its own `Process`:

- `repo_root = Path(__file__).resolve().parents[4]`, builds to `<repo>/build/LidAngle_Compiled`;
- `OVERRIDE_COMPILED = True` hardcoded, so it **recompiles on every process start**:
  `swiftc lid_angle.swift hardware_compat.swift -o <build_path>`, `shell=True`;
- on `CalledProcessError` from the compile it prints and `return`s — the process exits;
- loop: `subprocess.run(binary, shell=True, capture_output=True, check=True)`, regex
  `[-+]?\d*\.?\d+` over stdout, set `lid_angle_value.value` and `lid_timestamp.value = time.time()`;
  on failure set both to `None`; `time.sleep(0.5)`.

The Swift binary (`lid_angle.swift`) prints `"Lid angle sensor not available on this hardware."` and
exits 0 when the HID probe finds nothing, otherwise runs a 30 Hz poll loop for **0.15 s** and prints
the angle. So each poll costs ~150–200 ms of subprocess.

Two host-side facts complete the picture: `climbcv.__init__` disables the whole feature when
`sys.platform != "darwin"`, and the host loop **respawns the process on every frame if it is not
alive** — so a failing compile becomes a 30 Hz `swiftc` fork loop. `isolation.md` §1 names this as the
baseline bug the restart policy exists to correct.

### 6.2 Topics

| Direction | Topic | Kind | Exclusivity |
|---|---|---|---|
| publishes | `device.lid_angle` | stream | exclusive |

No subscriptions. `plugin-api.md`'s "source" case: a plugin that is nothing but a timer and a
`publish`.

### 6.3 Manifest — `platforms` removes the special case cleanly

```toml
[plugin]
id          = "mac_lid"
version     = "1.0.0"
api_version = "1.0"
entry       = "plugin:MacLid"
name        = "macOS Lid Angle Sensor"
description = "Reads the MacBook hinge angle via a Swift IOKit helper."
author       = "Aaron Nguyen"
license     = "Apache-2.0"
platforms   = ["darwin"]

[[publishes]]
topic = "device.lid_angle"
```

`platforms = ["darwin"]` replaces the `sys.platform` check in `climbcv.__init__` outright, and
`loader.md` §3.1's "skip with an INFO line, not an error" is the correct semantic: today's version
*prints a warning* at every construction on Linux, which is noise for a user who did nothing wrong.
This is the clearest case in the four of a first-party special case dissolving into a declaration —
exactly what Decision #7's dogfooding was supposed to surface.

**But `platforms` only covers the static case.** Three dynamic ways this plugin is legitimately
unavailable on a machine that passes `platforms`:

1. no `swiftc` (macOS without Xcode command line tools);
2. compile failure;
3. an Intel Mac or a Mac with no lid-angle HID device — the Swift helper prints
   "not available" and exits 0.

None is a crash, none is the user's fault, and all three should end with the app running normally and
the overlay showing "n/a". `finish()` gives me that (exit 0, no restart, `device.lid_angle` becomes
absent, all its subscribers are optional so nothing escalates) — but `finish()` means "my work is
done," and a log line reading *"plugin mac_lid completed its work"* for *"this Mac has no lid sensor"*
is a support ticket waiting to happen. See **F-13**.

### 6.4 Config section

```toml
[plugins.mac_lid]
poll_interval_s = 0.5     # today's read_swift_lid default
read_timeout_s  = 3.0
force_recompile = false   # today's hardcoded OVERRIDE_COMPILED = True
# build_dir     = "~/.cache/climb-cv/mac_lid"
```

### 6.5 The port

```python
import re, shutil, subprocess, time
from pathlib import Path

from climbcv.plugin import Plugin, every
from climbcv.contracts import Scalar

_NUMBER = re.compile(r"[-+]?\d*\.?\d+")
_UNAVAILABLE = "not available"


class MacLid(Plugin):
    """Publishes the MacBook lid angle, in degrees, roughly twice a second."""

    def setup(self) -> None:
        here = Path(__file__).resolve().parent
        self._sources = (here / "lid_angle.swift", here / "hardware_compat.swift")
        self._timeout = float(self.config.get("read_timeout_s", 3.0))

        build_dir = Path(self.config.get("build_dir") or here / "build").expanduser()
        self._binary = build_dir / "LidAngle_Compiled"

        if shutil.which("swiftc") is None:
            self.log.info(
                "swiftc was not found, so the lid angle sensor cannot be built. Install the "
                "Xcode command line tools (`xcode-select --install`) to enable it. climb-cv "
                "will run without lid angle data."
            )
            self.finish()                                  # see F-13
            return

        self._build_if_stale(build_dir)

        if _UNAVAILABLE in self._read_raw():
            self.log.info(
                "this Mac reports no lid angle sensor (needs an Apple silicon MacBook). "
                "climb-cv will run without lid angle data."
            )
            self.finish()                                  # see F-13
            return

        self.set_interval(self.poll, float(self.config.get("poll_interval_s", 0.5)))  # F-2

    @every(0.5)                        # fallback if set_interval is rejected
    def poll(self) -> None:
        out = self._read_raw()
        # Timestamp AFTER the read: the Swift helper samples at the end of its
        # 0.15 s run loop, so this is closer to the measurement instant than the
        # start would be. payloads.md §3.4 asks for measurement time, not publish time.
        t_ns = time.monotonic_ns()

        match = _NUMBER.search(out)
        if match is None:
            self.log.warning("unexpected sensor output: %r", out.strip())
            return

        self.publish("device.lid_angle", Scalar(value=float(match.group(0)), t_ns=t_ns))

    def _read_raw(self) -> str:
        try:
            done = subprocess.run([str(self._binary)], capture_output=True, text=True,
                                  timeout=self._timeout, check=True)
        except subprocess.TimeoutExpired:
            self.log.warning("lid sensor read timed out after %.1fs", self._timeout)
            return ""
        except subprocess.CalledProcessError as exc:
            self.log.warning("lid sensor read failed (exit %s): %s", exc.returncode, exc.stderr)
            return ""
        return done.stdout

    def _build_if_stale(self, build_dir: Path) -> None:
        force = bool(self.config.get("force_recompile", False))
        fresh = (self._binary.exists()
                 and all(s.stat().st_mtime <= self._binary.stat().st_mtime for s in self._sources))
        if fresh and not force:
            return

        build_dir.mkdir(parents=True, exist_ok=True)
        self.log.info("compiling the Swift lid sensor helper -> %s", self._binary)
        subprocess.run(["swiftc", *map(str, self._sources), "-o", str(self._binary)],
                       check=True, capture_output=True, text=True)
```

### 6.6 Deliberate changes

| Change | Why |
|---|---|
| No respawn loop | The supervisor owns restarts now. `setup_failure_max = 2` (`isolation.md` §5.2) is exactly right for this plugin: a missing compiler will not fix itself on retry five. |
| Compile once, cached by mtime, `force_recompile` opt-in | `OVERRIDE_COMPILED = True` is a hardcoded dev override that recompiles every start. mtime staleness keeps the editing workflow working without the cost. |
| `shell=True` dropped | It buys nothing (no shell features used) and breaks on any path containing a space — likely once the plugin lives under a user-chosen `plugins/` path. |
| `timeout=` added to every read | Today a wedged helper blocks the loop forever. `isolation.md` §5.4 would warn but deliberately not kill, so the plugin must bound its own subprocess. |
| Failed read no longer clears the value | It cannot: not publishing is the only option. The overlay's `lid_max_age_ms` restores the "n/a" behaviour. See §5.8. |
| `t_ns` sampled after the read | Preserves the *intent* of today's staleness display, more accurately. |
| `time.time()` → `time.monotonic_ns()` | Required by `Scalar`, and correct — the old display could go negative across a clock adjustment. |

**Not changed, though it should be later:** each poll pays ~150–200 ms of process spawn plus a 0.15 s
Swift run loop, and a long-lived helper streaming lines on stdout would give both a tighter `t_ns` and
a 30 Hz signal for near-zero cost. That is a redesign of the Swift side, not a port, and it is out of
scope here. Recorded so it is not lost.

### 6.7 The `build_dir` problem

The plugin needs somewhere to write a compiled binary. Today it writes to `<repo>/build/` via
`parents[4]`, which is fragile and will not survive the move into `plugins/`. My default is
`<plugin_dir>/build/`, reached with `Path(__file__).parent` — available to any plugin, so this is not
a first-party privilege.

But writing into `plugins/` is poor hygiene, and it breaks under three foreseeable conditions: a
read-only or system-owned `plugins/` directory, the bundled-plugin root of §2.3 (which lives inside
the installed package), and the `.ccvplugin` archive format `loader.md` §2 defers to v1.x. There is no
framework-provided per-plugin writable location — `log_dir` exists for logs and that is all. Any
plugin that compiles, downloads a model, or caches a calibration has the same gap. See **F-7**.

---

## 7. Cross-check: persistence, and the latent bug

`framework-core` found that `saved_frames.append(...)` sits inside `_queue_plot_landmarks`, which
returns early when `self.plot_queue is None`, so `enable_plotting=False` — **the default** — silently
saves nothing. `isolation.md` §1 and `broker.md` §8 both assign the fix to `core.persist_npy`, an
independent subscriber of `pose.smoothed`. Persistence is `framework-core`'s plugin, not mine, but I
was asked to confirm the structure makes the bug impossible. It does, and here is the argument rather
than the assertion:

1. **The two features share no code path.** `pose_plot` and `core.persist_npy` are separate plugins in
   separate OS processes with separate queues. There is no function either could return early from
   that the other depends on. The coupling is not fixed, it is *unrepresentable*.
2. **Neither can observe the other's enabled state.** `plugin-api.md` §3.6 removes plugin-to-plugin
   lookup entirely. `core.persist_npy` has no way to ask whether `pose_plot` is running, so it cannot
   condition on it even by mistake.
3. **`saved_frames` stops being a module-level global.** It becomes instance state in a process that
   contains nothing else, so the "assigned but never declared" class of bug
   (`self.average_landmarks`) has nowhere to hide.
4. **The failure is now loud rather than silent.** `[plugins."core.persist_npy"] enabled = false`
   appears in the config file the user wrote, and `climbcv topics` lists `core.persist_npy` as a
   subscriber of `pose.smoothed` or does not. The old failure was invisible in every artifact.

**But the fix introduces a new correctness problem in the same feature, and it is worse than it
looks.** `pose.smoothed` is a STREAM topic, and `isolation.md` §3.3 conflates the stream queue
*unconditionally* every loop turn — keeping only the newest message per topic. So a **recorder is
structurally unable to record every frame.** In practice `core.persist_npy` will almost always find one
message per turn and lose nothing, but "almost always" is the problem: any scheduling hiccup silently
drops a frame from the recording, and the resulting `.npy` is short by an unpredictable amount with no
indication anywhere. Today's version records every frame synchronously.

Silent gaps in recorded data are the same class of failure `payloads.md` was written to close. And it
is not specific to `core.persist_npy` — **no third party can write a correct recorder plugin on this
API**, which removes a whole category. See **F-14**; the requested fix is a subscription-level
`conflate = false`.

Two smaller notes on the same plugin, since my config file configures it:

- The `.npy` format should stay `(F, 33, 4)` float32 of **world** landmarks so the existing
  `sample-data/sample_replay.npy` keeps loading. Saving `image` too would need a new format.
- A bare `.npy` array records no `topology`, so a replay plugin must infer it from `L`. With
  `TOPOLOGY_SIZES` containing 33 and 17, `L == 17` is already ambiguous between `coco.17` and any
  future 17-point topology. An `.npz` with a `topology` string, or a sidecar, would close it. Not
  urgent while only one topology publishes, but it is a one-line fix now and a migration later.

---

## 8. Cross-check: `replay()` as a plugin

`broker.md` §8 recommends building `replay()` as a plugin publishing `pose.smoothed` from a `.npy`
file — "the cheapest end-to-end proof that exclusive-topic swapping works." I agree, and it is worth
recording that the pieces are all present:

- `[topics."pose.smoothed"] publisher = "npy_replay"` names it, so the swap is one config stanza;
- `PoseFrame.frame_seq = -1` is already specified for exactly this case (`payloads.md` §3.2);
- `pose_plot` needs **no change at all** to render a replay, which is the actual proof — today
  `replay()` is a 25-line method on `climbcv` that duplicates `plotting_process`'s figure setup and
  its draw loop. That duplication disappears.
- EOF → `self.finish()`, and because `pose.smoothed` has required subscribers, `isolation.md` §4.4
  turns the end of the file into an orderly app shutdown. That is the right behaviour and it falls
  out of declarations already made.

Two things it needs that do not exist: the topology inference above, and — if it is to replay at the
original rate rather than as fast as it can read — a configurable tick, which is **F-2** again.

Not in my assigned four, so not designed here. Recommended as the fifth conversion, because it is
small and it is the only end-to-end test of exclusive-topic swapping that uses real data.

---

## 9. Scorecard: did the API hold?

`plugin-api.md` §1 sets the test: the four extension types must be **the same thing to write**.

| | subscribes | publishes | timer | `setup()` | `teardown()` | lines of contact surface |
|---|---|---|---|---|---|---|
| `yolo_holds` (new stage) | 1 | 1 | — | yes | — | ~30 |
| `pose_plot` (sink) | 1 | — | yes | yes | yes | ~35 |
| `exo_live` (observer) | 4 | 1 | yes | yes | yes | ~55 |
| `mac_lid` (source) | — | 1 | yes | yes | — | ~40 |

They do look alike. No `Process`, `Queue`, `Lock`, `Event`, `Thread`, `async` or `pickle` appears in
any of them. `setup()`-not-`__init__` (Decision #15) is the single best decision in the interface: the
YOLO model, the matplotlib figure, the `cv2` window and the Swift compile are each created exactly
where they are used, and the `model_path` parameter today's `yolo_boxes_worker` had to invent to make
that possible simply does not exist any more.

Three things did make me reason about the process model, which the isolation guarantee says should not
happen:

1. **F-1** — I had to know that `latest()` collapses across publishers on a shared topic.
2. **F-4** — I had to know the framework's loop blocks in a queue read, in order to keep a GUI alive.
3. **F-14** — I had to know conflation is unconditional, to see that a recorder loses data.

All three are *delivery-semantics* leaks rather than concurrency-primitive leaks, so Decision #4 is
bent rather than broken: no author writes a lock, but three of them must understand the queue's drop
policy to be correct. `broker.md` §5.4 already accepts that delivery guarantees must be stated
plainly. The gap is that these three consequences are not among the ones it states.

---

## 10. Friction register, ranked by severity

Severity = (how silently it fails) × (how many third-party authors hit it).

| # | Finding | Severity | Fix requested from | Guardian? |
|---|---|---|---|---|
| **F-1** | `latest()` collapses across publishers, so it cannot express a SHARED topic. Two hold detectors make the reference overlay flicker between them, silently. Decision #12 relies on `meta.source`, which `latest()` does not expose. | **critical** | `framework-core` (`plugin-api.md` §3.5) | **yes** |
| **F-2** | `@every(seconds)` takes its interval at class-definition time, so no timer interval can be configured. `pose_plot.redraw_hz` and `mac_lid.poll_interval_s` (an actual parameter today) both need it. Workaround is per-plugin clock bookkeeping in a framework that promises none. Requested: `self.set_interval(handler, seconds)` from `setup()`. | **high** | `framework-core` (`plugin-api.md` §3.2) | **yes** |
| **F-3** | No mechanism ships the first-party plugins. `pip install climb-cv` + run = no overlay, no hold detection. Requested: a bundled plugin root scanned alongside `plugins/`, with `plugins/` shadowing by id — which requires relaxing `loader.md` §5 rule 7 (duplicate id = fatal) to "fatal within a root, shadow across roots with an INFO line". | **high** | `framework-core` (`loader.md` §2, §5) | no |
| **F-4** | A GUI-owning plugin must pump its own event loop from an `@every` tick, because the child loop blocks in a queue read with `timeout=None` when the plugin has no timers. Without it the window freezes whenever data pauses, and macOS reports "not responding". Affects 2 of my 4. Nothing in `plugin-api.md` says it, and `§2.1`'s `PosePlot` example has the bug. | **high** | `framework-core` (docs + example) and `docs-and-testing` | **yes** |
| **F-5** | `climbcv.contracts` ships `TOPOLOGY_SIZES` but no edge list, so every visualiser hardcodes its own copy of the MediaPipe skeleton — or imports `mediapipe` into its process purely for `PoseLandmarksConnections`, discarding an isolation win. Requested: `TOPOLOGY_EDGES: dict[str, tuple[tuple[int,int], ...]]`. | **high** | `framework-core` (`payloads.md` §4) | **yes** |
| **F-14** | STREAM conflation is unconditional, so a recorder cannot record every frame. `core.persist_npy` will silently lose frames under scheduling jitter, and **no third party can write a correct recorder**. Requested: `[[subscribes]] conflate = false` — deliver every queued message in arrival order (still bounded by depth, so still lossy under real overload, but not gratuitously). | **high** | `framework-core` (`broker.md` §5.1, `isolation.md` §3.3) | **yes** |
| **F-10** | `loader.md` §7 WARNs on any `[[subscribes]]` with no handler — which is exactly the "subscribe to populate `latest()`" pattern the same clause declares legal and `plugin-api.md` §3.5 recommends. The flagship overlay logs 2 warnings at every startup. Requested: `mode = "latest"` on the subscription, or drop the warning to DEBUG. | **medium-high** | `framework-core` (`loader.md` §7) | **yes** |
| **F-11** | `Frame.pixels` mutability is unspecified. Under T1 each subscriber unpickles a private copy, so mutating it is harmless; under T2 (shm) it would corrupt every other subscriber. So `broker.md` §5.3's "T2 is a non-breaking upgrade" is false at the authoring level — working code becomes data corruption on a framework minor bump. Also: does `as_bgr()` return a fresh array when no conversion is needed, or the same one? Requested: document `pixels` as read-only, and specify `as_bgr`/`as_rgb` to always return a writable copy or always a read-only view. | **medium-high** | `framework-core` (`payloads.md` §3.1) | **yes** |
| **F-7** | No framework-provided per-plugin writable directory. `mac_lid` must write a compiled binary somewhere; `<plugin_dir>/build/` works but breaks under a read-only `plugins/`, the bundled root (F-3) or a future archive format. Any plugin that compiles, downloads or caches has the same gap. Requested: `self.data_dir`, framework-created, or a documented convention. Related: §2.4's `importlib.resources` fallback for the 44 MB model. | **medium** | `framework-core` (`plugin-api.md` §2) | no |
| **F-13** | No first-class "gracefully unavailable" outcome. `platforms` covers the static case; the dynamic case (no `swiftc`, no HID sensor) has only `finish()`, whose meaning is "my work is done" — so the log reads "mac_lid completed its work" for "this Mac has no lid sensor". `isolation.md` §4.2 makes any other clean exit a crash, so there is no third option. Requested: `self.unavailable(reason)` (INFO, exit 0, no restart), or document `finish()` as covering it. | **medium** | `framework-core` (`isolation.md` §4.4) | **yes** |
| **F-12** | `payloads.md` §3.2 says "ALL FOUR COLUMNS are filtered" about `world` and says nothing about `image` on `pose.smoothed`. Whether the overlay's skeleton and body-tilt number change from today depends entirely on the answer, and both readings are defensible. Also unspecified: whether the visibility-threshold hold-last logic in today's `_update_landmarks` applies to `image`. | **medium** | `framework-core` (`payloads.md` §3.2) | no |
| **F-9** | `HoldBoxes` raising on out-of-`[0,1]` boxes means every detector author must `np.clip` or their plugin dies mid-run. Requested: clip the range on construction (a normal detector artifact) while still raising on `x1 > x2` (a genuine bug). | **medium** | `framework-core` (`payloads.md` §3.3) | no |
| **F-6** | `broker.md` §5.3's IPC budget treats three `frame` subscribers as alike, but the holds path consumed 0.6 MB/s today and will consume 6.9 MB/s — an 11× rise on one edge, three quarters of it discarded. Fits the 320×240 budget, so no v1 change requested; the throttle no longer saves IPC and the table should say so. A subscription-level decimation declaration would recover it. | **low-medium** | `framework-core` (`broker.md` §5.3) | no |
| **F-15** | `@every(seconds)` does not specify fixed-rate vs fixed-delay, nor catch-up when a handler overruns its interval. `mac_lid` (200 ms handler, 500 ms period) and `pose_plot` (40 ms redraw, 33 ms tick) both depend on the answer, and under fixed-rate-with-catch-up `pose_plot` would fall permanently behind. Requested: specify fixed-delay, no catch-up. | **low-medium** | `framework-core` (`plugin-api.md` §3.2) | **yes** |
| **F-16** | No `setup()` timeout is specified, and the heartbeat starts only after READY (`isolation.md` §3.1 step 9, §5.4 "from the loop"). A plugin whose `setup()` hangs is therefore invisible to the stall detector — and `mac_lid`'s `setup()` legitimately takes seconds (a Swift compile), so the honest cases and the hung case look identical. | **low** | `framework-core` (`isolation.md` §5.4) | no |

None of the sixteen required a private hook or a framework-internals import. That is the headline
result: **the public API is sufficient in kind, incomplete in three specific places (F-1, F-2, F-14),
and under-documented in two more (F-4, F-11).**

---

## 11. Proposed Decision Log entries

Returned for the main thread to integrate; see the summary for the full text.

- **The four first-party conversions ship as ordinary plugins with non-`core` ids** (`yolo_holds`,
  `pose_plot`, `exo_live`, `mac_lid`), discovered by the same scanner as third-party plugins, with no
  privileged code path. Alternative rejected: keeping them as `core.*` built-ins, which would make
  Decision #7's dogfooding claim untestable.
- **Frame-rate throttling in plugins is expressed against `frame.seq`, never against a count of
  handler invocations.** Follows from read-side conflation; the naive form is a silent 4× regression
  and it is the form currently shown in `plugin-api.md` §2.1.
- **GUI-owning plugins pump their own event loop from an `@every` tick.** A named, documented idiom
  rather than folklore, because the failure mode is a frozen window that looks like a hung app.
- **First-party plugins are bundled with the distribution and shadowed by `plugins/`** (pending F-3's
  loader change). Alternative rejected: requiring `climbcv init` to copy them, which makes four
  currently-default features opt-in.

---

## 12. Handoffs

**To `plugin-api-guardian`** — the eight findings marked **yes** above. F-1, F-2, F-4, F-14 are the
ones where a third-party author writes plausible code that is silently wrong; F-11 is the one where a
framework upgrade breaks working code.

**To `framework-core`** — every row of §10. F-3 and F-14 are the two that change your documents
structurally rather than adding to them. F-12 is a question only you can answer and my overlay's
behaviour depends on it.

**To `docs-and-testing`** — §2.2's `plugin.py`/pure-module split is the pattern the authoring guide
should teach; `yolo_holds` is the smallest complete example and `exo_live` the most complete one.
Three things need tests that only exist because of the process model: a GUI plugin with data stopping
(F-4), a recorder against a known frame count (F-14), and two detectors on `holds.boxes` at once
(F-1). Also: `README.md` says "python 3.10+" and `pyproject.toml` says `>=3.10`; Decision #17 makes
both 3.11.
