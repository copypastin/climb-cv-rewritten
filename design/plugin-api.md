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
