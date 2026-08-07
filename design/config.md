# Design: `climbcv.toml` — Format, Parsing, and Defaults

Owner: `plugins-and-config` · Status: **proposed, awaiting review** · Implements Decision #8 (one
unified file, sectioned by plugin id, no schema/validation layer in v1) and satisfies
`config-contract.md`

---

## 1. Answers to `config-contract.md` §8, up front

`framework-core` asked four questions. They gate its own design, so they are answered before anything
else.

**1. Python 3.10 vs 3.11 — resolved by Decision #17 (accepted).** Floor is **3.11**, `tomllib` from the
stdlib, **no TOML dependency added**. And yes, one parser for both files: `climbcv-plugin.toml` and
`climbcv.toml` go through the same `read_toml_file()` helper (§4.1) so a malformed file produces the
same three-line error whichever it is. That helper lives in my module and the loader imports it — a
dependency direction worth naming explicitly rather than duplicating six lines that will drift.

**2. Who owns the warnings — I do, with one correction.** `load_config()` returns them in
`LoadedConfig.warnings`, already formatted, and `framework-core` logs them verbatim. But **two of the
warnings the contract asks for cannot be produced there**, because §6 fixes the load order as
config → discovery and both of them need the discovered plugin ids. They move to a second entry point,
`check_against_plugins()`, called after discovery. See §7.2 and **C-1**.

**3. §5, values stay TOML primitives — confirmed, and stronger than asked.** Every value inside
`[plugins.<id>]` is handed over **exactly as `tomllib` produced it**. Zero post-processing: no `Path`
coercion, no enum lookup, no lazy accessor, no string interpolation, no environment expansion, not even
`str.strip()`. `tomllib` yields only `str`, `int`, `float`, `bool`, `list`, `dict`,
`datetime`/`date`/`time`, all of which pickle. The picklability constraint is therefore satisfied by
construction rather than by a check, which is the only way it stays true as this code is maintained.

**4. `plugins_dir` / `log_dir` relative to the config file's directory — confirmed and implemented**
(§4.3, stage 5). Returned as absolute `str`, not `Path`, so §3.1's "types must match the defaults'
types" stays literally true (the defaults are strings) *and* §5's primitives rule needs no exception.

---

## 2. Scope, and what this file deliberately is not

`climbcv.toml` is the app's single configuration file (Decision #8). It carries three things:

1. framework knobs;
2. which plugin owns a contended exclusive topic;
3. one opaque section per plugin.

It is **not** a schema, a manifest, a plugin registry, or a place plugin options are declared or
validated. Decision #8 rules out a validation layer in v1 and I am not smuggling one in: nothing in
this design inspects, types, defaults, or transforms a value inside `[plugins.<id>]`. §7.3 reports
exactly what that costs, because reporting it is my job and building around it is not.

Module: **`climbcv/config.py`**. **Host-only** — `isolation.md` §6 loads once in the host and children
receive their section as data, so this module is never imported in a child process and does not fall
under the "stdlib + numpy only" rule that binds `climbcv.contracts` and `climbcv.plugin`. (It imports
only `tomllib`, `pathlib`, `difflib` and `dataclasses` anyway.)

---

## 3. Where the file lives

### 3.1 Discovery order — two steps, and no third

1. **An explicit path** (`--config PATH`, or `ClimbCV(config=PATH)`) → use it. **If it does not exist,
   that is fatal**, because the user named a specific file and silently substituting defaults for a
   file they believe they are using is the worst outcome available.
2. **`./climbcv.toml`** — the process working directory, not walked upward. Absent → zero-config
   startup: `data == {}`, `source == "<defaults: no config file found>"`, no error, no warning
   (`config-contract.md` §4).

That is the whole search path.

### 3.2 Rejected alternatives

- **Walking up parent directories** (git/`pyproject.toml` style). Rejected: "which config did it pick"
  becomes archaeology, and in a tool whose premise is "drop a folder next to the app and run", the
  answer to *where does config live* should be "next to the app", full stop. `climbcv topics` printing
  `source` mitigates but does not fix a surprise it should not have.
- **A user-global `~/.config/climbcv/climbcv.toml`, merged.** Rejected on Decision #8's own terms:
  merging two files needs precedence rules per section and per key, which is a schema layer arriving
  through the back door. It also means a machine-wide file can silently change one project's
  behaviour, which is the failure mode this project has repeatedly refused elsewhere.
- **`$CLIMBCV_CONFIG` naming the file.** Rejected as scope creep against `config-contract.md` §7 ("no
  env-var overlay in v1"). Locating the file is arguably not overlaying values, but the distinction is
  too fine to be worth an entry point; `--config` covers the same need visibly.

### 3.3 The filename confusion guard

`loader.md` §2.1 already notes that `climbcv.toml` and `climbcv-plugin.toml` are "the kind of mistake
that costs an hour". One cheap check closes it: if the loaded app config has a top-level `[plugin]`
table (singular — never valid here), warn.

```
./climbcv.toml has a [plugin] section, which looks like a plugin manifest rather than
the climb-cv app config. A plugin's manifest belongs in its own folder and is named
climbcv-plugin.toml:

    plugins/my_plugin/climbcv-plugin.toml

Ignoring the [plugin] section.
```

Warning, not fatal: the rest of the file may be perfectly good.

---

## 4. The format and the parse

### 4.1 `read_toml_file()` — shared with the manifest loader

```python
def read_toml_file(path: Path, what: str) -> dict:
    """Parse one TOML file. Raises ConfigError with a message naming path,
    line and column. `what` is a noun for the message ("config file",
    "plugin manifest")."""
```

`tomllib.TOMLDecodeError` carries the line and column, which is the single most useful thing in a
TOML error and is easy to lose. Both call sites format it identically:

```
climb-cv cannot read the config file ./climbcv.toml:

  line 14, column 9: Expected '=' after a key in a key/value pair

Fix the file, or delete it to run with defaults.
```

Fatal, per `config-contract.md` §4's last row. `PermissionError` and `IsADirectoryError` get the same
shape with the OS message.

### 4.2 The public surface

```python
@dataclass(frozen=True)
class LoadedConfig:
    data: dict                   # exactly config-contract.md §2's shape
    source: str                  # absolute path, or "<defaults: no config file found>"
    config_dir: Path | None      # the file's directory; None when there is no file. See C-2.
    warnings: tuple[str, ...]    # formatted, user-facing; the framework logs them verbatim


def load_config(path: Path | None = None) -> LoadedConfig: ...

def check_against_plugins(
    cfg: LoadedConfig,
    plugin_ids: frozenset[str],      # every id discovered, including core.*
    topic_names: frozenset[str],     # standard set + every plugin-declared topic
) -> tuple[str, ...]:
    """Warnings that need the discovered graph. Called after discovery,
    before resolution. Pure; no I/O. See §7.2."""
```

`config_dir` is an addition to the contract's `LoadedConfig`, requested for the reason in **C-2**.
`source` is the **absolute** path rather than the contract's illustrative `"./climbcv.toml"`, because
it appears in fatal error messages and an absolute path is unambiguous and copy-pasteable. The literal
`"<defaults: no config file found>"` is kept verbatim.

### 4.3 Parse stages, in order

1. **Locate** (§3.1). Not found and not explicit → return the empty `LoadedConfig`. Done.
2. **Read and parse** via `read_toml_file` (§4.1). Any failure is fatal.
3. **Top-level check.** Known tables are `framework`, `topics`, `plugins`. Anything else → warning
   naming it and the nearest known table; the value is left in `data` untouched (it costs nothing and
   the framework ignores it). `[plugin]` gets §3.3's specific message.
4. **`[framework]`:** unknown-key warnings, type checks, `int → float` coercion (§5.1). Defaults are
   **not** filled in — `config-contract.md` §3.1 assigns that to the framework and one owner is
   better than two.
5. **Path resolution.** `plugins_dir` and `log_dir`, if present, are resolved against `config_dir`
   (§1 answer 4) and written back as absolute strings.
6. **`[topics]`:** heuristic missing-quotes detection (§7.1), shape check on each value.
7. **`[plugins]`:** heuristic missing-quotes detection. **Sections themselves are not touched.**
8. Return.

Total behaviour on `[plugins.<id>]`: stages 7 and 8. Which is the point.

### 4.4 TOML facts that shape the format, and what to tell authors

| Fact | Consequence |
|---|---|
| A dotted bare key **nests**: `[topics.pose.smoothed]` → `{"pose": {"smoothed": {}}}` | Quoting is mandatory for every dotted topic name and every `core.*` plugin id. This is the single most likely mistake in the file, so it gets both a heuristic warning at parse time and an authoritative one after discovery (§7). |
| Quoting a key containing dots makes it **one** key: `[topics."pose.smoothed"]` | Satisfies `config-contract.md` §3.2's flat-key requirement without any flattening on my side. I hand back exactly what `tomllib` produced, as instructed. |
| TOML has **no null** | A plugin cannot express "explicitly unset". The convention is **omit the key**, and `self.config.get(key, default)` is the only idiom the authoring guide should show. Recorded in **C-5** because authors will reach for `= ""` or `= 0`. |
| Arrays of tables work inside a plugin section | `[[plugins.my_plugin.zones]]` arrives as `{"zones": [{...}, {...}]}` — nested, picklable, passed through. Structured plugin config needs no framework support. This is also what makes the missing-quotes heuristic ambiguous (§7.1). |
| Datetimes become `datetime`/`date`/`time` objects | Picklable, so §5 holds. Worth knowing because they are the one TOML value type that is not a Python primitive. |
| Comments and key order are not preserved | Irrelevant: nothing writes this file (`config-contract.md` §7). `climbcv init` (§9) generates a new one; it never rewrites an existing one. |

---

## 5. Defaults: three layers, three owners

The single most important thing to keep straight, because "where does this default live" is asked once
per option forever.

| Layer | Owner | Applied where | Visible to |
|---|---|---|---|
| **Framework knobs** | `framework-core` | in the host, from `FRAMEWORK_DEFAULTS` | `climbcv topics`, error messages |
| **Topic ownership** | derived, not defaulted | the resolver, by candidate count (`broker.md` §4.2) | `climbcv topics` |
| **Plugin options** | the plugin author | in `setup()`, via `self.config.get(k, default)` | the plugin's own README only |

Layer 3 having no other home is the whole shape of Decision #8, and §7.3 states what it costs.

### 5.1 `[framework]` needs a defaults table with types, and it must have one owner

To emit `config-contract.md` §3.1's unknown-key warning *and* its type check, my parser needs the set
of valid keys and their types. Duplicating thirteen keys and thirteen types in my module guarantees
drift, and the drift fails in the worst direction: the first time `framework-core` adds a knob, my
parser warns "unknown key" about a key that works perfectly.

**Requested:** `framework-core` exports one table, e.g. `climbcv/framework_defaults.py`:

```python
FRAMEWORK_DEFAULTS: dict[str, object] = {
    "plugins_dir": "./plugins",
    "log_level": "INFO",
    "log_dir": "./logs",
    "stream_depth": 4,
    "event_depth": 256,
    "restart_max": 5,
    "restart_window_s": 60.0,
    ...
}
```

I import it for (a) the valid-key set, (b) nearest-match suggestions, (c) the expected type of each
key. `framework-core` keeps applying the defaults. One table, two consumers, no duplication, and
ownership stays exactly where `config-contract.md` §3.1 put it. See **C-3**.

Two implementation notes that are easy to get wrong and expensive to debug:

- **`bool` is a subclass of `int`.** `restart_max = true` must be a type error, so the check is
  `type(value) is int`, never `isinstance(value, int)`. Likewise `int → float` coercion must exclude
  bools explicitly.
- **Coercion is `int → float` only**, for keys whose default is a float. Never `float → int`, never
  `str → anything`. `config-contract.md` §3.1's "where the intent is unambiguous" is exactly this one
  direction and nothing else.

The warning, with the suggestion coming from `difflib.get_close_matches`:

```
./climbcv.toml [framework]: unknown key 'log_lvl'. Did you mean 'log_level'?
Ignoring it.
```

And the fatal:

```
climb-cv cannot start: ./climbcv.toml [framework] restart_max = "five"
expected an integer, got a string.
```

### 5.2 The one framework key whose value set is closed

`log_level` is typed `str`, so `log_level = "DEBGU"` passes every check in §5.1 and then fails inside
the framework's logging setup — probably as an `AttributeError` on the `logging` module, with no
mention of the config file. Either `framework-core` validates it with a message naming the file, or
`FRAMEWORK_DEFAULTS` grows an optional per-key allowed-value tuple and I check it. Either is fine;
neither is a schema layer, because it is one key with five legal values. See **C-4**.

---

## 6. `[topics]` — value shape

```toml
[topics."pose.smoothed"]
publisher = "core.smooth_oneeuro"
```

Checks I perform: the value must be a table; `publisher`, if present, must be a string; any other key
warns and is ignored (`config-contract.md` §3.2 leaves room to grow). Whether the plugin or the topic
exists is the resolver's business (§3.2 again), and the good error with the candidate list lives in
`broker.md` §4.3.

Note for the authoring guide: `[topics]` is **only** needed when two enabled plugins publish the same
exclusive topic. The single-candidate case resolves silently. So the honest description is "you will
never write this section until climb-cv prints the stanza for you to paste," which is a nice property
and should be documented as such rather than presented as routine configuration.

---

## 7. Warnings: which exist, where they fire, and where the contract asked for the impossible

### 7.1 The missing-quotes heuristic is genuinely ambiguous, and can only be a heuristic

`config-contract.md` §3.2/§3.3 asks me to detect a user who forgot the quotes and warn with the fix.
For `[topics]` that is mostly tractable. For `[plugins]` it is **not decidable at parse time**:

```toml
[plugins.core.capture]          # a mis-quoted "core.capture"?
width = 640
```
```toml
[plugins.my_plugin.zones]       # or a plugin with a legitimate sub-table?
overhang = true
```

Both parse to `{"plugins": {"<outer>": {"<inner>": {...}}}}`. Indistinguishable without knowing which
ids exist. So:

- **At parse time** (in `warnings`), a *heuristic*: under `[topics]`, warn when a value is a table
  whose own values are all tables and which has no `publisher` key. Under `[plugins]`, warn only when
  the joined name `"<outer>.<inner>"` starts with `core.`, since `core.*` is a reserved namespace
  (`loader.md` §3.1) and no legitimate plugin can be named `core` with a sub-table. That catches the
  overwhelmingly common real case — `[plugins.core.capture]` — with zero false positives.
- **After discovery** (in `check_against_plugins`), the authoritative version: warn when `<outer>` is
  not a discovered id but `"<outer>.<inner>"` is.

Message either way, from `config-contract.md` §3.2 verbatim:

```
./climbcv.toml: [plugins.core.capture] should be [plugins."core.capture"] —
dotted plugin ids need quotes. As written, this section configures a plugin
named 'core', which does not exist, and core.capture gets no configuration.
```

### 7.2 Two contract-required warnings cannot live where the contract puts them

`config-contract.md` §3.3 requires *"A section for an id not present in `plugins/` → warning naming
the closest match"*, and §3.2/§3.3 require the authoritative dotted-key warnings. All three need the
discovered plugin id set. §6 of the same document fixes the order as **config → discovery**. So
`LoadedConfig.warnings`, produced before discovery exists, is structurally unable to carry them.

This is a real inconsistency in the contract, not a limitation of my parser, and the fix is small:
`check_against_plugins()` (§4.2), called by the framework after discovery and before resolution, and
its return value logged the same way. See **C-1**.

Warnings it produces:

```
./climbcv.toml has a [plugins.yolo_hold] section, but no plugin with that id was
found in ./plugins. Did you mean 'yolo_holds'? Ignoring the section.
```

```
./climbcv.toml has a [topics."pose.smooth"] section, but no plugin publishes or
subscribes to a topic called 'pose.smooth'. Did you mean 'pose.smoothed'?
```

A section for a genuinely absent plugin stays a **warning**, never an error — `config-contract.md`
§3.3 is right that shared configs and temporarily-removed plugins make it a normal state.

### 7.3 What has no warning at all, and what that costs

**A typo in a plugin option is silent forever.** `every_n_frame = 8` (missing `s`) in
`[plugins.yolo_holds]` means the plugin reads its default of 4, publishes at twice the intended rate,
and nothing anywhere says a word. That is precisely the failure `config-contract.md` §3.1 refuses to
accept for `[framework]` — *"a typo'd `log_lvl` that silently does nothing is exactly the failure a
drop-in tool cannot afford"* — accepted without comment for `[plugins.<id>]`, which is where all the
options a user actually edits live.

Downstream consequences worth naming together, because they are one problem wearing three hats:

1. No typo detection for plugin options.
2. No `climbcv init` that can produce a useful example — it can emit `[framework]` and bare
   `[plugins.<id>]` stubs, and nothing about what any plugin actually accepts (§9).
3. No way to answer "what can I configure here" except reading the plugin's README, which in a
   drop-in ecosystem may not exist.

I am not building a validation layer, so I am not fixing this. But there is one option that lives
*inside* Decision #8, and I think it is worth putting on the record:

> **An optional, purely informational key-name list in the plugin manifest.**
>
> ```toml
> [config]
> keys = ["every_n_frames", "input_width", "imgsz", "min_score", "model_path"]
> ```
>
> Names only. No types, no required-ness, no defaults, no coercion, no validation of any value.
> Omitting it is legal and means "no warnings for this plugin." The loader hands the list to
> `check_against_plugins()`, which warns on an unlisted key with a nearest match. Four lines of
> manifest buys typo detection for the section a user edits most.

That is not a schema — it cannot reject a value, only notice a name nobody claimed — and it is
strictly opt-in. It is the cheapest thing that turns the most common config mistake from silent into
one line. **Offered for the review pass to accept or reject, not built.** See **C-6**.

---

## 8. Contract compliance

Every requirement in `config-contract.md`, and where it is satisfied.

| Contract | Requirement | Satisfied |
|---|---|---|
| §1 | `load_config(path) -> LoadedConfig{data, source, warnings}` | §4.2, plus `config_dir` (C-2) |
| §1 | no exception for a missing file | §4.3 stage 1 |
| §2 | three optional top-level sections; absent → `{}` | §4.3 stage 3; unknown tables warn and are left in place |
| §3.1 | every `[framework]` key optional | I never fill defaults; framework does |
| §3.1 | types must match; `int → float` where unambiguous | §5.1, `bool`-exclusion noted |
| §3.1 | unknown key → warning with nearest valid, not silence | §5.1, needs `FRAMEWORK_DEFAULTS` (C-3) |
| §3.1 | `plugins_dir`/`log_dir` resolved against the **config file's** dir, absolute | §4.3 stage 5; absolute `str` |
| §3.2 | dotted topic names arrive as single flat keys | §4.4; I hand back `tomllib`'s output unaltered |
| §3.2 | do not flatten or re-nest under `[topics]` | §4.3 stage 6 — shape checks only, no rewriting |
| §3.2 | detect a nested dict and warn with the fix | §7.1 heuristic + §7.2 authoritative (C-1) |
| §3.2 | value shape `{"publisher": str}`; extra keys warn | §6 |
| §3.2 | no existence validation at parse time | §6 — left to the resolver |
| §3.3 | plugin ids may contain dots; same flat-key rule | §4.4, §7.1 |
| §3.3 | `enabled` is the framework's key to read; non-bool fatal | **left to the framework**, deliberately — see note below |
| §3.3 | every other key opaque, verbatim, `enabled` not stripped | §1 answer 3, §4.3 stage 7 |
| §3.3 | unknown plugin id → warning with closest match | §7.2 (C-1: moved to `check_against_plugins`) |
| §4 | all nine absent-behaviour rows | §4.3 stages 1–3; `{}`-never-`None` follows from never constructing sections |
| §5 | every plugin-section value picklable | §1 answer 3 — guaranteed by doing nothing |
| §6 | loaded once, in the host, before discovery; framework owns `data` read-only | §2, `LoadedConfig` is frozen; `data` is a plain dict I do not retain |
| §7 | no schema validation / env overlay / writing / hot reload / required init | §2, §3.2, §9 |

**On `enabled`:** the contract makes it the framework's only read and its fatal. I am leaving it there
rather than pre-checking it, because the check needs the plugin id to be meaningful and the id is only
known after discovery — the same reason as §7.2. Pre-checking at depth 1 would also mis-fire on the
ambiguous nesting in §7.1. One owner, one message, at the point where the id exists.

---

## 9. `climbcv init`

`config-contract.md` §7 offers this as a suggestion. Worth building, small, and it is the only answer
to "what do I write in this file". Behaviour:

- Runs discovery (so it needs the loader), then writes `./climbcv.toml`, **refusing to overwrite an
  existing one**.
- Emits `[framework]` with every key from `FRAMEWORK_DEFAULTS` **commented out at its default value**,
  so the file is a readable inventory of what is tunable.
- Emits one `[plugins.<id>]` per discovered plugin, with the plugin's `name` and `description` as a
  comment above it — the manifest's required metadata (`loader.md` §3.1) earning its keep.
- If **C-6** is accepted, emits each declared key name as a commented line. Without C-6 the sections
  are empty but for a comment pointing at the plugin's folder, which is honest and not very useful —
  which is itself the argument for C-6.
- Emits `[topics]` as a comment block explaining it is only needed when climb-cv asks for it.

Pairs with `climbcv topics` (`broker.md` §7): one command says what you *can* set, the other says what
your settings *did*.

---

## 10. Worked example — all four first-party plugins plus the built-ins

Complete and valid. This is the file the authoring guide should ship.

```toml
# climbcv.toml — climb-cv application configuration.
#
# Every section and every key is optional. Delete this file and climb-cv still
# runs: every plugin in plugins/ is enabled and every framework knob takes its
# default. Sections are keyed by plugin id. A plugin's own options are passed to
# it verbatim and are documented by that plugin, not here.
#
# Run `climbcv topics` to see which plugin ended up publishing what.


# ─────────────────────────────────────────────────────────── framework knobs
[framework]
plugins_dir = "./plugins"     # relative to THIS FILE, not the working directory
log_dir     = "./logs"        # likewise
log_level   = "INFO"          # DEBUG | INFO | WARNING | ERROR | CRITICAL

# Fault tolerance — see design/isolation.md §5. Defaults shown; delete to accept.
# restart_max                     = 5
# restart_window_s                = 60.0
# restart_backoff_cap_s           = 30.0
# setup_failure_max               = 2
# heartbeat_warn_s                = 5.0
# grace_s                         = 2.0
# teardown_timeout_s              = 1.0
# shutdown_on_critical_quarantine = true
# stream_depth                    = 4      # 0/absent = computed per subscriber
# event_depth                     = 256


# ──────────────────────────────────────────────────── exclusive topic owners
# Only needed when two enabled plugins publish the same exclusive topic. In that
# case climb-cv refuses to start and prints the exact stanza to paste here, so
# you will normally never write this section by hand.
#
# [topics."pose.smoothed"]
# publisher = "core.smooth_oneeuro"


# ─────────────────────────────────────────────────────────── built-in stages
# Built-ins are configured exactly like plugins. Their ids contain a dot, so the
# quotes are REQUIRED: [plugins.core.capture] would configure a plugin named
# "core" and silently do nothing.

[plugins."core.capture"]
feed   = "live"      # "live", or a path to a video file
width  = 320
height = 240
mirror = true        # horizontal flip; sets Frame.mirrored

[plugins."core.pose_mediapipe"]
model    = "heavy"   # heavy | full | regular | lite
delegate = "gpu"     # "gpu" falls back to "cpu" automatically if unavailable

[plugins."core.smooth_oneeuro"]
min_cutoff           = 1.0
beta                 = 0.4
d_cutoff             = 1.0
visibility_threshold = 0.2
# passthrough        = false   # publish unfiltered instead of disabling this
                               # plugin — see the note under the table below

[plugins."core.persist_npy"]
output_dir = "./data"          # resolved against the working directory


# ────────────────────────────────────────────────────── first-party plugins

# YOLO hold detection → holds.boxes (a SHARED topic: you may run more than one
# detector, and the overlay tells them apart by source).
[plugins.yolo_holds]
every_n_frames = 4     # run inference at most once per N captured frames
input_width    = 192   # downscale width before inference; 0 disables
imgsz          = 256   # YOLO inference size
min_score      = 0.0   # drop detections below this confidence
# model_path   = "hold_detection.pt"

# Live overlay. Owns the video window, so it owns the ESC-to-quit path.
[plugins.exo_live]
window_title            = "climb-cv"
show_diagnostics        = false   # end-to-end latency and dropped-frame counts
lid_max_age_ms          = 2000    # older lid readings display as "n/a (stale)"
hold_box_max_age_frames = 0       # 0 = boxes never expire
box_color               = [0, 255, 0]

# Live 3D pose plot. Off by default, matching enable_plotting=False today.
[plugins.pose_plot]
enabled              = false
redraw_hz            = 30
visibility_threshold = 0.5
# backend            = "MacOSX"   # TkAgg | QtAgg | MacOSX; only if no window appears

# macOS lid-angle sensor. Skipped with one INFO line on Windows and Linux via
# platforms = ["darwin"] in its manifest — no configuration needed for that.
[plugins.mac_lid]
poll_interval_s = 0.5
read_timeout_s  = 3.0
force_recompile = false
# build_dir     = "~/.cache/climb-cv/mac_lid"
```

### 10.1 Migration from today's constructor arguments

The old API is `climbcv(feed=..., model=..., enable_plotting=..., ...)`. Every argument maps, and the
mapping is worth shipping in the docs:

| Today | Now |
|---|---|
| `feed`, `capture_width`, `capture_height` | `[plugins."core.capture"] feed / width / height` |
| `model`, `delegate` | `[plugins."core.pose_mediapipe"] model / delegate` |
| `smoothing_min_cutoff`, `smoothing_beta`, `smoothing_d_cutoff`, `smoothing_visibility_threshold` | `[plugins."core.smooth_oneeuro"]` |
| `output_dir` | `[plugins."core.persist_npy"] output_dir` |
| `enable_exo_live = False` | `[plugins.exo_live] enabled = false` |
| `enable_plotting = False` | `[plugins.pose_plot] enabled = false` |
| `enable_mac_lid = False` | `[plugins.mac_lid] enabled = false` |
| `exo_live_yolo_every_n_frames` | `[plugins.yolo_holds] every_n_frames` |
| `smoothing_enabled = False` | **no clean equivalent — see below** |

`delegate` deserves one note as evidence §5's primitives rule is doing real work: today it is
`BaseOptions.Delegate.GPU`, a MediaPipe enum. TOML can only carry `"gpu"`, and it *should* only carry
`"gpu"` — an enum value in a config section would be unpicklable-adjacent and would force `mediapipe`
to be importable in the host process, which full isolation exists to avoid.

**`smoothing_enabled = False` is the one argument with no clean translation, and it generalises.**
Today it makes the smoother pass raw landmarks through. The obvious translation,
`[plugins."core.smooth_oneeuro"] enabled = false`, does something quite different: it removes the only
publisher of `pose.smoothed`, which starves `exo_live`, `pose_plot` and `core.persist_npy` — all
`required = true` — and `broker.md` §4.2 step 5 makes that a **fatal startup error**.

The general rule, which every user of this file will need at some point:

> **For a plugin that publishes an exclusive topic, `enabled = false` does not mean "turn this feature
> off". It means "remove this data from the pipeline", and everything downstream of it stops.**

That is correct behaviour and the error message is good, but the mental model is not obvious and the
right shape for "I want unsmoothed data" is a plugin-level option
(`[plugins."core.smooth_oneeuro"] passthrough = true`, publishing `PoseFrame(smoothed=False)` and
keeping the topic alive), not a framework-level toggle. Requested of `framework-core` in **C-7**, and
flagged for `docs-and-testing` as a paragraph the authoring guide needs.

---

## 11. Friction register

| # | Finding | Severity | Fix from |
|---|---|---|---|
| **C-1** | `config-contract.md` §3.3 requires a warning ("section for an unknown plugin id, name the closest match") that needs the discovered plugin set, while §6 fixes the load order as config → discovery. `LoadedConfig.warnings` is structurally unable to carry it, along with both authoritative dotted-key warnings. Fix: a second entry point, `check_against_plugins(cfg, plugin_ids, topic_names)`, called after discovery. | **medium-high** | `framework-core` (accept the second call) |
| **C-3** | The `[framework]` unknown-key warning and type check both need the key/type table, so either it is duplicated in my module (and drifts the first time a knob is added — failing as a spurious warning about a valid key) or `framework-core` exports it. Requested: `FRAMEWORK_DEFAULTS` as an importable table; framework still applies the defaults. | **medium** | `framework-core` |
| **C-7** | For a plugin publishing an exclusive topic, `enabled = false` removes the topic and fatally starves required subscribers — so today's `smoothing_enabled = False` has no translation. Needs a `passthrough` option on `core.smooth_oneeuro`, plus prominent documentation of the general rule. | **medium** | `framework-core` + `docs-and-testing` |
| **C-2** | `[framework]` path keys are resolved against the **config file's** directory; plugin-section path values are untouched strings resolved by the plugin against its **working directory**. The asymmetry will surprise people, and a plugin has no way to opt into the framework's own rule because it never learns where the config file was. Requested: `config_dir` on `LoadedConfig` and a read-only `self.config_dir` on the `Plugin` base class. | **medium** | `framework-core` (`plugin-api.md` §2) |
| **C-6** | With no validation layer, a typo in a plugin option is silent forever, there is no machine-readable list of what a plugin accepts, and `climbcv init` cannot produce a useful example. The narrowest thing that stays inside Decision #8 is an **optional, informational key-name list** in the manifest (`[config] keys = [...]`) used only for nearest-match warnings — names only, no types, no rejection, ignoring it legal. Offered for the review pass; not built. | **medium** | review decision |
| **C-4** | `log_level` is the one framework key with a closed value set, and a type check cannot catch `"DEBGU"`. It will fail inside logging setup with no mention of the config file. Either `framework-core` validates it by name, or `FRAMEWORK_DEFAULTS` carries an optional allowed-value tuple. | **low-medium** | `framework-core` |
| **C-5** | TOML has no null, so a plugin cannot express "explicitly unset" and authors will reach for `""` or `0`. The convention is to omit the key and rely on `self.config.get(k, default)`; it needs to be stated once, prominently, rather than discovered. | **low** | `docs-and-testing` |
| **C-8** | The missing-quotes detection `config-contract.md` §3.2/§3.3 asks for is **not decidable** at parse time under `[plugins]`: `[plugins.core.capture]` and `[plugins.my_plugin.zones]` are the same shape, and sub-tables in plugin sections are legitimate. Handled as a `core.`-scoped heuristic now plus an authoritative check after discovery (C-1). Recording it so the contract is not read as asking for something stronger than is possible. | **low** | none — documented |

---

## 12. Proposed Decision Log entries

Full text in the summary.

- **Config discovery is exactly two steps** — an explicit `--config` path (fatal if missing), then
  `./climbcv.toml` (absent is fine). No parent-directory walk, no user-global file, no env var.
  Alternatives and why they were rejected: §3.2.
- **`[plugins.<id>]` values are passed through byte-for-byte from `tomllib`, with zero
  post-processing.** This is how `config-contract.md` §5's picklability constraint is *guaranteed*
  rather than checked, and it is the operational meaning of Decision #8's "raw section dict".
- **`framework-core` owns one exported `FRAMEWORK_DEFAULTS` table (names → default values, whose types
  drive coercion); `plugins-and-config` imports it to produce the unknown-key and type warnings.**
  Framework still applies the defaults. Alternative rejected: duplicating the table, whose first
  failure is a spurious warning about a valid key.
- **Config validation happens in two passes, not one:** file-level at load, graph-level after
  discovery. Follows from `config-contract.md` §6's load order, and it is what makes §3.3's
  closest-match warning possible at all.

---

## 13. Handoffs

**To `framework-core`:** all four §8 questions are answered in §1. Four asks: accept
`check_against_plugins()` (C-1), export `FRAMEWORK_DEFAULTS` (C-3), add `config_dir` to
`LoadedConfig` and the `Plugin` base (C-2), and add `passthrough` to `core.smooth_oneeuro` (C-7).
Everything else in `config-contract.md` is satisfied as written.

**To `plugin-api-guardian`:** the author-facing surfaces here are §10's file (it is what users
actually type), the error and warning strings in §4.1, §5.1, §7.1 and §7.2, and the "`enabled = false`
on an exclusive publisher removes the topic" rule in §10.1 — that last one is a config semantic with
teeth and no warning attached. **C-6** is a decision about the shape of the ecosystem rather than a
message, and it deserves an explicit yes or no rather than defaulting to no by omission.

**To `docs-and-testing`:** §10 is a shippable example file and §10.1 is the migration table for anyone
on the current API. Parsing is a pure function from a string to a dict-plus-warnings, so the whole of
§4–§7 tests as table-driven cases over TOML fixtures with no filesystem and no processes — the two
cases most worth fixtures are the quoting mistakes (§7.1) and the absent-file rows of
`config-contract.md` §4. §11's C-5 and C-7 both need a paragraph in the guide.
