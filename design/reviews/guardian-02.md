# `plugin-api-guardian` review 02 — post-revision, lockability gate

Date: 2026-08-07. Subject: all eight design files at `f4a1663`, plus `revision-01.md`'s claimed outcomes and `guardian-01.md` as a prior report.

Settled and not relitigated: Decisions #9 and #11–#23, no sandbox in v1, no config schema validation in v1, the five naming rulings, `join=` cut.

**Method note:** the load-bearing mechanism claims were verified by running them, not by reading the prose. Two do not hold.

**Verdict: NOT lockable — two blocking findings, both mechanical, neither requiring redesign. Fix them and proceed; do not schedule a third full review.**

---

## 1. The read-only / lifetime array guarantee does not survive a pickle round-trip as specified — **BLOCKING**

`payloads.md` §2.2.1 puts `__setstate__` on a `_Arrays` **mixin base class**. `@dataclass(frozen=True, slots=True)` installs its own `__setstate__` into the subclass's `__dict__`, shadowing the mixin — so `_adopt_arrays()` never runs on unpickle and every subscriber's arrays are writeable. Which is exactly what #19 was written to fix.

**Verified** on Python 3.13.11 / numpy 2.4.1, transcribing §2.2.1 as written:

```
Frame.__setstate__ resolves to: _dataclass_setstate
'__setstate__' in Frame.__dict__ : True
publisher writeable: False
mixin setstate called during unpickle? []
SUBSCRIBER writeable: True
!! subscriber MUTATED the payload array with no error
```

Cause: a one-line guard in CPython's `dataclasses._add_slots` — `if '__setstate__' not in cls_dict` — and an *inherited* method is not in `cls_dict`. So B3's `stabilize` plugin (`frame.pixels[:] = warped`) is still a silent no-op and the `latest()`-cache corruption case is still open. #19's own premise checks out (numpy does lose `writeable=False` across pickle: confirmed `True` after round-trip), which is precisely why the shadowing is fatal rather than cosmetic.

Two further holes in the same mechanism, both verified:

- **`base is not None` is the wrong ownership test.** `np.ascontiguousarray` returns *the same object* for a contiguous view (`out is view` → `True`), so the copy branch never fires and the payload keeps a view onto a caller-owned buffer. §2.2.1's comment ("no-op … when it is already C-contiguous **and owns its data**") is wrong: it is a no-op when contiguous, ownership irrelevant. Concretely — a publisher builds landmarks as `ring[:33]` (an ordinary preallocation), publishes, then writes `ring`; `mp.Queue`'s feeder thread has not serialised yet, so the *subscriber* receives mutated data. §2.2.1 consequence 1 claims this exact race is closed. It is not. Verified: writing the base changed the "read-only" payload's contents to `7.0`.
- **`Record.data` arrays are unreachable from `_ARRAY_FIELDS`.** `Record`'s docstring promises ndarray leaves are normalised C-contiguous and read-only "**recursively**". `_ARRAY_FIELDS` names top-level fields; `data` is a dict. Verified: nested array is writeable after unpickle. The one type B5 added to make the vocabulary open is the one type whose immutability claim is false.

**v2 cost.** `broker.md` §5.3.1 records read-only arrays *and* validity-for-lifetime as the two named preconditions for T2, and §5.4 states both as v1 guarantees to authors. Shipped stated-but-unenforced, a population of mutating plugins accumulates that works fine under T1 — then T2 corrupts every one on a framework *minor*, the precise outcome §5.3.1 exists to prevent. Retracting the guarantee later breaks a documented promise; enforcing it later breaks every plugin that mutates. Both doors close.

**Recommendation.** Keep the guarantee — it is the right one and the reasoning behind it is the best in the document set. Fix the mechanism before implementation:
- Define `__setstate__` **in each dataclass's own body** (verified working), or assign `cls.__setstate__` from a class decorator applied *outside* `@dataclass` (also verified working). The mixin may keep `_adopt_arrays`; it cannot keep `__setstate__`.
- Test ownership with `arr.flags["OWNDATA"]`, not `arr.base is not None`; copy with `np.array(arr, copy=True, order="C")`. Verified to close the aliasing case.
- Give `Record` a `__setstate__` that re-walks `data`, or drop the recursive read-only claim from its docstring. Do not leave the claim standing unenforced.
- Add the reflection test §5 already asks for, extended to assert the flag survives `pickle.loads(pickle.dumps(x))` for every contract type. That single test would have caught this and will catch the next `_ARRAY_FIELDS` omission.

Files: `payloads.md` §2.2.1, §3.6, §5; `broker.md` §5.3.1, §5.4.

## 2. `Record` has no declared, checked `kind` — **BLOCKING**

§3.6 makes `Record.kind` a "versioned id the publishing plugin owns" then never puts it anywhere the framework can check: no manifest key, no `TopicDescriptor` field, and §4.2's descriptor merge compares `kind`/`exclusivity`/`schema`/`unit` only — where `schema` is `record/1` for *every* record topic in the ecosystem.

**Failure.** This is S13 one level down, and S13 was accepted. `grip_viz` subscribes `acme.hand_state`, written against `kind = "acme.hand_state/1"` where `data["fingers"]` is five floats. A second plugin publishes the same topic with `kind = ".../2"` where `fingers` became a dict. Both manifests declare `payload = "record"`, `kind = "stream"`, `exclusivity = "shared"` — every checked field agrees, the topic wires, and the subscriber raises `KeyError` inside its handler where `isolation.md` §6.2's ladder logs "handler `on_hand` has raised 148 times" and blames the innocent plugin. That is the outcome B5's `isinstance` check was added to eliminate, reachable through the type B5 added. Same within one plugin across versions: bump `kind` to `/2` and every subscriber breaks silently.

**v2 cost.** Making a manifest key required later is a tightening, which §5's table calls breaking — and §4.0 spells out the consequence in the identical situation: "the unsafe default would have persisted and the mechanism would have protected only careful authors — who were never the risk." `Record` will carry most third-party topics. Highest-traffic corner of the payload surface, and the only one with no name and no version the framework can see.

**Recommendation.** Do for `Record` exactly what S13 did for `Scalar`: `record_kind` becomes a **required** `[[publishes]]` key when `payload = "record"`; it becomes a `TopicDescriptor` field and joins merge-equality (at which point §4.3's descriptor-contradiction error produces the message for free); `publish()` checks `payload.kind == descriptor.record_kind` alongside its `isinstance`. Cheap version: make the key required now and only *print* it in `climbcv topics`, so the constraint exists in the file format from day one. While there — `climbcv topics -v` prints depth, timeouts, `data_dir` and topologies but never payload type, `schema` or `unit`, the three things that actually constitute the contract.

Files: `payloads.md` §3.6; `loader.md` §3; `broker.md` §2, §4.2, §7.

## 3. `provides_topology` cannot express "same as my input", so the flagship pose swap dies on the first-party smoother — **SHOULD-FIX (high)**

§4.0 makes `provides_topology` a mandatory single concrete id for any `PoseFrame` publisher, with `"any"` offered only to subscribers — and a pass-through or filter stage has no honest value to put there.

**Failure.** A third party ships `fast_pose` providing `coco.17`. `core.smooth_oneeuro` is topology-agnostic (a One Euro filter filters columns; it indexes no joints) so it correctly declares `requires_topology = "any"` and is legally wired. It must then declare `provides_topology` as *something*, and `mediapipe.pose.33` is the only defensible guess. Its first `PoseFrame` carries `topology = "coco.17"` (copying the input is the only honest behaviour), the publisher-side first-payload check fires, it is quarantined non-retryably as a contract violation, it is the exclusive publisher of `pose.smoothed` with three required subscribers, and §5.3 shuts the app down. So installing a third-party pose plugin — the most-advertised capability in Decision #9 — ends the run with a contract error blaming a first-party plugin that did nothing wrong. Same hole hits a `replay` plugin (`broker.md` §8 recommends building one) reading a recording whose sidecar says `coco.17`.

§4.1 already prescribes the fix in prose — "declare both topologies and publish the one it is actually using" — while the manifest schema, §4.0's rule, and `PluginPlan.provides_topology: str | None` all make that inexpressible. That internal contradiction is what makes this real rather than hypothetical.

**v2 cost.** Loosening to accept a list is additive, so recoverable — hence should-fix, not blocking. What it costs meanwhile is the credibility of the swap: until fixed, "capture and pose are both swappable core concerns" (Assumption §3, marked **Confirmed**) holds only for a plugin providing the one topology the first-party stages were built around — the ceiling Approach 3 was rejected for, relocated into the topology field.

**Recommendation.** Thorough: add `provides_topology = "same_as_input"`, resolved by the wiring planner to the upstream publisher's declaration and carried on `PluginPlan` as the resolved id, so both the publisher spot check and §4.1's per-delivery subscriber check compare against a real value. Cheap and genuinely cheap: let it accept a list, wire when the downstream requirement intersects, and lean on §4.1's per-delivery check — i.e. make the schema accept what §4.1 already tells authors to do. Either way state the rule for transform stages explicitly, because a pass-through publisher is the second-most-common pose-topic shape after a model.

Files: `payloads.md` §4.0, §4.1; `loader.md` §3.1; `broker.md` §6.

## 4. `self.stopping` cannot change while the blocking handler it exists for is running — **SHOULD-FIX (high)**

§2 declares `stopping: bool` and §3.10 describes it as a value "set before the framework waits for your `teardown()`" — but the child is single-threaded (`isolation.md` §3.3, "One thread. No locks"), so nothing can assign to it while a handler executes, which is the only moment it is read.

**Failure.** The blessed idiom is §3.10's own example: `@every(0)` with `while not self.stopping: chunk = self.socket.recv(65536)`. The loop that would flip `stopping` is the loop currently blocked inside `pump()`. As written, `stopping` is `False` forever, the IP-camera plugin never notices shutdown, `grace_s = 2.0` expires, the process is terminated, `teardown()` never runs, the device handle leaks — the outcome S18 was accepted to prevent, with the fix in place and inert. Nobody would notice in review because the code reads correctly.

**v2 cost.** Low if fixed now, because the fix is invisible to authors. Discovering it after v1 means either every source plugin written against the documented idiom is broken, or the fix arrives as a semantic change to a member whose behaviour authors already built around.

**Recommendation.** Specify `stopping` as a read-only **property over the shared shutdown `Event`** (`isolation.md` §7 step 1 sets it), not a bool the loop assigns, and say in §3.10 that it becomes `True` *while your handler is running* — that sentence is the whole value of the member. Add the fixture §9 lacks: a plugin blocking 10 s inside `@every(0)`, asserting `teardown()` ran. As a property the "read-only" claim in §2 becomes enforceable rather than aspirational.

Files: `plugin-api.md` §2, §3.10; `isolation.md` §3.3, §7.

## 5. §7.4 tells embedders to touch host state freely without saying which thread they are on — **SHOULD-FIX (high)**

§7.4 says callbacks run "on the supervisor's dispatch thread" and then, two lines later, "A host callback may touch host state freely … It is the one place in this design where shared mutable state is normal, **because there is only one process**." One process, two threads; the sentence elides the boundary that matters.

**Failure, and the first half is a migration break.** Today (verified in `copypastin/climb-cv@main`, `src/climbcv/climbcv.py:470-484`) `start(blocking=True)` invokes `on_landmarks` on the **caller's** thread; only `blocking=False` moves it to a background thread. Under §7.1/§7.4 `app.run()` is blocking but callbacks still arrive on the supervisor's dispatch thread — so an existing embedder whose `on_landmarks` updates a Tk label or calls `cv2.imshow`, which works today, gets undefined behaviour or a hard crash after the refactor, with no diagnostic and no mention in the docs. Second: a PyQt/Tk host — the most likely embedding shape after Jupyter — writes `self.label.setText(...)` in the callback *because §7.4 told them shared mutable state is normal here* and gets intermittent crashes attributed to climb-cv.

Worth naming the asymmetry: a GUI-owning *plugin* gets a named, argued, worked-example idiom for this problem (§3.8, F-4). A GUI-owning *host* gets a sentence that points the wrong way.

**Recommendation.** Say plainly that callbacks run on a framework-owned thread that is **not** the thread which called `run()`/`start()`, and that a host owning a GUI must marshal to its own loop — with the two-line idiom (`queue.Queue` in the callback, drained from the host's timer), the host-side mirror of the stash-plus-tick idiom §3.8 already blesses. Add the migration note for `on_landmarks`. Cheap and worth taking now because additive: an `app.poll(timeout)` draining pending callbacks on the *caller's* thread, which is what a Qt/Tk embedder actually wants and what makes "the same function body works as a plugin handler or a host callback" true rather than nearly true. Also settle whether `app.status()` is reentrant-safe from a callback — §7.4 rules on `stop()`, `subscribe()` and `run()` and is silent on the fourth.

Files: `plugin-api.md` §7.1, §7.4.

## 6. §7.6's always-fatal unknown-topic rule lets a user's config change stop the app — **SHOULD-FIX**

The rule keys off the *enabled* plugin set, so a name's validity depends on runtime configuration rather than the embedder's code.

**Failure.** A desktop app embeds climb-cv and ships `acme_grip`, subscribing `acme.grip_force` with the default `required=False` because the feature is optional. The user writes `enabled = false`. The name is now published and subscribed by nobody, so it is "unknown", so the app **refuses to start**, with a did-you-mean message that has nothing to suggest — because the user turned off an optional feature. `broker.md` §4.4 states the governing principle in exactly this shape ("a user must never be unable to run the app because … a config file was written on a different machine") and relaxes an identical fatal for plugins. §7.6 reintroduces the pattern for the one participant that cannot declare its way out.

**Recommendation.** Compute "unknown" against the **discovered** topic vocabulary rather than the enabled one — the same relaxation §4.4(b) already made, for the same reason. Cheap version: keep the fatal only when the name appears in no discovered manifest and is not standard; downgrade the disabled/skipped case to the INFO line §7.6 already specifies.

Files: `plugin-api.md` §7.6; `broker.md` §4.4.

## 7. Host `required` defaults to the one direction the project's own change rule forbids reversing — **SHOULD-FIX**

**Failure, two halves.** Immediately: a headless embedder — the primary shape, `start(blocking=False)` plus `on_landmarks` — runs with only capture/pose/smooth and no overlay, so *nothing* requires `pose.smoothed` except the host. The user disables `core.smooth_oneeuro`, or the pose stage quarantines after five crashes, and the app runs contentedly forever producing no callbacks, with one INFO line in a log the embedder may route nowhere. `broker.md` §4.3 built a family of excellent starved-subscription errors for this and the default opts the host out of all of them.

Longer term: `False` → `True` is a tightening, which §5 rules "**treated as breaking**"; `True` → `False` is a loosening, permitted in a minor. Decision #12's decisive argument was "given genuine uncertainty, pick the direction cheap to reverse" — and §7.7 concedes the neighbouring `publish()` question is "genuinely close" while not applying reversibility to this one at all. The stated cost of default-true (an optional telemetry callback silently gaining power to end the run) is real, but it is the identical cost a plugin author pays, accepted there, where the affected party has *less* recourse: an embedder types `required=False` on the line they are already writing; a plugin's user must ask a third party to edit a manifest.

**Recommendation.** Make `required` a **mandatory keyword** on `ClimbCV.subscribe()`, exactly as §7.3 already does for `requires_topology`, omission raising at the call site listing both choices. No default means nothing to tighten later, it dodges the argument in both directions, and it reuses a mechanism this section invented and defended two subsections earlier.

Files: `plugin-api.md` §7.3, §7.6, §7.7.

## 8. An embedder cannot configure the framework programmatically — **SHOULD-FIX**

`ClimbCV.__init__` takes `config="climbcv.toml"` or `None` and nothing else, so every knob, every `enabled = false`, and every `[topics]` assignment is reachable only through a file on the user's disk.

**Failure.** §7.5 exists to support host-as-frame-source. `use_bundled_plugins` defaults true and `core.capture` lives in the bundled root, so *every* embedder calling `app.publishes("frame")` hits exclusive contention on first run. The message tells them to write `[plugins."core.capture"] enabled = false` — and with `config=None`, `broker.md` §4.3's own rule requires that message to read "**Create ./climbcv.toml** containing:". So a desktop application must instruct its end user to hand-author a TOML file next to the process CWD before it will start, or write a temp file at runtime. Both absurd for the shape §7.5 was added to support.

**Recommendation.** Let `config=` accept a **dict** as well as a path. Nearly free: `config-contract.md` §1 already says "The framework never opens `climbcv.toml`. It receives one plain `dict` and a source description" — so the internal contract is already dict-shaped. Skip `load_config()`, set `source = "<supplied by the embedding application>"` and `config_dir = None` (§3.5 already defines it). Then §7.5's error can print the Python form for an embedder and the TOML form for a file-based run.

Files: `plugin-api.md` §7.1, §7.5; `config-contract.md` §1, §4.

## 9. The shared-topic `latest()` warning fires on deployment shape, not on the declared contract — **SHOULD-FIX**

Both warnings guarding `latest()` on a shared topic trigger on *more than one publisher having resolved* — a property of the user's installation, not the plugin's code.

**Failure.** A third party writes an overlay, tests it with `yolo_holds` installed, and `latest("holds.boxes")` behaves perfectly with no warning, because there is one publisher. They ship. A user installs a second detector and the overlay flickers, or a once-publishing `route_map` becomes permanently invisible. The warning fires — at the user's site, naming a plugin the user cannot fix. B1's failure with the diagnostics moved one machine too late. Exclusivity is a *static, declared* property known before any process starts; the signal is available at the moment it is useful and is not being used.

**Recommendation.** Trigger off the descriptor's `exclusivity`, not resolved publisher count: the first `latest()` on a topic declared SHARED logs one WARNING pointing at `latest_by_source()`, always, on the author's own machine. Better, make it a startup warning — `mode = "latest"` is already a declared manifest key, so "this plugin reads a shared topic with `latest()`" is statically knowable at wiring time with no AST work. That also earns `mode = "latest"` its keep: it currently exists *only* to suppress a framework warning, which is thin justification for a permanent manifest key. For the handler-plus-`latest()` case, extend `climbcv validate`'s AST scan (S23) from `self.publish("<literal>")` to `self.latest("<literal>")` — same walk, one more node type.

Files: `plugin-api.md` §3.5; `broker.md` §4.2; `loader.md` §3.1.1, §7.

## 10. Unknown manifest keys are silently ignored — **SHOULD-FIX**

`loader.md` §3.1 specifies every valid key and never says what happens to an invalid one; the surrounding design implies "ignored". `config-contract.md` §3.1 refuses that exact behaviour for `[framework]` keys — "a typo'd `[framework] log_lvl = "DEBUG"` that silently does nothing is exactly the failure a drop-in tool cannot afford" — and the manifest, which gates every other check in the system, gets no equivalent.

**Failure — four, each disabling a fix that cost a review round.** `conflat = false` → subscription conflates, recorder silently loses frames: F-14 in full, reintroduced by one missing character. `platform = ["darwin"]` → mac-only plugin enabled on Linux, crashes twice into quarantine instead of an INFO skip. `teardown_timeout = 8.0` → budget stays 1.0, S8's lost session returns. `heartbeat_warn_s` misspelled → IP-camera plugin warns on every read forever, the noise §5.4 was reworded to prevent. `requires_topology` is the one key immune, precisely because B4 made it mandatory — which is the shape of the argument.

**Recommendation.** Unknown key in `[plugin]`, `[[publishes]]`, `[[subscribes]]` or `[config]` → **WARNING with a `difflib` nearest match**, key ignored, plugin still loads. The mechanism is already specified for `[framework]` in `config-contract.md` §1.2 and `config.md` §5.1; reuse it. Warning rather than fatal needs saying explicitly, because it is also the forward-compatibility rule: a plugin authored against API 1.3 run on 1.0 should be skipped by `api_version`, not killed by an unrecognised key. Make it an *error* in `climbcv validate`, where the author is the audience.

Files: `loader.md` §3.1, §3.1.1, §7.

## 11. `app.status`'s exclusivity is never stated — **SHOULD-FIX**

`app.status` is in the standard topic set and is the only standard topic missing from §3's exclusivity table; `payloads.md` §3.5 says `Status` is "Emitted ONLY by the framework" without anything making that true.

**Failure.** If SHARED, a plugin may legally declare `[[publishes]] topic = "app.status"` and publish `Status(plugin_id="core.capture", state="crashed")`; §3.5 acknowledges the forgeability and pushes the burden onto every status UI to check `Meta.source`, which the first one written will not do. If EXCLUSIVE, that plugin instead produces exclusive contention against `<host>`, which §7.2 says cannot be disabled from config — a fatal the user can only resolve by deleting a plugin. Unstated means whoever implements it picks, and the choice is not reversible: exclusive→shared is "a breaking change for every subscriber" by §3.1's own argument 3.

**Recommendation.** Add `app.status` as **EXCLUSIVE**, publisher `<host>`, and state the general rule that `app.*` is framework-published. While in §4.4(a): an `exclusivity = "shared"` override on an author-declared topic silently converts every subscriber's delivery contract from one message per cycle to N — the change §3.1 argument 3 calls breaking — and the design hands users that lever pre-pasted in an error message. One startup WARNING naming affected subscribers when an override *widens* exclusivity, mirroring §4.4(c)'s existing WARNING for `required = false`.

Files: `broker.md` §3, §4.4; `payloads.md` §3.5.

## 12. S9's deferral is defensible, but the one shared standard topic needing retention cannot have it — **SHOULD-FIX (documentation, or cheap machinery)**

`broker.md` §5.4.1 declines `retain` and offers two mitigations — republish on an `@every` tick, and "a topic you declare yourself for static data should be `kind = "event"`" — then concedes the second is unavailable for `holds.boxes`, whose kind is fixed by the standard set.

**On the decline itself: sound, and I would not overturn it.** `retain` makes the wiring planner hold message state and needs a retained-message lifetime policy across publisher restarts; deferring genuinely new machinery is right, and the zero-delivery INFO plus §3.5's honest bullet mean the case is detected rather than invisible. The residual gap is specific and sits on the design's own flagship example: `route_map` — used to justify making `holds.boxes` shared in the first place, and present in §7's sample output — is *only* correct if its author knows to run a republish timer, and gets no error, no warning, and one INFO after ten seconds if they do the obvious thing in `setup()`. So the interop story for static hold data is "every static publisher must implement a timer, and the framework will tell you at INFO if you didn't."

**Recommendation.** Two cheap moves inside existing machinery. Promote the zero-delivery diagnostic from INFO to **WARNING** when a subscription has received zero messages *while its topic has a resolved publisher* — not a normal state, and the only signal this failure produces. And add a fifth worked example to `plugin-api.md` §2.1: a static-data publisher with an `@every` republish. §2.1 is handed to `docs-and-testing` as the whole of "how to write a plugin", three of its four examples had to be corrected for bugs of exactly this class, and a rule living only in `broker.md` §5.4.1 is not in the file authors read. If you want machinery, the cheap form is publisher-side `[[publishes]] republish_s = 1.0` — the publisher's own runtime re-sends its last payload on a timer, avoiding the stated objection entirely because the planner holds nothing.

Files: `broker.md` §5.4.1; `plugin-api.md` §2.1, §3.5.

## 13. F-16's decline is right, but its interaction with the startup phase is unspecified — **NOTE**

`isolation.md` §3.1 declines a `setup()` timeout with reasoning I agree with. But two parts of the design imply opposite things about whether a startup barrier exists. §4.4.2 says `unavailable()` **before READY** is a "fatal startup error" — presupposing the app has not started and can still refuse. §5.4.1's whole scenario presupposes the opposite: `route_map` publishes and `exo_live` misses it because plugins start independently. If there is a barrier, a third-party plugin hanging in `setup()` prevents the app from *ever* starting with an INFO every 10 s — Decision #5 failing, and the one case where "diagnose, don't intervene" costs the user the whole app rather than one plugin. If there is no barrier, whether `unavailable()` produces a clean refusal or a mid-run shutdown depends on a race between two plugins' `setup()` durations, and `mac_lid`'s Swift compile makes that race real. No API change needed; the contract must say which, because it determines the error message an author's users see. If the answer is "barrier", reconsider a *bounded* one — start when all non-hung plugins are READY, treat the straggler as arriving late.

Files: `isolation.md` §3.1, §4.4.2; `broker.md` §5.4.1.

## 14. A class-level override of a reserved name is silently ignored — **NOTE**

`isolation.md` §3.1 step 6 checks for `__init__` and `Plugin` subclass-hood; step 10 checks names rebound *during `setup()`*. Neither catches `def latest(self, topic)` in the class body. Because step 7 binds framework members onto the *instance*, the instance attribute shadows the author's method, so their override never runs and their calls silently reach framework code — the inverse of S22's `TypeError`, and quieter. Same comparison one scope up, in the check that already exists: compare the entry class's own `__dict__` against the reserved set at step 6, same `PluginContractError`, same message text.

File: `isolation.md` §3.1, §4.5.

## 15. `first-party-plugins.md` was never revised, so the acceptance test now contradicts the surfaces it was meant to prove — **NOTE**

Every finding it raised was answered, but the document still shows pre-revision code: `frame.as_bgr().copy()` (§5.7) where `payloads.md` §3.1 now guarantees a fresh writable array and §6 says drop the copy; `self.finish()` for `mac_lid`'s two unavailable paths (§6.5) where `unavailable()` now exists; `<plugin_dir>/build/` where `data_dir` is now required; `np.clip` on boxes (§3.5) where F-9 clips in the contract; `@every(...)` "fallback if set_interval is rejected" comments throughout; and no `[config] keys`, `conflate`, `mode` or `unit` in any of the four manifests. This is tracked in `revision-01.md` and `isolation.md` §9, so it is not a gap in the audit trail. It matters because this file is the dogfood proof, and Decision #7's claim is only as good as the last time it was actually run. Re-run it against the revised surfaces before implementation, or it becomes the stale reference someone copies from. Separately: `plugin-api.md` §2 says "five data members, six methods" then, fourteen lines later, "Four members are data" — worth fixing, since the count is what a reviewer is being asked to judge.

Files: `first-party-plugins.md`; `plugin-api.md` §2.

---

## Direct answers to the four questions asked

**1. Is it still one uniform authoring model? YES.** The count grew; the shape did not. What would make it a toolkit is a *second way to be a plugin* — a second base class, a second lifecycle, a mode flag, a hook only some plugin types implement — and none of the six additions is any of those. Four are read-only data (`config_dir`, `data_dir`, `stopping`) or one-line calls (`set_interval`, `unavailable`, `latest_by_source`); none introduces a decision an author must make *before* they know what their plugin does, which is the test I actually apply. `first-party-plugins.md` §9's scorecard is still four rows of subscribes/publishes/timer/setup/teardown and would look identical with the six additions in place. The honest cost of growth is discoverability, not conceptual load: five of eleven members serve narrow plugin classes and an author reading §2 cannot tell which five apply to them — a docs problem, and `docs-and-testing` owns it.

I would cut nothing. The one member I would have challenged is `mode = "latest"`, whose entire semantics today is "do not warn at me"; finding 9 gives it a second load-bearing job instead of cutting it. The one member whose *specification* is wrong is `stopping` (finding 4). `latest`/`latest_by_source` stay two methods per ruling #2 — the problem was never the pair, it is when the warning fires.

**2. §7, the embedding API.** Structurally the right call: making the host a participant rather than an exemption is what closes S19, and §7.3 (mandatory `requires_topology` raising at the call site) is the best thing in the section — strictly better than the manifest error it mirrors. §7.7 volunteering what was decided rather than derived is exactly the disclosure that makes the section reviewable. On the three named choices: `required=False` is the one I would change, and not on taste — it is the direction §5 forbids reversing, and §7.7 does not apply the reversibility criterion Decision #12 was settled with (finding 7). The unknown-topic rule is right in intent, wrong in what it keys off (finding 6). `ClimbCV.publish()` should exist — the host-as-frame-source argument holds — but it ships unusable without a programmatic way to disable the competing publisher (finding 8). **The choice that reads fine alone and wrong against the rest of the system is not among the three you named: it is §7.4's thread** (finding 5), which contradicts the invariant that authors need no concurrency knowledge, in the one place the design says shared mutable state is normal.

**3. Verify, don't trust.** B1, B2, B4 and B5 are genuinely landed, not merely mentioned — each traced to the specific sections claimed, with downstream consequences present (`ExoLive` rewritten, latest-cache-before-dispatch, sidecar carries `mirrored`, `format_exc()`, `payload = "record"`). B5 is landed but incomplete in a way the revision could not have known it was creating (finding 2). **B3/#19 is claimed and is not true.** #19's diagnosis of B3 is correct and better than B3 was; its replacement mechanism fails on the same axis for a third reason neither review found, and I verified rather than reasoned. The immutability guarantee does **not** survive a pickle round-trip as specified, and does not hold in the publisher either for a contiguous view. The finding I would most want acted on, because a finding marked addressed that isn't is worse than one marked open, and this one is load-bearing for T2.

**4. Declines and deferrals.** F-6 (decimation): correct to decline — optimisation with no correctness content, additive later. F-16 (`setup()` timeout): correct, with one unspecified interaction (finding 13). Half of S17 (reusing `finish()`): correct, and for the right reason — the log line is the deliverable and `Status(state="unavailable")` is what a status UI needs. The two structural deferrals (co-location, `retain`) are both properly argued. S9 is the one where I would take the cheap half now (finding 12); documentation is acceptable, but not documentation that lives in `broker.md` when the file authors read is `plugin-api.md` §2.1. T2's deferral is right and its preconditions correctly identified — which is exactly why finding 1 blocks.

## Ruling: may two `ClimbCV` instances run in one process?

**No — one concurrently running instance per process.** The proposed answer is right; sharpen it two ways, both mattering for the environment §7.4 just certified.

The collisions are worse than file corruption. Two instances with the same config each spawn `core.capture`, so two processes open camera 0 and the second gets failure or garbage; both write `logs/<plugin_id>.log`; both hand the same `state_dir/<plugin_id>/` to different children, so two `mac_lid` processes compile to the same path concurrently; both publish with `Meta.source == "<host>"`, so nothing downstream can attribute a host publish; and `isolation.md` §7 lists SIGINT/SIGTERM in the host as a shutdown trigger, so the second instance's handler installation clobbers the first's and Ctrl-C stops one of the two.

**Refinement 1: scope the guard to *running*, not to construction**, releasing it when shutdown completes — a flat "one per process" would forbid `app.stop()` followed by a fresh `ClimbCV(...).run()`, precisely the loop a Jupyter user runs twenty times an afternoon, and the notebook case is the one §2.4 went to the trouble of verifying. Check at `run()`/`start()`; there is precedent in the current code, which already raises `RuntimeError("climbcv is already running")` per instance at `src/climbcv/climbcv.py:486`.

**Refinement 2: answer the adjacent question while you are there** — is a single instance re-runnable after `stop()`? Nothing says, a notebook user will try it on their second cell, and the honest answer is no: declarations are frozen at `run()` (§7.1), queues are consumed, children are dead. Raise with "this ClimbCV has already run; construct a new one", or you get a half-working second run that reports nothing.

Yes, make it a Decision Log entry: it is a public constraint on the embedding API, not an implementation detail, and it is the kind of thing quietly relaxed by someone who does not know about the camera and the signal handler.

## What I judge sound, and what convinced me

Recorded so the next pass does not churn, and because two of these are load-bearing for the verdict.

- **The exclusivity criterion and its application.** The thing I most expected to find wrong — a wrong exclusive/shared call — is right in every case I could test against a plausible plugin, and `holds.boxes` shared with a stated reversibility argument correctly resolves a genuine tie. The only gap is a missing row (finding 11), not a wrong judgement.
- **`Meta.source` framework-injected and unforgeable, plus §2.4's self-timestamping invariant.** §2.4 is the best structural addition in the revision: it answers stale-source expiry without a third accessor and it *binds every future contract type*, which is how a payload surface stays coherent as it grows. `Record` carrying a mandatory `t_ns` because of it is the invariant doing real work on the first new type after it was written.
- **`payloads.md` §5's change-rule table, and that it is now visibly deciding things.** Three of guardian-01's blockers were decided by it; two of my findings (2 and 7) are decided by it. A rule that constrains its own authors is working.
- **The correction of Decision #15's justification.** `isolation.md` §3.2.1 does not merely fix the sentence — it explains why the false model was worse than no model, and adds the narrowness argument (module-level code already runs in the child) that makes the rule comprehensible rather than superstitious. That is the shape a corrected error message should take.
- **`required` doing triple duty**, and both escalation messages (`isolation.md` §4.4.1, §5.3) stating what the user *loses* with a causal chain. `finish()`'s "This is a normal end of run, not an error" is one line that will prevent a lot of bug reports.
- **The two self-found bugs.** The false stall warning for every no-timer plugin, and the frozen-window regression in `PosePlot`, were both found by tracing a real plugin through the runtime rather than reading the spec — the only way that class of bug is found, and evidence the dogfooding is real work rather than a claim.
- **Closing both of Decision #14's self-inflicted asymmetries** (S20/F-3 in the loader, S19 in the embedding API) with one mechanism each, and the bundled root exercising the manifest parser on eight manifests every startup. That is the asymmetry question I would otherwise have led with, and it is answered.

## Beyond scope

- Whether `coco.17` belongs in `TOPOLOGY_SIZES` when nothing publishes it is a judgement I have no view on; finding 3 makes it consequential either way, since it is the topology exposing the transform-stage gap.
- `isolation.md` §8.2's space-padded rewritable `.npy` header plus JSON sidecar is a file format, not an API surface — but it is the format a third-party replay plugin must read. Consider whether `persist_npy/1` should be documented as a contract rather than as one plugin's implementation.
- Performance: nothing to add. The T1 decision and its measurements are the most honest part of `broker.md`.

---

## Verdict

**NOT lockable yet. Two things must change first, and neither requires redesign.**

1. **`payloads.md` §2.2.1's mechanism must be corrected** (finding 1). The guarantee stays as written; the implementation route in the document does not work. All three failures and both fixes were verified by execution. Blocking because the promise is public, T2's preconditions rest on it, and both exits from a broken-but-stated guarantee are breaking changes.
2. **`Record` must declare and check its `kind`** (finding 2). Blocking because making a manifest key required later is a tightening, which this project's own change table forbids — so v1 is the only opportunity, exactly as it was for B4.

Fix those two and the surfaces are lockable. Everything in findings 3–15 is recoverable by an additive change and I would not hold implementation for any of them — though 3, 4 and 5 will each cost someone a bad afternoon and are cheaper to fix now than to explain later. Findings 6–8 are worth an hour of §7 before it is published, because §7 is new and has no installed base yet, which is the only moment that hour is free.

To be plain about the shape of this verdict: after two review rounds and a revision, the design is in good condition and the remaining objections are narrow and mechanical, not architectural. Both blockers are the same species — a guarantee stated more confidently than the mechanism behind it delivers — and both are a day's work. That is a reasonable place for a design phase to end. **Do not schedule a third full review**; verify these two fixes specifically, with the pickle round-trip test as the acceptance criterion for the first, and proceed.
