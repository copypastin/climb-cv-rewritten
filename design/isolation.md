# Design: Isolation & Fault-Tolerance Runtime

Owner: `framework-core` · Status: **proposed, awaiting review** · Implements Decisions #4 (auto
isolation) and #5 (contained crashes)

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
  `PoseFrame.frame_seq` make the mismatch visible and correctable; the declared join in
  `plugin-api.md` §4.3 makes it correctable without the author writing buffering code.

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
  5. import the entry module, resolve the class, assert it subclasses Plugin
  6. cross-check @subscribe/@every against the manifest        (loader.md §7)
  7. instantiate;  bind self.config, self.log, self.publish, self.latest
  8. setup()
  9. send READY on the control queue
 10. enter the loop (§3.3)
```

Step 2 before step 5 is not cosmetic: a native crash during import produces output on stderr and
nothing else, and it must land in the plugin's own log file to be attributable. Step 3's
`faulthandler` is what turns a segfault into a Python-level stack dump — worth a great deal with
mediapipe, torch, and GPU delegates in the mix.

### 3.2 `setup()` is where the ergonomics live

`setup()` runs **in the child, after spawn**. This is what makes "authors need no concurrency
knowledge" true rather than aspirational:

- The author never pickles a model. They open it in `setup()`, exactly as `yolo_boxes_worker` does
  today — except that the framework no longer needs to invent a `model_path` parameter to make it
  possible, because `setup()` runs in the right place by construction.
- Unpicklable objects (cv2 captures, matplotlib figures, GPU handles, sockets, subprocesses) are
  created where they are used and never cross a boundary.
- `__init__` on a `Plugin` subclass is reserved by the framework. Authors are told: **put nothing in
  `__init__`; put it in `setup()`.** If a subclass defines `__init__`, the runtime raises with that
  sentence in it, because the failure otherwise is a confusing pickling error at spawn time.

### 3.3 The loop

```python
while not shutdown_event.is_set():
    drain event_q non-blocking, dispatch each in arrival order   # events never conflated away
    timeout = seconds until next @every timer is due (None if no timers)
    drain stream_q (blocking up to `timeout`, then non-blocking),
        keeping ONLY the newest message per topic                # conflation
    dispatch the conflated stream messages
    fire due timers
    heartbeat on control_q if >= 1s since last
teardown()
exit(0)
```

Properties that matter:

- **One thread.** No locks, no `async`, no author-visible concurrency. Handlers run one at a time, in
  the plugin's own process, on its main thread.
- **Events before streams**, so `app.shutdown` is never queued behind a burst of frames.
- **Conflation is read-side**, so a slow plugin gets the *freshest* value of each topic rather than
  working through a backlog it can never clear.
- **A blocking `@every(0)` handler is legal** and is how a source works: capture's handler calls
  `cap.read()` (which blocks), returns, and is called again immediately. Shutdown and heartbeat are
  checked between calls, so a 33 ms read delays them by at most 33 ms.
- A plugin with `@every(0)` **and** subscriptions would starve those subscriptions. Detected at
  startup → WARNING, not an error (a fast tick alongside subscriptions is legitimate).

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
| `0` without `finish()` | the plugin's loop returned unexpectedly | treat as a crash — an unexplained clean exit is still a plugin that stopped working |
| non-zero | Python-level failure; traceback already sent on the control queue and written to the log | restart per §5 |
| killed by signal (`< 0`) | native crash — segfault, abort, GPU driver fault. **No Python traceback exists.** | restart per §5; the report names the signal and points at the log, where `faulthandler` and the stderr redirect are the only evidence |

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

### 4.4 Intentional completion

`self.finish()` marks the plugin as done and exits 0. Used by a file-based capture reaching EOF.

If the finishing plugin is the resolved publisher of a topic with **required** subscribers, the
supervisor treats it as end-of-run and begins orderly shutdown — a video file ending should stop the
app, not leave it staring at a frozen frame. If all its subscribers are optional, or it publishes
nothing required, it is simply gone and the app continues. **The `required` flag on subscriptions does
triple duty**: startup wiring validation, crash-escalation policy (§5.3), and this. One declaration,
three uses, which is the sign it is at the right level of abstraction.

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
the loop**, meaning a handler that does not return stops the heartbeat. After
`heartbeat_warn_s = 5.0` of silence:

```
Plugin 'exo_live' has not returned from a handler for 5.2s. It is no longer receiving live
data (messages are being dropped). climb-cv is otherwise unaffected.
```

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
   crash output both land somewhere findable. Nothing is lost.
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
the same ladder, attributed to `<host>`.

---

## 7. Orderly shutdown

Triggered by any of: `app.shutdown` from a plugin (ESC in the overlay window), SIGINT/SIGTERM in the
host, `ClimbCV.stop()`, intentional completion of a critical publisher (§4.4), critical quarantine
(§5.3).

```
1. set the shared shutdown Event                       (mp.Event, not Manager -- no manager process)
2. wait up to grace_s = 2.0 for children to exit 0     (matches today's join(timeout=1) x2)
   -- children see the Event in their loop, run teardown(), exit
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

`teardown()` gets `teardown_timeout_s = 1.0`. Exceeding it is a WARNING and the process is terminated.
Authors are told: teardown is for closing files and releasing devices, not for finishing work.

The sentinel-`None`-on-the-queue pattern from the baseline is **dropped** in favour of the shared
Event. The sentinel only reaches a child that is actually reading its queue, and one sentinel is
consumed by one reader — it does not generalise to fan-out. An Event is observed by everyone,
including plugins with no subscriptions at all (capture, the lid sensor), which the sentinel could
never reach.

---

## 8. Configuration surface

All optional, all under `[framework]`, all with the defaults above. Full shapes in
`config-contract.md`:

`restart_max` (5), `restart_window_s` (60.0), `restart_backoff_cap_s` (30.0),
`setup_failure_max` (2), `heartbeat_warn_s` (5.0), `grace_s` (2.0), `teardown_timeout_s` (1.0),
`stream_depth` (4), `event_depth` (256), `log_level` ("INFO"), `log_dir` ("./logs"),
`shutdown_on_critical_quarantine` (true).

---

## 9. Handoffs and open items

**Ready for `plugin-api-guardian`:** §3.2 (the `__init__`-is-reserved rule and its message), §4.4
(`finish()` semantics), §5.2/5.3 (quarantine and shutdown messages), §5.4 (the stall warning), §6.2
(what an author sees when their handler raises), §7 (`teardown()` contract), and the missing
`__main__` guard message in §2.4.

**To `plugins-and-config`:**
- The overlay owns the `cv2` window and must publish `app.shutdown` on ESC; there is no host-side
  `waitKey` any more.
- The plot plugin creates its own figure in `setup()`, as `plotting_process` already does.
- Persistence is `core.persist_npy`, an independent subscriber of `pose.smoothed` — do **not**
  reproduce the plot-coupling described in §1.
- The lid plugin no longer respawns itself; it is a normal plugin with an `@every(0.5)` tick and the
  supervisor handles its failures.

**To `docs-and-testing`:** the interesting tests here need a plugin that crashes on demand — segfault
via `ctypes`, non-zero exit, hang, raise-every-handler, exceed teardown timeout. A `plugins/_test_*`
fixture set is the highest-value asset for this section, and the classification table in §4.2 is its
specification. Note also that testing timing policy (§5.1) requires the backoff clock be injectable.

**Open:**
- `--kill-stalled` (§5.4) — deferred.
- Per-plugin resource limits (CPU/memory) — explicitly out of scope per Assumption §3 ("no built-in
  resource budget/throttling"); process-per-plugin does make `resource.setrlimit` in the child
  tractable later.
- **Not sandboxing.** Decision #6 stands: process isolation here is for *fault* containment, not
  security. A plugin can still read the filesystem, open sockets, and `os.kill` the parent. Worth
  stating in the authoring guide so nobody mistakes process isolation for a security boundary.
