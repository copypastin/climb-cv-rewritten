# Consumer-Driven Config Contract

**From:** `framework-core` (consumer) · **To:** `plugins-and-config` (producer) · Status: **proposed**

This is what `framework-core` needs from parsed `climbcv.toml`. It does not specify the file format,
the parser, or the file's location — those belong to `plugins-and-config` per Decision #10's seam
note. It specifies the **shape of the parsed result**, the **keys the framework reads**, and
**behaviour when anything is absent**.

The framework never opens `climbcv.toml`. It receives one plain `dict` and a source description for
error messages.

---

## 1. The handoff signature

```python
def load_config(path: Path | None = None) -> LoadedConfig: ...

@dataclass(frozen=True)
class LoadedConfig:
    data: dict            # the shape specified in §2
    source: str           # "./climbcv.toml" | "<defaults: no config file found>"
    warnings: tuple[str, ...]   # already-formatted, user-facing; framework logs them verbatim
```

`source` exists so framework errors can say *where* a bad value came from. `warnings` lets the parser
report file-level problems (unreadable, unknown `[framework]` key) through the framework's logging
rather than printing on its own — one voice, one destination.

**No exceptions for a missing file.** Absent → `data == {}`, `source` says so. See §4.

---

## 2. The shape

Three top-level sections, **all optional**, and any absent section is `{}`.

```python
{
  "framework": {           # §3.1 -- framework knobs; every key optional
      "plugins_dir": "./plugins",
      "log_level": "INFO",
      "log_dir": "./logs",
      "stream_depth": 4,
      "event_depth": 256,
      "restart_max": 5,
      "restart_window_s": 60.0,
      "restart_backoff_cap_s": 30.0,
      "setup_failure_max": 2,
      "heartbeat_warn_s": 5.0,
      "grace_s": 2.0,
      "teardown_timeout_s": 1.0,
      "shutdown_on_critical_quarantine": True,
  },

  "topics": {              # §3.2 -- exclusive-topic ownership. Keys are TOPIC NAMES (dotted!)
      "pose.smoothed": {"publisher": "kalman_smooth"},
  },

  "plugins": {             # §3.3 -- per-plugin. Keys are PLUGIN IDS (may be dotted: "core.capture")
      "yolo_holds": {
          "enabled": True,           # the ONLY key the framework reads here
          "every_n_frames": 4,       # opaque -- passed to the plugin verbatim
          "imgsz": 256,              # opaque
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
  valid one, ignore the value, continue. The parser may emit this via `warnings`, or hand the raw dict
  over and let the framework do it — **either is fine, but it must happen somewhere.**
- `plugins_dir` and `log_dir` are relative to the config file's directory when relative (not to the
  process CWD). If there is no config file, relative to CWD. **This needs to be the parser's
  behaviour, since only it knows where the file was.** Resolved absolute paths, please.

### 3.2 `[topics]`

- Keys are **topic names, which contain dots** (`pose.smoothed`, `holds.boxes`, `device.lid_angle`).
- **Requirement: dotted topic names must arrive as single flat keys, not nested dicts.** In TOML,
  `[topics."pose.smoothed"]` yields `{"topics": {"pose.smoothed": {...}}}` while an unquoted
  `[topics.pose.smoothed]` yields `{"topics": {"pose": {"smoothed": {...}}}}`. The framework needs the
  first form. Two things follow for the parser:
  1. Do not flatten or re-nest keys under `[topics]`; hand back exactly what the TOML parser produced.
  2. **A nested dict under `[topics]` is almost certainly a user who forgot the quotes.** Detect it and
     warn with the fix, because the alternative is a topic assignment that silently does nothing:
     `[topics.pose.smoothed] should be [topics."pose.smoothed"] — dotted topic names need quotes.`
- Value shape: `{"publisher": "<plugin_id>"}`. Additional keys → warning, ignored (room to grow).
- The framework does **not** need the topic to exist at parse time; validation is the resolver's job
  (`broker.md` §4), which is where the good error message with the candidate list lives.

### 3.3 `[plugins.<id>]`

- Keys are plugin ids. **They may contain dots**, because built-in stages are `core.capture`,
  `core.pose_mediapipe`, `core.smooth_oneeuro`, `core.persist_npy`. Same flat-key requirement and same
  quoting warning as §3.2.
- **`enabled` (bool) is the only key the framework reads.** Absent → the plugin is enabled
  (`loader.md` §5, rule 6). Non-bool → fatal, naming the key and `source`.
- **Every other key is opaque and is handed to the plugin verbatim** as `self.config`, exactly per
  Decision #8. The framework does not read, validate, transform, or default them. It does not strip
  `enabled` — a plugin that wants to see it can.
- A section for an id not present in `plugins/` → **warning naming the closest match**, not an error.
  Config for a not-yet-installed plugin is a normal, legitimate state (shared configs, plugins pulled
  out temporarily).

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

The `{}`-never-`None` rule matters to the authoring interface: every plugin example uses
`self.config.get(key, default)`, and that must work without a guard on the first line of every
`setup()`.

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
- Load order: config → discovery (`plugins_dir` comes from config) → manifests → resolution. So the
  parser cannot depend on knowing which plugins exist, and §3.2/§3.3 are specified accordingly (no
  cross-validation at parse time).

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
  `climbcv topics`. Offered as a suggestion, not a requirement.

---

## 8. Questions back to `plugins-and-config`

1. **Python 3.10 vs 3.11.** `tomllib` is stdlib only from **3.11**; the project floor is 3.10
   (`pyproject.toml: requires-python = ">=3.10"`). Both of us need a TOML parser — you for
   `climbcv.toml`, framework-core for `climbcv-plugin.toml`. Either raise the floor to 3.11 (zero
   dependencies) or add `tomli; python_version < "3.11"` (a dependency imposed on every plugin
   author's environment, since there is no install step). `loader.md` §3.3 recommends **raising the
   floor**; it changes a stated project assumption, so it needs a Decision Log entry either way.
   **We should use the same parser.**
2. Do you want to own the `[framework]` unknown-key warning (§3.1) and the dotted-key warning
   (§3.2/§3.3), or hand the raw dict over and let framework-core produce them? Either works; the
   warnings just need to exist exactly once.
3. Confirm §5 (values stay TOML primitives).
4. Confirm you can resolve `plugins_dir` / `log_dir` relative to the **config file's** directory
   (§3.1) — only the parser knows where the file was.
