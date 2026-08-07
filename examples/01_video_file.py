"""Run the pipeline over a video FILE.

    python3 examples/01_video_file.py path/to/clip.mp4
    python3 examples/01_video_file.py --make-sample     # generates a clip first

What this shows: pointing `core.capture` at a file instead of a camera, consuming a topic
published by a third-party plugin from `examples/plugins/`, and the fact that a file ending is
an orderly end of run rather than a failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Only needed because these examples run from a source checkout. After `pip install climb-cv`
# you would just `from climbcv import ClimbCV`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.app import ClimbCV  # noqa: E402

HERE = Path(__file__).resolve().parent


def make_sample(path: Path) -> None:
    """A synthetic clip that fades dark and light, so `brightness` has something to report.

    It contains no human, so the pose stage will correctly find no skeleton in it — which is
    why this example disables pose and watches brightness instead. For real pose output you
    need real footage; see 02_live_webcam.py or pass your own file.
    """
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (320, 240))
    if not writer.isOpened():
        sys.exit("could not open an mp4v encoder to write the sample clip")
    for i in range(120):
        level = int(128 + 110 * np.sin(i / 12))
        writer.write(np.full((240, 320, 3), level, dtype=np.uint8))
    writer.release()
    print(f"wrote {path}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--make-sample":
        video = HERE / "sample.mp4"
        make_sample(video)
    elif args:
        video = Path(args[0])
        if not video.is_file():
            sys.exit(f"no such file: {video}")
    else:
        sys.exit(__doc__)

    app = ClimbCV({
        "framework": {
            # Where YOUR plugins live. Drop a folder in here and it loads at startup — no
            # pip install, no registration. The plugins shipped with climb-cv load from a
            # separate bundled root, and anything here shadows them by id.
            "plugin_dir": str(HERE / "plugins"),
            "state_dir": str(HERE / ".state"),
            "log_level": "INFO",
        },
        "plugins": {
            "core.capture": {"source": str(video), "mirror": False, "fps": 30},
            # A synthetic clip has no body in it, so there is nothing for pose to find.
            # Disabling the pose stage also removes `pose.smoothed`, which is why body_tilt
            # declares that subscription and would be starved — so it goes too.
            "core.pose_mediapipe": {"enabled": False},
            "core.smooth_oneeuro": {"enabled": False},
            "body_tilt": {"enabled": False},
            "brightness": {"dark_below": 60.0, "report_every_s": 1.0},
        },
    })

    # The host is an ordinary participant: it subscribes through the same resolution rules as
    # any plugin. `required` has no default on purpose — for a plugin it defaults to true to
    # catch a manifest typo, but a host subscription is a string literal in code you can see,
    # so saying which you mean is one word.
    readings: list[float] = []

    def on_brightness(scalar, meta) -> None:
        readings.append(scalar.value)
        if len(readings) % 20 == 0:
            print(f"  host saw {len(readings)} readings, latest {scalar.value:.1f} "
                  f"from {meta.source}")

    app.subscribe("example.brightness", on_brightness, required=False)

    print(f"running over {video.name} — Ctrl-C to stop early\n")
    app.run()   # returns when capture reaches the end of the file

    print(f"\ndone: {len(readings)} brightness readings")
    for pid, state in sorted(app.states.items()):
        print(f"  {pid:<24}{state.state}"
              + (f"  ({state.detail[:60]})" if state.detail else ""))


if __name__ == "__main__":
    # REQUIRED. climb-cv starts each plugin in its own process using 'spawn', which re-imports
    # this file in every child — so anything at module scope would run again there. Without
    # this guard climb-cv raises with an explanation rather than letting processes multiply.
    # (In a Jupyter notebook there is no __main__ file to re-import, so a bare call is fine.)
    main()
