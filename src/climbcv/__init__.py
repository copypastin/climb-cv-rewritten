"""climb-cv — plugin-based climbing motion analysis.

Design documents live in `design/`; `BRAINSTORM.md` carries the Decision Log.

Deliberately thin: importing this package must not pull in numpy-heavy or GUI machinery,
because under `spawn` every child process re-imports it. Payload contracts are in
`climbcv.contracts`, which imports dataclasses/numpy/typing and nothing else.
"""

from .plugin import Plugin, PluginContractError, every, subscribe

__all__ = [
    "Plugin",
    "PluginContractError",
    "subscribe",
    "every",
    "__version__",
    "PLUGIN_API_VERSION",
]

__version__ = "0.1.0.dev0"

# `from climbcv import Plugin` is the first line of every plugin ever written, so it has to
# work. Re-exporting is free here: climbcv.plugin imports logging and typing only — no numpy,
# no cv2 — so the thinness rule this module exists to enforce is not weakened by it. (An
# earlier version omitted these and the first fixture plugin failed with
# "cannot import name 'Plugin' from 'climbcv'. Did you mean: 'plugin'?", which is a miserable
# first five minutes for a third-party author.)

PLUGIN_API_VERSION = "1.0"
"""The plugin API version this framework provides.

A plugin manifest declares `api_version = "MAJOR.MINOR"` and is loaded when MAJOR matches
and the plugin's MINOR is <= ours. See design/loader.md §4.
"""
