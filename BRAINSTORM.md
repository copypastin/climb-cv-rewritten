# climb-cv Plugin Architecture — Brainstorm Summary

Status: **architecture locked (Decision #9); revision pass complete at `2a40560` except `plugin-api.md`, which is unfinished and self-contradictory. NOT lockable — second guardian pass required.** This document is the handoff artifact for continuing that work across agents/sessions. **See §0 SESSION HANDOFF below for exactly where to resume.**

Source repo studied: [copypastin/climb-cv](https://github.com/copypastin/climb-cv) (Aaron's existing project).

---

## 0. SESSION HANDOFF — read this first (2026-08-07, 03:41)

Work stopped mid-revision when credit ran out. **Everything is committed; nothing is lost.** Start here.

### Where things stand

| Commit | What it holds |
|---|---|
| `4bb6661` | Baseline — this doc, 4 agent definitions in `.claude/agents/`, `framework-core`'s 6 design files |
| `ca1d564` | `design/reviews/guardian-01.md` — 5 blocking (B1–B5), 18 should-fix (S6–S23), naming rulings, verdicts on #14/#15 |
| `d1e8345` | `plugins-and-config`'s `first-party-plugins.md` + `config.md` — friction registers F-1..F-16 (§10) and C-1..C-8 (§11) |
| `156739a` | Review convergence recorded here |
| `2b7e138` | **Clean pre-revision state.** C-6 accepted, naming rulings, #14/#15, T1/T2 coupling |
| `372673c` | **PARTIAL revision pass — current HEAD.** +2098/−261 across 5 files |

### What the interrupted revision pass did and did not do

**Revision pass complete as of `2a40560`**, with one concentrated gap. `design/revision-01.md` is the per-finding audit trail: **48 addressed, 2 partial, 6 declined, 3 deferred, 1 not reached.** `config-contract.md` is finished (C-1..C-8 plus all four of `config.md` §13's asks, all accepted). New proposed Decisions #18–#23 are in §4 above.

**The one remaining gap is `plugin-api.md`, and it is the highest-priority review target in the project:**

- **§3.6–§3.11, §5 and §7 were never written**, though §2/§2.1 reference them. Almost every "not reached" caveat in `revision-01.md` points here rather than being spread across files — the gap is one file, not a scatter.
- **§4.3 still documents `join=`** despite ruling #4 cutting it, and §6 still lists it as an open question. **The file authors read currently teaches a cut feature and contradicts itself.** This is the sharpest single item.
- **§4.2 still says "a decrease in `seq` means publisher restarted"** without the `meta.source` keying (S12 partial) — corrected where it is *stated* (`payloads.md` §2.3, `broker.md` §5.4) but not where it is *taught*.
- **S19 (the embedding API) is the largest outstanding design gap.** Only the verification half landed: `isolation.md` §2.4 records the empirically confirmed `spawn`-from-notebook result, and `broker.md` §6 reserves `WiringPlan.host: HostPlan | None` with §4.2 step 2 admitting `<host>` as a candidate publisher. The design section (`plugin-api.md` §7) does not exist, so `<host>` still has no manifest equivalent, no declared subscriptions, no `requires_topology`, and no place in the resolution rules — while being a public data-plane participant visible in the CLI output.

### Do these next, in order

1. **Finish `plugin-api.md`** — write §3.6–§3.11, §5, §7; delete `join=` from §4.3 and §6; apply the `meta.source` keying to §4.2. `revision-01.md` names what each unwritten section was to contain, and the intended shape of §7 is in the resumed agent's final summary.
2. **Second `plugin-api-guardian` pass.** `framework-core`'s own verdict is **not lockable** — partly because `plugin-api.md` is self-contradictory, partly because the surface genuinely grew: the base class went from 7 members to 11, `[[subscribes]]` gained three keys, `[topics]` gained three. That is new author-facing surface deserving review on its own merits, not as deltas.
3. **Then** `docs-and-testing`, which has deliberately never run.
4. Implementation stays gated behind §7 step 2's multi-agent review.

### A bug found in the design itself, worth not losing

Neither review caught it; `framework-core` found it in its own work. The child loop's blocking read used `timeout=None` when a plugin has no timers, while the heartbeat is sent *from that loop* — so any no-timer plugin whose input pauses would be reported **stalled while perfectly healthy**. `pose_plot` as originally sketched is exactly that plugin. Fixed in the revision; recorded here because it is the kind of interaction that only shows up when someone traces a real plugin through the runtime.

### Two operational notes

- **`.claude/agents/` is loaded at session start.** The four agents were created mid-session, so the early runs (framework-core's step-6 design, guardian review 01, plugins-and-config's design, and the interrupted revision pass) all used `general-purpose` with the agent file's contents inlined verbatim — functionally equivalent, and the reason those runs appear under a generic name. **They registered as of 2026-08-07 03:4x and are now invocable directly by name.** Prefer that; also prefer resuming an existing agent over spawning a fresh one, since a resume keeps its context instead of re-reading the whole `design/` tree.
- **`~/Desktop/2/`** holds stale copies of this file and `.claude/agents/`, stranded there by a permission-prompt glitch that renamed a path instead of running a command. Everything in it is superseded by this repo. Safe to delete; left in place because it is not mine to remove.

---

## 1. Understanding Summary

- **Goal:** refactor climb-cv's existing pipeline (webcam capture → MediaPipe pose estimation → OneEuroFilter smoothing → YOLO hold detection → live overlay/plot → landmark save/replay) into a plugin-based architecture.
- **UX model:** Fabric-mod-style — plugins are dropped into a `plugins/` folder next to the app and discovered/loaded at startup. No `pip install` required for v1.
- **Audience:** public ecosystem. Third parties (not just Aaron) are expected to write plugins, so the plugin API needs to be documented and reasonably stable, not just convenient for one author.
- **What plugins can do:**
  - React to data as observers (landmarks, hold boxes, frames)
  - Add brand-new detection/analysis stages
  - Replace/swap existing core stages (e.g. a different pose model, hold-detector, or camera source)
  - Add new output sinks
- **Dogfooding:** current built-in features — YOLO hold detection, live 3D plotting, exo_live overlay, mac lid-angle sensor — get rewritten as first-party plugins on the new API. Core shrinks to: camera capture, pose estimation, smoothing, landmark persistence/replay, and the plugin framework/loader itself.

## 2. Current Architecture (baseline, for reference)

- Single main thread: capture (OpenCV, per-OS backend) → MediaPipe `PoseLandmarker` (GPU w/ CPU fallback) → `OneEuroFilter` smoothing, all synchronous, one frame at a time.
- Heavy/optional work runs in separate OS `Process`es communicating over `multiprocessing.Queue`s, so it never blocks the main loop:
  - YOLO hold detection (`yolo_boxes_worker`) — runs every Nth frame, downscaled input, boxes drawn back onto the live feed.
  - Live 3D plotting (`plotting_process`) — consumes a queue of landmark frames.
  - macOS lid-angle sensor — Swift subprocess, macOS-only, auto-disabled elsewhere.
- On stop, accumulated smoothed landmark frames are saved to a `.npy` file; `replay()` reloads and re-animates them.
- `start(blocking=False)` supports running on a background thread with an `on_landmarks` callback, for embedding in other apps.

This existing worker-process/queue pattern is the direct precedent for how plugin isolation should work.

## 3. Assumptions (explicit, unconfirmed unless noted)

- Stays Python **3.11+**, same runtime/stack as today — no language rewrite. **Floor raised from 3.10 by Decision #17 (accepted 2026-08-07)**: `tomllib` is stdlib only from 3.11, and a permanent `tomli` dependency would be imposed on every plugin author given there is no `pip install` step.
- Plugins load once at startup from `plugins/`; no hot-reload requirement (restart to add/remove/change).
- No plugin-to-plugin dependency/versioning system in v1 — plugins are independent of each other.
- No hard cap on concurrently running plugins in v1; no built-in resource budget/throttling.
- Plugin manifest is `id`, `version`, entry point, `name`/`description`/`author`, plus `publishes`/`subscribes`/`platforms`/`api_version`. No homepage/registry/dependency-graph fields until an actual registry exists. **Revised 2026-08-07 from the original "minimal: `id`/`version`/`type`/entry point" on two counts:** the `type` field is dropped as obsoleted by Decision #9 — with one uniform authoring model there is no type to declare, and what a plugin *is* is fully described by which topics it publishes and subscribes to; and the topic/platform/version fields are structurally required because the host must never import plugin code to discover the topic graph. `platforms` also *removes* a first-party special case (the mac-lid `sys.platform` check).
- A simple plugin-API version check (plugin declares "needs API ≥ X.Y", framework fails with a clear error on mismatch) — not a full dependency resolver.
- **Confirmed:** capture, pose estimation are both swappable core concerns (not just hold-detection), consistent with the "replace/swap core stages" requirement explicitly naming camera source as an example.

## 4. Decision Log

| # | Decision | Alternatives considered | Why |
|---|----------|--------------------------|-----|
| 1 | Public ecosystem is the target audience | Personal-only tool; small collaborator group | User explicitly wants a Fabric-mod-like public plugin ecosystem |
| 2 | Plugins support all 4 extension types: observers, new stages, stage replacement, new sinks | Any subset of the above | User selected all four as needed |
| 3 | Discovery = drop-in `plugins/` folder | Installed pip packages (entry_points); both | Matches the explicit "drag and drop like Fabric mods" requirement; no pip install needed |
| 4 | Framework auto-isolates plugin "stages" into their own OS process/queues | Plugin author manages own concurrency; isolate only heavy types | Preserves the current guarantee that the camera loop never blocks; plugin authors shouldn't need to know concurrency to write a detector |
| 5 | Crashing plugin is isolated and logged; rest of app keeps running | Any plugin crash fails the whole run | Public ecosystem means untrusted-quality third-party code is expected; one bad plugin shouldn't take down the app |
| 6 | No sandboxing in v1 — plugins fully trusted | Restrict filesystem/network access from the start | Real Python sandboxing is heavy to build; matches Fabric's own trust model (mods are trusted JVM code); documented as a conscious trade-off, not an oversight |
| 7 | Existing features (hold detection, plotting, exo_live, lid sensor) become first-party plugins on the new API | Keep them built-in, plugins additive-only | Proves the plugin API is actually sufficient; core shrinks to capture/pose/smoothing/framework only |
| 8 | Config: single unified file (`climbcv.toml`) sectioned by plugin id, framework passes each plugin its raw section dict — **no schema/validation layer in v1** | Fully decentralized (each plugin invents its own config file/format); fully centralized *with* typed schema validation | Centralizing the file gives one place to see/edit everything and free enable/disable toggling, without paying for a validation-layer build now (YAGNI); can be layered on later without changing the file format |
| 9 | **[LOCKED 2026-08-07]** Architecture = Approach 1, event bus / pub-sub with **exclusive vs. shared topics**, over Approach 3's fixed 3-slot hybrid | Approach 2: ordered linear pipeline chain. Approach 3: hybrid — 3 fixed swappable core slots (capture/pose/smoothing) synchronous in-loop, everything else on an async bus | User explicitly prioritized long-term plugin-author simplicity/adaptability over short-term build cost ("harder backend if it means a simpler tool to build off of"). A single uniform authoring model (subscribe/publish to topics) has no structural ceiling — Approach 3's fixed core slots would require a framework change to add a 4th swappable core concern later; Approach 1 doesn't. "Replace a core stage" and "add a new stage" collapse into the same mechanism via exclusive topics (single active publisher) vs. shared topics (many publishers/subscribers) — precedented by ROS's node/topic model for exactly this kind of heterogeneous, swappable, third-party-extensible pipeline |
| 10 | Design+build split across 3 domain-specialist subagents (`framework-core`, `plugins-and-config`, `docs-and-testing`) plus a read-only `plugin-api-guardian` reviewer | The 6-way split in §7 as 6 separate agents; one agent doing everything; 6 agents plus an orchestrator | Consolidating §7's six concerns to three keeps ownership coarse enough to avoid cross-agent context-switching while still separating framework internals from its first consumer from its docs/tests. Specialists carry a slice design→implementation rather than being discarded after step 6. Guardian is read-only so review stays independent of authorship. Seam note: config parser lives with `plugins-and-config`; `framework-core` consumes it via a consumer-driven key contract rather than reading the TOML itself |

### Step-6 design decisions (#11–#17)

Produced by `framework-core` on 2026-08-07; full reasoning in `design/`. **#12 and #17 accepted 2026-08-07** and folded into §3/§5 above. The rest remain proposed pending review — most are internal mechanics, but #13 defines a public surface and should clear `plugin-api-guardian` before it is locked.

| # | Proposed decision | Status |
|---|-------------------|--------|
| 11 | Brokerless: static peer-to-peer wiring computed in the host at startup, plus a low-volume control plane for logs/heartbeats/crashes. Publisher process → subscriber process directly, no forwarding process in the data path | proposed |
| 12 | **`holds.boxes` is SHARED, not exclusive.** Exclusivity decided by a stated criterion — "a topic is exclusive iff its payload is a singleton observation of a unique subject, such that two publishers would make mutually contradictory rather than additive statements" — not by a framework-owned enumeration. Any plugin may declare a topic exclusive | **ACCEPTED 2026-08-07** — §5 prose corrected. Reversibility was decisive: shared→exclusive later is a config change, exclusive→shared later breaks every subscriber's delivery contract |
| 13 | Payload contracts = framework-shipped frozen dataclasses in `climbcv.contracts` validated at construction, plus `topology` ids checked at **wiring time** (resolver refuses to start on mismatch), plus `api_version` as the single resolution mechanism | proposed |
| 14 | **Full isolation** — every stage, built-in or third-party, in its own child process; host runs only supervisor/wiring/logging/embedding API. `spawn` forced on all platforms | proposed |
| 15 | `setup()` runs in the child after spawn; **`__init__` is reserved** so no author ever pickles a model | proposed |
| 16 | Restart: exponential backoff 0.5→30 s; quarantine after 5 crashes/60 s; 2 attempts for pre-READY setup failures; hangs warned about but never killed | proposed |
| 17 | **Raise the Python floor to 3.11** (stdlib `tomllib`, avoiding a permanent `tomli` dependency imposed on every author) | **ACCEPTED 2026-08-07** — §3 assumption updated from 3.10+ to 3.11+ |
| 18 | **T1 (queue transport) for v1, with stated T2 preconditions.** The original justification — "T2 is free because `transport` is invisible to authors" — is **withdrawn as wrong**: it is non-breaking at the API level and data corruption at the authoring level (F-11). T2 must not ship without (a) read-only payload arrays and (b) the arrays-stay-valid-for-the-payload's-lifetime guarantee, both now in v1. Measured: T2's win is bandwidth, not zero-copy (memcpy 5.6 µs vs pickle 10.5 µs at 320×240), so copy-out T2 keeps nearly all of it | proposed |
| 19 | **Payload arrays are immutable and remain valid for the payload's lifetime.** Enforced via `_ARRAY_FIELDS` + `__post_init__` + **`__setstate__`**, arrays normalised to owned C-contiguous buffers | proposed — supersedes guardian B3, whose mechanism was **measured insufficient**: `__post_init__` is bypassed on unpickle and numpy does not preserve `writeable=False` across a pickle round-trip, so B3 as written froze arrays in the publisher and left them writeable in every subscriber — exactly where both of B3's own failure cases live |
| 20 | **Two plugin roots, user `plugins/` shadowing bundled** (F-3). Duplicate id fatal within a root, shadow across roots with an INFO line. Absorbs S20: built-ins become ordinary directories with real manifests, so the manifest parser is dogfooded rather than bypassed | proposed |
| 21 | **Conflation is per-subscription** (`conflate = false`), making a correct recorder expressible by third parties and not only by first parties | proposed |
| 22 | **Deterministic authoring mistakes raise `PluginContractError`** — non-retryable, one message, no app-shutdown escalation. Plus a reserved-name set with a `cv_` prefix, which is what makes future base-class additions genuinely additive | proposed |
| 23 | **`requires_topology`/`provides_topology` mandatory** for any plugin touching a `PoseFrame` topic, keyed off payload type rather than a name prefix | proposed — closes guardian B4 |

### Review outcomes accepted 2026-08-07 (inputs to the revision pass)

- **C-6 ACCEPTED:** the plugin manifest gains an **optional, informational** `[config] keys = [...]` list — names only, no types, no rejection, ignoring it is legal. Used solely to turn a typo'd plugin option into a nearest-match warning instead of silence, and to let `climbcv init` generate a useful example. Deliberately inside Decision #8's no-validation-layer posture: it never rejects a value.
- **Naming questions settled** per `design/reviews/guardian-01.md`: keep `setup`/`teardown`, `latest()` (name kept, return type must change), `@every(seconds)`, `finish()` (name kept, contract must document the app-shutdown escalation). **`join=` is cut from v1** — undefined on shared topics, and silently broken against the `frame_seq == -1` replay sentinel, which matters because `replay()` is itself recommended as a plugin. Re-adding it later is a keyword argument with a default, i.e. a clean additive minor.
- **Decision #14 upheld** on review, with a third argument the design had not made: in-host built-ins would fork the *diagnostic* surface too, so "how do I debug this stage" would depend on who wrote the stage.
- **Decision #15's mechanism upheld, its justification rejected as factually wrong.** No plugin instance is ever pickled — the child constructs it — so the cited "opaque pickling error at spawn time" cannot occur. The reservation is still correct for a different reason: the framework binds `config`/`log`/`publish`/`latest` after construction, so inside `__init__` none exist. The Decision Log entry and the error text must both be corrected before lock, because the current message teaches authors a false model of what crosses a process boundary.
- **T1 vs T2 is no longer purely a performance call.** The guardian concurred with T1 on the grounds that `transport` is invisible to plugin authors; `first-party-plugins.md` F-11 refutes that — under T1 mutating `frame.pixels` is harmless (private copy per subscriber), under T2 it corrupts every other subscriber. Payload mutability must therefore be settled *before* T2 can ever ship, or a framework minor bump turns working plugins into data corruption.

## 5. Chosen Direction (locked 2026-08-07)

**Approach 1, refined: pub/sub event bus with exclusive and shared topics.**

- Some topics are **exclusive** — exactly one active publisher. Swapping a core stage (e.g. a different pose model) = config points the exclusive topic at a different plugin; framework refuses to start two publishers on the same exclusive topic.
- **Exclusivity is decided by criterion, not by a framework-owned list** (Decision #12, accepted 2026-08-07): *a topic is exclusive iff its payload is a singleton observation of a unique subject, such that two publishers would make mutually contradictory rather than additive statements.* Any plugin may declare a topic exclusive. An enumeration was rejected as a structural ceiling in disguise — it leaves the next person adding a topic with no way to decide.
  - `frame` and `pose.smoothed` are exclusive under this criterion: two skeletons for one climber contradict each other.
  - **`holds.boxes` is SHARED**, correcting this section's original prose. The union of two detectors' boxes is still a valid set of boxes, the plural case is ordinary (YOLO plus a hand-annotated route map), and framework-injected `meta.source` disambiguates overlaps. It remains part of the standard topic *vocabulary* — only exclusivity flipped.
- Arbitrary new signals a plugin invents (e.g. `grip_strength.estimate`) are **shared** topics — many publishers/subscribers, no ownership.
- One uniform plugin-authoring model for every plugin type: subscribe to topics, publish to topics. No separate "core-stage plugin" vs. "bus plugin" API to learn.
- Isolation (Decision #4) and fault tolerance (Decision #5) apply per-plugin-process regardless of topic type.

## 6. Open / Not Yet Decided

- Detailed design (skill step 6) **in progress** — `framework-core` delivered `broker.md`, `payloads.md`, `loader.md`, `isolation.md`, `plugin-api.md`, `config-contract.md` on 2026-08-07. `plugins-and-config` delivered `first-party-plugins.md` and `config.md` the same day, with friction registers F-1..F-16 (§10) and C-1..C-8 (§11). **`docs-and-testing` has not started** — deliberately held until the API stabilizes, so the authoring guide isn't written against a surface still in revision.
- **Consolidated revision pass not yet run.** Two independent reviews are outstanding against `framework-core`'s design: `design/reviews/guardian-01.md` and the two friction registers. They converge on `latest()` (guardian B1 = F-1) and payload mutability (B3 = F-11), which is corroboration from two directions rather than one reviewer's opinion.
- **`plugins-and-config`'s headline verdict:** none of its 16 findings required a private hook or a framework-internals import, so the public API is **sufficient in kind, incomplete in three specific places** (F-1 `latest()`, F-2 configurable timer intervals, F-14 unconditional conflation) **and under-documented in two more** (F-4, F-11). Decision #4's "authors need no concurrency knowledge" is judged *bent, not broken*: no plugin writes a lock, but three of four must understand the queue's drop policy to be correct — delivery-semantics leaks rather than concurrency-primitive leaks.
- Proposed Decisions #11–#17 above need user review; #12 and #17 contradict existing written positions.
- **Transport choice T1 vs T2** (`broker.md` §5.3): queue-transport only in v1 vs. shared-memory ring for `frame` now. `framework-core` recommends T1 — `transport` is invisible in the authoring interface so shm is a non-breaking upgrade later. Quantified ceiling: queue transport adequate to ~640×480 with ≤3 frame subscribers.
- **Dependency installation** (`loader.md` §6) — the sharpest edge of "no pip install". A plugin needing an absent package is contained with a clear error, but v1 offers nothing more. Recorded as an open risk.
- `plugin-api-guardian` review 01 delivered 2026-08-07 → `design/reviews/guardian-01.md`. **5 blocking, 18 should-fix**, plus rulings on the five open naming questions and on Decisions #14/#15. Not yet actioned — a consolidated revision pass is pending, to be run once `plugins-and-config` reports so both sets of findings are addressed together. Headline blockers: `latest()` discards `Meta` (which cancels the premise #12 was accepted on), `PoseFrame` cannot express `mirrored`, payload arrays are mutable, `requires_topology` defaults to unsafe, and author-defined payloads are inexpressible with a silent wrong-class unpickle path. #14 upheld; #15's mechanism upheld but its stated justification is factually wrong and must be corrected before lock.
- Documentation step (final design doc, plugin-authoring guide) not started.

## 7. Suggested Next Steps

1. Run the incremental section-by-section design (skill step 6) covering: architecture diagram, topic broker mechanics, plugin manifest/loader, isolation runtime, config loading, first-party plugin conversions, error handling, testing strategy. Design sections land as separate files under `design/`; the Decision Log here is updated only by the main thread, so concurrent agents don't collide. — *`framework-core`'s portion delivered 2026-08-07; `plugins-and-config` and `docs-and-testing` outstanding.*
2. Per this project's brainstorming process: because this is a **public, hard-to-reverse API decision**, the finalized design + this Decision Log must be handed to a **multi-agent-brainstorming** pass for review *before* implementation begins — not skipped in favor of just picking a stronger single model.
3. Only after that review: implementation.

### Step 1 is split across agents per Decision #10 — the original 6-way decomposition, and how it maps to the 3 agents actually created:

- **Broker & core pipeline** — topic registry, exclusive/shared semantics, cross-process pub/sub mechanics, how capture/pose/smoothing wire in as the default exclusive-topic publishers.
- **Plugin loader & manifest** — folder scanning, manifest format/parsing, API-version compatibility check, enable/disable via `climbcv.toml`.
- **Isolation & fault-tolerance runtime** — auto-spawning a process per stage plugin, crash detection/isolation/logging, restart or backoff policy.
- **First-party plugin conversions** — hold detection, live plotting, exo_live overlay, lid sensor rebuilt on the new API, serving as both dogfood proof and reference examples for third-party authors.
- **Config system** — `climbcv.toml` parsing, per-plugin section passthrough, defaults handling.
- **Docs & testing strategy** — plugin-authoring guide, example plugin template, test approach for a framework whose whole point is running untrusted third-party code.

These integrate through the manifest format and the topic broker's public interface rather than sharing deep internal state, which is what makes splitting them viable. Actual mapping onto the agents in `.claude/agents/` (Decision #10):

| Original bucket | Owning agent |
|---|---|
| Broker & core pipeline | `framework-core` |
| Plugin loader & manifest | `framework-core` |
| Isolation & fault-tolerance runtime | `framework-core` |
| First-party plugin conversions | `plugins-and-config` |
| Config system | `plugins-and-config` |
| Docs & testing strategy | `docs-and-testing` |

Cross-cutting: `plugin-api-guardian` (read-only) reviews any surface a third-party plugin author touches — topic names and payload shapes, the plugin base class, manifest fields, config contracts, lifecycle hooks, error messages — before it is locked.

---

## Appendix: file-history note

On 2026-08-07 this file and `.claude/` were moved out of the project directory to `~/Desktop/2/` outside this session; a copy was then reconstructed here from the session transcript, so two versions briefly existed. This version is the fuller one — it carries the §3/§5 challenge markers and the proposed Decisions #11–#17 from the step-6 design pass. The project is not under version control; initializing git would make this class of divergence self-evident.
