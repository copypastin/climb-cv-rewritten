# Design: Isolation & Fault-Tolerance Runtime

Owner: `framework-core` · Status: **revised 2026-08-07 (revision 01)** · Implements Decisions #4 (auto
isolation) and #5 (contained crashes)

Revision 01 actions guardian S6, S8, S11, S17, S18, S21, S22, the stall-warning and `__main__`-guard
notes, and ruling #5 on `finish()`; F-7, F-13, F-14, F-16; and C-7. It also **corrects Decision #15's
stated justification**, which was factually wrong. Changelog: [`revision-01.md`](revision-01.md).

---

## 1. What the baseline already tells us

The existing code is the precedent, and it is worth being precise about what it got right and what it
got wrong, because the design is a generalisation of the former and a correction of the latter.

**Right, and kept:**
- Heavy/optional work in a separate `Process`, communicating over bounded queues, so the capture loop
  never waits on it (`yolo_boxes_worker`, `plotting_process`).
- Drop-oldest-then-put on a full queue (`_queue_yolo_frame`, `_queue_plot_landmarks`).
- The child constructs its own heavy object from a **path**, not from a pickled model
  (`yolo_boxes_worker(model_path, ...)` → `YOLO(model_path)` inside). This is the single most
  important ergonomic fact in the whole design; §3.2 makes it structural.
- A GUI-owning worker creates its own figure in its own process (`plotting_process` calls
  `plt.figure()` itself).
- Shared `stop_event`, `None` sentinel, `join(timeout=1)` then `terminate()`.

**Wrong, and corrected here:**
- **Unbounded respawn with no backoff.** `start()` re-spawns the lid process *every frame* if it is
  not alive: `if self.enable_mac_lid and (self.thread_lid is None or not self.thread_lid.is_alive()):
  Process(...).start()`. A lid worker that fails immediately becomes a 30 Hz process fork loop.
  Corrected by §5.
- **Silent failure everywhere.** `except Exception: pass` appears in roughly ten places, including
  around the entire landmark-extraction path and around the user's own `on_landmarks` callback. The
  result is a pipeline that cannot be debugged. Corrected by §6: attributable, rate-limited logging
  replaces every bare swallow.
- **A latent bug worth naming**, because the new design must not reproduce it: landmark persistence is
  coupled to plotting. `saved_frames.append(...)` lives inside `_queue_plot_landmarks`, which returns
  early when `self.plot_queue is None`. So running with `enable_plotting=False` silently saves
  nothing. In the new design persistence is `core.persist_npy`, an independent subscriber of
  `pose.smoothed` — the coupling becomes structurally impossible.
- `saved_frames` is a module-level global, and `self.average_landmarks` is assigned but never declared.

---

## 2. Full isolation: every stage in its own process

**Decision: every stage — built-in or third-party — runs in its own child process. The host process
runs only the supervisor, wiring, log aggregation, and the embedding API. No stage runs in the host.**

### 2.1 Alternatives considered

| Option | Assessment |
|---|---|
| **X — capture, pose, smoothing synchronous in the host** (closest to today) | Cheapest and lowest-latency: zero IPC on the capture→pose→smooth path. **Rejected:** it privileges built-ins. A *third-party* pose plugin would run out-of-process while the built-in ran in-process — different latency, and a crash in the built-in kills the app while a crash in the replacement does not. That observable asymmetry is precisely what "one uniform authoring model" forbids, and it makes "capture is swappable" (§1 of BRAINSTORM, which names camera source explicitly) a second-class claim. |
| **Z — capture in the host, everything else isolated** | Tempting: the app's lifecycle *is* the capture loop's lifecycle, and `cv2.VideoCapture` has main-thread folklore attached to it. **Rejected** for the same asymmetry, one step further in: capture is the extension point most explicitly named in the requirements, and a third-party capture plugin would either run in the host (crash kills the app — violates Decision #5) or run in a child (asymmetric with the built-in). |
| **Y — chosen: everything isolated** | Total uniformity. No privileged code path. Nothing to special-case in the loader, the resolver, or the base class. |

### 2.2 What full isolation costs, honestly

- **Latency.** `capture → pose → smooth → overlay` becomes three IPC hops instead of zero. Each hop is
  a queue put/get: sub-millisecond for the small payloads, and see `broker.md` §5.3 for frames.
  Practically the overlay draws a frame or so later than today.
- **Frame throughput.** Capture and pose in different processes means every frame is serialised. This
  is the origin of the ~640×480 ceiling in `broker.md` §5.3 and the reason a shared-memory transport
  for `frame` exists as a designed-but-deferred upgrade.
- **Pose/frame pairing.** Today the overlay draws a frame with the pose computed from that exact
  frame. Now they arrive independently on lossy queues and may be a frame apart. `Frame.seq` /
  `PoseFrame.frame_seq` make the mismatch visible and correctable; `plugin-api.md` §4.3 has the
  three-line correlation idiom. (The `join=` decorator argument that previously appeared there is **cut
  from v1** — guardian ruling #4.)

### 2.3 What full isolation buys, beyond uniformity

- **GUI ownership stops fighting.** Today `cv2.imshow` (from `exo_live`) and `cv2.waitKey` and the
  capture loop all share one thread, and matplotlib is exiled to a worker precisely because it cannot
  share it. Under full isolation every GUI-owning plugin has its own process and its own main thread.
  The macOS main-thread constraint on `VideoCapture`/`imshow` is *satisfied by construction* — each
  child runs its loop on its own process's main thread.
- **Imports narrow.** Today one process imports mediapipe **and** cv2 **and** matplotlib **and**
  ultralytics/torch. Under isolation the pose process imports mediapipe but not torch; the holds
  process imports torch but not mediapipe; capture imports cv2 only. Total resident memory plausibly
  *drops* despite more processes, and startup wall time is the slowest single import rather than the
  sum, because children spawn in parallel.
- **Built-in crashes are contained too.** A MediaPipe GPU-delegate abort no longer takes the app down.
- **Native crashes become attributable.** They are currently indistinguishable from the app dying.

### 2.4 Start method

**`spawn` on every platform, explicitly**, via
`multiprocessing.get_context("spawn")` — not the platform default. macOS and Windows already default
to `spawn`; Linux defaults to `fork`. Forcing `spawn` everywhere means:

- One behaviour to design against and test. Fork-inherited state that works on Linux and breaks on
  macOS is the worst possible bug shape for third-party authors to hit.
- No inherited GPU/CUDA contexts, threads, or open camera handles — `fork` after initialising
  MediaPipe or CUDA is a classic hang, and the app *will* have initialised such things in some child.
- Everything passed to a child must be picklable, which is a constraint the design already embraces
  and which §3.2 turns into an ergonomic feature.

Cost: `spawn` re-imports the framework per child, so `climbcv.plugin` and `climbcv.contracts` must
stay stdlib+numpy (stated in `broker.md` §5.3, repeated because it is easy to erode).

Consequence for host applications: the app entry point must be guarded by
`if __name__ == "__main__":`. The existing README already documents this for Windows; it now applies
everywhere. The framework should detect the missing guard and emit a real message rather than let the
user watch processes multiply:

```
climb-cv must be started from a script guarded by `if __name__ == "__main__":`.

Without it, each worker process re-runs your script and starts climb-cv again.

    def main():
        ClimbCV().run()

    if __name__ == "__main__":
        main()
```

**The detection mechanism, named (guardian note).** `ClimbCV.run()` checks
`multiprocessing.parent_process() is not None` **on entry**. If it is not None, this interpreter is a
spawned child that has re-imported the user's script, so `run()` raises the message above instead of
starting a second app. This catches it on the **first** re-entry; a naive implementation (counting
processes, or noticing duplicate log lines) notices only after the tree has already multiplied, which is
the failure users report as "my machine locked up."

**A notebook is not the missing-guard case, and needs no guard — verified.** Running climb-cv from
Jupyter is the most likely embedding environment (`plugin-api.md` §7), and `__main__` re-import is the
classic `spawn` failure there. Tested on Python 3.13 with `__main__` having no `__file__` and
`__spec__ is None` — exactly the notebook condition:

- the child does **not** re-execute the parent's code (`spawn` installs a synthetic `__main__` when
  there is no importable script path to fix up);
- `parent_process() is not None` is `True` in the child, so the guard check above still works;
- a package-level target plus dataclass/dict/queue arguments pickled and arrived intact.

This works because everything crossing the boundary is defined at package level: `child_main` lives in
`climbcv`, `PluginPlan` and `TopicDescriptor` are `climbcv` dataclasses, and **host callbacks never
cross a boundary at all** — they are invoked in the host (`plugin-api.md` §7.4). The classic notebook
failure is pickling a class defined in a notebook cell, which this design never does. Worth a test in
the suite, because it is one refactor away from breaking: moving `child_main` or a plan dataclass into a
`__main__`-adjacent module would reintroduce it silently for notebook users only.

---

## 3. The child runtime

### 3.1 Startup sequence

```
spawn(child_main, args=(PluginPlan, TopicDescriptors, stream_r, event_r,
                        {topic: [write ends]}, control_w, shutdown_event))
  1. set process title to "climbcv:<plugin_id>"        (best effort; aids `ps`/Activity Monitor)
  2. redirect stdout/stderr to logs/<plugin_id>.log    (BEFORE importing plugin code)
  3. install sys.excepthook + faulthandler.enable(file=that log)
  4. sys.path[0] = plugin dir;  append plugin dir/vendor if present
  5. import the entry module, resolve the class
  6. CONTRACT CHECKS (all non-retryable -- §4.5):
       - is a Plugin subclass
       - does not define __init__
       - every @subscribe topic has a [[subscribes]] entry        (loader.md §7)
       - every @every-decorated handler takes only self
  7. instantiate;  bind config, log, publish, latest, latest_by_source, finish,
       unavailable, set_interval, stopping, data_dir, config_dir   (plugin-api.md §2)
  8. mkdir data_dir if it does not exist
  9. setup()
 10. VERIFY no reserved name was rebound during setup()            (§4.5, guardian S22)
 11. freeze the timer table (set_interval is illegal after this point)
 12. send READY on the control queue
 13. enter the loop (§3.3)
```

Step 2 before step 5 is not cosmetic: a native crash during import produces output on stderr and
nothing else, and it must land in the plugin's own log file to be attributable. Step 3's
`faulthandler` is what turns a segfault into a Python-level stack dump — worth a great deal with
mediapipe, torch, and GPU delegates in the mix.

**Steps 6 and 10 are the contract gate.** Everything checked there is deterministic — it depends only
on the code and the manifest, never on the machine, the data, or the timing — so retrying it is
guaranteed to fail identically. §4.5 makes that a distinct, non-retryable outcome rather than a crash.

**`setup()` has no timeout, deliberately (F-16).** `plugins-and-config` noted that the heartbeat starts
only after READY (step 12), so a plugin whose `setup()` hangs is invisible to the stall detector — and
`mac_lid`'s `setup()` legitimately takes seconds because it compiles Swift, so the honest and hung cases
look identical. Imposing a `setup_timeout_s` was considered and **declined**: any value large enough for
a legitimate model load or compile (tens of seconds) is too large to detect a hang usefully, and any
value small enough to be useful would kill honest plugins on a cold cache or a slow disk. Instead, the
supervisor emits a **progress INFO** for any plugin that has not sent READY after `heartbeat_warn_s`,
repeating every 10 s:

```
Plugin 'mac_lid' is still starting up (12s so far). Its setup() has not finished.
  log: logs/mac_lid.log
```

That distinguishes "slow" from "wedged" the only way that is actually available — by telling the user
which plugin to look at and where its log is — without inventing a threshold the framework cannot know.
Consistent with §5.4's diagnose-don't-intervene reasoning, applied one phase earlier.

### 3.2 `setup()` is where the ergonomics live

`setup()` runs **in the child, after spawn**. This is what makes "authors need no concurrency
knowledge" true rather than aspirational:

- The author never pickles a model. They open it in `setup()`, exactly as `yolo_boxes_worker` does
  today — except that the framework no longer needs to invent a `model_path` parameter to make it
  possible, because `setup()` runs in the right place by construction.
- Unpicklable objects (cv2 captures, matplotlib figures, GPU handles, sockets, subprocesses) are
  created where they are used and never cross a boundary.
- `__init__` on a `Plugin` subclass is reserved by the framework. Authors are told: **put nothing in
  `__init__`; put it in `setup()`.**

#### 3.2.1 Decision #15's mechanism is right; its stated justification was wrong (guardian S6)

Both this section and `plugin-api.md` §3.1 justified reserving `__init__` as avoiding *"an opaque
pickling error at spawn time"*, and the error message told the author that things created there
*"cannot come along."*

**That is factually wrong, and §3.1 of this document proves it: the plugin is instantiated in the child,
at step 7, after spawn.** No plugin instance is ever pickled. The cited failure cannot occur, and the
message taught authors a false model of what crosses a process boundary — which is worse than saying
nothing, because a wrong mental model of the boundary is exactly the thing this design is trying to keep
authors from needing.

**The reservation is still correct, for a different and simpler reason: ordering.** The framework binds
`config`, `log`, `publish`, `latest` and the rest at **step 7**, after construction. So inside `__init__`
none of them exist, and the first thing an author reaches for there is `self.config.get(...)` — which
would be an `AttributeError` on a half-built object, at the least helpful possible moment.

**Worth stating more confidently than before: the reservation is *narrow*.** Under `spawn`, module-level
code and class-body code also run in the child, so the usual "expensive work at import time" trap is
*already harmless here* — an author who writes `MODEL = YOLO("x.pt")` at module scope gets it loaded in
the right process by accident. `__init__` really is the only hole, and it is a hole about attribute
binding, not about pickling.

**Pickle-safety is a real property; it just does not come from this rule.** It comes from `spawn` plus
child-side construction, and it would hold even if `__init__` were permitted. Two corrections follow, and
both are actioned: the error text (`plugin-api.md` §3.1) and the Decision #15 log entry, whose proposed
replacement text is in revision 01's summary.

**Discoverability, which was the other half of S6.** The rule lived only in prose, across two design
files, and `def __init__(self): super().__init__(); ...` is among the most ingrained reflexes in Python —
nothing an author reads **in code** said otherwise. Fixed by putting a commented line in the §2 class
sketch and in **both** templates:

```python
class MyDetector(Plugin):
    # No __init__ -- climb-cv attaches self.config / self.log / self.publish
    # after construction. Everything you would put in __init__ goes in setup().
    def setup(self) -> None:
        ...
```

### 3.3 The loop

```python
while not shutdown_event.is_set():
    drain event_q non-blocking, dispatch each in arrival order   # events never conflated away
    timeout = min(seconds until next @every timer is due, 1.0)   # 1.0 = heartbeat interval
    drain stream_q (blocking up to `timeout`, then non-blocking):
        for a CONFLATING subscription  -> keep only the newest message per topic
        for a NON-CONFLATING one       -> keep every message, in arrival order
    update the latest-cache to the newest drained message of EVERY drained topic
    dispatch: conflated topics once each; non-conflating topics once per message, in order
    fire due timers                                              # fixed delay, no catch-up
    heartbeat on control_q if >= 1s since last
teardown()                                                       # §7.1
exit(0)
```

Properties that matter:

- **One thread.** No locks, no `async`, no author-visible concurrency. Handlers run one at a time, in
  the plugin's own process, on its main thread.
- **Events before streams**, so `app.shutdown` is never queued behind a burst of frames.
- **Conflation is read-side and per-subscription** (`broker.md` §5.1.0), so a slow plugin gets the
  *freshest* value of each topic rather than working through a backlog it can never clear — unless it
  declared `conflate = false`, which is what makes a correct recorder possible (F-14).
- **The blocking read is capped at the heartbeat interval, even with no timers.** Corrected in revision
  01: the original `None if no timers` meant a plugin with no timers whose input paused would stop
  heartbeating and be reported as **stalled while healthy** by §5.4. `pose_plot` is exactly that plugin.
  One wakeup per second per idle plugin buys the removal of a false positive that would have read as a
  framework bug.
- **The latest-cache is updated before dispatch, for every topic drained this turn.** So `on_frame`
  reading `latest("pose.smoothed")` sees *this* turn's pose rather than being needlessly one frame
  stale, which is what `plugin-api.md` §4.3 exists to fix. Previously unspecified (guardian B1). The
  consequence to document: inside a non-conflating handler, `latest(same_topic)` may be **ahead of** the
  payload being handled. `latest()` means "newest known", never "the one you are holding".
- **A blocking `@every(0)` handler is legal** and is how a source works: capture's handler calls
  `cap.read()` (which blocks), returns, and is called again immediately. Shutdown and heartbeat are
  checked between calls, so a 33 ms read delays them by at most 33 ms. A handler that blocks for
  *seconds* should check `self.stopping` inside its own loop (`plugin-api.md` §3.10) and declare
  `heartbeat_warn_s` in its manifest.
- A plugin with `@every(0)` **and** subscriptions would starve those subscriptions. Detected at
  startup → WARNING, not an error (a fast tick alongside subscriptions is legitimate).

#### 3.3.1 Timer semantics, specified (F-15, guardian note)

`@every(n)` did not define whether the interval measures from handler start or return, nor whether
missed ticks coalesce. Irrelevant at 0.5 Hz, load-bearing at `@every(0.033)` — and `plugins-and-config`
found both of its timer-using plugins depend on the answer (`mac_lid`: 200 ms handler on a 500 ms
period; `pose_plot`: 40 ms redraw on a 33 ms tick, which under fixed-rate-with-catch-up would fall
permanently behind).

> **Fixed delay, measured from handler return. Missed ticks are dropped, never queued. At most one
> invocation per timer per loop turn.**

So a handler that overruns its interval simply runs as often as it can — the next call happens on the
next loop turn — and never accumulates a backlog. "Ticks queue up" would be an unbounded-backlog bug
wearing a feature's clothes, and it is the behaviour an author would least expect to have to reason
about. `@every(0)` is the degenerate case: zero delay from return.

The cost of fixed-delay is that the *effective* rate of a slow handler is lower than its declared rate,
which is the honest outcome and is visible in `climbcv topics -v` as the handler's measured rate.

---

## 4. Crash detection and containment

### 4.1 Detection

A single supervisor thread in the host:
`multiprocessing.connection.wait([p.sentinel for p in children], timeout=0.5)`.

`sentinel` is a waitable handle that becomes ready when the process exits, so one thread watches all
children with no polling and no thread-per-child. Chosen over looping `is_alive()` (the existing
approach, which costs a poll per child per tick and detects late) and over `SIGCHLD` (not available on
Windows).

The 0.5 s timeout exists so the same thread can also service backoff timers and heartbeat staleness
checks.

### 4.2 Classification

| Exit | Meaning | Action |
|---|---|---|
| `0` after `self.finish()` | intentional completion (e.g. a file feed reached EOF) | §4.4 |
| `0` after `self.unavailable(reason)` | not applicable on this machine | §4.4 |
| `0` after a `PluginContractError` | a deterministic authoring mistake | §4.5 — **no restart** |
| `0` without any of those | the plugin's loop returned unexpectedly | treat as a crash — an unexplained clean exit is still a plugin that stopped working |
| non-zero | Python-level failure; traceback already sent on the control queue and written to the log | restart per §5 |
| killed by signal (`< 0`) | native crash — segfault, abort, GPU driver fault. **No Python traceback exists.** | restart per §5; the report names the signal and points at the log, where `faulthandler` and the stderr redirect are the only evidence |

**The traceback on the control queue is a string, always** — `traceback.format_exc()`, never an
exception object (`payloads.md` §3.6). A plugin-defined exception class in the control stream would fail
to unpickle in the host and break the crash-reporting path *precisely when a plugin has already
crashed*, which is the worst possible time for the reporting channel to be the second failure. The same
reasoning is why the stdlib `QueueHandler` is the right log transport: its `prepare()` formats the
record and clears `exc_info`/`args`, so log records are picklable by construction.

The native-crash row is not hypothetical. The existing code already has a GPU→CPU fallback for the
MediaPipe delegate because creating it can fail hard, and torch/CUDA in the holds process adds more of
the same. **Signal-level containment with a log file is the primary mechanism for the most likely
class of crash in this application**, which is why §3.1 puts the stderr redirect before the import.

### 4.3 Containment — why nothing else breaks

- **Subscribers of a dead publisher** simply stop receiving. Their loop is a `get` with a timeout plus
  timers, so they never block indefinitely; they idle. No deadlock is possible because no participant
  ever waits without a timeout.
- **Publishers to a dead subscriber** keep publishing into a queue that fills and then drops. The
  publisher is unaffected — this is the never-block guarantee doing its job.
- **Queues and shared handles** stay allocated for the plugin's lifetime including restarts, so a
  restarted child reattaches to the same endpoints and its peers need no rewiring. A restart is
  invisible to every other process except as a gap in `Meta.seq`.
- **`seq` continuity across restart:** the restarted plugin's per-topic `seq` restarts at 0. Consumers
  must therefore treat a *decrease* in `seq` as "publisher restarted," not as reordering. Stated in
  `payloads.md` §2.3's delivery notes and worth a line in the authoring guide.

### 4.4 Intentional completion, and "not applicable here"

Two author-callable exits, and they mean different things to a reader of the log. Both exit 0, neither
is a crash, and neither is ever retried.

| Call | Meaning | Log |
|---|---|---|
| `self.finish()` | "my work is done" — a file feed reached EOF, a batch finished | INFO, `Status(state="finished")` |
| `self.unavailable(reason)` | "this machine can't run me" — no GPU, no sensor, no compiler, no camera | INFO, `Status(state="unavailable")` |

#### 4.4.1 `finish()` can end the user's session, and the docstring must say so (guardian ruling #5)

If the finishing plugin is the resolved publisher of a topic with **required** subscribers, the
supervisor treats it as end-of-run and begins orderly shutdown — a video file ending should stop the
app, not leave it staring at a frozen frame. If all its subscribers are optional, or it publishes
nothing required, it is simply gone and the app continues. **The `required` flag on subscriptions does
triple duty**: startup wiring validation, crash-escalation policy (§5.3), and this. One declaration,
three uses, which is the sign it is at the right level of abstraction.

**But `plugin-api.md` documented `finish()` as `# "my work is done" -- orderly, not an error`, which
reads local and modest**, while this section lets it shut down the entire application. An author writing
a batch plugin over a config-specified image list calls `finish()` when the list is exhausted and, if it
publishes `frame`, has just ended the user's live session without knowing it. Two fixes:

1. The docstring states the escalation (`plugin-api.md` §2, §3.7).
2. **The missing log message.** §5.3 has a good causal message for quarantine-triggered shutdown; this
   path had none, and needed the same shape:

```
climb-cv is shutting down: 'npy_replay' finished its work (reached the end of
sample_replay.npy), and it is the only publisher of 'pose.smoothed', which 3 running
plugins require (exo_live, pose_plot, core.persist_npy).

This is a normal end of run, not an error.

To keep running after it finishes, those plugins' subscriptions would have to be optional.
```

The last two lines are the difference between a user thinking the app crashed and a user thinking the
file ended.

#### 4.4.2 `unavailable()` — the third outcome that did not exist (F-13, guardian S17)

The gap both reviews found from opposite ends: `platforms` gives a **static** precondition a graceful,
declarative skip — INFO line, no drama — while a **dynamic** precondition had nothing.

- F-13, from the first-party side: `mac_lid` must handle "no `swiftc`" and "this Mac has no lid sensor".
  Its only clean-exit option was `finish()`, so the log would read *"mac_lid completed its work"* for
  *"this Mac has no lid angle sensor"*, and §4.2 makes any **other** clean exit a crash. There was no
  third option.
- S17, from the third-party side: this is a **first-party/third-party asymmetry**. The mac lid sensor
  gets a declarative skip via `platforms = ["darwin"]`; a third-party plugin needing a GPU, a camera, a
  file or an env var must raise in `setup()`, producing two spawn attempts and
  `Plugin 'depth_holds' has been disabled: it crashed 2 times` — which reads as *broken* rather than
  *"you don't have that hardware."*

**Resolution: `self.unavailable(reason)`, legal from `setup()` and from a handler**, and `finish()` is
also made explicitly legal in `setup()` (S17 asked whether it was; it now is, meaning "nothing to do").

```
Plugin 'mac_lid' is not available on this machine: swiftc was not found, so the lid
angle sensor helper cannot be built. Install the Xcode command line tools
(`xcode-select --install`) to enable it.

climb-cv will run without it. 'device.lid_angle' now has no publisher.
```

Mechanics — deliberately identical to a `platforms` skip, so there is one concept with two triggers:

- exit 0, INFO, **no restart, no quarantine, no crash count.**
- The plugin is removed from the graph, and **the absent-topic check is re-run**. If it was the resolved
  publisher of a topic with required subscribers, the outcome depends on when:
  - **before READY** → **fatal startup error**, using `broker.md` §4.3's starved-subscription message.
    At startup we can still print something actionable, and starting an app that cannot work is worse
    than refusing to.
  - **after READY** → §5.3's orderly shutdown, with `unavailable`'s reason in the causal message.
- `Status(state="unavailable")` on `app.status`, so a status UI can distinguish it from a crash. That
  distinction is the entire point of adding a method rather than overloading `finish()`.

**Why a new method rather than reusing `finish()` (declining half of S17).** S17's cheapest fix was to
define `finish()`-in-`setup()` as meaning "skip me". Declined, because the log line is the deliverable
here: the whole finding is that *"mac_lid completed its work"* is the wrong sentence, and a mechanism
that produces the right mechanics with the wrong words has fixed the smaller half of the problem. One
extra method on the base class, traceable to a first-party need and a third-party symmetry, is worth an
accurate log line — this is the same reasoning that made `Frame.color` a declared field rather than an
assumption.

### 4.5 `PluginContractError` — deterministic mistakes are not crashes (guardian S6)

A distinct, **non-retryable** outcome for a mistake that cannot come out differently on a second
attempt. Raised by the step 6 and step 10 checks in §3.1:

- the entry class does not subclass `Plugin`;
- it defines `__init__` (§3.2.1);
- `@subscribe("x")` with no `[[subscribes]] topic = "x"` (`loader.md` §7);
- an `@every` handler with the wrong signature, or `set_interval` on a handler with no `@every`;
- **a reserved name was rebound during `setup()`** (guardian S22 — see below);
- a publisher's first payload contradicts its `provides_topology`, or a per-delivery topology mismatch is
  reported against it (`payloads.md` §4.1).

Behaviour: **one message, quarantine on first occurrence, no retry, and no §5.3 whole-app escalation.**

The old path was three stacked messages with the useful one buried: two spawn attempts (two identical
copies of the right message), a quarantine summary, and — for a critical publisher — a §5.3 escalation to
whole-app shutdown, all for a one-line typo. Suppressing the escalation is *not* the same as ignoring the
consequence: if the plugin was a critical publisher, the failure happens before READY, so §4.4.2's
before-READY rule applies and the user gets **exactly two** messages in causal order — the contract error,
then the starved-subscription error naming what it cost. That is the minimum honest number.

**Reserved names, and why the check is worth ten lines (guardian S22).** The base class occupies member
names, and authors assign freely to `self` in `setup()`. `self.latest = deque(maxlen=10)` shadows a
framework method and produces `TypeError: 'deque' object is not callable` from a handler, hundreds of
lines away from the cause. Conversely, any member the framework *adds* in a minor version can collide
with an attribute an existing plugin already uses — which would make §4's "additive minors" claim false
for the base class specifically.

Both directions are closed by one rule and one check:

> **Reserved on `self`:** `config`, `config_dir`, `data_dir`, `log`, `publish`, `latest`,
> `latest_by_source`, `finish`, `unavailable`, `set_interval`, `stopping`, `setup`, `teardown`, **and any
> name beginning with `cv_` or `_cv_`.** Everything else is yours, now and in every future v1.x.

Step 10 compares the instance's attributes against that set after `setup()` returns and raises
attributably:

```
Plugin 'my_analyzer': setup() replaced self.latest, which is a climb-cv method.

These names belong to climb-cv and cannot be reassigned: config, config_dir, data_dir,
log, publish, latest, latest_by_source, finish, unavailable, set_interval, stopping,
setup, teardown, and anything starting with cv_.

Rename yours -- self.my_latest, or self._latest.
```

The `cv_`/`_cv_` prefix reservation is what makes future additions genuinely additive, and it is why
S18's `self.stopping` and F-2's `self.set_interval` could be added in v1 rather than waiting: with a
reserved prefix in place, the *next* six additions cost nothing, but these six had to land before the
first plugin shipped.

---

## 5. Restart and backoff policy

Correcting the baseline's respawn-every-frame behaviour.

### 5.1 Backoff

Delays: **0.5 s, 1, 2, 4, 8, 16, 30 s (capped).** Per plugin, independent.

### 5.2 Budget and quarantine

- `restart_max = 5` crashes within `restart_window_s = 60` → **quarantined**: no further restarts, one
  clear summary line, the app keeps running.
- The counter resets once the plugin has stayed alive for a full window, so a plugin that fails once
  an hour keeps being restarted forever, which is correct for transient faults.
- **Setup-phase failures give up faster: 2 attempts, then immediate quarantine.** A plugin that
  crashes before ever publishing usually cannot initialise at all — a missing model file, a missing
  module, no camera — and retrying five times with backoff just delays the message the user needs by
  half a minute. Distinguishing "failed before READY" from "failed after READY" is a one-bit
  distinction with real diagnostic value.

```
Plugin 'yolo_holds' has been disabled: it crashed 5 times in 47s.

  last exit: signal 11 (SIGSEGV) -- native crash, no Python traceback
  log:       logs/yolo_holds.log

climb-cv is still running. 'holds.boxes' now has no publisher, so hold boxes will not
appear. To silence this, set:

    [plugins.yolo_holds]
    enabled = false
```

The message states what the user *loses*, not only what failed. In a plugin ecosystem "why did the
boxes stop" is the question actually being asked.

### 5.3 Escalation for critical plugins

Quarantining the resolved publisher of a topic that has **required** subscribers means the app can no
longer do its job. Rather than sit there presenting a frozen skeleton, the supervisor logs at ERROR
and begins orderly shutdown:

```
climb-cv is shutting down: 'core.pose_mediapipe' was disabled after 5 crashes in 38s,
and it is the only publisher of 'pose.smoothed', which 3 running plugins require
(exo_live, pose_plot, core.persist_npy).

  log: logs/core.pose_mediapipe.log

To keep running without pose data, those plugins' subscriptions would have to be optional.
```

Note this fires for pose but **not** for hold detection, because the overlay's `holds.boxes`
subscription is `required = false`. The behaviour falls out of declarations already made rather than
from a hardcoded list of critical plugins — no structural ceiling.

Overridable via `[framework] shutdown_on_critical_quarantine = false` for embedded uses that would
rather degrade than exit.

### 5.4 Hangs — detected, deliberately not killed

A plugin stuck in an infinite loop or a wedged native call never exits, so `sentinel` never fires. The
app is *unaffected* — the hung plugin stops consuming, its queue fills, publishers drop, everything
else continues. The never-block invariant holds with no liveness detection at all.

But the author gets no signal, so: the child runtime heartbeats on the control queue every 1 s **from
the loop**, meaning a handler that does not return stops the heartbeat. Since revision 01 the loop's
blocking read is capped at 1 s (§3.3), so an *idle* plugin still heartbeats — the original design would
have flagged every no-timer plugin as stalled the moment its input paused.

After `heartbeat_warn_s` (default 5.0, declarable per plugin in the manifest) of silence:

```
Plugin 'ip_camera' has been inside a handler for 10.4s without returning
(heartbeat_warn_s = 5.0).

This is expected if it makes long blocking calls -- a slow network read, a large model
load, a subprocess. It is not an error and climb-cv is unaffected; while it is in there
it receives no new messages and its incoming messages are being dropped.

If this plugin normally blocks for that long, its author can declare a longer budget:

    [plugin]
    heartbeat_warn_s = 15.0

  log: logs/ip_camera.log
```

**The wording matters here (guardian note).** §3.3 explicitly *blesses* a blocking `@every(0)` handler as
the way to write a source, so this warning will fire on the design's own recommended idiom — a 10 s IP
camera read warns twice per read. Worded as *"has not returned from a handler"* with no context, that
reads as a framework complaining about a bug it told you to write. The message therefore names the
threshold, names the config key that raises it, says plainly that it is not an error, and states the only
actual consequence (dropped inbound messages). And `heartbeat_warn_s` becomes a per-plugin manifest key
(`loader.md` §3.1) so a plugin that legitimately blocks for 10 s can say so once instead of warning
forever.

**It is not killed.** Killing a process mid-native-call risks leaving a GPU context or a camera device
in a bad state, and the failure is already contained, so termination would trade a contained problem
for an uncontained one. Diagnose, don't intervene. (A `--kill-stalled` opt-in is noted for later; not
v1.)

---

## 6. Attributable logging

Replaces every `except Exception: pass` in the baseline.

### 6.1 Two destinations, deliberately

1. **`logs/<plugin_id>.log`** — the plugin's own file. Receives its `self.log` output *and* its raw
   stdout/stderr, so `print()` in a plugin (which third-party authors will absolutely use) and native
   crash output both land somewhere findable. Nothing is lost. (`log_level` is validated against
   `{CRITICAL, ERROR, WARNING, INFO, DEBUG}` — C-4; see §8.)
2. **The aggregated host log/console** — every plugin's records interleaved in time order, each
   prefixed with its plugin id. Shipped over the control queue (a `QueueHandler`, stdlib
   `logging.handlers`, so no dependency). This is where causality across plugins is visible: pose
   stopped at T, overlay started warning at T+0.2.

Both, not one: the aggregate answers "what happened," the per-plugin file answers "what exactly did
this plugin do," including output that never reached the logging system.

### 6.2 Handler exceptions: an escalation ladder, never a silent swallow

A handler raising is **not** fatal — a per-frame transient (a truncated frame, an empty detection)
should not quarantine a plugin. But an endless stream of exceptions is both noise and a plugin that is
not working.

| Occurrence | Behaviour |
|---|---|
| 1st | ERROR with the full traceback, plugin id, topic, and `Meta.seq` |
| 2nd … Nth within 5 s | suppressed |
| every 5 s | `handler on_frame has raised 148 times in 5s (last: ValueError: ...)` |
| 100 consecutive failures with zero successes | the plugin is broken, not glitching → exit non-zero → §5 restart/quarantine |

Every record carries the plugin id, injected by the framework. A plugin cannot log anonymously, and
`self.log` cannot be pointed at another plugin's identity.

The user's own embedding callback is included in this discipline. Today `on_landmarks` is wrapped in
`try: ... except Exception: pass`, so a bug in the host application's callback is invisible. It gets
the same ladder, attributed to `<host>` (`plugin-api.md` §7.4).

### 6.3 The lifecycle vocabulary — one state per transition (guardian S21)

`Status.state` is a closed set (`payloads.md` §3.5). This table is the mapping, and it is the
specification: every lifecycle transition in this document emits **exactly one** of these, with
`Meta.source == "<host>"`.

| Transition | `state` | Where |
|---|---|---|
| READY received | `ready` | §3.1 step 12 |
| child exited unexpectedly, restart pending | `crashed` | §4.2, §5.1 |
| replacement child reached READY | `restarted` | §5.1 |
| restart budget exhausted, or a contract error | `quarantined` | §5.2, §4.5 |
| no heartbeat for `heartbeat_warn_s` | `stalled` | §5.4 |
| `unavailable()` called | `unavailable` | §4.4.2 |
| `finish()` called | `finished` | §4.4.1 |
| shutdown Event set for this plugin | `shutdown` | §7 |

Human-facing message prose stays human — "has been disabled", "is shutting down", "is not available on
this machine" are all better sentences than their state names. The point of the closed set is that the
*machine-readable* field a status UI matches on is one of eight strings with a change rule, rather than
prose that drifts. The divergence guardian S21 found — this document's messages saying "has been
disabled" while `payloads.md` listed "quarantined" — was exactly that drift, one document apart, before
anything shipped.

---

## 7. Orderly shutdown

Triggered by any of: `app.shutdown` from a plugin (ESC in the overlay window), SIGINT/SIGTERM in the
host, `ClimbCV.stop()`, intentional completion of a critical publisher (§4.4), critical quarantine
(§5.3).

```
1. set the shared shutdown Event                       (mp.Event, not Manager -- no manager process)
2. wait up to max(grace_s, max declared teardown_timeout_s) for children to exit 0
   -- children see the Event in their loop, run teardown(), exit
   -- a plugin that declared teardown_timeout_s = 20 gets 20s here, not 2
3. terminate() survivors;  wait 1.0s
4. kill() survivors
5. drain the control queue;  flush logs
6. report: for each plugin -- clean | terminated | killed | already dead
```

**There is no teardown ordering, and the design does not pretend to offer one.** A single shared Event
is observed by every child at once, so all `teardown()` calls run concurrently. Ordered shutdown would
need per-plugin flags released in dependency order, which is machinery for a problem that does not
exist here: a publisher still running while its subscriber tears down just puts into a queue nobody
drains, and those messages drop — which is the normal, harmless steady-state behaviour of every stream
topic. Authors are therefore told plainly: **`teardown()` may run while your inputs are still
publishing and while your outputs are already gone.** Close files, release devices, don't coordinate.

### 7.1 The teardown budget is declarable, because 1 second can lose the user's session (guardian S8)

Originally: `teardown_timeout_s = 1.0`, framework-wide, exceeding it is a WARNING and the process is
terminated — with authors told that teardown "is for closing files, not for finishing work."

**That instruction is right in general and wrong for the one first-party plugin whose entire purpose is
to write something at the end.** `core.persist_npy` exists to save the run's landmarks. Hour-long
session, slow external drive, teardown exceeds 1 s, process terminated **mid-write**, hour of climbing
gone with a WARNING. A third-party MP4 writer finalising a container is worse — that routinely takes
seconds. This is the dogfood test failing in the sense Decision #7 cares about: a first-party plugin
needing something the public API cannot express.

Two changes, and it needs both — either alone leaves a hole:

**(1) The budget is per plugin.** `[plugin] teardown_timeout_s` in the manifest, default `1.0`, hard cap
`30.0`, config-overridable per plugin. The shutdown sequence waits `max(grace_s, max declared)`, so one
patient plugin does not slow the others' *termination* but does get its own time. A declared value above
1.0 gets one INFO line at startup, so a shutdown pause is explained in advance rather than looking like a
hang:

```
Plugin 'mp4_writer' has asked for up to 8.0s to finish writing when climb-cv stops.
Shutdown may take that long.
```

The cap exists because the budget is a *promise to the user about how long quitting takes*, and an
unbounded one turns ESC into a coin flip. 30 s is long enough for any container finalisation and short
enough that a user has not concluded the app is wedged.

**(2) `core.persist_npy` appends incrementally, so its teardown is a `close()`.** Even with a generous
budget, a recorder that buffers a whole session in memory and writes it at the end is one crash away
from losing everything — and a crash is *more* likely than a slow teardown. §8.2 specifies the
incremental format. With it, `teardown_timeout_s` for persistence stays at the default 1.0, and the
declarable budget is there for the third-party cases that genuinely cannot be made incremental.

Together these mean: **the framework no longer terminates a recorder mid-flush, and a recorder no longer
has a single point at which it can lose everything.** Fixing only the timeout would have left the crash
path; fixing only the format would have left the third-party MP4 writer.

Authors are still told: teardown is for closing files and releasing devices, not for finishing work — and
now also: *if closing your file takes more than a second, say so in your manifest, and prefer writing as
you go.*

The sentinel-`None`-on-the-queue pattern from the baseline is **dropped** in favour of the shared
Event. The sentinel only reaches a child that is actually reading its queue, and one sentinel is
consumed by one reader — it does not generalise to fan-out. An Event is observed by everyone,
including plugins with no subscriptions at all (capture, the lid sensor), which the sentinel could
never reach.

---

## 8. Configuration surface, and the two built-ins that shaped this document

### 8.1 `[framework]` knobs

All optional, all under `[framework]`, all with the defaults below. Full shapes and the exported
`FRAMEWORK_DEFAULTS` table in `config-contract.md` §2 / §1.2:

`plugins_dir` ("./plugins"), `use_bundled_plugins` (true), `log_level` ("INFO"), `log_dir` ("./logs"),
`state_dir` ("./.climbcv"), `stream_depth` (**0** = computed), `max_stream_depth` (256),
`event_depth` (256), `restart_max` (5), `restart_window_s` (60.0), `restart_backoff_cap_s` (30.0),
`setup_failure_max` (2), `heartbeat_warn_s` (5.0), `grace_s` (2.0), `teardown_timeout_s` (1.0),
`shutdown_on_critical_quarantine` (true).

Four revision-01 changes to this list:

- **`stream_depth`'s default is `0`, not `4`** (guardian S10). `0` means "computed per subscriber" per
  `broker.md` §5.1.2. Documenting `4` invited a user to paste it in and silently disable the
  cross-topic starvation mitigation.
- **`state_dir` is new** (F-7), the parent of every plugin's `data_dir` (`plugin-api.md` §3.9). Resolved
  relative to the config file's directory, exactly like `log_dir`.
- **`max_stream_depth` is new**, capping the arithmetic in `broker.md` §5.1.2 so a typo'd per-subscription
  `depth` cannot ask for gigabytes of in-flight frames.
- **`use_bundled_plugins` is new** (F-3), for running against only the user's own `plugins/`.

`teardown_timeout_s` and `heartbeat_warn_s` are additionally **per-plugin manifest keys** (§7.1, §5.4);
the `[framework]` value is the default for plugins that do not declare one, and
`[plugins.<id>] teardown_timeout_s` lets a user override an author's choice.

`log_level`'s value set is closed — `{CRITICAL, ERROR, WARNING, INFO, DEBUG}` — and that is carried in
`FRAMEWORK_DEFAULTS` as an allowed-value tuple so the message naming the config file comes from the
parser that knows the file (C-4, `config-contract.md` §1.2). It is the only framework key with a closed
set, which is why one allowed-value field is not a schema layer.

---

### 8.2 `core.persist_npy` — the recorder, and what it needed from the framework

Two review findings meet in this one built-in, and together they made persistence unsound at **both**
ends. Recorded here rather than only in the changelog, because it is the clearest instance of Decision
#7's dogfooding doing the job it exists to do.

| | The problem | The framework change it forced |
|---|---|---|
| **During the run** (F-14) | STREAM conflation was unconditional, so a recorder was *structurally unable* to record every frame. In practice one message per loop turn and nothing lost; under any scheduling jitter, a silently missing frame and a `.npy` short by an unpredictable amount. **And no third party could write a correct recorder at all.** | `[[subscribes]] conflate = false` (`broker.md` §5.1.0) |
| **At the end** (S8) | `teardown_timeout_s = 1.0` with termination on overrun. An hour-long session, a slow drive, and the process is killed mid-write. | declarable per-plugin budget (§7.1) |

A recorder that drops frames during the run and gets killed mid-flush at the end is not two small bugs;
it is a feature that does not work, in a first-party plugin, expressible in no combination of public API
calls. Both halves are now expressible, and both are expressible *by third parties* — which was the
actual test.

**The plugin, specified:**

```toml
[[subscribes]]
topic    = "pose.smoothed"
required = true
conflate = false          # a recorder records everything it is given
depth    = 128            # ~4s of 30 Hz pose at ~1 KB each
```

- **Incremental append.** The `.npy` header is written once at first message with a **space-padded**
  shape field, frames are appended as raw rows, and the shape field is rewritten in place on every flush
  (default every 30 frames, ~1 s). So a crash, a `kill -9`, or a power cut loses at most the last second,
  and the file on disk is a **valid** `.npy` at every flush boundary. Today's code loses the entire
  session in all three cases. `teardown()` becomes a final flush plus `close()` and fits inside the
  default 1.0 s budget.
- **Format unchanged:** `(F, 33, 4)` float32 of **world** landmarks, so `sample-data/sample_replay.npy`
  keeps loading, as `plugins-and-config` asked.
- **A sidecar carries the semantics the bare array cannot.** `<name>.climbcv.json`, written at the same
  flush points:

  ```json
  {"schema": "persist_npy/1", "topology": "mediapipe.pose.33", "mirrored": true,
   "smoothed": true, "frames": 5417, "t0_ns": 51884211039, "source": "core.smooth_oneeuro",
   "gaps": [[1204, 3], [4890, 1]]}
  ```

  This closes three separate holes at once: `plugins-and-config` §7 noted a bare `.npy` records no
  `topology`, so a replay plugin must infer it from `L` — already ambiguous at `L == 17` between
  `coco.17` and any future 17-point topology. Guardian B2 noted the same array records no `mirrored`, so
  every offline left/right analysis of a saved session was a coin flip. And `gaps` — `[first_missing_seq,
  count]` pairs derived from `Meta.seq` discontinuities — means a recorder that *did* lose data says so,
  in the file, rather than producing a plausible shorter array. **A non-conflating subscription makes
  loss rare; the sidecar makes it honest.**

  A replay plugin that finds no sidecar (an old file, or `sample_replay.npy`) logs one INFO line and
  assumes `mediapipe.pose.33` / `mirrored = true` — today's behaviour, stated rather than guessed.

### 8.3 `core.smooth_oneeuro` — `passthrough`, and why disabling is not neutralising (C-7)

`plugins-and-config` found that today's `smoothing_enabled = False` has **no translation** under this
design. `core.smooth_oneeuro` is the resolved publisher of the exclusive topic `pose.smoothed`, so
`[plugins."core.smooth_oneeuro"] enabled = false` does not "turn off smoothing" — it **removes the
topic** and fatally starves `exo_live`, `pose_plot` and `core.persist_npy`. A user following the obvious
path gets a startup error for asking a reasonable question.

```toml
[plugins."core.smooth_oneeuro"]
passthrough = true      # subscribe pose.raw, republish it unchanged as pose.smoothed
```

With `passthrough = true` the stage stays wired and does no filtering: it republishes each `PoseFrame`
with `smoothed = False` — which is honest, because `smoothed` describes the payload, not the topic, and a
subscriber thresholding a filtered visibility value (`payloads.md` §3.2) needs to know.

**The general rule this is an instance of, and it needs to be prominent:**

> **Disabling a plugin removes the topics it publishes. To neutralise a stage while keeping the pipeline
> intact, the stage must offer its own passthrough option — the framework cannot synthesise one, because
> only the stage knows what "do nothing" means for its payload.**

The framework cannot generalise this: a "passthrough mode" for an arbitrary exclusive publisher would
require the framework to know which of its subscriptions to forward to which of its publications, which
is plugin-specific knowledge. What the framework *can* do is make the failure legible, and it now does —
`broker.md` §4.3's second starved-subscription variant detects that the absent topic's publisher was
disabled *by config* and prints both the rule and this stanza. Flagged for `docs-and-testing` as a
paragraph the authoring guide needs for any author writing an exclusive publisher.

---

## 9. Handoffs and open items

**Ready for `plugin-api-guardian`:** §3.2.1 (the corrected `__init__` rule, its rationale and its
message), §4.4 (`finish()` and `unavailable()` semantics and both messages), §4.5
(`PluginContractError` and the reserved-name set), §5.2/5.3 (quarantine and shutdown messages), §5.4
(the reworded stall warning), §6.2 (what an author sees when their handler raises), §6.3 (the state
vocabulary), §7/§7.1 (the `teardown()` contract and the declarable budget), §8.2–§8.3 (the two built-ins
that are also reference examples), and the `__main__` guard message and notebook verification in §2.4.

**To `plugins-and-config`:**
- The overlay owns the `cv2` window and must publish `app.shutdown` on ESC; there is no host-side
  `waitKey` any more. Read ESC from an `@every` tick (`plugin-api.md` §3.8) so the quit path survives
  the data path stopping.
- The plot plugin creates its own figure in `setup()`, as `plotting_process` already does, **and pumps
  its own event loop from an `@every` tick** (F-4 accepted).
- Persistence is `core.persist_npy` — see §8.2, which now specifies the incremental format and the
  sidecar. Your §7's `.npy`-carries-no-topology note and guardian B2's mirror-flag hole are both closed
  there, and your requested `sample_replay.npy` compatibility is preserved.
- `core.smooth_oneeuro` gains `passthrough` (§8.3). **C-7 accepted**; your `smoothing_enabled = False`
  migration row now has a translation.
- The lid plugin no longer respawns itself; it is a normal plugin with a timer and the supervisor
  handles its failures. Its two "not available here" paths use **`self.unavailable(reason)`**, not
  `finish()` (§4.4.2, **F-13 accepted**), and its poll interval comes from
  `self.set_interval(self.poll, ...)` in `setup()` (**F-2 accepted**) — so the `@every(0.5)` fallback
  comment in your §6.5 sketch can go.
- `mac_lid`'s `build_dir` default becomes `self.data_dir` (**F-7 accepted**, `plugin-api.md` §3.9),
  which is required rather than merely tidier now that the bundled root is read-only (`loader.md` §2.1).
  Your §6.7 analysis of why `<plugin_dir>/build/` breaks was correct on all three counts.
- **F-16 declined** as a timeout, addressed as a progress INFO (§3.1). `mac_lid`'s slow Swift compile
  now produces a "still starting up" line every 10 s instead of being indistinguishable from a hang.

**To `docs-and-testing`:** the interesting tests here need a plugin that crashes on demand — segfault
via `ctypes`, non-zero exit, hang, raise-every-handler, exceed teardown timeout. A `plugins/_test_*`
fixture set is the highest-value asset for this section, and the classification table in §4.2 is its
specification. Note also that testing timing policy (§5.1) requires the backoff clock be injectable.
Revision 01 adds five cases worth fixtures: a plugin that rebinds `self.latest` (§4.5); one that calls
`unavailable()` from `setup()` while being a critical publisher (§4.4.2); a recorder killed with
`SIGKILL` mid-run, asserting the `.npy` on disk is valid and the sidecar's frame count matches (§8.2);
a `spawn` round-trip from a `__main__` with no `__file__`, i.e. the notebook case (§2.4); and a plugin
with no timers whose input pauses, asserting **no** stall warning (§3.3).

**Open:**
- `--kill-stalled` (§5.4) — deferred.
- Per-plugin resource limits (CPU/memory) — explicitly out of scope per Assumption §3 ("no built-in
  resource budget/throttling"); process-per-plugin does make `resource.setrlimit` in the child
  tractable later.
- **`setup()` timeout — declined** (F-16). Any threshold large enough for an honest model load is too
  large to detect a hang; §3.1's progress INFO is the diagnose-don't-intervene answer.
- **Not sandboxing.** Decision #6 stands: process isolation here is for *fault* containment, not
  security. A plugin can still read the filesystem, open sockets, and `os.kill` the parent. Worth
  stating in the authoring guide so nobody mistakes process isolation for a security boundary. Note
  `self.data_dir` (F-7) does **not** change this: it is a convenience location, not a confinement.
