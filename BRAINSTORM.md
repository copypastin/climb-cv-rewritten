# climb-cv Plugin Architecture — Brainstorm Summary

Status: **architecture locked (Decision #9, confirmed 2026-08-07); detailed section-by-section design in progress**. This document is the handoff artifact for continuing that work, potentially across multiple agents/sessions.

Source repo studied: [copypastin/climb-cv](https://github.com/copypastin/climb-cv) (Aaron's existing project).

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

- Detailed design (skill step 6) **in progress** — `framework-core` delivered its sections on 2026-08-07 into `design/` (`broker.md`, `payloads.md`, `loader.md`, `isolation.md`, `plugin-api.md`, `config-contract.md`). Not yet reviewed. `plugins-and-config` and `docs-and-testing` have not started.
- Proposed Decisions #11–#17 above need user review; #12 and #17 contradict existing written positions.
- **Transport choice T1 vs T2** (`broker.md` §5.3): queue-transport only in v1 vs. shared-memory ring for `frame` now. `framework-core` recommends T1 — `transport` is invisible in the authoring interface so shm is a non-breaking upgrade later. Quantified ceiling: queue transport adequate to ~640×480 with ≤3 frame subscribers.
- **Dependency installation** (`loader.md` §6) — the sharpest edge of "no pip install". A plugin needing an absent package is contained with a clear error, but v1 offers nothing more. Recorded as an open risk.
- `plugin-api-guardian` review of the API surfaces has not run yet.
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
