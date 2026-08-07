"""Manifest parsing — `climbcv-plugin.toml`.

design/loader.md §3–§4. The manifest is the single source of truth for the topic graph,
because **the host must never import plugin code**: discovering what a plugin publishes and
subscribes to by importing it would mean running untrusted code in the supervisor process,
which is the one process whose survival everything depends on.

Every error here is read by a third-party author with no install step and no registry, so
the message is the primary documentation. Say which field, which file, and what was expected.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .topics import PAYLOAD_NAMES, Exclusivity, Kind, validate_topic_name

__all__ = [
    "ManifestError",
    "Publish",
    "Subscribe",
    "Manifest",
    "parse_manifest",
    "MANIFEST_FILENAME",
]

MANIFEST_FILENAME = "climbcv-plugin.toml"

ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
ID_MAX_LEN = 64
RESERVED_ID_PREFIXES = ("core.", "climbcv.")
RESERVED_IDS = frozenset({"<host>"})

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
API_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")

_PLUGIN_KEYS = frozenset({
    "id", "version", "api_version", "entry", "name", "description", "author",
    "platforms", "requires", "provides_topology", "requires_topology",
    "teardown_timeout_s", "heartbeat_warn_s",
})
_PUBLISH_KEYS = frozenset({
    "topic", "kind", "exclusivity", "payload", "unit", "record_kind", "doc",
})
_SUBSCRIBE_KEYS = frozenset({"topic", "required", "conflate", "depth", "mode"})

TEARDOWN_TIMEOUT_CAP = 30.0
HEARTBEAT_WARN_CAP = 120.0


class ManifestError(Exception):
    """A manifest is malformed. The plugin is skipped; the app keeps running."""


@dataclass(frozen=True, slots=True)
class Publish:
    topic: str
    kind: Kind | None = None            # required for non-standard topics
    exclusivity: Exclusivity | None = None
    payload: str | None = None          # PAYLOAD_NAMES key
    unit: str | None = None
    record_kind: str | None = None
    doc: str = ""


@dataclass(frozen=True, slots=True)
class Subscribe:
    topic: str
    required: bool = True
    conflate: bool = True
    depth: int = 0                      # only meaningful when conflate=False; 0 -> 64
    mode: str = "handler"               # "handler" | "latest"


@dataclass(frozen=True, slots=True)
class Manifest:
    id: str
    version: str
    api_version: tuple[int, int]
    entry: str
    name: str
    description: str
    author: str
    directory: Path
    root: str                           # "user" | "bundled"
    platforms: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    provides_topology: tuple[str, ...] = ()   # list form, per guardian-02 finding 3
    requires_topology: str | None = None
    teardown_timeout_s: float = 1.0
    heartbeat_warn_s: float = 5.0
    publishes: tuple[Publish, ...] = ()
    subscribes: tuple[Subscribe, ...] = ()
    config_keys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())

    @property
    def module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def class_name(self) -> str:
        return self.entry.split(":", 1)[1]


def _unknown_key_warnings(
    got: dict, known: frozenset[str], where: str
) -> list[str]:
    """Unknown keys WARN with a nearest match; they never fail the load.

    Silently ignoring them is what the framework cannot afford (guardian-02 finding 10): a
    typo'd `conflat = false` reinstates the silent frame loss that F-14 was raised to fix,
    and `platform = ["darwin"]` reinstates the crash-loop the platform skip exists to
    prevent. WARNING and not fatal, deliberately — it is also the forward-compatibility
    rule: a plugin authored against a later API minor should be skipped by `api_version`,
    not killed by a key this version has never heard of.
    """
    out = []
    for key in got:
        if key in known:
            continue
        close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        out.append(f"{where}: unknown key {key!r}, ignored.{hint}")
    return out


def _require_str(table: dict, key: str, where: str) -> str:
    val = table.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ManifestError(f"{where}: {key!r} is required and must be a non-empty string")
    return val


def _validate_id(pid: str, where: str) -> None:
    if len(pid) > ID_MAX_LEN:
        raise ManifestError(f"{where}: id {pid!r} is over {ID_MAX_LEN} chars")
    if not ID_RE.match(pid):
        raise ManifestError(
            f"{where}: id {pid!r} is not a valid id. Grammar: lowercase letters, digits "
            "and underscores, dot-separated segments each starting with a letter — "
            "e.g. 'yolo_holds' or 'acme.grip'."
        )
    if pid in RESERVED_IDS:
        raise ManifestError(f"{where}: id {pid!r} is reserved by the framework")
    for prefix in RESERVED_ID_PREFIXES:
        if pid.startswith(prefix):
            raise ManifestError(
                f"{where}: id {pid!r} uses the reserved prefix {prefix!r}. "
                "Those belong to plugins shipped with climb-cv."
            )


def _parse_publish(raw: dict, where: str, warnings: list[str]) -> Publish:
    warnings.extend(_unknown_key_warnings(raw, _PUBLISH_KEYS, where))
    topic = _require_str(raw, "topic", where)
    try:
        validate_topic_name(topic, where)
    except ValueError as exc:
        raise ManifestError(str(exc)) from None

    payload_name = raw.get("payload")
    if payload_name is not None:
        if payload_name not in PAYLOAD_NAMES:
            raise ManifestError(
                f"{where}: payload {payload_name!r} is not a contract type. "
                f"Known: {sorted(PAYLOAD_NAMES)}. Use 'record' to carry a shape the "
                "framework has no type for."
            )

    kind = raw.get("kind")
    exclusivity = raw.get("exclusivity")
    try:
        kind_enum = Kind(kind) if kind is not None else None
        excl_enum = Exclusivity(exclusivity) if exclusivity is not None else None
    except ValueError:
        raise ManifestError(
            f"{where}: kind must be 'stream' or 'event' and exclusivity must be "
            f"'exclusive' or 'shared'; got kind={kind!r}, exclusivity={exclusivity!r}"
        ) from None

    unit = raw.get("unit")
    record_kind = raw.get("record_kind")

    # unit and record_kind are required for their payload types and forbidden otherwise.
    # Same rule, same reasoning, one level apart: `schema` cannot distinguish two scalar
    # topics measuring different units, and it is "record/1" for every record topic in the
    # ecosystem so it cannot distinguish two incompatible data layouts either.
    if payload_name == "scalar" and not unit:
        raise ManifestError(
            f"{where}: unit is required when payload = \"scalar\" — otherwise two "
            "publishers can declare the same topic in newtons and kgf, agree on every "
            "checked field, wire successfully, and disagree by a factor of 9.8 forever."
        )
    if payload_name == "record" and not record_kind:
        raise ManifestError(
            f"{where}: record_kind is required when payload = \"record\", in the form "
            "\"<name>/<major>\" e.g. \"acme.hand_state/1\". schema is \"record/1\" for "
            "every record topic, so record_kind is the only thing that can tell two "
            "incompatible data layouts apart."
        )
    if unit and payload_name != "scalar":
        raise ManifestError(
            f"{where}: unit applies only to payload = \"scalar\", not {payload_name!r}"
        )
    if record_kind and payload_name != "record":
        raise ManifestError(
            f"{where}: record_kind applies only to payload = \"record\", "
            f"not {payload_name!r}"
        )

    return Publish(
        topic=topic, kind=kind_enum, exclusivity=excl_enum, payload=payload_name,
        unit=unit, record_kind=record_kind, doc=raw.get("doc", ""),
    )


def _parse_subscribe(raw: dict, where: str, warnings: list[str]) -> Subscribe:
    warnings.extend(_unknown_key_warnings(raw, _SUBSCRIBE_KEYS, where))
    topic = _require_str(raw, "topic", where)
    try:
        validate_topic_name(topic, where)
    except ValueError as exc:
        raise ManifestError(str(exc)) from None

    mode = raw.get("mode", "handler")
    if mode not in ("handler", "latest"):
        raise ManifestError(
            f"{where}: mode must be 'handler' or 'latest', got {mode!r}. "
            "'latest' declares 'I read this with latest(), not with a handler.'"
        )
    depth = raw.get("depth", 0)
    if not isinstance(depth, int) or depth < 0:
        raise ManifestError(f"{where}: depth must be a non-negative integer, got {depth!r}")

    return Subscribe(
        topic=topic,
        required=bool(raw.get("required", True)),
        conflate=bool(raw.get("conflate", True)),
        depth=depth,
        mode=mode,
    )


def _clamp(
    value: object, default: float, cap: float, key: str, where: str, warnings: list[str]
) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ManifestError(f"{where}: {key} must be a positive number, got {value!r}")
    if value > cap:
        warnings.append(
            f"{where}: {key} = {value} exceeds the cap of {cap}; using {cap}."
        )
        return cap
    return float(value)


def parse_manifest(path: Path, root: str) -> Manifest:
    """Parse one `climbcv-plugin.toml`. Raises ManifestError; never raises anything else.

    `root` is "user" or "bundled" — a user plugin shadows a bundled one with the same id.
    """
    where = str(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"{where}: cannot be read ({exc})") from None
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{where}: is not valid TOML ({exc})") from None

    plugin = raw.get("plugin")
    if not isinstance(plugin, dict):
        raise ManifestError(
            f"{where}: missing the [plugin] table. A manifest needs at least "
            "[plugin] with id, version, api_version, entry, name, description, author."
        )

    warnings: list[str] = []
    warnings.extend(_unknown_key_warnings(plugin, _PLUGIN_KEYS, f"{where} [plugin]"))
    warnings.extend(
        _unknown_key_warnings(raw, frozenset({"plugin", "publishes", "subscribes", "config"}), where)
    )

    pid = _require_str(plugin, "id", f"{where} [plugin]")
    _validate_id(pid, f"{where} [plugin]")

    version = _require_str(plugin, "version", f"{where} [plugin]")
    if not SEMVER_RE.match(version):
        # Validated now because it is free now: authors write semver anyway, and any future
        # registry or update check needs to compare these. Tightening later is breaking.
        raise ManifestError(
            f"{where} [plugin]: version {version!r} is not semver "
            "(MAJOR.MINOR.PATCH[-prerelease][+build]), e.g. '1.2.0' or '0.3.1-beta'."
        )

    api_raw = _require_str(plugin, "api_version", f"{where} [plugin]")
    m = API_VERSION_RE.match(api_raw)
    if not m:
        raise ManifestError(
            f"{where} [plugin]: api_version {api_raw!r} must be \"MAJOR.MINOR\", e.g. \"1.0\"."
        )
    api_version = (int(m.group(1)), int(m.group(2)))

    entry = _require_str(plugin, "entry", f"{where} [plugin]")
    if entry.count(":") != 1 or not all(part.strip() for part in entry.split(":")):
        raise ManifestError(
            f"{where} [plugin]: entry {entry!r} must be \"<module>:<ClassName>\", "
            "e.g. \"plugin:HoldDetector\"."
        )

    platforms = plugin.get("platforms", [])
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise ManifestError(f"{where} [plugin]: platforms must be a list of strings")

    requires = plugin.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        raise ManifestError(f"{where} [plugin]: requires must be a list of strings")

    # provides_topology accepts a list (guardian-02 finding 3): a topology-agnostic
    # transform stage -- core.smooth_oneeuro filters columns and indexes no joints -- has no
    # single honest value to declare, and forcing one quarantines it the moment a
    # third-party pose plugin provides a different topology.
    prov = plugin.get("provides_topology")
    if prov is None:
        provides_topology: tuple[str, ...] = ()
    elif isinstance(prov, str):
        provides_topology = (prov,)
    elif isinstance(prov, list) and all(isinstance(p, str) for p in prov):
        provides_topology = tuple(prov)
    else:
        raise ManifestError(
            f"{where} [plugin]: provides_topology must be a topology id or a list of them"
        )

    req_topo = plugin.get("requires_topology")
    if req_topo is not None and not isinstance(req_topo, str):
        raise ManifestError(
            f"{where} [plugin]: requires_topology must be a topology id, or \"any\" to opt out"
        )

    publishes: list[Publish] = []
    for i, item in enumerate(raw.get("publishes", []) or []):
        if not isinstance(item, dict):
            raise ManifestError(f"{where}: [[publishes]] #{i} is not a table")
        publishes.append(_parse_publish(item, f"{where} [[publishes]] #{i}", warnings))

    subscribes: list[Subscribe] = []
    for i, item in enumerate(raw.get("subscribes", []) or []):
        if not isinstance(item, dict):
            raise ManifestError(f"{where}: [[subscribes]] #{i} is not a table")
        subscribes.append(_parse_subscribe(item, f"{where} [[subscribes]] #{i}", warnings))

    if not publishes and not subscribes:
        warnings.append(
            f"{where}: declares no [[publishes]] and no [[subscribes]], so it can neither "
            "receive nor produce anything. Legal, but it will do nothing."
        )

    dup_pub = _first_duplicate(p.topic for p in publishes)
    if dup_pub:
        raise ManifestError(f"{where}: topic {dup_pub!r} is declared twice in [[publishes]]")
    dup_sub = _first_duplicate(s.topic for s in subscribes)
    if dup_sub:
        raise ManifestError(f"{where}: topic {dup_sub!r} is declared twice in [[subscribes]]")

    config_table = raw.get("config") or {}
    if not isinstance(config_table, dict):
        raise ManifestError(f"{where}: [config] must be a table")
    warnings.extend(_unknown_key_warnings(config_table, frozenset({"keys"}), f"{where} [config]"))
    config_keys = tuple(config_table.get("keys", []) or ())
    if not all(isinstance(k, str) for k in config_keys):
        raise ManifestError(f"{where} [config]: keys must be a list of strings")

    if pid != path.parent.name:
        warnings.append(
            f"{where}: id {pid!r} does not match the directory name "
            f"{path.parent.name!r}. Legal — renaming a folder should not break a config — "
            "but worth knowing when you are looking for this plugin's section."
        )

    return Manifest(
        id=pid,
        version=version,
        api_version=api_version,
        entry=entry,
        name=_require_str(plugin, "name", f"{where} [plugin]"),
        description=_require_str(plugin, "description", f"{where} [plugin]"),
        author=_require_str(plugin, "author", f"{where} [plugin]"),
        directory=path.parent,
        root=root,
        platforms=tuple(platforms),
        requires=tuple(requires),
        provides_topology=provides_topology,
        requires_topology=req_topo,
        teardown_timeout_s=_clamp(
            plugin.get("teardown_timeout_s"), 1.0, TEARDOWN_TIMEOUT_CAP,
            "teardown_timeout_s", f"{where} [plugin]", warnings,
        ),
        heartbeat_warn_s=_clamp(
            plugin.get("heartbeat_warn_s"), 5.0, HEARTBEAT_WARN_CAP,
            "heartbeat_warn_s", f"{where} [plugin]", warnings,
        ),
        publishes=tuple(publishes),
        subscribes=tuple(subscribes),
        config_keys=config_keys,
        warnings=tuple(warnings),
    )


def _first_duplicate(items) -> str | None:
    seen = set()
    for item in items:
        if item in seen:
            return item
        seen.add(item)
    return None
