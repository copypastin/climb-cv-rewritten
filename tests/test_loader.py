"""Tests for climbcv.config and climbcv.loader — design/config.md, loader.md §2/§5."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.config import (  # noqa: E402
    FRAMEWORK_DEFAULTS,
    ConfigError,
    check_against_plugins,
    load_config,
)
from climbcv.loader import LoaderError, discover  # noqa: E402
from climbcv.manifest import MANIFEST_FILENAME  # noqa: E402


def plugin_dir(root: Path, pid: str, *, api="1.0", platforms=None, entry="plugin:P",
               module="plugin", body_extra="") -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    plat = f"platforms = {platforms!r}\n" if platforms else ""
    (d / MANIFEST_FILENAME).write_text(
        f'[plugin]\nid = "{pid}"\nversion = "1.0.0"\napi_version = "{api}"\n'
        f'entry = "{entry}"\nname = "N"\ndescription = "D"\nauthor = "A"\n{plat}'
        f'{body_extra}\n[[publishes]]\ntopic = "holds.boxes"\n',
        encoding="utf-8",
    )
    if module:
        (d / f"{module}.py").write_text("class P: pass\n", encoding="utf-8")
    return d


def cfg_for(tmp_path: Path, toml: str = "", *, plugin_dir_name="plugins"):
    p = tmp_path / "climbcv.toml"
    p.write_text(
        f'[framework]\nplugin_dir = "{plugin_dir_name}"\n{toml}', encoding="utf-8"
    )
    return load_config(p)


# ---------------------------------------------------------------- config: discovery


def test_absent_default_config_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.framework["log_level"] == "INFO"
    assert "no config file found" in cfg.source


def test_explicitly_named_missing_config_is_fatal(tmp_path):
    """The user asked for it by name; silently running on defaults would be worse."""
    with pytest.raises(ConfigError, match="named explicitly"):
        load_config(tmp_path / "nope.toml")


def test_none_means_defaults_with_no_file(tmp_path):
    assert load_config(None).framework == FRAMEWORK_DEFAULTS


def test_dict_config_is_accepted_for_embedders(tmp_path):
    """Guardian-02 finding 8: an embedding app has no TOML file to point at, and telling its
    end user to hand-author one before the app starts is absurd."""
    cfg = load_config({"framework": {"log_level": "DEBUG"}, "plugins": {"x": {"a": 1}}})
    assert cfg.framework["log_level"] == "DEBUG"
    assert cfg.section("x") == {"a": 1}
    assert "embedding application" in cfg.source
    assert cfg.config_dir is None


def test_malformed_config_is_fatal(tmp_path):
    p = tmp_path / "climbcv.toml"
    p.write_text("[framework\nx =", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(p)


# ---------------------------------------------------------------- config: framework keys


def test_unknown_framework_key_warns_with_hint(tmp_path):
    cfg = cfg_for(tmp_path, 'log_lvl = "DEBUG"\n')
    assert any("log_lvl" in w and "log_level" in w for w in cfg.warnings)
    assert cfg.framework["log_level"] == "INFO", "the typo must not take effect"


def test_bad_log_level_warns_and_falls_back(tmp_path):
    """C-4: log_level is the one framework key with a closed value set, so a type check
    cannot catch 'DEBGU' — it would fail inside logging setup with no mention of the config."""
    cfg = cfg_for(tmp_path, 'log_level = "DEBGU"\n')
    assert cfg.framework["log_level"] == "INFO"
    assert any("DEBUG" in w for w in cfg.warnings)


def test_wrong_type_warns_and_keeps_default(tmp_path):
    cfg = cfg_for(tmp_path, "quarantine_crashes = \"five\"\n")
    assert cfg.framework["quarantine_crashes"] == FRAMEWORK_DEFAULTS["quarantine_crashes"]
    assert any("quarantine_crashes" in w for w in cfg.warnings)


def test_stream_depth_default_is_auto_not_a_literal(tmp_path):
    """Guardian S10: documenting a literal default of 4 means a user who copies it into their
    config pins the depth and silently reintroduces cross-topic starvation."""
    assert FRAMEWORK_DEFAULTS["stream_depth"] == 0


def test_path_keys_resolve_against_the_config_file_directory(tmp_path):
    cfg = cfg_for(tmp_path, 'log_dir = "mylogs"\n')
    assert Path(cfg.framework["log_dir"]) == tmp_path / "mylogs"
    assert cfg.config_dir == str(tmp_path.resolve())


def test_absolute_path_keys_are_left_alone(tmp_path):
    cfg = cfg_for(tmp_path, f'log_dir = "{tmp_path / "abs"}"\n')
    assert Path(cfg.framework["log_dir"]) == tmp_path / "abs"


# ---------------------------------------------------------------- config: plugin sections


def test_plugin_section_is_passed_through_untouched(tmp_path):
    """Decision #8's operational meaning: byte-for-byte from tomllib, no post-processing.
    That is what *guarantees* picklability for the child-spawn path."""
    cfg = cfg_for(tmp_path, '[plugins.yolo_holds]\nevery_n_frames = 4\nnested = { a = 1 }\n')
    assert cfg.section("yolo_holds") == {"every_n_frames": 4, "nested": {"a": 1}}


def test_absent_plugin_section_is_an_empty_dict_not_none(tmp_path):
    assert cfg_for(tmp_path).section("nobody") == {}


def test_enabled_tri_state(tmp_path):
    cfg = cfg_for(tmp_path, "[plugins.a]\nenabled = false\n[plugins.b]\nenabled = true\n")
    assert cfg.enabled("a") is False
    assert cfg.enabled("b") is True
    assert cfg.enabled("c") is None, "config saying nothing must be distinguishable"


# --------------------------------------------- config: pass two (C-1)


def test_check_against_plugins_names_the_closest_id(tmp_path):
    """Structurally impossible in pass one: naming the nearest match needs the discovered
    plugin set, and the load order is config -> discovery."""
    cfg = cfg_for(tmp_path, "[plugins.yolo_hold]\nx = 1\n")
    out = check_against_plugins(cfg, {"yolo_holds"}, set())
    assert len(out) == 1 and "yolo_holds" in out[0]


def test_check_against_plugins_mentions_toml_quoting_for_dotted_ids(tmp_path):
    cfg = cfg_for(tmp_path, '[plugins."core.captur"]\nx = 1\n')
    out = check_against_plugins(cfg, {"core.capture"}, set())
    assert "quoting" in out[0]


def test_check_against_plugins_silent_when_all_known(tmp_path):
    cfg = cfg_for(tmp_path, "[plugins.a]\nx = 1\n")
    assert check_against_plugins(cfg, {"a"}, set()) == []


# ---------------------------------------------------------------- loader: discovery


def test_discovers_a_plugin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "yolo_holds")
    d = discover(cfg_for(tmp_path))
    assert [m.id for m in d.plugins] == ["yolo_holds"]
    assert d.skipped == ()


def test_missing_manifest_is_reported_not_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plugins" / "junk").mkdir(parents=True)
    d = discover(cfg_for(tmp_path))
    assert d.plugins == ()
    assert any("no climbcv-plugin.toml" in s.reason for s in d.skipped)


def test_dot_and_underscore_dirs_are_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plugins" / ".git").mkdir(parents=True)
    (tmp_path / "plugins" / "__pycache__").mkdir(parents=True)
    d = discover(cfg_for(tmp_path))
    assert d.plugins == () and d.skipped == ()


def test_missing_entry_module_caught_at_discovery_listing_what_exists(tmp_path, monkeypatch):
    """A filename check costs nothing here; discovering it in the child costs two spawn
    attempts and reads to the user as a crash."""
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", entry="detector:P", module="plugin")
    d = discover(cfg_for(tmp_path))
    assert d.plugins == ()
    assert any("plugin.py" in s.reason for s in d.skipped)


def test_bad_manifest_skips_only_that_plugin(tmp_path, monkeypatch):
    """Decision #5: one bad plugin must not take down the app."""
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "good")
    bad = tmp_path / "plugins" / "bad"
    bad.mkdir(parents=True)
    (bad / MANIFEST_FILENAME).write_text("[plugin]\nid = 'bad'\n", encoding="utf-8")
    d = discover(cfg_for(tmp_path))
    assert [m.id for m in d.plugins] == ["good"]
    assert any(s.identifier.endswith("bad") for s in d.skipped)


# ---------------------------------------------------------------- loader: two roots (F-3)


def test_user_root_shadows_bundled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "bundled", "yolo_holds")
    plugin_dir(tmp_path / "plugins", "yolo_holds")
    d = discover(cfg_for(tmp_path), bundled_root=tmp_path / "bundled")
    assert len(d.plugins) == 1
    assert d.plugins[0].root == "user", "the user's copy must win"
    assert ("yolo_holds", "bundled") in d.shadowed


def test_bundled_plugins_load_when_user_root_is_empty(tmp_path, monkeypatch):
    """Without this, pip install + run gives no overlay and no hold detection."""
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "bundled", "yolo_holds")
    d = discover(cfg_for(tmp_path), bundled_root=tmp_path / "bundled")
    assert [m.id for m in d.plugins] == ["yolo_holds"]
    assert d.plugins[0].root == "bundled"


def test_use_bundled_plugins_false_skips_the_bundled_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "bundled", "yolo_holds")
    d = discover(cfg_for(tmp_path, "use_bundled_plugins = false\n"),
                 bundled_root=tmp_path / "bundled")
    assert d.plugins == ()


def test_duplicate_id_within_one_root_is_fatal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "a")
    # A second directory declaring the same id — what two copies of a template produce.
    other = tmp_path / "plugins" / "b"
    other.mkdir(parents=True)
    (other / MANIFEST_FILENAME).write_text(
        '[plugin]\nid = "a"\nversion = "1.0.0"\napi_version = "1.0"\nentry = "plugin:P"\n'
        'name = "N"\ndescription = "D"\nauthor = "A"\n[[publishes]]\ntopic = "frame"\n',
        encoding="utf-8",
    )
    (other / "plugin.py").write_text("class P: pass\n", encoding="utf-8")
    with pytest.raises(LoaderError, match="both declare id"):
        discover(cfg_for(tmp_path))


# ---------------------------------------------------------------- loader: filters


def test_disabled_in_config_is_skipped_at_info_not_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p")
    d = discover(cfg_for(tmp_path, "[plugins.p]\nenabled = false\n"))
    assert d.plugins == ()
    assert d.skipped[0].level == "INFO", "a deliberate choice is not a failure"


def test_platform_mismatch_is_info_not_error(tmp_path, monkeypatch):
    """The mac lid sensor on Linux: the user has done nothing wrong."""
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", platforms=["nonesuch"])
    d = discover(cfg_for(tmp_path))
    assert d.plugins == ()
    assert d.skipped[0].level == "INFO"


def test_platform_match_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", platforms=[sys.platform])
    assert len(discover(cfg_for(tmp_path)).plugins) == 1


def test_explicitly_enabled_but_wrong_platform_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", platforms=["nonesuch"])
    d = discover(cfg_for(tmp_path, "[plugins.p]\nenabled = true\n"))
    assert d.plugins == ()
    assert "cannot run here" in d.skipped[0].reason


def test_unrecognised_platform_tag_warns_but_does_not_disable(tmp_path, monkeypatch):
    """Guardian's note: a framework-owned platform enumeration means every plugin declaring
    platforms is skipped on FreeBSD while every plugin omitting it runs."""
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", platforms=["plan9", sys.platform])
    d = discover(cfg_for(tmp_path))
    assert len(d.plugins) == 1
    assert any("plan9" in w for w in d.warnings)


# ---------------------------------------------------------------- loader: api_version


def test_future_minor_is_skipped_with_an_upgrade_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", api="1.99")
    d = discover(cfg_for(tmp_path))
    assert d.plugins == ()
    assert "Upgrade climb-cv" in d.skipped[0].reason


def test_wrong_major_is_skipped_pointing_at_the_author(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", api="2.0")
    d = discover(cfg_for(tmp_path))
    assert d.plugins == ()
    assert "needs updating by its author" in d.skipped[0].reason


def test_older_minor_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", api="1.0")
    assert len(discover(cfg_for(tmp_path)).plugins) == 1


def test_manifest_warnings_propagate_to_discovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin_dir(tmp_path / "plugins", "p", body_extra='platform = ["darwin"]')
    d = discover(cfg_for(tmp_path))
    assert any("platform" in w for w in d.warnings)
