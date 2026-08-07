# Revision 01 — per-finding changelog

Date: 2026-08-07 · Author: `framework-core` · Inputs: `design/reviews/guardian-01.md` (B1–B5, S6–S23,
notes, rulings), `first-party-plugins.md` §10 (F-1..F-16), `config.md` §11 (C-1..C-8).

**This file is the audit trail.** One row per finding, its outcome, and where it landed. A finding
marked *addressed* means a design file now says something different; *declined* means a stated reason;
*deferred* means agreed but explicitly out of v1; **not reached** means the revision pass ran out of
budget before it — those rows are listed rather than omitted, so the gaps are visible.

Outcome counts: **48 addressed, 2 partial, 6 declined, 3 deferred, 1 not reached.**

The pass was interrupted once and resumed. **One thing remains known-incomplete and must be finished
before any lock:**

- **`plugin-api.md` §3.6–§7 were not written.** Sections §3.7–§3.11 and §7 are *referenced by* §2/§2.1
  but do not exist yet, and **§4.3 still documents `join=`** as the recommended API even though ruling #4
  cut it. Anyone reading `plugin-api.md` alone today gets a file that contradicts itself. This is where
  S19 (the embedding API) and the remainder of S12 live.

`config-contract.md` was written in the resumed pass and is complete: C-1 through C-8 and all four of
`config.md` §13's asks are now in it.

---

## Blocking (B1–B5)

| # | Outcome | Where it landed |
|---|---|---|
| **B1** `latest()` returns no `Meta` | **addressed** | `latest_by_source(topic) -> Mapping[str, payload]` added: `plugin-api.md` §2, §3.5. Multi-publisher WARNING at wiring time: `broker.md` §4.2. Runtime one-time WARNING from `latest()`: `plugin-api.md` §3.5. `UndeclaredTopicError` on `latest()`, symmetric with `publish()`: `loader.md` §7, `plugin-api.md` §3.5. Latest-cache updated before dispatch, so `latest()` is not a frame stale: `isolation.md` §3.3. `ExoLive` example rewritten to key by `meta.source`: `plugin-api.md` §2.1. `broker.md` §3.1's unreachable-mitigations paragraph corrected. Staleness expiry answered without a third accessor via the new self-timestamping invariant: `payloads.md` §2.4. |
| **B2** `PoseFrame` cannot express `mirrored` | **addressed** | `mirrored: bool` added with rationale: `payloads.md` §3.2. `world`'s axes restated as *image*-right rather than *subject's* image-right. Downstream half (a saved `.npy` carries no mirror flag) closed by the sidecar: `isolation.md` §8.2. |
| **B3** payload arrays are mutable | **addressed, and the recommended mechanism was insufficient** | `payloads.md` §2.2.1. **Measured finding neither review had:** `__post_init__` is bypassed on unpickle (frozen `slots=True` dataclasses reduce via `__newobj__`) *and* numpy does not preserve `writeable=False` across a pickle round-trip — so B3-as-recommended freezes arrays in the publisher and leaves them writeable in **every subscriber**, which is where both of B3's own failure cases live. Fixed with a shared `_Arrays` mixin implementing `__setstate__` plus a per-type `_ARRAY_FIELDS` list; arrays are also normalised to owned C-contiguous buffers. Cost measured: 0.23 µs/array/delivery. `as_rgb()`/`as_bgr()` specified as always a fresh writable C-contiguous array; `ExoLive`'s `.copy()` dropped so the two examples agree. |
| **B4** `requires_topology` defaults to unsafe | **addressed** | `payloads.md` §4.0 — mandatory for any plugin publishing/subscribing a topic whose descriptor payload is `PoseFrame`, with `"any"` as the explicit opt-out and a manifest error stating both choices. Keyed off payload type rather than a `pose.*` name prefix. Manifest rule: `loader.md` §3.1; skip outcome: §5 rule 3. |
| **B5** author-defined payloads inexpressible; wrong-class unpickle | **addressed** | `Record(kind, t_ns, data)` added: `payloads.md` §3.6, with recursive primitive validation, ndarray leaves allowed and normalised, depth/leaf caps, and an honest O(size) cost note. `publish()` `isinstance` check → `PayloadTypeError` in the publisher: `plugin-api.md` §3.4. Rule stated once — *nothing but `climbcv.contracts` types crosses the data plane, nothing but strings and primitives crosses the control plane*: `payloads.md` §3.6, with the traceback-as-`format_exc()` consequence in `isolation.md` §4.2. `payload = "record"` in the manifest: `loader.md` §3. |

## Should-fix (S6–S23)

| # | Outcome | Where it landed |
|---|---|---|
| **S6** Decision #15's justification and `__init__` message are wrong | **addressed** | `isolation.md` §3.2.1 (full correction, including that the reservation is *narrow* — module and class-body code already run in the child under `spawn`) and `plugin-api.md` §3.1 (new message text). Discoverability: `# no __init__` comment in the §2 sketch and §2.1's first example. Non-retryable `PluginContractError`: `isolation.md` §4.5. Proposed Decision #15 replacement text is in the summary, not in a file, since `BRAINSTORM.md` is not mine to edit. |
| **S7** startup fatals with no local recourse; `source` unused in messages | **addressed** | `broker.md` §4.4 — (a) `[topics.<n>] exclusivity=/kind=` overrides, restricted to non-standard topics; (b) benignly-skipped named publisher falls back with a WARNING, split three ways by whether the user erred; (c) `[topics.<n>] required = false` as the user-side subscription-optional override. `source` threading + "Create ./climbcv.toml containing:" when there is no file: `broker.md` §4.3. |
| **S8** `teardown_timeout_s = 1.0` can lose the session | **addressed, jointly with F-14** | `isolation.md` §7.1 (per-plugin declarable budget, default 1.0, cap 30.0, INFO when >1.0, shutdown waits `max(grace_s, max declared)`) **and** §8.2 (`core.persist_npy` appends incrementally with a space-padded rewritable `.npy` header, so teardown is a `close()` and a `SIGKILL` loses ≤1 s). Manifest key: `loader.md` §3.1. Both halves were needed — the budget alone leaves the crash path, the format alone leaves the third-party MP4 writer. |
| **S9** a once-published STREAM message may never be seen | **addressed as documentation; structural fix deferred** | `broker.md` §5.4.1 states it plainly, gives the `@every` republish idiom, points author-declared static topics at `kind = "event"`, and adds a zero-delivery INFO after 10 s. A `retain`/`state` kind is **deferred** — it makes the wiring planner hold message state, which it currently does not. |
| **S10** `stream_depth`'s documented default disables the mitigation | **addressed** | Default is `0` = computed: `broker.md` §5.1.2, `isolation.md` §8.1. Resolved per-subscriber depth printed by `climbcv topics -v`: `broker.md` §7. Flagged to `plugins-and-config` to fix `config.md` §10's `# stream_depth = 4`. |
| **S11** `topology` spot-checked once, in the publisher | **addressed** | `payloads.md` §4.1 — per-delivery check in the subscriber, attribution to the publisher: subscriber logs one ERROR and stops delivering that topic to itself; host quarantines the publisher as a non-retryable contract violation. Consequence (a GPU-delegate fallback now ends the run) defended explicitly. |
| **S12** `Meta.seq` claim is false on shared topics | **partial** | Corrected where it is *stated*: `payloads.md` §2.3, `broker.md` §5.4. **Not fixed where it is taught:** `plugin-api.md` §4.2 still says "a decrease in `seq` means publisher restarted" without the `meta.source` keying. That file section was not reached. |
| **S13** `Scalar`'s unit is prose and unchecked | **addressed** | `unit` required in `[[publishes]]` when `payload = "scalar"`: `loader.md` §3. Field on `TopicDescriptor` and in the merge-equality check: `broker.md` §2, §4.2. Rationale and the deliberate non-validation of unit *strings* (an enumeration would be the Decision #12 ceiling one level down): `payloads.md` §3.4. |
| **S14** `version` free-form now, unparseable later | **addressed** | Semver validated at manifest load, resolved on never: `loader.md` §3.1. |
| **S15** (a) `id` grammar forbids dots (b) `entry` typo costs two spawns | **addressed** | (a) grammar is now the same as topic names, dots permitted, `core.`/`climbcv.`/`<host>` reserved: `loader.md` §3.1. (b) entry module file existence checked at discovery by filename, error lists the `.py` files present: `loader.md` §3.1. |
| **S16** vendoring with no arch declaration; unhelpful dependency message | **addressed** | `platforms` accepts any `sys.platform` string (removing the enumeration) plus optional `-<machine>` arch tags: `loader.md` §3.1. Message now prints `sys.executable` and a ready `-m pip install` line, omits the `requires` block entirely when empty, and states that `vendor/` is Python-version-, ABI- and arch-specific: `loader.md` §6. |
| **S17** no graceful "not applicable here"; is `finish()` legal in `setup()`? | **addressed, one half declined** | `self.unavailable(reason)` added: `isolation.md` §4.4.2, `plugin-api.md` §2, §2.1. `finish()` also made explicitly legal in `setup()`. **Declined:** S17's cheaper suggestion of *reusing* `finish()` for this — the entire finding is that *"mac_lid completed its work"* is the wrong sentence, so a fix with the right mechanics and the wrong words solves the smaller half. |
| **S18** a blocking source cannot notice shutdown | **addressed** | `self.stopping` added to the class surface: `plugin-api.md` §2. Idiom and the per-plugin `heartbeat_warn_s` companion: `isolation.md` §3.3, §5.4, `loader.md` §3.1. **Caveat:** `plugin-api.md` §3.10, where the semantics were to be written out, was not reached. |
| **S19** the embedding API is an undesigned public surface | **not reached — the largest outstanding gap** | Only the verification half landed: `isolation.md` §2.4 records the empirically confirmed `spawn`-from-notebook result (tested with `__main__` having no `__file__` and `__spec__ is None`: the child does not re-execute parent code, `parent_process() is not None` still works as the missing-guard detector, package-level target and dataclass args round-trip). `broker.md` §6 reserves `WiringPlan.host: HostPlan \| None` and §4.2 step 2 admits `<host>` as a candidate publisher. **The design section itself (`plugin-api.md` §7) does not exist**, so `<host>` still has no manifest equivalent, no declared subscriptions, no `requires_topology`, and no place in the resolution rules. The intended shape is in the summary. |
| **S20** synthetic built-in manifests weaken the dogfood claim | **addressed** | `loader.md` §2.1 — built-ins ship as ordinary directories in a bundled root with real `climbcv-plugin.toml` files parsed by the same code; the built-in provider list and synthetic manifests are gone. §5 rule 9 and the closing paragraph updated. Absorbed into F-3's mechanism, so one change serves both. |
| **S21** `Status.state` is free-form and already divergent | **addressed** | `STATUS_STATES` frozenset of eight values, validated in `__post_init__`, with a forward-compatibility obligation on subscribers and a change rule: `payloads.md` §3.5, §5. Transition→state mapping table: `isolation.md` §6.3. `plugin_id`-is-a-subject / `Meta.source`-is-the-sender documented on the type. |
| **S22** no reserved namespace on `self` | **addressed** | `isolation.md` §4.5 — the reserved set plus `cv_`/`_cv_` prefixes, verified after `setup()` at startup step 10, with the error text. Explicitly the mechanism that makes the six revision-01 additions safe and future ones additive. |
| **S23** `validate` cannot catch a conditional publish | **addressed** | `loader.md` §7 — `validate` AST-scans `self.publish("<literal>")` call sites with no import, diffs against `[[publishes]]`, and reports when it saw a computed topic name so the author knows the check was incomplete. |

## Guardian notes

| Note | Outcome | Where |
|---|---|---|
| `app.shutdown` shares the drop-newest event queue | **addressed** | `broker.md` §5.2.1 — rule-shaped: a topic whose subscriber is `<host>` in its supervisor role travels on the control queue. Guarantee stated; a dropped `app.shutdown` logs at ERROR. |
| `@every(n)` interval semantics undefined | **addressed** | `isolation.md` §3.3.1 — fixed delay from return, missed ticks dropped never queued, ≤1 invocation per timer per turn. Same as F-15. |
| grayscale `(H,W,3)` cost belongs in the budget table | **addressed** | `payloads.md` §3.1 and a row in `broker.md` §5.3. One-code-path argument kept. |
| `Frame.pixels` / `PoseFrame.image` state no origin | **addressed** | `payloads.md` §3.1, §3.2 — origin top-left, row-major, `pixels[y, x]`, and `image`'s normalisation direction. |
| `platforms` is a framework-owned enumeration | **addressed** | `loader.md` §3.1 — any string accepted, unrecognised warns. |
| stall warning will false-positive on the blessed `@every(0)` source | **addressed** | `isolation.md` §5.4 reworded (names the threshold and the key, says it is not an error, states the only real consequence) plus per-plugin `heartbeat_warn_s`. Separately, `broker.md` §5.1 / `isolation.md` §3.3 cap the blocking read at 1 s, which fixes a *second* false-stall case neither review found: a no-timer plugin whose input pauses stopped heartbeating entirely. |
| name the `__main__`-guard detection mechanism | **addressed** | `isolation.md` §2.4 — `multiprocessing.parent_process() is not None` checked on entry to `ClimbCV.run()`, catching it on first re-entry. |
| template ids should be obviously placeholder | **addressed** | `loader.md` §8 handoff (`my_detector`, not `detector`, tied to §5 rule 8's duplicate-id fatal). **Caveat:** `plugin-api.md` §5, which fixes template scope, was not reached. |

## Rulings

| Ruling | Outcome | Where |
|---|---|---|
| #1 keep `setup`/`teardown` | **accepted** | Unchanged. Docstring caveat ("once, in this plugin's own process, before any handler runs") — *not reached* (`plugin-api.md` §3.6+). |
| #2 keep `latest()`, fix the return | **accepted** | `plugin-api.md` §3.5 — name and return type unchanged; shared case gets `latest_by_source()`. |
| #3 keep `@every(seconds)` | **accepted** | Unchanged; `set_interval` re-parameterises rather than replaces (`plugin-api.md` §3.2). |
| #4 cut `join=` | **accepted, incompletely applied** | Cut and recorded in `isolation.md` §2.2 and `broker.md` §5.4. **`plugin-api.md` §4.3 still documents it** — not reached. This is the sharpest of the incomplete items: the file authors read still teaches a cut feature. |
| #5 keep `finish()`, fix the contract | **addressed** | `isolation.md` §4.4.1 — escalation stated, plus the missing causal log message for the `finish()`-triggered shutdown, shaped like §5.3's. Legality in `setup()` settled (§4.4.2). Docstring half in `plugin-api.md` §3.7 — not reached. |
| On #14 (full isolation) | **upheld** | Guardian's third argument (a forked *diagnostic* surface) is now the closing bullet of `loader.md` §2.1. Both self-inflicted asymmetries: loader one closed (S20), embedding one **not** (S19). |
| On #15 | **mechanism upheld, justification corrected** | See S6. |

## First-party friction (F-1..F-16)

| # | Outcome | Where |
|---|---|---|
| **F-1** `latest()` cannot express a shared topic | **addressed** | Same as B1. |
| **F-2** timer intervals cannot be configured | **addressed** | `self.set_interval(handler, seconds)`, `setup()`-only, requires an existing `@every`: `plugin-api.md` §2, §3.2. Used in the `PosePlot`, `LidSensor` and `ExoLive` examples in §2.1. |
| **F-3** nothing ships the first-party plugins | **addressed — rule: relaxation accepted** | `loader.md` §2.1. Two roots (user, bundled), `use_bundled_plugins` knob, duplicate id **fatal within a root / shadow across roots with an INFO line** exactly as requested; shadowing is by id and all-or-nothing; `origin` on `PluginPlan` and in `climbcv topics -v`. §5 rule 8 relaxed. Costs stated (read-only bundled root forces F-7; a copied plugin silently stops receiving updates). Bonus: collapses `first-party-plugins.md` §2.4's three-step model resolution to two and deletes its `importlib.resources` special case. |
| **F-4** GUI plugins must pump their own event loop | **addressed** | `PosePlot` example rewritten to stash-plus-tick with `plt.pause`: `plugin-api.md` §2.1. `ExoLive` likewise, which also moves the ESC read off the data path. Honest cost paragraph at the end of §2.1 conceding Decision #4 is *bent*. **Caveat:** the named idiom section `plugin-api.md` §3.8 was not reached, so this currently lives in example comments rather than in prose. |
| **F-5** no edge list ships | **addressed** | `TOPOLOGY_EDGES` in `climbcv.contracts`: `payloads.md` §4, with the "pure data, part of what a topology id already claims" argument and a minor-bump change rule. |
| **F-6** the three `frame` subscribers are not alike | **declined for v1, recorded** | `broker.md` §5.3 now carries the 0.6 → 6.9 MB/s figure and states the table's misleading reading. Subscription-level decimation (`every_nth`) declined as an optimisation with no correctness content; listed in §8 as the first thing to add if the ceiling bites. |
| **F-7** no per-plugin writable directory | **addressed** | `self.data_dir`, framework-created at `<state_dir>/<plugin_id>/`: `plugin-api.md` §2, `broker.md` §6 (`PluginPlan.data_dir`), `isolation.md` §3.1 step 8, §8.1 (`state_dir` knob). Promoted from nice-to-have to required by F-3's read-only bundled root. Used in the `LidSensor` example. **Caveat:** `plugin-api.md` §3.9 not reached. |
| **F-8** no machine-readable list of a plugin's option names | **addressed via C-6** | F-8 is referenced in `first-party-plugins.md` §2.5 but **has no row in that document's own §10 register** — a numbering gap worth noting. Its subject is identical to C-6, which is accepted: `loader.md` §3.1.2. |
| **F-9** `HoldBoxes` raising on out-of-range boxes | **addressed** | Clip `[0,1]` silently, still raise on `x1 > x2`: `payloads.md` §3.3, with the normal-artifact vs genuine-bug split and a "loosening is a minor bump" row in §5. |
| **F-10** WARNING on the recommended no-handler subscription | **addressed** | `mode = "latest"`: `loader.md` §3.1.1, §7. Alternatives (DEBUG, AST inference) rejected with reasons. |
| **F-11** `frame.pixels` mutability; T2 would corrupt | **addressed** | Same mechanism as B3. The T1/T2 consequence is resolved separately in `broker.md` §5.3.1 — see the dedicated row below. |
| **F-12** is `image` filtered on `pose.smoothed`? | **addressed — answered** | `payloads.md` §3.2: **both arrays are filtered**, with three reasons. The visibility hold-last logic is named as *publisher* behaviour the contract does not fix, with `core.smooth_oneeuro`'s own documented as `visibility_threshold`. |
| **F-13** no first-class "gracefully unavailable" | **addressed** | Same as S17 — `self.unavailable(reason)`. |
| **F-14** unconditional conflation makes a correct recorder impossible | **addressed, jointly with S8** | `[[subscribes]] conflate = false` + `depth`: `broker.md` §5.1.0, `loader.md` §3.1.1, `broker.md` §6. Drops on a non-conflating subscription log at WARNING rather than counting silently. Honest bound stated (still lossy under overload). `isolation.md` §8.2 applies it to `core.persist_npy` and adds a `gaps` array to the sidecar so loss is recorded rather than hidden. |
| **F-15** `@every` fixed-rate vs fixed-delay | **addressed** | `isolation.md` §3.3.1 — fixed delay, no catch-up, as requested. |
| **F-16** no `setup()` timeout; a hung `setup()` is invisible | **declined as a timeout, addressed as a diagnostic** | `isolation.md` §3.1 — any threshold large enough for an honest model load or Swift compile is too large to detect a hang, and any useful threshold kills honest plugins on a cold cache. Instead a progress INFO every 10 s naming the plugin and its log, consistent with §5.4's diagnose-don't-intervene. |

## The two cross-cutting resolutions the task asked for explicitly

| Item | Outcome | Where |
|---|---|---|
| **T1 vs T2 × F-11** | **resolved: T1, on corrected reasoning** | `broker.md` §5.3.1. The original argument (and the guardian's concurrence) that T2 is a free non-breaking upgrade because `transport` is invisible to authors is **wrong at the authoring level**, exactly as F-11 says. B3 is confirmed as the mechanism that decouples them *on the write axis* — with read-only arrays in v1 no mutating plugin can ever ship, so there is no working population for T2 to corrupt. **B3 is not sufficient on its own:** a second axis, *lifetime*, remains — a shm view's contents change under a retained reference, and retention is what `latest()` does and what F-4's blessed stash idiom recommends. Resolved by stating the strong guarantee now (*arrays are immutable and stay valid for the lifetime of the payload*) and making T2's copy-out its cost, not the author's. Measured to justify it: memcpy 5.6 µs vs pickle 10.5 µs at 320×240, so T2's win was never the pickle and copy-out T2 keeps nearly all of it. Recorded as a **precondition**: T2 must not ship without both. |
| **F-14 + S8 together** | **resolved jointly** | `isolation.md` §8.2 is the single place both are worked, plus §7.1 and `broker.md` §5.1.0. Explicitly framed as Decision #7's dogfooding detecting a first-party plugin needing something the public API could not express — and the test that matters is that both halves are now expressible **by third parties**. |

## Config friction (C-1..C-8)

| # | Outcome | Where |
|---|---|---|
| **C-1** `check_against_plugins()` second entry point | **addressed** | `config-contract.md` §1, §1.1, §6. Signature declared with the C-6 argument; runs after discovery and **before** resolution so warnings appear above the fatals they explain; returns warnings only, never raises. The structural point is conceded explicitly — §3.3 asked for a warning that cannot exist at the point §6 puts it, which is a defect in the contract rather than a limitation of the parser. Load-order diagram updated in §6. |
| **C-2** `config_dir` on `LoadedConfig` and the `Plugin` base | **addressed** | `config-contract.md` §1 (on `LoadedConfig`, `str \| None`) and §3.5 (semantics, the path-resolution asymmetry it fixes, the recommended idiom, and why the framework deliberately does *not* resolve plugin paths itself — that would need the typed schema Decision #8 excludes). Also `plugin-api.md` §2 and `broker.md` §6. **Caveat:** `plugin-api.md` §3.11 was to restate it author-side and was not reached; §3.5 of the contract carries the substance. |
| **C-3** export `FRAMEWORK_DEFAULTS` | **addressed** | `config-contract.md` §1.2 — exported from `climbcv/framework_defaults.py`, which imports nothing. Shape upgraded from a bare name→value mapping to name→`Knob(default, allowed, is_path)` because C-4 needs `allowed` and §3.1's path rule needs `is_path`; the *type* still comes from `default`'s type as requested. Framework still applies the defaults. Full key list in §2 and `isolation.md` §8.1. |
| **C-4** `log_level`'s closed value set | **addressed** | `config-contract.md` §1.2 via `Knob.allowed`, with the message text. Deliberately a **warning with a stated fallback to INFO**, not a fatal — an unreadable log level should not stop a run. Also `isolation.md` §6.1, §8.1. |
| **C-5** TOML has no null | **addressed** | `config-contract.md` §4 — two new table rows plus the convention stated once (omit the key, rely on `self.config.get(key, default)`), tied to the `{}`-never-`None` rule that makes it work. Adds the corollary `plugins-and-config` did not state: a plugin author must **not** treat `""` or `0` as "unset", or a user who wants zero cannot say so. Prominent placement in the guide remains `docs-and-testing`'s. |
| **C-6** informational `[config] keys` | **addressed** | Accepted per the 2026-08-07 ruling: `loader.md` §3.1.2 and the §3 schema; consumer side in `config-contract.md` §3.3 and §1.1's `plugin_config_keys` argument. Names only, no types, never rejects, ignoring it legal; consumed by the config-warning pass and `climbcv init` only. The "a mechanism that can only notice a name nobody claimed is not a schema" argument is stated in both places so it stays inside Decision #8. |
| **C-7** `passthrough` on `core.smooth_oneeuro` | **addressed** | `isolation.md` §8.3 and `config-contract.md` §3.4, with the general rule (*disabling a plugin removes its topics; neutralising a stage needs the stage's own option, because only the stage knows what "do nothing" means*) and why the framework cannot generalise it. Nothing changes for the parser — it is an opaque plugin-section value. Plus a materially better error: `broker.md` §4.3's second starved-subscription variant detects that the absent topic's publisher was disabled *by config* and prints both the rule and the stanza. |
| **C-8** missing-quotes detection is undecidable under `[plugins]` | **addressed** | `config-contract.md` §3.2 (requirement reworded to "where decidable") and §3.3's C-8 note, which restates it as two obligations — a parse-time `core.`-scoped heuristic plus the authoritative post-discovery check — and endorses `config.md` §7.1 with a stronger justification than that document claimed: `core.` is a reserved *prefix* in the id grammar (`loader.md` §3.1), so `[plugins.core.<x>]` cannot be a legitimate plugin-with-a-sub-table. Recorded explicitly so nobody later "fixes" the heuristic into something that misfires on a legitimate sub-table. |

## Also changed, not from any finding

Two things the revision found on its own and fixed, recorded so they are not mistaken for review items:

- **A false stall warning for every no-timer plugin.** The loop's blocking read was `timeout = next timer
  due, None if no timers`, while the heartbeat is sent *from the loop* — so a plugin with no timers whose
  input paused would stop heartbeating and be reported stalled while healthy. `pose_plot` as originally
  sketched is exactly that plugin. Read now capped at the 1 s heartbeat interval: `broker.md` §5.1,
  `isolation.md` §3.3.
- **`payloads.md` §2.4, the self-timestamping invariant.** Introduced to answer B1's stale-source
  problem without a `Meta`-exposing accessor, and it now binds every future contract type. It is also why
  `Record` carries a mandatory `t_ns`.
