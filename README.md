# climb-cv

Climbing motion analysis as a plugin architecture. Capture, pose estimation and smoothing are
plugins, not built-ins, and third parties write plugins against the same API the first-party
ones use.

This is a rewrite of [copypastin/climb-cv](https://github.com/copypastin/climb-cv), whose
pipeline was a single loop with optional worker processes bolted on. Here the framework is the
loop, and everything else — including the camera and the pose model — is a plugin.

## What works today

```bash
pip install -e '.[plugins]'
python3 examples/01_video_file.py --make-sample --loop     # overlay + live 3D plot
python3 examples/02_live_webcam.py                         # the same, on a camera
```

- **Framework**: topic broker, manifest/loader, process-per-plugin isolation, crash containment,
  the plugin API, and the `ClimbCV` embedding API. 212 tests, all passing in about 4 seconds.
- **First-party plugins**: `core.capture`, `core.pose_mediapipe`, `core.smooth_oneeuro`.
- **Examples**: five plugins including a live exo-skeleton overlay and a matplotlib 3D view.

Not yet ported from the original: YOLO hold detection, `.npy` session persistence and replay,
the macOS lid-angle sensor. Those are the next slice, and they are the ones that will exercise
shared topics and `latest_by_source()` for the first time.

## How a plugin works

Drop a folder into `plugins/`. No pip install, no entry points, no registration.

```python
from climbcv import Plugin, subscribe, every
from climbcv.contracts import Scalar

class Brightness(Plugin):
    def setup(self):                       # runs in THIS plugin's own process
        self._threshold = float(self.config.get("dark_below", 40.0))

    @subscribe("frame")
    def on_frame(self, frame, meta):
        self.publish("example.brightness", Scalar(value=..., t_ns=...))
```

Plus a `climbcv-plugin.toml` declaring what it publishes and subscribes to. The framework builds
the topic graph from manifests and **never imports plugin code in the supervisor** — discovering
your topics by importing you would mean running untrusted code in the one process whose survival
everything depends on.

[`examples/README.md`](examples/README.md) is the practical guide, including the traps: why
`__init__` is reserved, why payload arrays are read-only, and why a plugin that owns a window
must stash on the handler and draw on a tick.

## The architecture in one paragraph

Plugins publish and subscribe to **topics**. A topic is **exclusive** when its payload is a
singleton observation of a unique subject — two publishers would contradict each other, as with
one climber's skeleton — and **shared** when publishers merely add, as with hold boxes from two
detectors. That single distinction is what collapses "replace a core stage" and "add a new stage"
into the same mechanism: swapping the pose model is pointing an exclusive topic at a different
plugin, and nothing downstream knows. Every plugin runs in its own process, so a crash is
contained and no author writes a `Queue`.

## Layout

```
src/climbcv/          the framework, plus bundled first-party plugins
examples/             runnable examples and five example plugins
design/               section-by-section design docs and two API reviews
BRAINSTORM.md         the Decision Log — every architectural decision and what it rejected
tests/                212 tests; `pytest -m e2e` adds the slow pipeline test
```

`BRAINSTORM.md` and `design/` are worth reading before changing anything in `src/climbcv/`. The
design is not decoration: several of its constraints exist because a plausible-looking
alternative was measured and failed, and the documents say which.

## Status

The API is not stable yet. `PLUGIN_API_VERSION` is `1.0` and plugins declare the version they
need, but nothing has been published against it, so breaking changes are still cheap and will be
taken while that is true.
