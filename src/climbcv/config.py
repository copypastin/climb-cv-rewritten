"""`climbcv.toml` — one unified file, sectioned by plugin id.

design/config.md and design/config-contract.md. Decision #8: no schema/validation layer in
v1. Each plugin receives its **raw section dict, passed through byte-for-byte from tomllib
with zero post-processing** — which is how the picklability constraint is *guaranteed* rather
than checked, since plain TOML values are always picklable and a parser that built custom
objects would break the child-spawn path with an error pointing nowhere near the config.

Validation happens in **two passes, not one** (C-1): file-level at load, graph-level after
discovery. The closest-match warning for an unknown plugin id is structurally impossible in
one pass, because the load order is config -> discovery and `LoadedConfig` cannot carry a
warning about a plugin set it has not seen yet.
"""

from __future__ import annotations

import difflib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ConfigError",
    "LoadedConfig",
    "FRAMEWORK_DEFAULTS",
    "load_config",
    "check_against_plugins",
]


class ConfigError(Exception):
    """The config file was named explicitly and cannot be used. Fatal."""


FRAMEWORK_DEFAULTS: dict[str, Any] = {
    "log_level": "INFO",
    "log_dir": "logs",
    "state_dir": ".climbcv",
    "plugin_dir": "plugins",
    "use_bundled_plugins": True,
    "stream_depth": 0,          # 0 == auto: computed per subscriber, see broker.md §5.1.2
    "max_stream_depth": 256,
    "grace_s": 2.0,
    "restart_backoff_min_s": 0.5,
    "restart_backoff_max_s": 30.0,
    "quarantine_crashes": 5,
    "quarantine_window_s": 60.0,
    "setup_failure_max": 2,
}
"""Exported so `config.py` can warn on unknown keys and coerce types without duplicating the
table (C-3). The framework still applies the defaults; duplicating this list would drift, and
its first failure mode is a spurious warning about a perfectly valid key.

`stream_depth` defaults to **0 meaning auto**, never to a literal number (guardian S10): a
user who copies a documented default of `4` into their config pins the depth and silently
reintroduces the cross-topic starvation the computed value exists to prevent.
"""

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

_PATH_KEYS = frozenset({"log_dir", "state_dir", "plugin_dir"})


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """The result of pass one. `framework` is coerced and defaulted; `plugins` is raw."""

    framework: dict[str, Any]
    plugins: dict[str, dict[str, Any]]
    topics: dict[str, dict[str, Any]]
    source: str
    config_dir: str | None
    warnings: tuple[str, ...] = field(default=())

    def section(self, plugin_id: str) -> dict[str, Any]:
        """The raw section dict for one plugin. Absent section -> empty dict, never None."""
        return self.plugins.get(plugin_id, {})

    def enabled(self, plugin_id: str) -> bool | None:
        """Explicit enable/disable, or None if the config says nothing."""
        val = self.plugins.get(plugin_id, {}).get("enabled")
        return None if val is None else bool(val)


def _coerce_framework(raw: dict, source: str, warnings: list[str]) -> dict[str, Any]:
    out = dict(FRAMEWORK_DEFAULTS)
    for key, value in raw.items():
        if key not in FRAMEWORK_DEFAULTS:
            close = difflib.get_close_matches(key, sorted(FRAMEWORK_DEFAULTS), n=1, cutoff=0.6)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            warnings.append(
                f"{source} [framework]: unknown key {key!r}, ignored.{hint} "
                "A typo that silently does nothing is exactly what a drop-in tool cannot "
                "afford, so this is a warning rather than silence."
            )
            continue
        want = type(FRAMEWORK_DEFAULTS[key])
        if want is bool:
            if not isinstance(value, bool):
                warnings.append(
                    f"{source} [framework]: {key} must be true or false, got {value!r}; "
                    f"using the default {FRAMEWORK_DEFAULTS[key]!r}."
                )
                continue
        elif want is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                warnings.append(
                    f"{source} [framework]: {key} must be a number, got {value!r}; "
                    f"using the default {FRAMEWORK_DEFAULTS[key]!r}."
                )
                continue
            value = float(value)
        elif want is int:
            if isinstance(value, bool) or not isinstance(value, int):
                warnings.append(
                    f"{source} [framework]: {key} must be an integer, got {value!r}; "
                    f"using the default {FRAMEWORK_DEFAULTS[key]!r}."
                )
                continue
        elif want is str and not isinstance(value, str):
            warnings.append(
                f"{source} [framework]: {key} must be a string, got {value!r}; "
                f"using the default {FRAMEWORK_DEFAULTS[key]!r}."
            )
            continue
        out[key] = value

    # log_level is the one framework key with a closed value set, so a type check cannot
    # catch "DEBGU" -- it would fail later inside logging setup with no mention of the
    # config file at all (C-4).
    level = str(out["log_level"]).upper()
    if level not in _LOG_LEVELS:
        close = difflib.get_close_matches(level, sorted(_LOG_LEVELS), n=1, cutoff=0.5)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        warnings.append(
            f"{source} [framework]: log_level {out['log_level']!r} is not a level.{hint} "
            f"Valid: {sorted(_LOG_LEVELS)}. Using 'INFO'."
        )
        level = "INFO"
    out["log_level"] = level
    return out


def load_config(path: str | Path | None = "climbcv.toml") -> LoadedConfig:
    """Pass one. Two-step discovery, deliberately: an explicit path, then `./climbcv.toml`.

    No parent-directory walk, no user-global file, no env var — for a drop-in tool, "which
    config am I actually running" must be answerable by looking at one place.

    An explicitly named path that does not exist is **fatal**: the user asked for it by name.
    An absent `./climbcv.toml` is fine — every default is usable.

    `path=None` runs on defaults with no file at all. A dict is also accepted, for an
    embedding application that has no file to point at (guardian-02 finding 8).
    """
    warnings: list[str] = []

    if isinstance(path, dict):
        raw = path
        source = "<supplied by the embedding application>"
        config_dir = None
    elif path is None:
        raw = {}
        source = "<defaults: no config file requested>"
        config_dir = None
    else:
        p = Path(path)
        explicit = str(path) != "climbcv.toml"
        if not p.exists():
            if explicit:
                raise ConfigError(
                    f"Config file {p} was named explicitly but does not exist."
                )
            raw = {}
            source = "<defaults: no config file found>"
            config_dir = None
        else:
            try:
                raw = tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{p} is not valid TOML: {exc}") from None
            except OSError as exc:
                raise ConfigError(f"{p} cannot be read: {exc}") from None
            source = str(p)
            config_dir = str(p.resolve().parent)

    for key in raw:
        if key not in ("framework", "plugins", "topics"):
            close = difflib.get_close_matches(key, ["framework", "plugins", "topics"], n=1, cutoff=0.5)
            hint = f" Did you mean [{close[0]}]?" if close else ""
            warnings.append(f"{source}: unknown top-level table [{key}], ignored.{hint}")

    framework_raw = raw.get("framework") or {}
    if not isinstance(framework_raw, dict):
        raise ConfigError(f"{source}: [framework] must be a table")
    framework = _coerce_framework(framework_raw, source, warnings)

    plugins_raw = raw.get("plugins") or {}
    if not isinstance(plugins_raw, dict):
        raise ConfigError(f"{source}: [plugins] must be a table")
    plugins: dict[str, dict[str, Any]] = {}
    for pid, section in plugins_raw.items():
        if not isinstance(section, dict):
            warnings.append(
                f"{source}: [plugins.{pid}] must be a table, got {type(section).__name__}; "
                "ignored."
            )
            continue
        # Passed through byte-for-byte. No post-processing, which is what guarantees every
        # value is picklable for the child-spawn path rather than merely likely to be.
        plugins[pid] = section

    topics_raw = raw.get("topics") or {}
    if not isinstance(topics_raw, dict):
        raise ConfigError(f"{source}: [topics] must be a table")
    topics: dict[str, dict[str, Any]] = {}
    for name, section in topics_raw.items():
        if not isinstance(section, dict):
            warnings.append(f"{source}: [topics.{name}] must be a table; ignored.")
            continue
        topics[name] = section

    if config_dir is not None:
        for key in _PATH_KEYS:
            value = framework[key]
            if isinstance(value, str) and not Path(value).is_absolute():
                framework[key] = str(Path(config_dir) / value)

    return LoadedConfig(
        framework=framework,
        plugins=plugins,
        topics=topics,
        source=source,
        config_dir=config_dir,
        warnings=tuple(warnings),
    )


def check_against_plugins(
    cfg: LoadedConfig,
    plugin_ids: set[str],
    topic_names: set[str],
) -> list[str]:
    """Pass two — run after discovery. Returns warnings; never raises.

    This entry point exists because the authoritative version of these checks is
    structurally impossible in pass one (C-1): naming the closest matching plugin id
    requires the discovered plugin set, and the load order is config -> discovery.
    """
    out: list[str] = []
    for pid in cfg.plugins:
        if pid in plugin_ids:
            continue
        close = difflib.get_close_matches(pid, sorted(plugin_ids), n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        out.append(
            f"{cfg.source}: [plugins.{pid}] configures a plugin that was not found.{hint} "
            "The section is ignored. If you meant a dotted id, remember TOML needs quoting: "
            f'[plugins."{pid}"].'
        )
    for name in cfg.topics:
        if name in topic_names:
            continue
        close = difflib.get_close_matches(name, sorted(topic_names), n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        out.append(
            f"{cfg.source}: [topics.{name}] configures a topic no enabled plugin declares."
            f"{hint} The section is ignored."
        )
    return out
