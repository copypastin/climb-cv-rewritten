# Design: The Plugin Base Class / Authoring Interface

Owner: `framework-core` · Status: **revised 2026-08-07 (revision 01)** · Implements the invariant "one
uniform authoring model" (Decision #9)

Revision 01 actions guardian B1, S6, S12, S17, S18, S19, S22, and rulings #1–#5 on the naming
questions; F-1, F-2, F-4, F-7, F-13; and C-2. The four examples in §2.1 are rewritten — three of them
demonstrated bugs. Changelog: [`revision-01.md`](revision-01.md).

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
from climbcv.contracts import Frame, PoseFrame, HoldBoxes, Scalar, Record, Shutdown, Meta


class Plugin:
    # NOTE TO AUTHORS: do not write __init__. climb-cv attaches everything below
    # AFTER constructing you, so inside __init__ none of it exists yet. Whatever
    # you would have put there goes in setup(). See §3.1.

    # --- data, provided by the framework; do not reassign ---
    config: dict                 # this plugin's climbcv.toml section; {} if absent, never None
    config_dir: Path | None      # dir of the config file, for resolving relative paths (§3.11)
    data_dir: Path               # yours to write in; created for you (§3.9)
    log: logging.Logger          # pre-tagged with this plugin's id
    stopping: bool               # True once shutdown has begun; read-only (§3.10)

    # --- methods, provided by the framework; do not override ---
    def publish(self, topic: str, payload) -> None: ...
    def latest(self, topic: str): ...                 # newest payload on a subscribed topic, or None
    def latest_by_source(self, topic: str) -> Mapping[str, Any]: ...   # {publisher_id: payload} (§3.5)
    def set_interval(self, handler, seconds: float | None) -> None: ...  # setup() only (§3.2)
    def finish(self) -> None: ...            # "my work is done" -- may end the run (§3.7)
    def unavailable(self, reason: str) -> None: ...   # "this machine can't run me" (§3.7)

    # --- the author's two optional overrides ---
    def setup(self) -> None: ...             # runs in this plugin's own process
    def teardown(self) -> None: ...          # best effort; 1s budget, declarable (§3.7)
    # ...plus any number of handlers, bound by decorator
```

That is the whole surface: five data members, six methods, two lifecycle hooks, two decorators.

**It grew from seven members to eleven in revision 01, and every addition is traceable to a review
finding rather than to a hunch** — which is worth recording, because "the base class grew" is exactly
the kind of drift a reviewer should be suspicious of:

| Added | Because | Would a third party need it? |
|---|---|---|
| `latest_by_source` | guardian **B1** = **F-1**, found independently by both reviews. Without it a shared topic is inexpressible, which cancels the premise Decision #12 was accepted on. | Yes — any overlay, on the one shared topic in the standard set. |
| `set_interval` | **F-2**. `@every(0.5)` is evaluated at class-definition time, so no timer interval could be configured. Two of four first-party plugins need it. | Yes — every plugin with a configurable rate. |
| `unavailable` | **F-13** + guardian **S17**. There was no third outcome between "worked" and "crashed", so "this Mac has no lid sensor" logged as either a crash or *"completed its work"*. | Yes, and S17's whole point is that first-party plugins had a declarative escape (`platforms`) and third parties had none. |
| `stopping` | guardian **S18**. §3.3 blesses a blocking handler as the way to write a source, then gives it no way to notice shutdown. | Yes — any source with a long blocking read. |
| `data_dir` | **F-7**, made mandatory by **F-3**: bundled plugins live inside a read-only installed package, so `<plugin_dir>/build/` stops working. | Yes — anything that compiles, downloads, or caches. |
| `config_dir` | **C-2**. `[framework]` paths resolve against the config file's directory; plugin-section paths resolved against the CWD, and a plugin had no way to opt into the framework's own rule. | Yes — every plugin with a path option. |

Four members are data and read like data; six are methods and read like methods; nothing here is a
concurrency primitive, and nothing requires knowing that a process exists. The `cv_`/`_cv_` name
reservation in `isolation.md` §4.5 is what makes the *next* addition genuinely additive — these six had
to land before the first plugin shipped, because adding a member that collides with an author's
attribute is not additive at all.

### 2.1 The four cases, written out

`docs-and-testing` is told (§6) to lift these into the authoring guide as *the whole of* "how to write a
plugin", so a bug in one of them is a bug in the guide and then in every plugin copied from it. **Three
of the four had one.** All are corrected here, and each correction is labelled with the finding, because
the wrong version is the version an author's instinct produces.

```python
class YoloHolds(Plugin):                                    # NEW STAGE
    # no __init__ -- see §3.1

    def setup(self):
        from ultralytics import YOLO                        # heavy import, in the child
        self.model   = YOLO(self.config.get("model_path", DEFAULT_MODEL))
        self.every_n = max(1, int(self.config.get("every_n_frames", 4)))
        self.imgsz   = int(self.config.get("imgsz", 256))

    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta):
        # Throttle against frame.seq, NOT a count of calls: you are not called once
        # per captured frame, you are called once per loop turn with the newest frame
        # (broker.md §5.1). Counting calls would compound with conflation. [F-6/F-14 note]
        if frame.seq % self.every_n:
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

> **Corrected: the `self._i % every_n` throttle.** `plugins-and-config` worked the numbers: at 30 fps
> capture and ~130 ms inference, the call-counting form publishes **~1.9 detections/s** against today's
> ~7.5, because the plugin's own decimation compounds with the framework's conflation. `frame.seq` is
> correct in both regimes and no test of the plugin in isolation would have caught the difference.
> `frame.as_bgr()` also no longer needs a `.copy()` — `payloads.md` §3.1 now guarantees it returns a
> fresh, writable, C-contiguous array.

```python
class PosePlot(Plugin):                                     # NEW SINK
    def setup(self):
        import matplotlib.pyplot as plt
        plt.ion()
        self.fig  = plt.figure()
        self.ax   = self.fig.add_subplot(111, projection="3d")
        self._pose = None
        # Interval from config -- a decorator argument cannot read self.config. [F-2]
        self.set_interval(self.redraw, 1.0 / float(self.config.get("redraw_hz", 30)))

    @subscribe("pose.smoothed")
    def on_pose(self, pose: PoseFrame, meta: Meta):
        self._pose = pose                # cheap stash; the redraw happens on the tick

    @every(0.033)                        # default; overridden by set_interval above
    def redraw(self):
        # A GUI-owning plugin MUST pump its own event loop from a timer. Without a
        # timer the child loop blocks in a queue read, so the window would freeze the
        # moment pose data stopped -- and macOS would report "not responding". [F-4]
        import matplotlib.pyplot as plt
        if self._pose is not None:
            draw(self.ax, self._pose.world)
        plt.pause(0.001)                 # THIS is what services the window

    def teardown(self):
        import matplotlib.pyplot as plt
        plt.close(self.fig)
```

> **Corrected: the frozen window.** The original had no timer and called `draw_idle()` from the handler
> — *"queues a repaint that nothing ever services."* `isolation.md` §3.3 gives a plugin with no timers a
> blocking read, so the window repainted only while data flowed and **froze completely** when the
> climber stepped out of frame, the pose plugin quarantined, or a file feed reached EOF. Today's code
> does not do this; it was a regression introduced purely by the process model, invisible to any test
> that keeps publishing. The tick also decouples redraw rate from data rate, which is an improvement.

```python
class LidSensor(Plugin):                                    # SOURCE
    def setup(self):
        if shutil.which("swiftc") is None:
            # Not a crash, and not "my work is done" either. [F-13 / S17]
            self.unavailable("swiftc was not found, so the lid sensor helper cannot be "
                             "built. Install the Xcode command line tools "
                             "(`xcode-select --install`) to enable it.")
            return
        self.binary = compile_swift_once(into=self.data_dir)     # framework-provided [F-7]
        self.set_interval(self.poll, float(self.config.get("poll_interval_s", 0.5)))

    @every(0.5)
    def poll(self):
        v = read_angle(self.binary)
        if v is not None:
            self.publish("device.lid_angle", Scalar(value=v, t_ns=time.monotonic_ns()))
```

```python
class ExoLive(Plugin):                                      # OBSERVER
    def setup(self):
        self._frame = None
        self._pose  = None
        self._holds = {}                          # keyed by source -- see below
        self.set_interval(self.render, 1.0 / float(self.config.get("max_fps", 60)))

    @subscribe("frame")
    def on_frame(self, frame: Frame, meta: Meta):
        self._frame = frame

    @subscribe("pose.smoothed")
    def on_pose(self, pose: PoseFrame, meta: Meta):
        self._pose = pose

    @subscribe("holds.boxes")
    def on_holds(self, holds: HoldBoxes, meta: Meta):
        # holds.boxes is SHARED: several detectors may publish for the same frame.
        # Key by meta.source so a second detector ADDS boxes instead of replacing
        # them. self.latest_by_source("holds.boxes") does the same thing without a
        # handler -- use whichever reads better. [B1 / F-1]
        self._holds[meta.source] = holds

    @every(0.016)                                 # overridden by set_interval
    def render(self):
        # Drawing and the ESC key live on the TIMER, not in on_frame, so the quit
        # path keeps working even if the frame publisher dies. [F-4]
        if self._frame is None:
            return
        img = self._frame.as_bgr()                # already a fresh writable copy
        lid = self.latest("device.lid_angle")     # optional; absent off macOS
        draw_all(img, self._pose, self._holds, lid, mirrored=self._frame.mirrored)
        cv2.imshow(self.config.get("window_title", "climb-cv"), img)
        if cv2.waitKey(1) & 0xFF == 27:
            self.publish("app.shutdown", Shutdown(reason="ESC pressed"))
```

> **Corrected twice.** (a) `self.latest("holds.boxes")` on a **shared** topic returns whichever detector
> published most recently, so with two detectors the overlay **flickers between two sets of boxes rather
> than showing both** — and with a once-publishing route map plus a periodic YOLO detector, the route map
> becomes permanently invisible after the first detection. No error, no log. Both reviews found this
> independently (guardian B1, F-1) and it is the highest-severity finding in either. (b) `cv2.waitKey`
> lived in `on_frame`, so **ESC stopped working the moment frames stopped** — the one path that must
> never fail, coupled to the most failure-prone topic in the graph.
>
> A second-order problem the `_holds` dict creates, and which the framework has an opinion about: **stale
> sources never expire.** If a detector crashes, its last boxes stay in the dict forever. Expire them
> from the payload's own `frame_seq` — which is why `payloads.md` §2.4 makes every payload
> self-timestamping, rather than adding an accessor that hands back `Meta`.

Four plugin types. One shape. No author writes `Process`, `Queue`, `Lock`, `Event`, `Thread`,
`async`, or `pickle`.

**What the corrections cost, stated honestly.** Three of the four examples are now longer, and two have
a stash-plus-timer structure that the naive version did not. That is a real ergonomic cost and it is
`isolation.md` §3.3's blocking read leaking into the authoring interface — `plugins-and-config` was right
to call Decision #4 *bent* rather than broken: no author writes a lock, but a GUI-owning author must know
that the loop blocks. The alternative was worse. The framework cannot pump an arbitrary GUI toolkit's
event loop on the plugin's behalf, and the failure mode of not saying so is a window the OS reports as
hung. So it becomes a **named, documented idiom** (§3.8) rather than folklore, which is the most this
layer can do.

---

## 3. Design decisions, with what was rejected

### 3.1 `setup()`, not `__init__`

The single most consequential decision in the interface. `setup()` runs **in the child, after
spawn** — so a model, a `cv2.VideoCapture`, a matplotlib figure, a GPU context, or a subprocess is
created where it is used and never crosses a process boundary. The author's mental model is "my code
runs in one place," which happens to be true.

This is what the existing `yolo_boxes_worker(model_path, ...)` → `YOLO(model_path)` pattern was
working around by hand. The framework now makes it the default shape instead of a trick.

**`__init__` is reserved**, and the message says why — corrected in revision 01, because the old one
said something false:

```
Plugin 'yolo_holds': YoloHolds defines __init__, which climb-cv reserves.

climb-cv creates your plugin and THEN attaches self.config, self.log, self.publish,
self.latest and the rest to it. Inside __init__ none of those exist yet, so the first
one you touch will fail. Put your setup in setup(), which runs afterwards, in this
plugin's own process:

    def setup(self):
        self.model = YOLO(self.config.get("model_path", "hold_detection.pt"))
```

> **Corrected (guardian S6).** The previous message said *"Your plugin runs in its own process, and
> `__init__` happens before that process is set up, so anything you create there cannot come along"*, and
> this section justified the rule as avoiding *"an opaque pickling error at spawn time."* **Both are
> factually wrong.** `isolation.md` §3.1 instantiates the plugin **in the child, at step 7, after
> spawn** — no plugin instance is ever pickled, so the cited failure cannot occur and nothing "fails to
> come along". The old message taught a false model of what crosses a process boundary, which is worse
> than teaching nothing in a design whose whole premise is that authors should not have to think about
> the boundary.
>
> The rule is still right, for a plainer reason: **ordering**. The framework binds the members at step
> 7, after construction, so inside `__init__` they do not exist.
>
> And the reservation is *narrow*, which is worth saying confidently: under `spawn`, module-level and
> class-body code also run in the child, so `MODEL = YOLO("x.pt")` at module scope is already loaded in
> the right process. The usual "expensive work at import time" trap does not exist here. `__init__` is
> the only hole, and it is a hole about attribute binding.
>
> Pickle-safety *is* a real property of this design — it comes from `spawn` plus child-side
> construction, and would hold even if `__init__` were permitted. Decision #15's log entry needs the same
> correction; proposed replacement text is in revision 01's summary.

Violating it is a `PluginContractError` — non-retryable, one message, no restart, no whole-app
escalation (`isolation.md` §4.5). The old path spent two spawn attempts printing the same message twice
and could escalate to shutting down the entire app, for a one-line mistake.

### 3.2 Handlers bound by decorator

```python
@subscribe("frame")                                 # one topic
@subscribe("pose.raw", "pose.smoothed")             # several topics, one handler; use meta.topic
@every(0.5)                                         # timer, seconds
@every(0)                                           # "call me again as soon as I return" -- sources
```

**`self.set_interval(handler, seconds)` — configurable intervals (F-2).** A decorator argument is
evaluated at class-definition time, when `self.config` does not exist, so **no timer interval could be
configured.** `plugins-and-config` called this the API gap it would fix first: `pose_plot`'s `redraw_hz`
and `mac_lid`'s `poll_interval_s` — *an actual parameter with a default in today's code* — both need it,
as does an original-rate `replay` plugin. The workaround (tick fast and no-op until the period elapses)
is per-plugin clock bookkeeping in a framework that promises none, it burns wakeups, and every author
would write it slightly differently.

```python
def setup(self):
    self.set_interval(self.redraw, 1.0 / float(self.config.get("redraw_hz", 30)))
    self.set_interval(self.debug_dump, None)        # None disables a timer entirely
```

Four rules, each with a reason:

- **The handler must already carry `@every(...)`.** `set_interval` re-parameterises a declared timer; it
  cannot create one. So "what timers does this plugin have" is still answerable by reading the class, and
  the decorator's argument is the documented default.
- **Legal only during `setup()`.** After READY the timer table is frozen (`isolation.md` §3.1 step 11).
  Calling it later raises. Dynamic re-rating is a real want for an adaptive plugin, but allowing it means
  the loop's timer set is mutable from a handler, and relaxing this later is additive whereas tightening
  it would not be.
- `seconds = None` disables the timer; `0` means "as fast as possible" exactly as `@every(0)` does.
- Calling it on a method with no `@every` is a `PluginContractError` (`isolation.md` §4.5), not a silent
  no-op.

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
    unit        = "newton"     # required when payload = "scalar"
    doc         = "..."
```

**`publish` also type-checks the payload against the topic (guardian B5).** One `isinstance` per call,
in the publisher's process:

```
Plugin 'my_analyzer' published a PoseFrame to 'holds.boxes', which carries HoldBoxes.

  topic     holds.boxes   (schema holds.boxes/1)
  expected  climbcv.contracts.HoldBoxes
  got       climbcv.contracts.PoseFrame
```

Without it, the wrong payload enqueued fine and the `AttributeError` surfaced in the **subscriber's**
handler, where `isolation.md` §6.2's ladder logs "handler `on_holds` has raised 148 times" and attributes
the bug to the innocent plugin. The check is also what makes the custom-class trap fail loudly: a
plugin-defined class is not a `climbcv.contracts` type, so it never reaches a queue — see `payloads.md`
§3.6 for why an author-defined class in the pickle stream would otherwise unpickle into the *wrong class*
with no error, and for `Record`, which is the supported way to carry your own data.

### 3.5 `latest()` and `latest_by_source()` — the multi-input pattern, without state juggling

`self.latest(topic)` returns the newest payload seen on a subscribed topic, or `None`. Sugar for what
an author would otherwise write by hand (stash it on `self` in one handler, read it in another) — but
it is the *right* sugar, because the overlay case needs four inputs at once and hand-rolling four
stashes is four chances to get it wrong.

**`self.latest_by_source(topic)` returns `{publisher_id: newest payload from that publisher}`** — an
empty mapping when nothing has arrived, **never `None`**.

> **Why it had to exist (guardian B1 = F-1, found independently by both reviews).** `latest()` discarded
> the envelope, so a **shared**-topic subscriber could not tell which publisher a value came from — the
> exact capability `broker.md` §3.1 cites as what makes `holds.boxes` safe to share, and therefore the
> premise Decision #12 was accepted on. Install the pair from the design's own sample output,
> `yolo_holds` + `route_map`: the route map publishes once, YOLO publishes every 4th frame, and from the
> first detection onward `latest("holds.boxes")` never returns the route map again. Annotated route
> invisible. No error, no log. All three mitigations §3.1 offered — colour by source, filter to one
> source, draw everything — were unreachable through the only accessor the API had.
>
> The name `latest()` is kept and its **return type** is unchanged, per guardian ruling #2, so nothing
> breaks; the shared case gets a second accessor rather than a changed signature.

Rules for both, several previously unspecified:

- **Both raise `UndeclaredTopicError` on a topic the manifest does not subscribe to**, symmetrically with
  `publish()` (guardian B1). Previously undefined, and the asymmetry would have meant a typo'd read
  returned `None` forever while a typo'd write raised immediately.
- **Both reflect messages drained this turn, before any handler has run** (`isolation.md` §3.3). So
  `on_frame` reading `latest("pose.smoothed")` sees *this* turn's pose rather than being needlessly a
  frame stale — which is the whole reason §4.3 exists.
- **`latest()` means "newest known", never "the one you are holding."** Inside a non-conflating handler
  (`broker.md` §5.1.0) `latest(same_topic)` may be ahead of the payload you were given.
- **On a topic with more than one publisher, `latest()` logs a one-time WARNING** naming both sources and
  pointing at `latest_by_source()`, then returns the newest. A wiring-time WARNING fires too
  (`broker.md` §4.2), but the runtime one is the honest signal: it fires exactly when the ambiguity is
  real.
- **Expire stale sources from the payload, not from `Meta`.** A crashed publisher's last payload stays in
  `latest_by_source()`'s mapping forever — the framework does not know the difference between "crashed"
  and "slow". Every payload type carries a `frame_seq` or a `t_ns` (`payloads.md` §2.4) precisely so this
  is a field read: `frame.seq - holds.frame_seq > max_age`. That invariant exists so a third accessor
  exposing `Meta` did not have to.
- Deliberately **not** cross-process retained state: a subscriber that starts after the only publish on
  a topic sees `None` until the next one. For a 2 Hz sensor that is a ≤500 ms wait — but for a
  **once**-publishing source it is forever, which is guardian S9 and is now stated plainly in
  `broker.md` §5.4.1 rather than implied to be brief. A cross-process retained store would need a shared
  mutable region, which is a large amount of machinery; the documented mitigation is to republish static
  data on an `@every` tick.

### 3.6 What is *not* in the interface

| Omitted | Why |
|---|---|
| `on_start` / `on_stop` / `on_pause` / `on_config_change` | No hot reload (Assumption §3). Two hooks are enough; more hooks are more contract to keep. |
| Access to other plugins | Plugins are independent (Assumption §3). No lookup, no direct calls, no shared state. Topics are the only channel — which is what makes any plugin replaceable by any other. |
| `self.frame_size`, `self.fps`, ambient pipeline state | It would be a second, undeclared data channel competing with topics. Everything a plugin needs arrives in a payload. |
| `self.spawn_thread()` / any concurrency helper | Handing authors concurrency primitives is how concurrency knowledge leaks back in. A plugin needing background work should be two plugins, or use `@every`. **Narrowed in revision 01:** `self.stopping` (§3.10) is a read-only boolean, not a concurrency primitive — same category as `self.config`. The blanket phrasing was over-broad and made a blessed idiom unwritable. |
| Priority / ordering control | No ordering semantics exist (`broker.md` §5.4). |
| `join=` on `@subscribe` | Cut from v1 — see §4.3 for the four reasons and the three-line replacement. |
| Path resolution for plugin options | The framework hands over `self.config_dir` (§3.11) and the plugin decides. Resolving for them would require knowing which opaque options are paths, i.e. the typed schema Decision #8 excludes. |

---

### 3.7 `setup()`, `teardown()`, `finish()`, `unavailable()` — the four lifecycle calls

Two the author overrides, two the author calls. Mechanics live in `isolation.md`; this is the contract as
an author reads it.

```python
def setup(self) -> None:
    """Called ONCE, in this plugin's own process, before any handler runs.

    Not once per message and not once per test -- open your model, your camera, your
    window here, and store them on self.
    """

def teardown(self) -> None:
    """Called ONCE, when climb-cv is stopping. Best effort.

    For CLOSING things -- files, devices, windows -- not for finishing work. You get
    1 second by default; if closing genuinely takes longer, declare it:

        [plugin]
        teardown_timeout_s = 8.0        # cap 30.0

    It may run while your inputs are still publishing and while your outputs are
    already gone. Close things; do not coordinate.
    """
```

> **Ruling #1's docstring caveat, which is why "ONCE" is shouted.** `setup`/`teardown` were kept over
> `start`/`stop` (which collides with `ClimbCV.start()`/`.stop()` and would make every doc sentence
> ambiguous about app-versus-plugin) and over `on_setup`/`on_teardown` (which reads as a handler for an
> event named "setup", and no such topic exists). But the vocabulary comes from `pytest`/`unittest`, where
> these run **per test** — so the intuition most Python authors carry is "once per unit of work", and an
> author acting on it would reopen a camera on every frame. Saying "once, in this plugin's own process,
> before any handler runs" costs one line and corrects the one thing the borrowed name gets wrong.

```python
def finish(self) -> None:
    """Declare that your work is complete. Exits cleanly; not an error.

    *** This can end the whole run. *** If you are the resolved publisher of a topic
    that other plugins REQUIRE, climb-cv treats your completion as end-of-run and
    shuts down -- which is right for a video file reaching EOF, and surprising if you
    did not expect it. Check `climbcv topics` to see whether anything requires what
    you publish.

    Legal from setup() too, meaning "there was nothing for me to do".
    """

def unavailable(self, reason: str) -> None:
    """Declare that this machine cannot run you -- no GPU, no sensor, no compiler,
    no camera. Exits cleanly; NOT a crash and NOT a restart.

    `reason` is shown to the user verbatim, so write it as a sentence that tells them
    what to install or plug in. This is the dynamic counterpart to the manifest's
    `platforms` key, and it gets the same treatment: one INFO line, and the topics you
    would have published are simply absent.

    Legal from setup() (the usual case) and from a handler (a device disappeared).
    """
```

**Why `unavailable()` exists rather than overloading `finish()` (F-13, guardian S17).** There was no
third outcome between "worked" and "crashed": `platforms` covered the *static* case declaratively, while
a *dynamic* precondition had only `finish()` — so the log would read *"mac_lid completed its work"* for
*"this Mac has no lid angle sensor"*, and `isolation.md` §4.2 makes any other clean exit a crash. S17
notes the same gap is a first-party/third-party asymmetry: the mac lid sensor gets a declarative skip,
while a third-party plugin needing a GPU gets `Plugin 'depth_holds' has been disabled: it crashed 2
times`, which reads as broken rather than as "you don't have that hardware."

The cheaper fix — defining `finish()`-in-`setup()` as "skip me" — was **declined**, because the log line
*is* the deliverable here. A mechanism with the right mechanics and the wrong sentence has fixed the
smaller half of the finding.

**Both are terminal.** After either, no further handlers or timers run. Neither is retried, and neither
counts against the restart budget. `isolation.md` §4.4 has the full consequence table, including the one
case with teeth: calling either from `setup()` when you are a critical publisher is a **fatal startup
error** rather than a mid-run shutdown, because at startup the framework can still print something
actionable.

### 3.8 If your plugin owns a window, pump its event loop from a timer

**The rule, named so it is documentation rather than folklore:**

> **A plugin that owns a GUI window MUST service that window's event loop from an `@every` handler.
> Never rely on a subscription handler to do it.**

```python
@every(0.016)
def render(self):
    ...
    cv2.waitKey(1)            # cv2:        services the window
    # plt.pause(0.001)        # matplotlib: services the window
```

**Why the framework cannot do this for you.** Your plugin's process runs one loop
(`isolation.md` §3.3): it drains queues, dispatches handlers, fires timers, repeats. The blocking read
between turns is bounded by *your next timer* — so **a plugin with no timers, whose input stops, stops
turning over.** `draw_idle()` queues a repaint that nothing ever services, the window freezes, and on
macOS the OS reports the process as "not responding". The user's conclusion is that climb-cv hung.

Data stopping is not an edge case: the climber steps out of frame, the pose plugin quarantines, a file
feed reaches EOF. And today's single-process version does **not** do this — it is a regression introduced
purely by the process model, and it is invisible to any test that keeps publishing.

The framework cannot fix it centrally because pumping an event loop is toolkit-specific: `cv2.waitKey`,
`plt.pause`, a Qt `processEvents`. There is no generic call, and guessing would be worse than asking.
What the framework *does* do is bound its own blocking read at 1 s regardless of timers
(`broker.md` §5.1), which keeps heartbeats honest — but it cannot pump your window.

**The shape this produces, and the honest cost.** The subscription handler becomes a one-line stash and
the expensive work happens on the tick:

```python
@subscribe("pose.smoothed")
def on_pose(self, pose, meta):
    self._pose = pose                 # cheap

@every(0.033)
def redraw(self):
    draw(self.ax, self._pose)         # expensive, and paced by YOU
    plt.pause(0.001)
```

This is a real ergonomic cost and it is `isolation.md` §3.3's blocking read leaking into the authoring
interface — `plugins-and-config` was right to call Decision #4 *bent* rather than broken: no author
writes a lock, but a GUI-owning author must know that the loop blocks. Two consolations, neither of them
a full defence: it affects only GUI-owning plugins, and it **decouples redraw rate from data rate**,
which is an improvement — the redraw no longer runs once per message it cannot keep up with, and the rate
becomes configurable (§3.2).

A related consequence worth stating separately, because it is the one that costs a user their session:
**read your quit key on the timer too.** `ExoLive` (§2.1) reads ESC from `render`, not from `on_frame`,
so the quit path survives the frame publisher dying. A quit key that only works while data flows is a
quit key that fails exactly when the user most wants it.

### 3.9 `self.data_dir` — somewhere to write

```python
self.data_dir      # Path, absolute, created before setup() runs. Yours alone.
```

`<state_dir>/<plugin_id>/`, where `state_dir` defaults to `./.climbcv` next to the config file. The
framework creates it (`isolation.md` §3.1 step 8), so an author never writes `mkdir(parents=True,
exist_ok=True)`, and never has to decide where a cache belongs.

**Why it had to exist (F-7, forced by F-3).** Any plugin that compiles something, downloads a model, or
caches a calibration needs a writable location, and there was none — `log_dir` exists for logs and that
is all. `mac_lid` compiles a Swift helper; today it writes to `<repo>/build/` via `parents[4]`, which
will not survive the move into `plugins/`. The obvious replacement, `<plugin_dir>/build/`, is available
to any plugin and so is not a first-party privilege — but `plugins-and-config` identified three
conditions that break it, and **`loader.md` §2.1 made one of them the normal case**: bundled plugins live
inside an installed package, which is usually read-only. So F-7 went from tidier to required.

Not a sandbox. `isolation.md` §9 says it plainly: process isolation here is fault containment, not
security (Decision #6), and `data_dir` is a convenience location, not a confinement. A plugin can still
write anywhere it has permission to.

### 3.10 `self.stopping` — how a blocking handler notices shutdown

```python
self.stopping      # bool, read-only. False until climb-cv begins shutting down.
```

Set before the framework waits for your `teardown()`. Read it inside any loop that could run for more
than a moment:

```python
@every(0)
def pump(self):
    while not self.stopping:
        chunk = self.socket.recv(65536)     # may block for seconds
        if not chunk:
            return
        self.publish("acme.audio", Record(...))
```

**Why it is in v1 (guardian S18).** `isolation.md` §3.3 explicitly *blesses* a blocking `@every(0)`
handler as the way to write a source, and then checks the shutdown Event only *between* calls. So an
IP-camera plugin whose `read()` stalls for ten seconds is terminated after `grace_s = 2.0`,
**`teardown()` never runs, and the device handle leaks** — the framework killing a plugin for doing
exactly what it was told to do.

§3.6 previously ruled out "any concurrency helper" and that phrasing was too broad: a read-only boolean is
not a primitive an author can misuse, it is the same category as `self.config`, and there is nothing to
lock. The deeper reason it had to ship in v1 rather than v1.1 is `isolation.md` §4.5's reserved-name set —
adding a member later can collide with an author's attribute, so the additions had to land before the
first plugin shipped.

Companion for the same class of plugin: declare `heartbeat_warn_s` in your manifest if your handler
legitimately blocks for a long time, or the stall warning will fire on every read.

### 3.11 `self.config_dir` — resolving a path a user typed

```python
self.config_dir    # Path | None. The directory containing climbcv.toml, or None if there isn't one.
```

```python
p = Path(self.config.get("model_path", "hold_detection.pt"))
if not p.is_absolute():
    p = (self.config_dir or Path.cwd()) / p
```

**The asymmetry this closes (C-2).** `[framework]` path keys — `plugins_dir`, `log_dir`, `state_dir` — are
resolved against **the config file's directory**. A plugin-section path value is an untouched string that
the plugin resolves against **its working directory**. So `model_path = "models/holds.pt"` in a config
file meant one thing to `log_dir` and a different thing to `yolo_holds`, and a plugin had no way to opt
into the framework's own rule *because it never learned where the config file was*. Nothing warned.

The framework hands over the base directory and stops there. Resolving plugin paths automatically would
require knowing which of a plugin's opaque options are paths — the typed schema Decision #8 excludes —
and a "looks like a path" heuristic would be wrong in both directions. `None` rather than defaulting to
the CWD, because "there was no config file" and "the config file is in the CWD" are genuinely different
situations for a plugin resolving a user-supplied path. Full reasoning: `config-contract.md` §3.5.

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

### 4.2 `Meta.seq` — keyed by publisher, always

`seq` is per **`(publisher, topic)`**, not per topic. Two consequences, and the second one is the
correction:

- **A *decrease* in `seq` from the same source means "that publisher restarted,"** not reordering. A
  restarted child's per-topic `seq` starts again at 0 (`isolation.md` §4.3).
- **On a shared topic, comparing `seq` across sources is meaningless.** Two hold detectors sitting at
  seq 400 and seq 3 are not 397 messages apart; they are two publishers who have each been counting
  their own messages.

So any bookkeeping on `seq` — drop detection, gap counting, restart detection — must be **keyed by
`meta.source`**:

```python
@subscribe("holds.boxes")
def on_holds(self, holds: HoldBoxes, meta: Meta):
    prev = self._seq.get(meta.source)
    if prev is not None and meta.seq > prev + 1:
        self.log.debug("missed %d from %s", meta.seq - prev - 1, meta.source)
    self._seq[meta.source] = meta.seq
```

> **Corrected (guardian S12).** This section previously said only *"a decrease in `Meta.seq` means
> publisher restarted, not reordering"* — which holds per `(publisher, topic)` and is **false per
> topic**, and `holds.boxes` is shared by an accepted decision (#12). Two detectors would make an
> overlay's drop detection fire on alternating messages forever. The wording mattered more than most
> because §6 hands this file to `docs-and-testing` as the whole of "how to write a plugin", so it would
> have been copied into the guide and then into plugins.

### 4.3 Pairing two topics — `latest()` plus `frame_seq`

Full isolation means `frame` and `pose.smoothed` arrive independently and may be a frame apart
(`isolation.md` §2.2). An overlay drawing `latest("pose.smoothed")` onto the current frame may
misregister slightly. Three lines fix it:

```python
@every(0.016)
def render(self):
    frame = self._frame
    pose  = self.latest("pose.smoothed")
    if frame is None or pose is None:
        return
    if pose.frame_seq != frame.seq:        # not the pose for THIS frame
        return                             # ...or draw it anyway and accept the lag
    draw(frame, pose)
```

The choice in that last comment is the author's and the framework should not make it for them: an
overlay usually prefers a slightly stale skeleton to a missing one, while a measurement plugin computing
joint angles against pixel positions wants the exact pair or nothing. `latest()` reflects messages
drained this turn before any handler runs (`isolation.md` §3.3), so this is as fresh as the data gets.

**A declared `join=` was considered and cut from v1 (guardian ruling #4.)** The proposal was
`@subscribe("frame", "pose.smoothed", join="frame_seq")` with the framework buffering and matching on
the named field. It was cut for four reasons, and the first two are correctness problems rather than
cost:

1. **Undefined on shared topics.** With two detectors both publishing `frame_seq = 100`, does the handler
   fire once or twice, and with which source's payload? There is no answer that is right for both an
   overlay and a recorder.
2. **Silently broken against the replay sentinel.** `PoseFrame.frame_seq == -1` means "not derived from a
   live frame" (`payloads.md` §3.2), and `broker.md` §8 *recommends* building `replay()` as a plugin
   publishing `pose.smoothed`. So two recommended features would combine into a handler that never fires
   — a blank overlay, with no error anywhere.
3. **It breaks §3.3's invariant** that the handler signature is always `(self, payload, meta)`, by
   introducing a variadic form whose parameter order must silently match the decorator's topic order.
   Swap the two topics in the decorator and the payloads swap, with no error.
4. **Re-adding it later is a keyword argument with a default** — a clean additive minor. So deferring is
   the cheap direction and shipping it was the expensive one.

If it ever returns, it must specify the shared-topic multiplicity and the `-1` case before anything else.

---

## 5. Templates

Two, shipped in-repo, because in a drop-in ecosystem the first thing an author does is copy something.

- `templates/my_logger/` — manifest + a subscriber that logs. ~20 lines, both files.
- `templates/my_detector/` — subscribes `frame`, publishes a shared topic it declares itself, has
  config, has `setup()`/`teardown()`. Demonstrates every mechanism an average plugin needs.

**The ids must be obviously placeholder — `my_detector`, not `detector` (guardian note).** Two authors who
each copy `templates/detector/` without renaming ship two plugins with the id `detector`, and the user who
installs both gets a **duplicate-id fatal** (`loader.md` §5 rule 8) for a mistake neither author made
visibly. A `my_` prefix is a rename prompt that costs nothing, and the directory name, the `id`, and the
class name should all carry it so that renaming one and forgetting another is caught by the
`id`-versus-directory INFO line.

Three things both templates must contain, each closing a discoverability gap a review found:

- **`# No __init__ -- see setup()`**, because nothing an author reads *in code* said so and
  `def __init__(self): super().__init__()` is the most ingrained reflex in Python (guardian S6).
- **`requires_topology`**, if the template subscribes to a pose topic. `templates/my_detector/` subscribes
  `frame`, so it has no such line — which is precisely how guardian B4's walkthrough starts, an author
  copying a detector template and never learning the concept exists. It is now a mandatory manifest key
  (`payloads.md` §4.0), so the manifest error teaches it; the template should still show the `"any"` form
  in a comment.
- **A commented `[config] keys = [...]`**, so C-6's opt-in typo detection is visible rather than
  discovered.

`docs-and-testing` owns their content; this section fixes their scope so they stay minimal and don't
become a third documentation surface.

---

## 6. Handoffs and open items

**Ready for `plugin-api-guardian` review 02 — this is the highest-priority review target in
framework-core, as every symbol here is public and effectively permanent:** §2 (the whole class surface,
member names, and the six revision-01 additions), §2.1 (all four examples, three of which were
rewritten), §3.1–3.5 (each decision and its error text), §3.6 (the omissions), §3.7–§3.11 (the four
lifecycle calls and the four new members), §4.1–4.3 (what authors must understand), §5 (template scope),
**§7 (the embedding API, entirely new and previously undesigned)**.

**The five naming questions from review 01 are settled** and are recorded here rather than re-asked:
`setup`/`teardown` kept with a docstring caveat (§3.7); `latest()` kept with its return type unchanged and
`latest_by_source()` added beside it (§3.5); `@every(seconds)` kept, with `set_interval` re-parameterising
rather than replacing it (§3.2); **`join=` cut** (§4.3); `finish()` kept with its escalation now documented
(§3.7).

Two things a review-02 reader should push on hardest, because they are where this revision took the most
liberty:

1. **§2's surface grew from 7 members to 11.** Each addition has a finding and a first-party need behind it
   and the table says which — but "every addition was justified individually" is exactly how a surface
   creeps, and a reviewer is better placed than the author to say whether the total is still one uniform
   authoring model.
2. **§7 decided several things it could not derive** (see §7.7). The host is the one participant with no
   manifest, so its declarations had to be invented rather than mapped, and three of the choices are
   deliberately *asymmetric* with the plugin rules.

**To `plugins-and-config`:** the four conversions are the acceptance test for this interface. **If any
of them needs something not in §2, that is a defect in this layer — report it rather than reaching
around it.** Every one of your sixteen findings is answered — `revision-01.md` maps each to a file and
section. The five that change your sketches directly: `latest_by_source` for `holds.boxes` (F-1), a timer
on both GUI plugins (F-4), `set_interval` from `setup()` (F-2), `self.unavailable()` for `mac_lid`'s two
dead ends (F-13), and `self.data_dir` for its build directory (F-7).

**To `docs-and-testing`:** §2.1's four examples are deliberately written to be liftable into the
authoring guide as the *whole* of "how to write a plugin." If the guide needs substantially more than
these plus the payload docstrings, the interface is too complicated and that is a finding worth
reporting back. Note that **three of the four examples previously demonstrated bugs** — a call-counting
throttle that regressed detection 4×, a frozen GUI window, and `latest()` on a shared topic — so if you
have already lifted them, lift them again. §3.8 is the one process-model fact the guide cannot omit, and
§7 is a second audience (host applications) that needs its own short page, not a paragraph inside the
plugin guide.

**Open:**
- Nothing in this file is open. The naming questions are ruled on and `join=` is cut.
- Cross-file items still open are listed in `broker.md` §8 (T2's preconditions, subscription decimation,
  the `retain` kind) and `loader.md` §8 (dependency installation, the archive format).
