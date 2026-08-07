# Design: Plugin Loader & Manifest

Owner: `framework-core` · Status: **revised 2026-08-07 (revision 01)** · Implements Decision #3
(drop-in `plugins/`), refines Assumption §3 (manifest fields)

Revision 01 actions guardian S14, S15, S16, S20, S23, the `platforms`-enumeration and template-id
notes; F-3, F-10; and C-6. Changelog: [`revision-01.md`](revision-01.md).

---

## 1. The constraint that shapes everything here

**The host process must never import plugin code.**

A third-party module with a broken top-level import, a `sys.exit()` at module scope, or a segfaulting
native extension would take the whole app down at load time — before any of the fault-tolerance
machinery in `isolation.md` exists to contain it. That directly violates "a crashing plugin is
contained and logged; the app keeps running," and it fails at the worst possible moment, when the
user has no output to diagnose from.

Therefore: **the manifest is data, and it is the single source of truth for the topic graph.**
Discovery, validation, enable/disable resolution, and wiring all run against parsed TOML. Plugin code
is imported exactly once, inside its own child process, after that process exists and its log file is
already capturing output.

This is the reason the manifest is bigger than Assumption §3's sketch — it must carry
`publishes`/`subscribes`, because the resolver needs the graph before any code runs. See §7 for the
duplication this creates with the `@subscribe` decorators and how it is handled.

---

## 2. Layout and discovery

```
<app root>/
  climbcv.toml                     # optional; owned by plugins-and-config
  plugins/
    yolo_holds/
      climbcv-plugin.toml          # required
      plugin.py                    # entry module
      hold_detection.pt            # whatever else the author wants
      vendor/                      # optional; appended to sys.path in the child
    exo_live/
      climbcv-plugin.toml
      plugin.py
    notes.txt                      # ignored, not a directory
    .DS_Store                      # ignored
```

Scan rules — deliberately boring, because surprises here are unfixable by the user:

- One level deep only. `plugins/*/climbcv-plugin.toml`. No recursion: nested plugin trees make
  "which folder is this plugin" ambiguous, and the flat rule is trivially explainable.
- A directory whose name starts with `.` or `_` is skipped silently (`_disabled_thing/` is a
  recognised way people park a plugin).
- A directory **without** a manifest gets one INFO line and is skipped. Not a warning: `__pycache__`
  and editor scratch dirs are normal.
- Files at the top level of `plugins/` are ignored silently.
- Discovery order is `sorted()` by directory name **within a root**, so logs and `climbcv topics`
  output are stable run to run. Load order carries **no** semantics — no precedence, no ordering
  guarantees between plugins. Anyone relying on it is relying on a bug. (Root *priority* is a separate
  thing and is defined in §2.1.)
- `plugins_dir` is overridable via config (`config-contract.md`); absent directory → INFO line, zero
  user plugins. Not an error.

**Archives (`.zip`, `.ccvplugin`) are out of scope for v1.** Folders only. Fabric ships jars, and an
archive format is the natural next step, but it adds extraction, caching, and staleness questions to
a v1 whose whole premise is "drop in a folder."

### 2.1 Two roots, and the shadowing rule (F-3, guardian S20) — **rule: relaxation accepted**

`plugins-and-config` reported a release blocker: **nothing ships the first-party plugins.** `climb-cv`
is pip-installable today and `enable_exo_live` defaults to `True`, so after this refactor
`pip install climb-cv` in an empty directory gives **no overlay and no hold detection** — Decision #7
turns four working features into four features that do not exist until the user locates and copies four
folders, one of which is 44 MB. The requested fix was a bundled plugin root scanned alongside
`plugins/`, which requires relaxing §5 rule 7 (duplicate id = fatal) to "fatal within a root, shadow
across roots with an INFO line".

**Ruling: accepted, and it absorbs guardian S20 at the same time.** S20 objects that built-ins carry
"synthetic manifests" constructed in code, so the four `core.*` stages are not readable
`climbcv-plugin.toml` reference examples and **the manifest parsing path is not exercised by the
first-party conversions at all** — meaning manifest bugs get found by third parties rather than by
dogfooding. Both findings want the same thing, and one mechanism serves both.

```
<installed climbcv package>/
  plugins/                       # the BUNDLED root -- ordinary directories, real manifests
    core.capture/       climbcv-plugin.toml  plugin.py
    core.pose_mediapipe/
    core.smooth_oneeuro/
    core.persist_npy/
    yolo_holds/         climbcv-plugin.toml  plugin.py  hold_detection.pt
    pose_plot/
    exo_live/
    mac_lid/

<app root>/
  climbcv.toml
  plugins/                       # the USER root -- from [framework] plugins_dir
    my_detector/
    exo_live/                    # shadows the bundled exo_live entirely
```

**Rules:**

1. **Two roots, in priority order: user, then bundled.** `[framework] use_bundled_plugins = false`
   drops the bundled root entirely, for anyone who wants to run against only their own plugins.
   `plugins_dir` moves the user root only.
2. **Duplicate id *within* one root → fatal**, listing both directory paths. Unchanged, and for the
   original reason: which one survives would depend on scan order and "it worked on my machine" is the
   outcome. Two authors who both shipped `templates/detector/` without renaming produce this.
3. **Duplicate id *across* roots → the user root wins, one INFO line naming both paths.** This is the
   relaxation, and it is what makes "drop in your own `exo_live/` and it replaces ours" work.
4. **Shadowing is by id, all-or-nothing.** The user's directory replaces the bundled one entirely: no
   manifest merging, no asset fallback, no partial override. A half-shadowed plugin would be a
   configuration nobody could reason about.
5. **Every plugin's origin (`user` / `bundled`) is on its `PluginPlan`** and printed by
   `climbcv topics -v`, so "which `exo_live` am I running" is answerable.
6. **Built-ins are ordinary directories in the bundled root**, with real `climbcv-plugin.toml` files
   parsed by exactly this code path. There is no built-in provider list and no synthetic manifest
   anywhere. `core.*` remains a reserved *id prefix* (§3.1), which is the only privilege they keep.

**What this fixes beyond shipping the plugins:**

- The manifest parser is now exercised by eight first-party manifests on every single startup. A
  manifest bug is found by us, before release, rather than by a third party after it.
- The four built-ins become the reference examples for the four hardest plugin shapes — an exclusive
  source (`core.capture`), an exclusive transform (`core.pose_mediapipe`), a passthrough-capable
  transform (`core.smooth_oneeuro`), and a non-conflating recorder (`core.persist_npy`).
- It removes the last asymmetry Decision #14 created. Guardian's review of #14 notes it left two of
  its own: the loader (this) and the embedding API (`plugin-api.md` §7). Both are now closed.

**What it costs, honestly:**

- **The bundled root is inside an installed package and is usually read-only.** So the "write a
  compiled binary next to my source" pattern `mac_lid` uses stops working for bundled plugins, which
  is what makes F-7's `self.data_dir` a requirement rather than a nicety. It is added
  (`plugin-api.md` §3.9).
- A user who copies a bundled plugin into `plugins/` to edit it gets a **silently shadowing** copy that
  will not be updated by `pip install -U`. The INFO line is the only defence, and it is the right
  amount: this is exactly what they asked for.
- `climbcv init` and `climbcv topics` must both distinguish the roots, or their output is misleading.

**One simplification for `plugins-and-config`:** with the bundled root, `first-party-plugins.md`
§2.4's three-step model resolution collapses to two. `hold_detection.pt` lives in
`<bundled>/yolo_holds/`, reachable by the same `Path(__file__).parent` any third-party plugin uses, so
the `importlib.resources.files("climbcv")` step — the one that was "a first-party plugin reaching into
the host package for an asset" with "no third-party equivalent" — can be deleted. The example gets
*more* honest, not less.

### 2.2 Manifest filename

`climbcv-plugin.toml`. Rejected: `plugin.toml` (collides if a plugin is also a pip-installable
package with its own tooling config), `manifest.toml` (too generic to grep for across a machine),
`climbcv.toml` (fatally confusable with the app config file — the wrong one of those two is the kind
of mistake that costs an hour). Distinctive and searchable; precedent is Fabric's `fabric.mod.json`.

---

## 3. Manifest schema (v1)

```toml
[plugin]
id             = "yolo_holds"                # required
version        = "1.2.0"                     # required -- semver, validated (§3.1)
api_version    = "1.0"                       # required -- see §4
entry          = "plugin:YoloHolds"          # required -- "<module>:<ClassName>"
name           = "YOLO Hold Detection"       # required
description    = "Detects climbing holds with a YOLOv8 model."   # required
author         = "Aaron Nguyen"              # required
license        = "Apache-2.0"                # optional
platforms      = ["darwin", "linux-x86_64"]  # optional; default = all -- see §3.1
provides_topology = "mediapipe.pose.33"      # REQUIRED if you publish a pose topic
requires_topology = ["mediapipe.pose.33"]    # REQUIRED if you subscribe to one; or "any"
requires       = ["ultralytics>=8.0"]        # optional, INFORMATIONAL ONLY -- see §6
teardown_timeout_s = 5.0                     # optional; default 1.0, cap 30.0 -- guardian S8
heartbeat_warn_s   = 15.0                    # optional; default 5.0 -- guardian S18/note

[[publishes]]
topic = "holds.boxes"

[[subscribes]]
topic    = "frame"
required = true                              # default true

# Declaring a topic the standard set does not define requires describing it:
[[publishes]]
topic       = "acme.grip_force"
kind        = "stream"                       # required for non-standard topics
exclusivity = "shared"                       # required for non-standard topics
payload     = "scalar"                       # required; names a climbcv.contracts type
unit        = "newton"                       # required when payload = "scalar" -- guardian S13
doc         = "Estimated grip force per hand."            # required

# Optional, informational, never rejects anything (C-6, ACCEPTED):
[config]
keys = ["every_n_frames", "input_width", "imgsz", "min_score", "model_path"]
```

### 3.1 Field rules

| Field | Rule |
|---|---|
| `id` | `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*`, ≤ 64 chars — **the same grammar as topic names** (`broker.md` §2.1). Dots are permitted. `core.` and `climbcv.` are **reserved prefixes** and `<host>` is reserved; using either is a manifest error. Must be globally unique across enabled plugins — duplicate within a root → **fatal**, across roots → shadowing (§2.1). Need not match the directory name (renaming a folder should not break a config), but a mismatch gets one INFO line so it is visible. |
| `version` | **Semver, validated:** `MAJOR.MINOR.PATCH[-prerelease][+build]`. Still informational — nothing resolves on it — but see the note below. |
| `api_version` | `"MAJOR.MINOR"`. See §4. |
| `entry` | `"<module>:<ClassName>"`. Module is resolved relative to the plugin directory. Dotted submodules allowed (`src.detector:Thing`). **The module file's existence is checked at discovery** (see below). Must be a `Plugin` subclass — checked in the child. |
| `name`, `description`, `author` | Required non-empty strings. Required, not optional, because Assumption §3 is right that they are cheap and useful even when just browsing a local folder — and because in a public ecosystem an unattributed plugin is a support problem. |
| `platforms` | List of `"<sys.platform>"` or `"<sys.platform>-<machine>"` tags. **Any string is accepted**; unrecognised ones warn. Absent → all. Mismatch → **skipped with an INFO line, not an error.** |
| `provides_topology` / `requires_topology` | See `payloads.md` §4–§4.0. **Mandatory** for any plugin publishing/subscribing a topic whose payload is `PoseFrame`; `requires_topology = "any"` is the explicit opt-out. Unknown id → fatal, listing known ids. |
| `teardown_timeout_s` | Optional float, default `1.0`, hard cap `30.0`. Guardian S8 — see `isolation.md` §7.1. Above 1.0 gets one INFO line at startup, so a long shutdown pause is explained rather than looking like a hang. |
| `heartbeat_warn_s` | Optional float, default `5.0`, hard cap `120.0`. For a plugin whose handler legitimately blocks for a long time (an IP camera read). Guardian S18 and the stall-warning note. |
| `[[publishes]]` / `[[subscribes]]` | Zero or more each. A plugin with neither is legal but useless — one WARNING. Sub-keys in §3.1.1. |
| `[config] keys` | Optional list of strings. **Purely informational** — see §3.1.2. |

`platforms` deserves its own note: the current code special-cases the mac lid sensor
(`if self.enable_mac_lid and sys.platform != "darwin": ... disable`). Making platform gating
declarative removes a first-party special case from the framework, which is precisely the kind of
thing Decision #7's dogfooding is meant to surface. "Skip, don't error" is the right semantic — a
Linux user running a config that mentions a mac-only plugin has done nothing wrong.

**`platforms` accepts any string, and gained arch tags (guardian note + S16).** It was a subset of
`{"darwin", "linux", "win32"}` — *"a framework-owned enumeration of the kind Decision #12 rejected"*,
and on FreeBSD every plugin declaring `platforms` would be skipped while every plugin omitting it ran,
which is precisely backwards. Now: a tag matches if it equals `sys.platform`, or if it is
`f"{sys.platform}-{platform.machine()}"`. Unrecognised platform strings produce a WARNING (`unknown
platform tag 'sunos5' — this plugin will never run here`) rather than being silently ignored or
rejected. Arch tags exist because §6 *recommends vendoring* and a vendored x86_64 wheel on Apple
Silicon fails with `ImportError: incompatible architecture` — a cryptic failure produced by the
framework's own recommended mitigation, with previously no way to declare against it.

**Semver is validated now because it cannot be validated later (guardian S14).** `version` remains
informational in v1 — Assumption §3 rules out a dependency resolver, so parsing it into a comparable
form would be machinery with no consumer. But leaving the *grammar* free-form means the ecosystem fills
with `"v1.2 beta"` and `"2024-06-01"`, and then any future registry, update check, or "you have an old
version of this plugin" message cannot compare them. By this project's own change rule, tightening a
validation later is breaking. So the cheap direction is to validate the shape today and resolve on it
never: one regex, no dependency, option preserved.

**`entry`'s module file is checked at discovery (guardian S15b).** A typo in `entry` was discovered in
the child at import, costing two spawn attempts and reading as a crash rather than a typo. Module-file
existence is a pure filename check needing no import — the same trick already used for stdlib shadowing
(§6) — so `plugin:YoloHolds` requires `plugin.py` or `plugin/__init__.py`, and `src.detector:Thing`
requires `src/detector.py` or `src/detector/__init__.py`. Missing → manifest error listing the `.py`
files actually present:

```
Plugin 'yolo_holds' (plugins/yolo_holds/climbcv-plugin.toml): entry = "detector:YoloHolds"
names the module 'detector', but there is no detector.py or detector/__init__.py in
plugins/yolo_holds/.

Python files found there: plugin.py, detect.py
```

The class name is still resolved in the child — that genuinely needs an import — but the far more
common half of the mistake is now caught before a process is spawned.

### 3.1.1 `[[subscribes]]` sub-keys

| Key | Default | Meaning |
|---|---|---|
| `topic` | — | required |
| `required` | `true` | Absent topic → fatal unless false (`broker.md` §4.2). User-overridable per topic (`broker.md` §4.4c). |
| `conflate` | `true` | `false` = deliver every queued message in arrival order. F-14; `broker.md` §5.1.0. |
| `depth` | `0` | Only meaningful when `conflate = false`; `0` → 64. Capped by `max_stream_depth`. |
| `mode` | `"handler"` | `"latest"` = "I read this with `latest()`, not with a handler." Suppresses §7's no-handler WARNING. F-10. |

**`mode = "latest"` exists to stop the framework warning about a pattern it recommends (F-10).** §7
emits a WARNING for any `[[subscribes]]` with no handler — which is exactly the "subscribe purely to
populate `self.latest()`" pattern that the same clause declares legal and that `plugin-api.md` §3.5
*recommends*. As written, the flagship first-party overlay logged two warnings at every startup, which
trains users to ignore warnings. Alternatives considered: dropping the warning to DEBUG (rejected — the
warning catches a real mistake, a subscription whose handler was renamed, and that mistake is silent);
inferring intent from whether `latest()` is called (rejected — needs the AST scan of §7 for something a
one-word declaration says exactly). `mode` also makes the intent readable in the manifest, which is
where a reviewer of somebody else's plugin is looking.

A handler on a `mode = "latest"` subscription is still allowed and still fires; the only effect of
`mode` is on the warning. Declaring it and then adding a handler is not worth a second diagnostic.

### 3.1.2 `[config] keys` — informational only (C-6, accepted)

```toml
[config]
keys = ["every_n_frames", "imgsz", "model_path"]
```

**Names only.** No types, no required-ness, no defaults, no coercion, and **it never rejects a
value**. Omitting the table is legal and means "no config warnings for this plugin". The loader hands
the list to `check_against_plugins()` (`config-contract.md` §1.1), which warns on a key in
`[plugins.<id>]` that is not in the list, with a nearest match.

It exists because without it a typo in a plugin option is silent forever: `every_n_frame = 8` in
`[plugins.yolo_holds]` means the plugin reads its default of 4, publishes at twice the intended rate,
and nothing anywhere says a word — the exact failure `config-contract.md` §3.1 refuses to accept for
`[framework]` keys, accepted without comment for the section users actually edit. It also gives
`climbcv init` something useful to emit.

It stays inside Decision #8's no-validation-layer posture by construction: **a mechanism that can only
notice a name nobody claimed, and can never reject a value, is not a schema.** The framework does not
read it; only the config-warning pass and `climbcv init` do.

### 3.2 What is deliberately *not* in the manifest

- **`type`.** Assumption §3 lists `type` as a manifest field. **Decision #9 obsoletes it.** With one
  uniform authoring model, there is no plugin type to declare: what a plugin *is* — observer, new
  stage, stage replacement, sink — is fully and more precisely described by which topics it publishes
  and subscribes to. A plugin that subscribes to `frame` and publishes `holds.boxes` is a stage; one
  that subscribes to `pose.smoothed` and publishes nothing is a sink; one that publishes an exclusive
  topic something else already published is a replacement. Keeping `type` would mean either (a) it is
  advisory and can contradict the topic graph, or (b) the framework enforces it, which reintroduces a
  finite set of plugin categories — the structural ceiling we rejected. Surfaced as a proposed
  revision to Assumption §3.
- **`priority` / `order`.** No ordering semantics exist between plugins. Providing the field would
  invite dependence on behaviour the design does not offer.
- **Plugin-to-plugin dependencies.** Assumption §3 rules them out for v1. `requires` (§6) is a
  human-readable hint about Python packages only, and even that is informational.
- **`homepage` / registry metadata.** Assumption §3: not until a registry exists.

### 3.3 Parsing

**Resolved: `tomllib` from the standard library, Python 3.11 floor** — Decision #17, accepted
2026-08-07. R1 as recommended; R2 (`tomli` on 3.10) is dead. Zero TOML dependency, and none imposed on
any plugin author.

One parser for both files: `climbcv-plugin.toml` and `climbcv.toml` go through `plugins-and-config`'s
`read_toml_file()` helper (`config.md` §4.1), so a malformed file produces the same three-line error
whichever it is. That is a deliberate dependency from the loader into `climbcv/config.py` rather than
six duplicated lines that would drift. `climbcv/config.py` is host-only and imports only stdlib, so
this does not touch the "stdlib + numpy in every child" rule — the loader runs in the host.

Parse failures are per-plugin and non-fatal to the app (§5): malformed TOML → that plugin is skipped
with the `tomllib` error, the file path, and the line number.

---

## 4. API version compatibility

Framework: `climbcv.api.API_VERSION = (1, 0)`.

Plugin: `api_version = "1.0"`, meaning **"needs at least API 1.0, same major."**

```
compatible  ⟺  plugin_major == framework_major  and  plugin_minor <= framework_minor
```

Alternatives rejected:

- **Packaging-style ranges** (`">=1.2,<2.0"`, `"^1.2"`). Every syntax accepted is a comparator I must
  hand-write (no dependency allowed), document, and defend against creative input. The expressiveness
  buys nothing the one-rule form doesn't: "same major, at least this minor" *is* the compatibility
  model — additive minors, breaking majors.
- **Major only** (`api_version = "1"`). Loses the ability to say "I need the feature added in 1.3,"
  so a plugin using a 1.3 feature on a 1.0 framework fails with an `AttributeError` deep inside a
  child instead of a clear startup message.
- **A full resolver.** Explicitly out of scope per Assumption §3.

Failure is **per-plugin and non-fatal**, with the two directions distinguished because the fix
differs:

```
Skipping plugin 'fancy_holds' (plugins/fancy_holds/): it needs climb-cv plugin API 1.4
or newer, and this build provides 1.0.

Update climb-cv, or use a version of fancy_holds built for API 1.0.
```

```
Skipping plugin 'ancient' (plugins/ancient/): it was built for climb-cv plugin API 0.4,
which is not compatible with this build's API 1.0.

API 1.x changed the plugin base class and payload types. 'ancient' needs updating by its
author; there is no configuration that makes it work.
```

The second message's last sentence exists because the alternative is a user spending an hour looking
for the config flag.

---

## 5. Enable/disable resolution

Ordered, and every outcome is one of exactly three: **enabled**, **skipped** (with a reason), or
**fatal**.

1. **Discovered** in a root with a manifest → candidate. Both roots are scanned (§2.1).
2. **Shadowed** by a same-id plugin in a higher-priority root → **skipped**, INFO line naming both
   paths (§2.1 rule 3). Runs before validation, so a broken bundled plugin cannot make a working user
   plugin's shadow noisy.
3. **Manifest invalid** (bad TOML, missing required field, bad `id` pattern, non-semver `version`,
   reserved id prefix, missing entry module, unknown `topology`, missing mandatory
   `requires_topology`/`provides_topology`, unparseable `api_version`) → **skipped**, ERROR line, app
   continues. *A malformed plugin is a crashing plugin caught early; Decision #5 says the app keeps
   running.*
4. **`api_version` incompatible** → **skipped**, ERROR line (§4).
5. **`platforms` mismatch** → **skipped**, INFO line.
6. **`config["plugins"][id]["enabled"] == false`** → **skipped**, INFO line.
7. **Default: enabled.** A plugin present in either root with no config entry runs. Fabric semantics,
   and it matches the "drag and drop like a mod" mental model — a folder you dropped in that does
   nothing until you also edit a config file would be a bad surprise. Consequence accepted: dropping
   in a second pose plugin produces a startup error rather than being ignored. That error is the
   feature (`broker.md` §4.3).
8. **Duplicate `id` within one root, among plugins that survived 3–7** → **fatal**, listing both
   paths. Not skip-one-arbitrarily: which one survives would depend on scan order, and "it worked on my
   machine" is the outcome. **Across roots this is not an error** — see §2.1, F-3.
9. Enabled set → the resolver (`broker.md` §4), which may still produce a fatal for exclusive-topic
   contention, a starved required subscription, or a topology mismatch.

**Explicit `enabled = true` on a plugin auto-skipped by 3/4/5** → still skipped, but escalated to
WARNING: the user asked for this specifically, so silence would be wrong, while a crash would be an
overreaction to a config file that is merely optimistic (a shared config across machines legitimately
names a mac-only plugin).

Built-in stages participate in **exactly** this resolution, as ordinary directories in the bundled root
with ordinary manifests, and are disabled the same way (`[plugins."core.pose_mediapipe"] enabled =
false`). No privilege, no separate code path, **and no synthetic manifest** — which is what makes a
third-party capture plugin a first-class citizen rather than a bolt-on, and what makes the manifest
parser dogfooded rather than only third-party-tested (§2.1, guardian S20).

---

## 6. Dependencies without an install step — a known v1 weakness

The `requires = [...]` field is **informational only**. Nothing is installed, nothing is checked
before import.

Import resolution in the child, in order: (1) the plugin's own directory, at `sys.path[0]`; (2) the
plugin's `vendor/` directory if present, appended; (3) the application's environment.

So a plugin needing `ultralytics` either finds it in the app environment (true for the first-party
YOLO plugin, since it is already a `climb-cv` dependency) or vendors it, or fails. Failure is a clean
setup-phase crash (`isolation.md` §4) with an attributable message:

```
Plugin 'fancy_holds' failed to start: no module named 'torch'.

fancy_holds/climbcv-plugin.toml lists these requirements, which climb-cv does not install:
  torch>=2.0
  timm

climb-cv is running on this interpreter:
  /Users/you/.venvs/climb/bin/python

Install into THAT interpreter -- copy and paste:
  /Users/you/.venvs/climb/bin/python -m pip install 'torch>=2.0' 'timm'

Or ask the author to vendor them in plugins/fancy_holds/vendor/.

Full traceback: logs/fancy_holds.log
```

Three details in that message that guardian S16 asked for and that are not cosmetic:

- **`sys.executable`, printed literally.** "Install them into the environment running climb-cv" *is* the
  entire difficulty, for a user who plausibly has a system Python, a Homebrew Python, and two venvs. A
  path plus a ready command removes the only genuinely hard step.
- **The `requires` block is omitted entirely when the list is empty**, rather than printing
  `lists these requirements: (none)`, which reads as a framework bug.
- **`requires` is unvalidated**, so it is quoted as-is and only echoed. It is a hint from the author to
  the user, not a specification.

**Vendoring's real constraint, stated (guardian S16).** `vendor/` is
**Python-version-, ABI- and architecture-specific**, and the framework checks none of that. A plugin
vendoring an x86_64 wheel runs on Apple Silicon and fails with `ImportError: incompatible
architecture` — a cryptic failure produced by the framework's own recommended mitigation. Two things
follow: `platforms` now carries optional arch tags (§3.1) so an author can at least declare it, and the
authoring guide must say plainly that vendoring a compiled wheel makes a plugin
platform-and-interpreter-specific and that pure-Python vendoring is the only portable kind.

**This is the sharpest edge of "no pip install," and it should be recorded as an open risk rather
than smoothed over.** The failure is contained, attributable, and clearly explained — but for a
public ecosystem, "plugin needs a package you don't have" will be the single most common install
problem, and the framework's answer in v1 is a good error message and nothing more. Options for v1.x,
none designed here: bundled wheels in `vendor/`, an opt-in `--install-plugin-deps` that shells out to
pip, or per-plugin virtual environments (which the process-per-plugin architecture actually makes
tractable — each child could run a different interpreter). Flagged for `docs-and-testing` as something
the authoring guide must address prominently, and for `plugins-and-config` as a constraint on the
first-party YOLO conversion.

`sys.path[0]` also creates a shadowing hazard: a plugin file named `queue.py` or `types.py` shadows
the stdlib **for its own process only** (plugins cannot shadow each other, since each child has only
its own directory on the path). Cheap mitigation: the loader compares the plugin directory's top-level
`.py` filenames against `sys.stdlib_module_names` during discovery — a pure filename check, no import
— and emits a WARNING naming the file. Catches a genuinely baffling class of bug for near-zero cost.

---

## 7. Manifest / code divergence

The manifest declares the topic graph; the `@subscribe` / `@every` decorators in the code declare
handler bindings (`plugin-api.md`). These can disagree.

Handled in three layers:

1. **The manifest wins for wiring.** The host has already allocated queues from it before any code
   exists. Non-negotiable, per §1.
2. **The child cross-checks after import** and reports divergence attributably, in the child, where
   it is contained:
   - `@subscribe("x")` with no `[[subscribes]] topic = "x"` → **`PluginContractError`, non-retryable**
     (`isolation.md` §4.5). Its handler would never fire and the author would be debugging silence.
     Message includes the exact TOML to add. Non-retryable because it is deterministic: retrying twice
     and printing the same message twice helps nobody.
   - `[[subscribes]] topic = "x"` with no handler → **WARNING**, unless the subscription declares
     `mode = "latest"` (§3.1.1), which is the legitimate case saying so.
   - `self.publish("y", ...)` with no `[[publishes]] topic = "y"` → **raises `UndeclaredTopicError`**
     at the call, because the plugin has no queues for `y` and the publish would be a silent
     data-loss no-op. Message includes the TOML to add.
   - `self.latest("y")` / `self.latest_by_source("y")` on an undeclared topic → **raises
     `UndeclaredTopicError`** too, symmetrically with `publish`. Previously unspecified (guardian B1);
     the asymmetry would have meant a typo'd read returned `None` forever while a typo'd write raised.
3. **`climbcv validate ./plugins/my_plugin`** — a dev-time command that imports the plugin **in a
   throwaway subprocess** and diffs manifest against code, so an author finds the divergence before
   shipping rather than a user finding it at startup. This is the intended answer to "isn't the
   duplication annoying": the duplication is load-bearing (it is what keeps the host safe), so pay
   for a tool that keeps the two honest.

**`validate` also AST-scans publish call sites (guardian S23).** Layer 2's `UndeclaredTopicError` fires
*at the call*, so it catches an undeclared publish only if that line actually executes. A **conditional**
publish — `acme.rare_event`, fired only when some rare condition holds — ships, works for months, and
then raises inside a handler, where `isolation.md` §6.2's ladder logs it and suppresses it rather than
failing loudly. So §7's claim that `validate` "keeps the two honest" was true for subscriptions and
false for publishes.

Fix: `validate` walks the plugin's own `.py` files with `ast` — **no import, no execution** — collecting
every `self.publish("<string literal>")` and diffing against `[[publishes]]`. It reports undeclared
literals as errors and declared-but-never-published topics as warnings. It cannot see a computed topic
name (`self.publish(topic_var, ...)`), and reports that it saw one so the author knows the check was
incomplete rather than clean. This is the same instinct as the stdlib-shadowing filename check in §6 and
the entry-module check in §3.1: a static, import-free check that catches nearly all of a bug class for
nearly nothing.

---

## 8. Handoffs and open items

**Ready for `plugin-api-guardian`:** the whole of §3 (manifest schema is an author-facing surface,
field by field), §2.1 (the two-root and shadowing rules), §4 (version rule and both error messages),
§5 (enable/disable outcomes and their log levels), §6 (the dependency error message and the vendoring
constraint), §7 (divergence rules and the AST scan).

**To `plugins-and-config`:**
- Config keys the loader consumes are specified in `config-contract.md`; `enabled` is the only key the
  framework reads inside `[plugins.<id>]`.
- Built-in stages have plugin ids `core.*` and are configured through the same `[plugins."core.x"]`
  mechanism, so `climbcv.toml` must tolerate dotted, quoted keys there. Your §7.1 `core.`-scoped
  missing-quotes heuristic remains exactly right, and it now has a stronger justification: `core.` is a
  reserved *prefix* in the id grammar (§3.1), so `[plugins.core.<anything>]` cannot be a legitimate
  plugin-with-a-sub-table.
- **F-3 accepted (§2.1).** The four first-party plugins live in the bundled root inside the package,
  shadowable by `plugins/`. Your §2.3 is unblocked, and §2.4's `importlib.resources` step can be
  deleted — `Path(__file__).parent` reaches the bundled model.
- **C-6 accepted (§3.1.2).** `[config] keys = [...]` is in the schema. `check_against_plugins()`
  receives, per plugin id, the declared key list (empty tuple = "no warnings for this plugin", which is
  distinguishable from an absent plugin).
- **F-10 accepted (§3.1.1).** `mode = "latest"` exists; your `exo_live` manifest is valid as written.
- **F-14 accepted (`broker.md` §5.1.0).** `conflate` and `depth` are `[[subscribes]]` keys.
- The four conversions each need a `climbcv-plugin.toml` written against §3, with three new
  obligations: `version` must be semver; `requires_topology` is mandatory for the three pose consumers
  (all three already declare it); and `platforms = ["darwin"]` for `mac_lid`, still rather than a
  runtime check.

**To `docs-and-testing`:** §2, §2.1 and §5 are pure functions over a directory listing plus a config
dict. Testing them means building fixture directories, not spawning processes. §6 needs prominent
treatment in the authoring guide. The shadowing rules in §2.1 are the highest-value new fixture set:
same id in both roots, same id twice in one root, a broken bundled plugin shadowed by a working user
one. Template plugin ids must be obviously placeholder — **`my_detector`, not `detector`** (guardian
note) — because two authors who both copy `templates/detector/` without renaming produce a
duplicate-id **fatal** for the user who installs both, under §5 rule 8.

**Open:**
- **Python floor — resolved.** Decision #17 accepted; §3.3 rewritten. R2 is dead.
- Archive plugin format — out of scope, recorded. Note §2.1's `data_dir` requirement removes one of the
  three blockers an archive format would have hit.
- Dependency installation (§6) — still the largest acknowledged v1 gap in the loader. Revision 01
  improved the *message* (interpreter path, ready command, honest vendoring caveat) and did not change
  the mechanism, because there is no mechanism to change without an install step.
