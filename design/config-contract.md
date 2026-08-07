# Consumer-Driven Config Contract

**From:** `framework-core` (consumer) · **To:** `plugins-and-config` (producer) · Status: **revised
2026-08-07 (revision 01)**

This is what `framework-core` needs from parsed `climbcv.toml`. It does not specify the file format,
the parser, or the file's location — those belong to `plugins-and-config` per Decision #10's seam
note. It specifies the **shape of the parsed result**, the **keys the framework reads**, and
**behaviour when anything is absent**.

The framework never opens `climbcv.toml`. It receives one plain `dict` and a source description for
error messages.

Revision 01 answers `config.md` §13's four asks and actions C-1 through C-8. All four asks are
**accepted**. Changelog: [`revision-01.md`](revision-01.md).

---

## 1. The handoff signature

**Two entry points, not one** — this is C-1, and `plugins-and-config` is right that the original
single-pass contract asked for something impossible.

```python
def load_config(path: Path | None = None) -> LoadedConfig: ...

@dataclass(frozen=True)
class LoadedConfig:
    data: dict            # the shape specified in §2
    source: str           # "./climbcv.toml" | "<defaults: no config file found>"
    config_dir: str | None      # absolute dir containing the file; None if there was none  [C-2]
    warnings: tuple[str, ...]   # already-formatted, user-facing; framework logs them verbatim


def check_against_plugins(
    cfg: LoadedConfig,
    plugin_ids: frozenset[str],                  # every id that survived discovery
    topic_names: frozenset[str],                 # every topic any enabled plugin publishes/subscribes
    plugin_config_keys: Mapping[str, tuple[str, ...]],   # id -> [config] keys, () if undeclared  [C-6]
) -> tuple[str, ...]: ...                        # more already-formatted warnings
```

`source` exists so framework errors can say *where* a bad value came from. `warnings` lets the parser
report file-level problems (unreadable, unknown `[framework]` key) through the framework's logging
rather than printing on its own — one voice, one destination.

**No exceptions for a missing file.** Absent → `data == {}`, `source` says so. See §4.

### 1.1 `check_against_plugins()` — C-1 accepted, and the contract was wrong

`plugins-and-config` found that three warnings this document requires — §3.3's "section for an unknown
plugin id, name the closest match", and §3.2/§3.3's authoritative dotted-key warnings — **all need the
discovered plugin id set**, while §6 of this same document fixes the load order as
**config → discovery**. So `LoadedConfig.warnings`, produced before discovery has happened, is
structurally unable to carry them.

**That is a defect in this contract, not a limitation of their parser, and it is worth naming as such:**
§3.3 asked for a warning that cannot exist at the point §6 puts it. Requiring a producer to satisfy
both would have forced either a wrong load order (the parser cannot discover plugins — `plugins_dir`
comes *from* the config) or a silently dropped requirement.

Accepted as specified:

- The framework calls `load_config()` first, uses `data["framework"]["plugins_dir"]` to discover, then
  calls `check_against_plugins()` **after discovery and before resolution**, and logs the returned
  strings exactly as it logs `LoadedConfig.warnings`. Same voice, same destination, two moments.
- It returns warnings only. **It never raises and never fails startup.** Everything it can detect is a
  config statement about something absent, which §3.3 already establishes is a normal state.
- `plugin_ids` is the post-discovery enabled-or-skipped set, so a section for a plugin that exists but
  was skipped for platform reasons does **not** warn — that would be the same false alarm
  `loader.md` §3.1 avoids for `platforms`.
- `plugin_config_keys` carries C-6's manifest key lists. An **empty tuple** means "declared nothing, so
  emit no option warnings for this plugin", which must stay distinguishable from "this id is absent" —
  those are different messages.
- `topic_names` lets it warn on a `[topics."pose.smooth"]` section naming a topic nothing uses. The
  framework still owns the *fatal* topic errors (`broker.md` §4.3), because those need the candidate
  publisher list.

Revision 01's `[topics]` additions (§3.2) and C-6 both flow through this call, so the two-pass split
turns out to be load-bearing for more than it was introduced for.

### 1.2 `FRAMEWORK_DEFAULTS` — C-3 and C-4 accepted

`framework-core` exports one table, importable without pulling in the framework:

```python
# climbcv/framework_defaults.py -- imports nothing
@dataclass(frozen=True)
class Knob:
    default: object                       # its TYPE drives coercion and the type check
    allowed: tuple[object, ...] | None = None    # closed value set, or None  [C-4]
    is_path: bool = False                 # resolve relative to the config file's dir  [§3.1]

FRAMEWORK_DEFAULTS: Mapping[str, Knob] = {...}   # the §2 key list
```

`plugins-and-config` imports it for (a) the valid-key set, (b) `difflib` nearest-match suggestions,
(c) each key's expected type, and (d) each key's closed value set if it has one. **The framework still
applies the defaults** — ownership stays where §3.1 put it. One table, two consumers, no duplication.

Accepted because the failure mode of duplication is specific and bad, and `config.md` §5.1 identified
it exactly: the first time `framework-core` adds a knob, a duplicated table in the parser warns
*"unknown key"* about a key that works perfectly. A spurious warning about valid config is worse than
no warning, because it trains users to ignore the channel.

**C-4 — `log_level` is the one key with a closed set.** It is typed `str`, so `log_level = "DEBGU"`
passes every type check and then fails inside logging setup with no mention of the config file. Handled
by `Knob.allowed`, so `plugins-and-config` produces the message — it owns `source` and the message
voice:

```
./climbcv.toml [framework] log_level = "DEBGU" is not a log level.
Valid values: CRITICAL, ERROR, WARNING, INFO, DEBUG. Using INFO.
```

It is a **warning with a stated fallback**, not a fatal: an unreadable log level should not stop a run.
`log_level` is currently the only key carrying `allowed`, which is why one optional field is not a
schema layer — and if a second knob ever needs one, that is still not a schema layer.

`is_path` exists so the resolution rule in §3.1 is data rather than a hardcoded list of key names in
the parser. Currently `plugins_dir`, `log_dir`, `state_dir`.

Two implementation notes from `config.md` §5.1 that this contract endorses rather than restates:
`type(value) is int` (never `isinstance`, because `bool` is an `int` subclass, so `restart_max = true`
must be a type error), and coercion is `int → float` only.

## 2. The shape

Three top-level sections, **all optional**, and any absent section is `{}`.

```python
{
  "framework": {           # §3.1 -- framework knobs; every key optional
      "plugins_dir": "./plugins",
      "use_bundled_plugins": True,     # NEW (F-3) -- loader.md §2.1's bundled root
      "log_level": "INFO",             # closed value set -- C-4, §1.2
      "log_dir": "./logs",
      "state_dir": "./.climbcv",       # NEW (F-7) -- parent of each plugin's data_dir
      "stream_depth": 0,               # CHANGED: 0 = "computed per subscriber" -- see below
      "max_stream_depth": 256,         # NEW -- caps broker.md §5.1.2's arithmetic
      "event_depth": 256,
      "restart_max": 5,
      "restart_window_s": 60.0,
      "restart_backoff_cap_s": 30.0,
      "setup_failure_max": 2,
      "heartbeat_warn_s": 5.0,         # per-plugin overridable in the manifest
      "grace_s": 2.0,
      "teardown_timeout_s": 1.0,       # per-plugin overridable in the manifest -- guardian S8
      "shutdown_on_critical_quarantine": True,
  },

  "topics": {              # §3.2 -- topic-level settings. Keys are TOPIC NAMES (dotted!)
      "pose.smoothed": {"publisher": "kalman_smooth"},
      "grip.force":    {"kind": "stream", "exclusivity": "shared"},   # NEW -- guardian S7a
      "holds.boxes":   {"required": False},                           # NEW -- guardian S7c
  },

  "plugins": {             # §3.3 -- per-plugin. Keys are PLUGIN IDS (may be dotted: "core.capture")
      "yolo_holds": {
          "enabled": True,           # the ONLY key the framework reads here
          "every_n_frames": 4,       # opaque -- passed to the plugin verbatim
          "imgsz": 256,              # opaque
      },
      "core.smooth_oneeuro": {
          "passthrough": True,       # opaque to the framework; the STAGE reads it -- C-7
      },
  },
}
```

Corresponding TOML, for concreteness — note the **required quoting** on dotted keys:

```toml
[framework]
log_level = "DEBUG"

[topics."pose.smoothed"]
publisher = "kalman_smooth"

[plugins.yolo_holds]
every_n_frames = 4

[plugins."core.capture"]
width  = 640
height = 480
```

**`stream_depth`'s default changed from `4` to `0`, and this is not cosmetic (guardian S10).** `4` was
listed here and in `isolation.md` §8 while `broker.md` §5.1.1 computed `max(4, 2 × subscriptions)` — so a
user who copied the documented default into their file would pin depth 4 and **silently reintroduce the
cross-topic starvation** that computation exists to prevent. `0` means "computed"; `climbcv topics -v`
prints what it resolved to. Please also correct `config.md` §10's worked example, which currently shows
`# stream_depth = 4`.

**`teardown_timeout_s` and `heartbeat_warn_s` are now also per-plugin manifest keys.** The
`[framework]` value is the default for plugins that do not declare one, and
`[plugins.<id>] teardown_timeout_s` lets a user override an author's choice — so both names appear in
two sections with different scopes. Nothing changes for the parser: under `[plugins.<id>]` it is an
opaque value like any other, and the **framework** reads it (as it does `enabled`).

**`passthrough` is deliberately *not* a framework key (C-7).** It is an option on one built-in stage, so
it lives in `[plugins."core.smooth_oneeuro"]` and is opaque to the parser. See §3.4.

---

## 3. Per-section requirements

### 3.1 `[framework]`

- Every key optional; the framework applies the defaults above. Absent section → all defaults.
- **Types must match** the defaults' types (int stays int, float stays float, bool stays bool). Where
  the intent is unambiguous the framework coerces (int → float for `*_s` keys) and everything else is
  a fatal error naming the key, the value, the expected type, and `source`.
- **Unknown keys → a warning, not an error, and not silence.** Decision #8 rules out a schema
  validation layer, and this is not one: a typo'd `[framework] log_lvl = "DEBUG"` that silently does
  nothing is exactly the failure a drop-in tool cannot afford. One line naming the key and the nearest
  valid one, ignore the value, continue. **Resolved: the parser emits it**, from `FRAMEWORK_DEFAULTS`
  (§1.2). The earlier "either is fine" left it possible for neither side to do it.
- **Closed value sets:** `log_level` only, via `Knob.allowed` (§1.2, C-4).
- `plugins_dir`, `log_dir` and `state_dir` are relative to the config file's directory when relative
  (not to the process CWD). If there is no config file, relative to CWD. **This needs to be the parser's
  behaviour, since only it knows where the file was.** Resolved absolute paths, please. Which keys these
  are is carried by `Knob.is_path` (§1.2) rather than by a hardcoded name list in the parser.

### 3.2 `[topics]`

- Keys are **topic names, which contain dots** (`pose.smoothed`, `holds.boxes`, `device.lid_angle`).
- **Requirement: dotted topic names must arrive as single flat keys, not nested dicts.** In TOML,
  `[topics."pose.smoothed"]` yields `{"topics": {"pose.smoothed": {...}}}` while an unquoted
  `[topics.pose.smoothed]` yields `{"topics": {"pose": {"smoothed": {...}}}}`. The framework needs the
  first form. Two things follow for the parser:
  1. Do not flatten or re-nest keys under `[topics]`; hand back exactly what the TOML parser produced.
  2. **A nested dict under `[topics]` is almost certainly a user who forgot the quotes.** Detect it
     **where it is decidable** and warn with the fix, because the alternative is a topic assignment
     that silently does nothing:
     `[topics.pose.smoothed] should be [topics."pose.smoothed"] — dotted topic names need quotes.`
     See §3.3's C-8 note on why "where decidable" is the honest phrasing.
- **Value shape, extended in revision 01.** All four keys optional; the section may be empty.

  | Key | Type | Meaning | Added for |
  |---|---|---|---|
  | `publisher` | str | which plugin owns this exclusive topic | original |
  | `kind` | str | `"stream"` \| `"event"` — override a descriptor contradiction | guardian S7a |
  | `exclusivity` | str | `"exclusive"` \| `"shared"` — same | guardian S7a |
  | `required` | bool | `false` makes every subscription to this topic optional | guardian S7c |

  Check them as `config.md` §6 already checks `publisher`: the value must be a table, each key must
  have the right primitive type, unknown keys warn and are ignored. **Do not check the values'
  meanings** — `kind = "streem"` and "you cannot override a standard topic" are both the framework's
  fatals, because only the resolver knows the topic's descriptor and its candidate list.
- The framework does **not** need the topic to exist at parse time; validation is the resolver's job
  (`broker.md` §4), which is where the good error message with the candidate list lives. A section for a
  topic *nothing uses* is a warning from `check_against_plugins()` (§1.1), not a parse-time error.

`config.md` §6's note that a user "will never write this section until climb-cv prints the stanza for you
to paste" stays true of all four keys — each one is printed verbatim by the error it fixes
(`broker.md` §4.3, §4.4). That is the property worth documenting, and it is why extending this section
does not make it something users are expected to author.

### 3.3 `[plugins.<id>]`

- Keys are plugin ids. **They may contain dots**, because built-in stages are `core.capture`,
  `core.pose_mediapipe`, `core.smooth_oneeuro`, `core.persist_npy`. Same flat-key requirement and same
  quoting warning as §3.2.
- **`enabled` (bool) is the only key the framework reads.** Absent → the plugin is enabled
  (`loader.md` §5, rule 6). Non-bool → fatal, naming the key and `source`.
- **Every other key is opaque and is handed to the plugin verbatim** as `self.config`, exactly per
  Decision #8. The framework does not read, validate, transform, or default them. It does not strip
  `enabled` — a plugin that wants to see it can. **Two exceptions the framework does read**, added in
  revision 01: `teardown_timeout_s` and `heartbeat_warn_s`, as user overrides of a manifest value (§2).
  They are still passed through to the plugin unstripped.
- A section for an id not present in either plugin root → **warning naming the closest match**, not an
  error, emitted from `check_against_plugins()` (§1.1). Config for a not-yet-installed plugin is a
  normal, legitimate state (shared configs, plugins pulled out temporarily).
- **Option-name typos: warn when, and only when, the plugin declared its key names (C-6, accepted).**
  `[config] keys = [...]` in the manifest (`loader.md` §3.1.2) is an optional, informational list of
  names. `check_against_plugins()` receives it as `plugin_config_keys[id]` and warns on an unlisted key
  with a nearest match. **Names only: no types, no required-ness, no defaults, and it never rejects a
  value.** An undeclared list (empty tuple) means no warnings for that plugin, and that must stay
  distinguishable from an absent plugin id — different messages.

  This closes what `config.md` §7.3 correctly identified as the sharpest asymmetry in the design:
  `every_n_frame = 8` (missing `s`) is silent forever, while `[framework] log_lvl` warns — and
  `[plugins.<id>]` is where every option a user actually edits lives. It stays inside Decision #8
  because a mechanism that can only notice a name nobody claimed, and can never reject a value, is not a
  validation layer.

**C-8, acknowledged: this contract asked for more than is decidable.** §3.2/§3.3's missing-quotes
requirement is **not decidable at parse time under `[plugins]`** — `[plugins.core.capture]` and
`[plugins.my_plugin.zones]` are the same shape, and sub-tables in plugin sections are legitimate. The
requirement is therefore restated as two obligations rather than one:

1. **At parse time, a heuristic.** `config.md` §7.1's `core.`-scoped rule is exactly right, and it has a
   stronger justification than that document claimed: `core.` is a **reserved id prefix**
   (`loader.md` §3.1), so `[plugins.core.<anything>]` cannot be a legitimate plugin-with-a-sub-table. Zero
   false positives, and it catches the overwhelmingly common real case.
2. **After discovery, the authoritative check.** In `check_against_plugins()`: warn when `<outer>` is not
   a discovered id but `"<outer>.<inner>"` is.

Recording this so the contract is not read as asking for something stronger, and so nobody later
"fixes" the heuristic into something that misfires on a legitimate sub-table.

### 3.4 `passthrough` on `core.smooth_oneeuro` — C-7 accepted

`plugins-and-config` found that today's `smoothing_enabled = False` has **no translation**:
`core.smooth_oneeuro` is the resolved publisher of the exclusive topic `pose.smoothed`, so
`enabled = false` does not turn smoothing off — it **removes the topic** and fatally starves
`exo_live`, `pose_plot` and `core.persist_npy`.

```toml
[plugins."core.smooth_oneeuro"]
passthrough = true      # republish pose.raw unchanged as pose.smoothed
```

**Nothing changes for the parser.** It is an ordinary opaque value in a plugin section, read by the stage
in its own `setup()`. It appears in this contract only because the *general rule* it instances needs to
be written down somewhere both sides can see:

> **Disabling a plugin removes the topics it publishes. Neutralising a stage while keeping the pipeline
> intact requires the stage's own option — the framework cannot synthesise one, because only the stage
> knows what "do nothing" means for its payload.**

Design and the general-rule discussion: `isolation.md` §8.3. The consequence for error messages is
`broker.md` §4.3's second starved-subscription variant, which detects that an absent topic's only
publisher was disabled *by config* and prints both this rule and the stanza above — so a user who takes
the obvious wrong path is told the right one rather than just being refused.

---

## 4. Behaviour when things are absent — the exhaustive table

| Situation | Required behaviour |
|---|---|
| No config file at all | `data == {}`, `source == "<defaults: no config file found>"`. **Not an error, not a warning.** Zero-config startup is a first-class case: drop plugins in, run. |
| Config file present but empty | `data == {}`. Identical to above except `source` names the file. |
| `[framework]` absent | `{}` → all defaults. |
| A `[framework]` key absent | that default applies. |
| `[topics]` absent | `{}` → exclusivity resolves by candidate count alone (`broker.md` §4.2). |
| `[plugins]` absent | `{}` → every discovered plugin enabled. |
| `[plugins.<id>]` absent for a discovered plugin | plugin enabled; **`self.config == {}`**, never `None`. |
| `[plugins.<id>]` present but empty | identical: `self.config == {}`. |
| Config file unreadable (permissions, malformed TOML) | **Fatal.** A config the user wrote and the app cannot read must not be silently replaced by defaults. Message: path, parser error, line number. |
| No config file at all, and a plugin reads `self.config_dir` | **`None`.** Not the CWD, not a guess — see §3.5. |
| A key the user meant to unset | There is no way to express it; **omit the key** — see below (C-5). |

The `{}`-never-`None` rule matters to the authoring interface: every plugin example uses
`self.config.get(key, default)`, and that must work without a guard on the first line of every
`setup()`.

**C-5 — TOML has no null, and the convention needs stating once.** A plugin cannot express "explicitly
unset", and authors will reach for `""` or `0`, which are indistinguishable from a deliberate empty
string or zero. The convention is therefore: **omit the key and rely on
`self.config.get(key, default)`.** `plugins-and-config` is right that this needs to be stated
prominently rather than discovered, and it is `docs-and-testing`'s to put in the guide — recorded here
because the `{}`-never-`None` rule above is what makes it work, so the two belong next to each other.
The corollary for plugin authors: **do not treat `""` or `0` as "unset"** in your own option handling, or
a user who genuinely wants zero cannot say so.

### 3.5 `config_dir` — C-2 accepted

`LoadedConfig.config_dir` (§1) is the absolute directory containing the config file, or `None` if there
was none. The framework puts it on `PluginPlan` (`broker.md` §6) and binds a read-only
`self.config_dir: Path | None` on the `Plugin` base (`plugin-api.md` §2).

The asymmetry it fixes, which `plugins-and-config` identified precisely: **`[framework]` path keys are
resolved against the config file's directory (§3.1), while a plugin-section path value is an untouched
string the plugin resolves against its working directory** — and a plugin previously had **no way to opt
into the framework's own rule, because it never learned where the config file was.** So
`model_path = "models/holds.pt"` in a config file meant one thing to `log_dir` and another to
`yolo_holds`, with nothing to warn about it.

The recommended idiom, which the guide should teach next to `self.config.get`:

```python
p = Path(self.config.get("model_path", "hold_detection.pt"))
if not p.is_absolute():
    base = self.config_dir or Path.cwd()       # config file's dir, else CWD
    p = base / p
```

Deliberately **not** resolved for the plugin by the framework. Doing so would require the framework to
know which of a plugin's opaque options are paths, which is the typed schema Decision #8 excludes — and a
heuristic ("looks like a path") would be wrong in both directions. Handing over the base directory is the
smallest thing that makes the plugin's choice *possible*; the plugin still decides.

`None` rather than defaulting to the CWD, because "there was no config file" and "the config file is in
the CWD" are genuinely different situations and a plugin resolving a user-supplied path should be able to
tell them apart.

---

## 5. Value-type constraint — please enforce or document

**Every value in `[plugins.<id>]` must be picklable.**

The section dict is passed to a child process at spawn (`isolation.md` §2.4). TOML's own value types
(str, int, float, bool, list, dict, and datetime/date/time) are all picklable, so a plain TOML parser
satisfies this automatically — **the constraint only bites if the parser ever post-processes values
into custom objects** (a `Path` for path-looking strings, an enum for known strings, a lazy accessor).
A section containing an unpicklable value produces a spawn-time failure whose error message points at
multiprocessing rather than at the config, which is a genuinely bad afternoon.

Request: keep values as TOML primitives, or if post-processing is wanted, confirm picklability. This is
also consistent with Decision #8's "raw section dict."

---

## 6. Timing and ownership

- **Loaded once, in the host, before discovery.** Children never read the file; each receives its
  section as data. One consistent snapshot per run, and no file access from plugin processes.
- The framework calls `load_config` and then owns `data` read-only. It will not mutate it.
- Load order, revised for C-1:

  ```
  load_config()  ->  discovery (plugins_dir comes from config)  ->  manifests
      ->  check_against_plugins()  ->  resolution (broker.md §4)  ->  spawn
  ```

  The parser still cannot depend on knowing which plugins exist at `load_config()` time, which is why
  §3.2/§3.3 do no cross-validation there. What changed is that the checks needing that knowledge now have
  a defined home (§1.1) instead of being asked of a function that could not perform them.
- `check_against_plugins()` runs **before** resolution so its warnings appear above the resolver's
  fatals. A user whose `[topics."pose.smooth"]` typo caused a starved-subscription error should see the
  typo warning first; the reverse order buries the cause under the symptom.

---

## 7. What framework-core does *not* need

Stated so effort isn't spent on it:

- **No schema validation of plugin sections.** Decision #8. The framework does not want types,
  defaults, or required-key declarations for `[plugins.<id>]`.
- **No env-var or CLI overlay** in v1. If added later, the framework only needs the merged result — the
  contract in §2 is unchanged.
- **No config writing.** Nothing needs to persist changes.
- **No hot reload.** Assumption §3. One read per run.
- **No default-config generation** required by the framework — though a `climbcv init` that writes a
  commented `climbcv.toml` would be genuinely useful to users, and would pair well with
  `climbcv topics`. Offered as a suggestion, not a requirement. `plugins-and-config` is building it
  (`config.md` §9); with C-6 accepted it can now emit each plugin's declared option names, which was the
  argument for C-6 in the first place.
- **No validation of `[plugins.<id>]` values, still.** C-6 adds warnings about *names*; nothing inspects,
  types, defaults, coerces or rejects a **value**. That line is the whole of Decision #8 and revision 01
  does not cross it.

---

## 8. Questions back to `plugins-and-config` — all four answered

Answered in `config.md` §1. Recorded here so the contract is self-contained.

1. **Python 3.10 vs 3.11 — resolved by Decision #17 (accepted).** Floor is **3.11**, `tomllib` from the
   stdlib, no TOML dependency imposed on any author. And yes, one parser for both files: the loader
   imports `plugins-and-config`'s `read_toml_file()` (`config.md` §4.1) so a malformed
   `climbcv-plugin.toml` and a malformed `climbcv.toml` produce the same three-line error. That
   dependency direction is deliberate and is named in `loader.md` §3.3. `climbcv/config.py` is host-only,
   so it does not fall under the stdlib+numpy rule that binds `climbcv.contracts` and `climbcv.plugin`.
2. **Who owns the warnings — the parser does**, with the C-1 correction: the three that need the
   discovered plugin set move to `check_against_plugins()` (§1.1). The framework logs both return values
   verbatim. The earlier "either is fine" is withdrawn — it left it possible for neither side to emit
   them.
3. **§5, values stay TOML primitives — confirmed, and more strongly than asked.** Every value inside
   `[plugins.<id>]` is handed over exactly as `tomllib` produced it: no `Path` coercion, no enum lookup,
   no lazy accessor, no `str.strip()`. So §5's picklability constraint is satisfied **by construction
   rather than by a check**, which is the only version of it that stays true as the code is maintained.
   This is the answer the guardian's "beyond scope" note asked to have confirmed before the child-spawn
   path is written; it is confirmed.
4. **`plugins_dir` / `log_dir` (and now `state_dir`) resolved against the config file's directory —
   confirmed**, returned as absolute `str` rather than `Path` so §3.1's "types must match the defaults'
   types" stays literally true and §5's primitives rule needs no exception. `Knob.is_path` (§1.2) is how
   the parser knows which keys these are.

### 8.1 What this document now asks of `plugins-and-config`

All four asks from `config.md` §13 are **accepted**: `check_against_plugins()` (§1.1), exported
`FRAMEWORK_DEFAULTS` (§1.2), `config_dir` on `LoadedConfig` and the `Plugin` base (§1, §3.5), and
`passthrough` on `core.smooth_oneeuro` (§3.4). C-4 and C-6 are accepted (§1.2, §3.3); C-5 and C-8 are
acknowledged and recorded (§4, §3.3).

Six things changed on this side that your parser or worked example must follow:

1. **Four new `[framework]` keys** — `use_bundled_plugins`, `state_dir`, `max_stream_depth`, and
   `stream_depth`'s default changing from `4` to **`0`** (§2). The last one is a correctness fix, not a
   cosmetic one.
2. **`config.md` §10's `# stream_depth = 4` must become `# stream_depth = 0`**, or the worked example
   ships an invitation to disable a starvation mitigation.
3. **Three new `[topics]` keys** — `kind`, `exclusivity`, `required` (§3.2). Type-check only; the
   framework owns their meaning.
4. **`plugin_config_keys` in `check_against_plugins()`** — C-6's manifest lists, with `()` distinguishable
   from an absent id.
5. **`FRAMEWORK_DEFAULTS` is a table of `Knob`, not of bare values** (§1.2), because C-4 needs
   `allowed` and §3.1 needs `is_path`. The type still comes from `default`'s type, as you asked.
6. **`climbcv init` gains a root distinction.** With `loader.md` §2.1's bundled root, discovery returns
   plugins from two places; emitting them undifferentiated would tell a user they have eight plugins in
   `plugins/`. A comment naming the origin is enough.
