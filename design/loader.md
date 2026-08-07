# Design: Plugin Loader & Manifest

Owner: `framework-core` · Status: **proposed, awaiting review** · Implements Decision #3 (drop-in
`plugins/`), refines Assumption §3 (manifest fields)

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
- Discovery order is `sorted()` by directory name, so logs and `climbcv topics` output are stable
  run to run. Load order carries **no** semantics — no precedence, no ordering guarantees between
  plugins. Anyone relying on it is relying on a bug.
- `plugins_dir` is overridable via config (`config-contract.md`); absent directory → INFO line, zero
  plugins, app runs with built-ins only. Not an error.

**Archives (`.zip`, `.ccvplugin`) are out of scope for v1.** Folders only. Fabric ships jars, and an
archive format is the natural next step, but it adds extraction, caching, and staleness questions to
a v1 whose whole premise is "drop in a folder."

### 2.1 Manifest filename

`climbcv-plugin.toml`. Rejected: `plugin.toml` (collides if a plugin is also a pip-installable
package with its own tooling config), `manifest.toml` (too generic to grep for across a machine),
`climbcv.toml` (fatally confusable with the app config file — the wrong one of those two is the kind
of mistake that costs an hour). Distinctive and searchable; precedent is Fabric's `fabric.mod.json`.

---

## 3. Manifest schema (v1)

```toml
[plugin]
id             = "yolo_holds"                # required
version        = "1.2.0"                     # required
api_version    = "1.0"                       # required -- see §4
entry          = "plugin:YoloHolds"          # required -- "<module>:<ClassName>"
name           = "YOLO Hold Detection"       # required
description    = "Detects climbing holds with a YOLOv8 model."   # required
author         = "Aaron Nguyen"              # required
license        = "Apache-2.0"                # optional
platforms      = ["darwin", "linux", "win32"]  # optional; default = all
provides_topology = "mediapipe.pose.33"      # optional; pose publishers only
requires_topology = ["mediapipe.pose.33"]    # optional; pose subscribers that index joints
requires       = ["ultralytics>=8.0"]        # optional, INFORMATIONAL ONLY -- see §6

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
doc         = "Estimated grip force per hand, newtons."   # required
```

### 3.1 Field rules

| Field | Rule |
|---|---|
| `id` | `[a-z][a-z0-9_]{1,63}`. Must be globally unique across enabled plugins. Duplicate → **fatal**, naming both directory paths. Need not match the directory name (renaming a folder should not break a config), but a mismatch gets one INFO line so it is visible. `core.*` is reserved for built-in stages. |
| `version` | Free-form string, **informational in v1.** Nothing resolves on it — Assumption §3 rules out a dependency resolver, so parsing it into a comparable form would be machinery with no consumer. Shown in errors and `climbcv topics`. Recommend semver in the guide. |
| `api_version` | `"MAJOR.MINOR"`. See §4. |
| `entry` | `"<module>:<ClassName>"`. Module is resolved relative to the plugin directory. Dotted submodules allowed (`src.detector:Thing`). Must be a `Plugin` subclass — checked in the child. |
| `name`, `description`, `author` | Required non-empty strings. Required, not optional, because Assumption §3 is right that they are cheap and useful even when just browsing a local folder — and because in a public ecosystem an unattributed plugin is a support problem. |
| `platforms` | Subset of `{"darwin", "linux", "win32"}` (`sys.platform` values). Absent → all. Mismatch → **skipped with an INFO line, not an error.** |
| `provides_topology` / `requires_topology` | See `payloads.md` §4. Unknown id → fatal, listing known ids. |
| `[[publishes]]` / `[[subscribes]]` | Zero or more each. A plugin with neither is legal but useless — one WARNING. |

`platforms` deserves its own note: the current code special-cases the mac lid sensor
(`if self.enable_mac_lid and sys.platform != "darwin": ... disable`). Making platform gating
declarative removes a first-party special case from the framework, which is precisely the kind of
thing Decision #7's dogfooding is meant to surface. "Skip, don't error" is the right semantic — a
Linux user running a config that mentions a mac-only plugin has done nothing wrong.

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

`tomllib` — **and here is a real problem.** `tomllib` entered the standard library in **Python
3.11**. The project floor is **3.10** (`pyproject.toml`: `requires-python = ">=3.10"`). TOML parsing
is unavoidable for both this manifest and `climbcv.toml`, so one of two things must happen:

- **R1 — raise the floor to Python 3.11.** Zero dependencies. 3.11 released Oct 2022 and 3.10 is
  past its own upstream bugfix window.
- **R2 — keep 3.10, add `tomli; python_version < "3.11"`.** One conditional dependency, imposed on
  every plugin author's environment (though not imported by `climbcv.contracts`/`plugin`, so it does
  not inflate the per-child import cost).

**Recommend R1.** Every framework dependency is imposed on every author in a system with no install
step, and paying a permanent dependency to support one EOL-adjacent minor version is the wrong trade.
This crosses into `plugins-and-config`'s territory (they parse `climbcv.toml`) and changes a stated
project assumption ("stays Python 3.10+"), so it is surfaced rather than decided unilaterally.

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

1. **Discovered** in `plugins/` with a manifest → candidate.
2. **Manifest invalid** (bad TOML, missing required field, bad `id` pattern, unknown `topology`,
   unparseable `api_version`) → **skipped**, ERROR line, app continues. *A malformed plugin is a
   crashing plugin caught early; Decision #5 says the app keeps running.*
3. **`api_version` incompatible** → **skipped**, ERROR line (§4).
4. **`platforms` mismatch** → **skipped**, INFO line.
5. **`config["plugins"][id]["enabled"] == false`** → **skipped**, INFO line.
6. **Default: enabled.** A plugin present in `plugins/` with no config entry runs. Fabric semantics,
   and it matches the "drag and drop like a mod" mental model — a folder you dropped in that does
   nothing until you also edit a config file would be a bad surprise. Consequence accepted: dropping
   in a second pose plugin produces a startup error rather than being ignored. That error is the
   feature (`broker.md` §4.3).
7. **Duplicate `id` among plugins that survived 2–6** → **fatal**, listing both paths. Not
   skip-one-arbitrarily: which one survives would depend on scan order, and "it worked on my machine"
   is the outcome.
8. Enabled set → the resolver (`broker.md` §4), which may still produce a fatal for exclusive-topic
   contention or a starved required subscription.

**Explicit `enabled = true` on a plugin auto-skipped by 2/3/4** → still skipped, but escalated to
WARNING: the user asked for this specifically, so silence would be wrong, while a crash would be an
overreaction to a config file that is merely optimistic (a shared config across machines legitimately
names a mac-only plugin).

Built-in stages participate in the same resolution: they are candidates from a built-in provider list
with ids `core.capture`, `core.pose_mediapipe`, `core.smooth_oneeuro`, `core.persist_npy`, they carry
synthetic manifests, and they are disabled the same way (`[plugins."core.pose_mediapipe"] enabled =
false`). No privilege, no separate code path — which is what makes a third-party capture plugin a
first-class citizen rather than a bolt-on.

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

Install them into the environment running climb-cv, or ask the author to vendor them in
plugins/fancy_holds/vendor/.

Full traceback: logs/fancy_holds.log
```

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
   - `@subscribe("x")` with no `[[subscribes]] topic = "x"` → **fatal for that plugin.** Its handler
     would never fire and the author would be debugging silence. Message includes the exact TOML to
     add.
   - `[[subscribes]] topic = "x"` with no handler → **WARNING.** Legal (a plugin may subscribe purely
     to populate `self.latest("x")`) but usually a mistake.
   - `self.publish("y", ...)` with no `[[publishes]] topic = "y"` → **raises `UndeclaredTopicError`**
     at the call, because the plugin has no queues for `y` and the publish would be a silent
     data-loss no-op. Message includes the TOML to add.
3. **`climbcv validate ./plugins/my_plugin`** — a dev-time command that imports the plugin **in a
   throwaway subprocess** and diffs manifest against code, so an author finds the divergence before
   shipping rather than a user finding it at startup. This is the intended answer to "isn't the
   duplication annoying": the duplication is load-bearing (it is what keeps the host safe), so pay
   for a tool that keeps the two honest.

---

## 8. Handoffs and open items

**Ready for `plugin-api-guardian`:** the whole of §3 (manifest schema is an author-facing surface,
field by field), §4 (version rule and both error messages), §5 (enable/disable outcomes and their log
levels), §6 (the dependency error message), §7 (divergence rules).

**To `plugins-and-config`:**
- Config keys the loader consumes are specified in `config-contract.md`; `enabled` is the only key the
  framework reads inside `[plugins.<id>]`.
- Built-in stages have plugin ids `core.*` and are configured through the same `[plugins."core.x"]`
  mechanism, so `climbcv.toml` must tolerate dotted, quoted keys there.
- The four first-party conversions each need a `climbcv-plugin.toml` written against §3; the mac lid
  sensor should use `platforms = ["darwin"]` rather than a runtime check.

**To `docs-and-testing`:** §2 and §5 are pure functions over a directory listing plus a config dict.
Testing them means building fixture directories, not spawning processes. §6 needs prominent treatment
in the authoring guide.

**Open:**
- **R1 vs R2 in §3.3 (Python 3.11 floor vs a `tomli` dependency) needs a decision, and it changes a
  stated project assumption.** Recommend R1.
- Archive plugin format — out of scope, recorded.
- Dependency installation (§6) — the largest acknowledged v1 gap in the loader.
