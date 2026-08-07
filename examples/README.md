# climb-cv examples

Four things demonstrated here: using climb-cv as a package, writing a plugin, reading a video
file, and running against a live camera.

```
examples/
  01_video_file.py            run the pipeline over a video file
  02_live_webcam.py           run the full pipeline on a live camera
  climbcv.toml                the same configuration as a file instead of a dict
  plugins/
    brightness/               a plugin that needs nothing but frames
    body_tilt/                a plugin that consumes pose data
```

## Setup

From a source checkout, the scripts add `src/` to `sys.path` themselves, so:

```bash
python3 examples/01_video_file.py --make-sample
```

Installed as a package, drop the `sys.path` line and `from climbcv import ClimbCV` works
directly:

```bash
pip install -e '.[plugins]'
```

`[plugins]` pulls in `opencv-python` and `mediapipe`. They are deliberately **not** core
dependencies — the framework itself imports only numpy, because every core dependency is
imposed on every plugin author's environment.

## 1. A video file

```bash
python3 examples/01_video_file.py --make-sample     # generates examples/sample.mp4 first
python3 examples/01_video_file.py path/to/clip.mp4
```

Points `core.capture` at a file instead of a camera. The generated sample contains no person,
so this example disables the pose stage and watches brightness instead — MediaPipe finding no
skeleton in synthetic noise would be correct behaviour, not a bug worth demonstrating. Pass
your own footage for real pose output.

Watch for the end: reaching the end of a file is an **orderly completion**, and because
`core.capture` is the only publisher of `frame`, the run then ends with a message saying
exactly that. A finite source ending the run is usually what you want; it is worth knowing it
happens.

## 2. A live camera

```bash
python3 examples/02_live_webcam.py         # camera 0
python3 examples/02_live_webcam.py 1       # camera 1
```

The full default pipeline — capture → pose estimation → smoothing — plus both example plugins.
Stand in front of the camera and the tilt reading moves. First run downloads a ~5 MB pose
model into `examples/.state/`.

No camera, or permission denied? `core.capture` reports itself **unavailable** rather than
crashing, and the log says why. Not having a camera is not a plugin defect, and the difference
between "unavailable" and "crashed twice and has been disabled" matters when you are the one
reading the log.

This example uses `app.poll()` rather than `app.run()`. `poll()` drains callbacks on the
thread that calls it, which is what you want when your own program owns the main loop — a Qt
or Tk host can touch its own widgets from a callback instead of being handed one on a framework
thread.

## 3. Writing a plugin

Drop a folder into `plugins/`. No pip install, no registration, no entry points. Two files:

**`climbcv-plugin.toml`** — the manifest. This is the single source of truth for the topic
graph, because the framework never imports your code to discover what you publish; doing that
would mean running untrusted code inside the supervisor process. Anything not declared here
does not exist as far as wiring is concerned.

**`plugin.py`** — your class.

```python
from climbcv import Plugin, subscribe, every
from climbcv.contracts import Scalar

class Brightness(Plugin):
    def setup(self):
        self._threshold = float(self.config.get("dark_below", 40.0))

    @subscribe("frame")
    def on_frame(self, frame, meta):
        self.publish("example.brightness", Scalar(value=..., t_ns=...))

    @every(2.0)
    def report(self):
        self.log.info("...")
```

### Things worth knowing before you write one

**Do not define `__init__`.** The framework binds `self.config`, `self.log` and
`self.publish` *after* constructing you, so inside `__init__` none of them exist yet — and
`self.config.get(...)` there is the first thing everyone tries. Put it in `setup()`, which runs
in your own process with everything available. That is also why you never pickle a model:
it gets built where it is used.

**You get your own process, and you never write concurrency.** No `Process`, `Queue`, `Lock`,
`Event` or `pickle` appears in either example. The one thing to know is `self.stopping`, for a
plugin that blocks — it becomes `True` *while your handler is running*, so a source doing a
long device read can still notice shutdown:

```python
@every(0)                       # "as fast as possible" — how you write a source
def pump(self):
    while not self.stopping:
        chunk = self.socket.recv(65536)
```

**Payload arrays are read-only.** Every subscriber receives its own copy, so mutating one
would only affect you — and it raises rather than silently doing nothing. Take a `.copy()`, or
use `frame.as_bgr()` / `as_rgb()`, which always return a fresh writable array.

**Declare `requires_topology` if you touch pose data.** `body_tilt` indexes landmarks 11, 12,
23 and 24, and those numbers only mean "shoulders and hips" under MediaPipe's 33-point
topology — under COCO's 17-point topology, index 11 is a hip. Declaring what you were written
against turns "plausible-looking wrong skeleton forever" into a startup error naming both
plugins.

**Timer intervals are fixed at class-definition time.** `@every(2.0)` cannot read your config.
Use `self.set_interval(self.handler, seconds)` from `setup()` for a configurable rate.

## 4. Configuration

`climbcv.toml` in this folder is the same setup as `01_video_file.py` passes as a dict — one
file, sectioned by plugin id. Each plugin receives its section as a plain dict, passed through
byte for byte with no validation. That is deliberate: it is one place to see and change
everything, without a schema layer nobody has needed yet.

A typo in a plugin option is therefore silent unless the plugin lists its option names in
`[config] keys`, which both examples do — that turns `dark_belw = 60` into a warning naming
the nearest real key instead of an option that quietly does nothing.

## Inspecting what actually got wired

Both scripts print each plugin's final state. To see the resolved graph without starting
anything, call `app.plan()` — that is what a `climbcv topics` command would print, and it
answers "who is actually publishing this" in one place. Startup warnings go through the same
path: a shared topic with two publishers, a config section naming a plugin that is not
installed, an unknown manifest key.
