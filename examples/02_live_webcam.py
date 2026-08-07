"""Run the full pipeline against a LIVE webcam.

    python3 examples/02_live_webcam.py            # camera 0
    python3 examples/02_live_webcam.py 1          # camera 1
    python3 examples/02_live_webcam.py --no-plot  # skip the 3D plot (the costliest window)

What this shows: the default pipeline (capture → pose → smoothing) with a real camera, a
third-party plugin consuming pose data, and `poll()` — the method to use when your own program
owns the main loop.

Needs a camera and mediapipe. The first run downloads a ~5 MB pose model. If the camera cannot
be opened, `core.capture` reports itself *unavailable* rather than crashing: not having a
camera is not a plugin defect, and the log says so.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.app import ClimbCV  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    device = int(args[0]) if args else 0
    # matplotlib is the most expensive thing on screen. --no-plot isolates it, which is the
    # first thing to try if the feed feels slow.
    plot = "--no-plot" not in sys.argv[1:]

    app = ClimbCV({
        "framework": {
            "plugin_dir": str(HERE / "plugins"),
            "state_dir": str(HERE / ".state"),
            "log_level": "INFO",
        },
        "plugins": {
            "core.capture": {
                "device": device, "mirror": True,
                # 640x480 deliberately. A webcam left at its native 1280x720 or 1920x1080
                # costs real time in read(), in the mirror flip, and in every draw -- and
                # the pose model downscales internally anyway, so the extra pixels buy
                # nothing. Raise it if you want a prettier overlay, not for accuracy.
                "width": 640, "height": 480,
            },
            # "gpu" is opt-in: on some machines MediaPipe's GPU delegate initialises fine and
            # then aborts during inference, which no exception handler can catch. CPU is the
            # default because it works everywhere.
            "core.pose_mediapipe": {"delegate": "cpu"},
            "core.smooth_oneeuro": {"min_cutoff": 1.0, "beta": 0.3},
            "brightness": {"report_every_s": 5.0},
            "body_tilt": {"min_visibility": 0.5},
            # The two windows: overlay + live 3D. Both run in their own processes, so a slow
            # matplotlib redraw cannot stall the capture loop.
            # draw_hz matches the source rather than exceeding it: drawing faster than
            # frames arrive is a resize and a blit of the same image for nothing.
            "exo_live": {"scale": 1.0, "draw_hz": 30},
            "pose_plot": {"enabled": plot, "redraw_hz": 10, "limit_m": 1.0},
            # Only one publisher of pose.raw may run, and here it is the real model.
            "demo_pose": {"enabled": False},
        },
    })

    latest = {"tilt": None, "brightness": None, "poses": 0}

    def on_tilt(scalar, meta) -> None:
        latest["tilt"] = scalar.value

    def on_brightness(scalar, meta) -> None:
        latest["brightness"] = scalar.value

    def on_pose(pose, meta) -> None:
        latest["poses"] += 1

    app.subscribe("example.body_tilt", on_tilt, required=False)
    app.subscribe("example.brightness", on_brightness, required=False)
    # Subscribing to a pose topic without requires_topology raises HERE, at the call site, so
    # your own traceback names the exact line. "any" opts out when you do not index joints.
    app.subscribe("pose.smoothed", on_pose, required=False, requires_topology="any")

    print("starting — stand in front of the camera.\n"
          "Two windows open: overlay and 3D pose. ESC in the overlay (or Ctrl-C) stops.\n")
    app.start()

    try:
        # poll() drains callbacks on the thread that calls it. That matters if your program
        # owns a GUI: your callbacks run where you can touch your own widgets, instead of
        # arriving on a framework thread you know nothing about.
        while True:
            app.poll(0.05)

            tilt = latest["tilt"]
            bright = latest["brightness"]
            line = (
                f"\rposes {latest['poses']:<6}"
                f"tilt {tilt:+6.1f}deg  " if tilt is not None else
                f"\rposes {latest['poses']:<6}tilt   --     "
            )
            line += f"brightness {bright:5.1f}" if bright is not None else "brightness   -- "
            print(line, end="", flush=True)

            capture = app.states.get("core.capture")
            if capture is not None and capture.state in ("unavailable", "quarantined"):
                print(f"\n\ncapture is {capture.state}: {capture.detail}")
                break
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n\nstopping…")
    finally:
        app.stop()

    print("\nfinal plugin states:")
    for pid, state in sorted(app.states.items()):
        print(f"  {pid:<24}{state.state}"
              + (f"  ({state.detail[:60]})" if state.detail else ""))


if __name__ == "__main__":
    # REQUIRED — see the note at the bottom of 01_video_file.py.
    main()
