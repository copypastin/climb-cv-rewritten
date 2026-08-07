# Design: The Plugin Base Class / Authoring Interface

Owner: `framework-core` · Status: **proposed, awaiting review** · Implements the invariant "one
uniform authoring model" (Decision #9)

---

## 1. The test this design must pass

Four extension types (Decision #2) must be **the same thing to write**. If an author has to learn two
different shapes, the premise the architecture was chosen for is broken. So the design is validated
against the four real cases from the existing codebase, all of which must come out looking alike:

| Extension type | Real example | Subscribes | Publishes |
|---|---|---|---|
| **new stage** | YOLO hold detection | `frame` | `holds.boxes` |
| **new sink** | live 3D matplotlib plot | `pose.smoothed` | — |
| **observer** | `exo_live` overlay | `frame`, `pose.smoothed`, `holds.boxes`, `device.lid_angle` | `app.shutdown` |
| **stage replacement** | a different pose model | `frame` | `pose.raw` |
| **source** (a fifth, implied) | mac lid sensor; capture itself | — | `device.lid_angle` |

Nothing in the base class distinguishes these. What a plugin *is* falls entirely out of which topics
it names — which is also why the manifest needs no `type` field (`loader.md` §3.2).

---

## 2. The interface

```python
from climbcv.plugin import Plugin, subscribe, every
from climbcv.contracts import Frame, PoseFrame, HoldBoxes, Scalar, Shutdown, Meta


class Plugin:
    # --- provided by the framework; do not override ---
    config: dict            # this plugin's climbcv.toml section; {} if absent, never None
    log: logging.Logger     # pre-tagged with this plugin's id

    def publish(self, topic: str, payload) -> None: ...
    def latest(self, topic: str): ...        # newest payload seen on a subscribed topic, or None
    def finish(self) -> None: ...            # "my work is done" -- orderly, not an error

    # --- the author's three optional overrides ---
    def setup(self) -> None: ...             # runs in this plugin's own process
    def teardown(self) -> None: ...          # best effort, 1s budget
    # ...plus any number of handlers, bound by decorator
```

That is the whole surface. Five framework members, two lifecycle hooks, two decorators.

### 2.1 The four cases, written out

```python
class YoloHolds(Plugin):                                    # NEW STAGE
    def setup(self):
        from ultralytics import YOLO                        # heavy import, in the child
        self.model = YOLO(self.config.get("model_path", DEFAULT_MODEL))
        self.every_n = self.config.get("every_n_frames", 4)
        self.imgsz   = self.config.get("imgsz", 256)
        self._i = 0

    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta):
        self._i += 1
        if self._i % self.every_n:
            return
        h, w = frame.pixels.shape[:2]
        r = self.model.predict(frame.as_bgr(), imgsz=self.imgsz, verbose=False)[0]
        xyxy = r.boxes.xyxy.cpu().numpy()
        self.publish("holds.boxes", HoldBoxes(
            frame_seq=frame.seq,
            boxes=(xyxy / [w, h, w, h]).astype("float32"),   # normalise -- payloads.md §3.3
            labels=tuple(r.names[int(c)] for c in r.boxes.cls.cpu().numpy()),
            scores=r.boxes.conf.cpu().numpy().astype("float32"),
        ))
```

```python
class PosePlot(Plugin):                                     # NEW SINK
    def setup(self):
        import matplotlib.pyplot as plt
        plt.ion()
        self.fig = plt.figure()
        self.ax  = self.fig.add_subplot(111, projection="3d")

    @subscribe("pose.smoothed")
    def on_pose(self, pose: PoseFrame, meta: Meta):
        draw(self.ax, pose.world)
        self.fig.canvas.draw_idle()

    def teardown(self):
        import matplotlib.pyplot as plt
        plt.close(self.fig)
```

```python
class LidSensor(Plugin):                                    # SOURCE
    def setup(self):
        self.binary = compile_swift_once()

    @every(0.5)
    def poll(self):
        v = read_angle(self.binary)
        if v is not None:
            self.publish("device.lid_angle", Scalar(value=v, t_ns=time.monotonic_ns()))
```

```python
class ExoLive(Plugin):                                      # OBSERVER
    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta):
        img   = frame.as_bgr().copy()
        pose  = self.latest("pose.smoothed")     # may be None early on
        holds = self.latest("holds.boxes")       # optional subscription; may never arrive
        lid   = self.latest("device.lid_angle")  # optional; absent off macOS
        draw_all(img, pose, holds, lid, mirrored=frame.mirrored)
        cv2.imshow("climb-cv", img)
        if cv2.waitKey(1) & 0xFF == 27:
            self.publish("app.shutdown", Shutdown(reason="ESC pressed"))
```

Four plugin types. One shape. No author writes `Process`, `Queue`, `Lock`, `Event`, `Thread`,
`async`, or `pickle`.

---

## 3. Design decisions, with what was rejected

### 3.1 `setup()`, not `__init__`

The single most consequential decision in the interface. `setup()` runs **in the child, after
spawn** — so a model, a `cv2.VideoCapture`, a matplotlib figure, a GPU context, or a subprocess is
created where it is used and never crosses a process boundary. The author's mental model is "my code
runs in one place," which happens to be true.

This is what the existing `yolo_boxes_worker(model_path, ...)` → `YOLO(model_path)` pattern was
working around by hand. The framework now makes it the default shape instead of a trick.

**`__init__` is reserved.** A subclass defining it raises at load with the fix in the message,
because the alternative failure is an opaque pickling error at spawn time:

```
Plugin 'yolo_holds': YoloHolds defines __init__, which climb-cv reserves.

Your plugin runs in its own process, and __init__ happens before that process is set up,
so anything you create there cannot come along. Move it to setup():

    def setup(self):
        self.model = YOLO(...)
```

### 3.2 Handlers bound by decorator

```python
@subscribe("frame")                                 # one topic
@subscribe("pose.raw", "pose.smoothed")             # several topics, one handler; use meta.topic
@every(0.5)                                         # timer, seconds
@every(0)                                           # "call me again as soon as I return" -- sources
```

Rejected — **naming convention** (`on_frame` derived from `frame`). Topic names contain dots, so
`pose.smoothed` → `on_pose_smoothed` collides with a topic literally named `pose_smoothed`, and the
mangling is unguessable in both directions. Explicit is shorter to explain and allows one handler for
several topics.

Rejected — **decorator as the sole source of truth**, dropping `subscribes` from the manifest. It
reads better (DRY) but requires the host to import plugin code to discover the graph, which is
exactly what `loader.md` §1 forbids. The duplication is load-bearing; `climbcv validate` (§loader §7)
keeps the two honest.

`@every(0)` is how a source works: capture's handler blocks in `cap.read()`, returns, is called
again. It is not a spin loop unless the handler doesn't block, and a plugin combining `@every(0)`
with subscriptions gets a startup warning that its subscriptions may be starved.

### 3.3 Handler signature is always `(self, payload, meta)`

Rejected — **a single `msg` object** with `.payload`. Makes the common case wordier
(`msg.payload.pixels`) for metadata most handlers ignore.

Rejected — **making `meta` optional by inspecting the signature.** Signature introspection breaks
under `functools.wraps`, type annotations, and any decorator an author stacks on top, and it fails
with a confusing arity error rather than a clear one. One word of boilerplate beats magic that
misfires.

The annotations in the examples are not required, but they are the cheapest possible documentation of
what arrives, so every example and template uses them.

### 3.4 `publish()` is fire-and-forget, and cannot block

`publish` returns immediately, always. Under the hood it does drop-oldest-then-`put_nowait` on each
subscriber's queue (`broker.md` §5.2). There is no `flush`, no `await`, no return value, no way for a
slow subscriber to slow a publisher down. **This is the mechanism by which "the camera loop never
blocks" survives the refactor**, and it is invisible in the API — which is the point.

`publish` to a topic not in the manifest raises rather than silently dropping, because the plugin has
no queues for it:

```
Plugin 'my_analyzer' published to 'grip.force', which it does not declare.

Add it to plugins/my_analyzer/climbcv-plugin.toml:

    [[publishes]]
    topic       = "grip.force"
    kind        = "stream"     # only the newest value matters
    exclusivity = "shared"     # several plugins may publish it
    payload     = "scalar"
    doc         = "..."
```

### 3.5 `latest()` — the two-input pattern, without state juggling

`self.latest(topic)` returns the newest payload seen on a subscribed topic, or `None`. Sugar for what
an author would otherwise write by hand (stash it on `self` in one handler, read it in another) — but
it is the *right* sugar, because the overlay case needs four inputs at once and hand-rolling four
stashes is four chances to get it wrong.

Deliberately **not** cross-process retained state: a subscriber that starts after the only publish on
a topic sees `None` until the next one. For a 2 Hz sensor that is a ≤500 ms wait. A cross-process
retained store would need a shared mutable region, which is a large amount of machinery for a
half-second.

### 3.6 What is *not* in the interface

| Omitted | Why |
|---|---|
| `on_start` / `on_stop` / `on_pause` / `on_config_change` | No hot reload (Assumption §3). Two hooks are enough; more hooks are more contract to keep. |
| Access to other plugins | Plugins are independent (Assumption §3). No lookup, no direct calls, no shared state. Topics are the only channel — which is what makes any plugin replaceable by any other. |
| `self.frame_size`, `self.fps`, ambient pipeline state | It would be a second, undeclared data channel competing with topics. Everything a plugin needs arrives in a payload. |
| `self.spawn_thread()` / any concurrency helper | Handing authors concurrency primitives is how concurrency knowledge leaks back in. A plugin needing background work should be two plugins, or use `@every`. |
| Priority / ordering control | No ordering semantics exist (`broker.md` §5.4). |

---

## 4. Three points where authors will need help, and what the framework does

### 4.1 "I don't get every frame"

A stream subscriber sees the freshest value, not every value. This is a domain fact, not concurrency
mechanics, so it must be stated plainly rather than hidden: **write handlers per-message; never
assume you saw the previous one.** `Meta.seq` gaps make drops observable, and `frame_seq` on derived
payloads makes correlation possible.

The framework helps by making the drop *visible* rather than by pretending it doesn't happen:
`climbcv topics -v` reports per-subscriber drop counts, so "my plugin is too slow" is measurable
rather than a hunch.

### 4.2 A restarted publisher's `seq` restarts at 0

So a *decrease* in `Meta.seq` means "publisher restarted," not reordering. One line in the guide,
because it will otherwise be discovered as a bug.

### 4.3 Pairing two topics — the declared join

Full isolation means `frame` and `pose.smoothed` arrive independently and may be a frame apart
(`isolation.md` §2.2). An overlay drawing `latest("pose.smoothed")` onto the current frame may
misregister slightly.

```python
@subscribe("frame", "pose.smoothed", join="frame_seq")
def on_pair(self, frame: Frame, pose: PoseFrame, meta: Meta):
    ...   # called once per frame_seq for which BOTH have arrived
```

The framework buffers (bounded, drop-oldest, depth 4) and matches on the named field. This is
precisely the kind of thing that must not be hand-rolled — the naive version grows unbounded and the
careful version is 40 lines of buffering an author should never write. It is also, by the same token,
the most complex thing in the interface.

**It is the one item to cut under scope pressure**, and the fallback is honest: `self.latest()` plus a
`frame_seq` comparison, three lines, correct, marginally more work for the author. Flagged as such
rather than presented as settled.

---

## 5. Templates

Two, shipped in-repo, because in a drop-in ecosystem the first thing an author does is copy something.

- `templates/minimal/` — manifest + a subscriber that logs. ~20 lines, both files.
- `templates/detector/` — subscribes `frame`, publishes a shared topic it declares itself, has
  config, has `setup()`/`teardown()`. Demonstrates every mechanism an average plugin needs.

`docs-and-testing` owns their content; this section fixes their scope so they stay minimal and don't
become a third documentation surface.

---

## 6. Handoffs and open items

**Ready for `plugin-api-guardian` — this is the highest-priority review target in framework-core, as
every symbol here is public and effectively permanent:** §2 (the whole class surface, member names),
§3.1–3.5 (each decision and its error text), §3.6 (the omissions — a reviewer should push back on
anything genuinely needed), §4.1–4.3 (what authors must understand), §5 (template scope).

Specific questions for the guardian:
1. `setup`/`teardown` vs. `on_setup`/`on_teardown` vs. `start`/`stop` — `start`/`stop` collides with
   the host façade's `ClimbCV.start()`/`.stop()` and would confuse search results; `setup`/`teardown`
   matches test-framework vocabulary most Python authors already carry.
2. `latest()` vs. `last()` vs. `current()`.
3. `@every(seconds)` vs. `@tick(hz)`. Seconds composes with `0` for "as fast as possible"; Hz does
   not (`@tick(0)` reads as "never").
4. Is `join=` (§4.3) worth its complexity in v1?
5. Is `finish()` (`isolation.md` §4.4) the right name for "my work is done, not an error"?

**To `plugins-and-config`:** the four conversions are the acceptance test for this interface. **If any
of them needs something not in §2, that is a defect in this layer — report it rather than reaching
around it.** Two known pressure points: the overlay needs four simultaneous inputs (§3.5 / §4.3), and
the lid sensor's Swift compile step needs to happen exactly once in `setup()` rather than per poll as
it does today.

**To `docs-and-testing`:** §2.1's four examples are deliberately written to be liftable into the
authoring guide as the *whole* of "how to write a plugin." If the guide needs substantially more than
these plus the payload docstrings, the interface is too complicated and that is a finding worth
reporting back.

**Open:** §4.3 `join=`; the naming questions above.
