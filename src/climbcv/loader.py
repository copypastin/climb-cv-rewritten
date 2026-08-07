"""Plugin discovery — scanning roots, resolving ids, applying enable/disable.

design/loader.md §2, §5. Two roots (F-3): the **bundled** root shipped with the
distribution, and the **user** root (`plugins/`) which shadows it by id. Without the bundled
root, `pip install climb-cv` and run gives no overlay and no hold detection, because the
first-party features are plugins now — four currently-default features would silently become
opt-in.

Duplicate id is **fatal within a root** and **shadowing across roots**, with an INFO line.

The host never imports plugin code. Everything here is filesystem and TOML only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import PLUGIN_API_VERSION
from .config import LoadedConfig
from .manifest import MANIFEST_FILENAME, Manifest, ManifestError, parse_manifest

__all__ = ["Skipped", "Discovery", "discover", "LoaderError"]


class LoaderError(Exception):
    """Discovery cannot proceed. Fatal."""


@dataclass(frozen=True, slots=True)
class Skipped:
    """A plugin that was found but will not run, and why. Never silent."""

    identifier: str        # plugin id if known, else the directory path
    reason: str
    level: str             # "INFO" | "ERROR" — a platform skip is not a failure


@dataclass(frozen=True, slots=True)
class Discovery:
    plugins: tuple[Manifest, ...]
    skipped: tuple[Skipped, ...]
    warnings: tuple[str, ...]
    shadowed: tuple[tuple[str, str], ...] = ()   # (id, shadowed_root)


def _api_compatible(declared: tuple[int, int]) -> tuple[bool, str]:
    """Same major, and the plugin's minor must not exceed ours.

    `api_version = "1.3"` means "needs at least API 1.3, same major" — so a plugin asking
    for a feature this framework does not have yet is skipped with an explanation rather
    than failing later on a missing attribute.
    """
    ours = tuple(int(p) for p in PLUGIN_API_VERSION.split("."))
    if declared[0] != ours[0]:
        return False, (
            f"needs plugin API {declared[0]}.x but this climb-cv provides "
            f"{PLUGIN_API_VERSION}. A major difference means the plugin base class or the "
            "payload types changed; the plugin needs updating by its author."
        )
    if declared[1] > ours[1]:
        return False, (
            f"needs plugin API {declared[0]}.{declared[1]} but this climb-cv provides "
            f"{PLUGIN_API_VERSION}. Upgrade climb-cv, or use an older version of the plugin."
        )
    return True, ""


def _platform_tags() -> set[str]:
    """`sys.platform` and `sys.platform-machine`, for `platforms` matching."""
    import platform as _platform

    return {sys.platform, f"{sys.platform}-{_platform.machine()}"}


def _scan_root(root_dir: Path, root_name: str) -> tuple[list[Manifest], list[Skipped]]:
    found: list[Manifest] = []
    skipped: list[Skipped] = []
    if not root_dir.is_dir():
        return found, skipped

    for entry in sorted(root_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            skipped.append(
                Skipped(
                    str(entry),
                    f"no {MANIFEST_FILENAME}. Every plugin directory needs one; a folder "
                    "without it is ignored rather than guessed at.",
                    "ERROR",
                )
            )
            continue
        try:
            manifest = parse_manifest(manifest_path, root=root_name)
        except ManifestError as exc:
            skipped.append(Skipped(str(entry), str(exc), "ERROR"))
            continue

        # The module file's existence is a pure filename check, so it belongs at discovery
        # rather than costing two spawn attempts and reading to the user as a crash.
        module_rel = Path(*manifest.module.split(".")).with_suffix(".py")
        if not (entry / module_rel).is_file():
            present = sorted(p.name for p in entry.glob("*.py"))
            skipped.append(
                Skipped(
                    manifest.id,
                    f"entry names module {manifest.module!r} but "
                    f"{entry / module_rel} does not exist. Python files present: "
                    f"{present or 'none'}.",
                    "ERROR",
                )
            )
            continue

        found.append(manifest)
    return found, skipped


def discover(
    cfg: LoadedConfig,
    *,
    bundled_root: Path | None = None,
) -> Discovery:
    """Scan both roots, resolve shadowing, apply enable/disable and platform filters."""
    warnings: list[str] = []
    skipped: list[Skipped] = []
    shadowed: list[tuple[str, str]] = []

    user_root = Path(cfg.framework["plugin_dir"])
    roots: list[tuple[Path, str]] = []
    if cfg.framework.get("use_bundled_plugins", True) and bundled_root is not None:
        roots.append((bundled_root, "bundled"))
    roots.append((user_root, "user"))

    # Later roots win, so `user` shadows `bundled`.
    by_id: dict[str, Manifest] = {}
    for root_dir, root_name in roots:
        found, root_skipped = _scan_root(root_dir, root_name)
        skipped.extend(root_skipped)

        seen_in_root: dict[str, Manifest] = {}
        for manifest in found:
            if manifest.id in seen_in_root:
                other = seen_in_root[manifest.id]
                raise LoaderError(
                    f"Two plugins in the {root_name} root both declare id "
                    f"{manifest.id!r}: {other.directory} and {manifest.directory}. "
                    "Ids must be unique — rename one, or remove it. (If you copied a "
                    "template, its id needs changing too.)"
                )
            seen_in_root[manifest.id] = manifest

        for manifest in found:
            if manifest.id in by_id:
                shadowed.append((manifest.id, by_id[manifest.id].root))
            by_id[manifest.id] = manifest

    tags = _platform_tags()
    enabled: list[Manifest] = []
    for manifest in by_id.values():
        warnings.extend(manifest.warnings)

        ok, why = _api_compatible(manifest.api_version)
        if not ok:
            skipped.append(Skipped(manifest.id, why, "ERROR"))
            continue

        explicit = cfg.enabled(manifest.id)
        if explicit is False:
            skipped.append(
                Skipped(manifest.id, "disabled in climbcv.toml (enabled = false).", "INFO")
            )
            continue

        if manifest.platforms and not (set(manifest.platforms) & tags):
            reason = (
                f"declares platforms {list(manifest.platforms)}; this machine is "
                f"{sorted(tags)}."
            )
            if explicit is True:
                # The user asked for it by name, so say so plainly rather than skipping
                # quietly -- but still skip, because running it would crash.
                skipped.append(
                    Skipped(
                        manifest.id,
                        reason + " It is enabled in your config, but cannot run here.",
                        "INFO",
                    )
                )
            else:
                skipped.append(Skipped(manifest.id, reason, "INFO"))
            continue

        for tag in manifest.platforms:
            if "-" not in tag and tag not in {
                "darwin", "linux", "win32", "cygwin", "freebsd", "aix",
            }:
                warnings.append(
                    f"{manifest.id}: platforms tag {tag!r} is not a sys.platform value "
                    "this framework recognises. Accepted anyway — an unrecognised platform "
                    "should not make a plugin unloadable on the platform it does name."
                )

        enabled.append(manifest)

    return Discovery(
        plugins=tuple(enabled),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
        shadowed=tuple(shadowed),
    )
