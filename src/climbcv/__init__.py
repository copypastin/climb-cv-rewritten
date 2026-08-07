"""climb-cv — plugin-based climbing motion analysis.

Design documents live in `design/`; `BRAINSTORM.md` carries the Decision Log.

Deliberately thin: importing this package must not pull in numpy-heavy or GUI machinery,
because under `spawn` every child process re-imports it. Payload contracts are in
`climbcv.contracts`, which imports dataclasses/numpy/typing and nothing else.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"

PLUGIN_API_VERSION = "1.0"
"""The plugin API version this framework provides.

A plugin manifest declares `api_version = "MAJOR.MINOR"` and is loaded when MAJOR matches
and the plugin's MINOR is <= ours. See design/loader.md §4.
"""
