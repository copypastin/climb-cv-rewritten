"""Run the pipeline over a video FILE, with a live overlay window and a 3D pose plot.

    python3 examples/01_video_file.py --make-sample      # generates a clip, uses a fake pose
    python3 examples/01_video_file.py path/to/clip.mp4    # real footage, real pose model
    python3 examples/01_video_file.py clip.mp4 --no-display   # headless
    python3 examples/01_video_file.py --make-sample --loop    # replay until ESC

Two windows open: the camera feed with the exo skeleton drawn over it, and a matplotlib 3D view
of the same skeleton in world coordinates. Press ESC in the overlay window to stop.

What this shows: pointing `core.capture` at a file, two GUI-owning plugins each running in
their own process, and swapping the pose stage for a different implementation without anything
downstream noticing.
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
    """A synthetic clip: a moving box on a shifting background.

    It contains no human, so a real pose model would correctly find no skeleton in it. That is
    why `--make-sample` switches the pose stage to `demo_pose` — see main().
    """
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (480, 360))
    if not writer.isOpened():
        sys.exit("could not open an mp4v encoder to write the sample clip")
    for i in range(200):
        level = int(90 + 60 * np.sin(i / 18))
        canvas = np.full((360, 480, 3), level, dtype=np.uint8)
        x = int(240 + 150 * np.sin(i / 15))
        cv2.rectangle(canvas, (x - 30, 150), (x + 30, 260), (40, 40, 200), -1)
        writer.write(canvas)
    writer.release()
    print(f"wrote {path}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    display = "--no-display" not in flags
    loop = "--loop" in flags

    if "--make-sample" in flags:
        video = HERE / "sample.mp4"
        make_sample(video)
        synthetic_pose = True
    elif args:
        video = Path(args[0])
        if not video.is_file():
            sys.exit(f"no such file: {video}")
        synthetic_pose = False
    else:
        sys.exit(__doc__)

    # `pose.raw` is an EXCLUSIVE topic: exactly one publisher, because subscribers compute
    # geometry from a single skeleton and two would contradict each other. So exactly one of
    # these two runs. Enabling both is a startup error whose message hands you the TOML to
    # pick one -- try it by flipping both to true.
    #
    # This is the architecture's central claim in one place: replacing a core stage and adding
    # a plugin are the same mechanism. Nothing downstream -- smoothing, the overlay, the 3D
    # plot, body_tilt -- knows or cares which of these produced the pose.
    plugins = {
        "core.capture": {"source": str(video), "mirror": False, "fps": 30, "loop": loop},
        "core.pose_mediapipe": {"enabled": not synthetic_pose, "delegate": "cpu"},
        "demo_pose": {"enabled": synthetic_pose, "cycle_frames": 45},
        "core.smooth_oneeuro": {"min_cutoff": 1.0, "beta": 0.3},
        "brightness": {"dark_below": 60.0, "report_every_s": 5.0},
        "body_tilt": {"min_visibility": 0.4},
        "exo_live": {"enabled": display, "scale": 1.4, "draw_hz": 60},
        "pose_plot": {"enabled": display, "redraw_hz": 15, "limit_m": 1.0, "trail": 0},
    }

    app = ClimbCV({
        "framework": {
            # Drop a folder in here and it loads at startup -- no pip install, no
            # registration. Plugins shipped with climb-cv load from a separate bundled root,
            # and anything here shadows them by id.
            "plugin_dir": str(HERE / "plugins"),
            "state_dir": str(HERE / ".state"),
            "log_level": "INFO",
        },
        "plugins": plugins,
    })

    # The host is an ordinary participant: it subscribes through the same rules as any plugin.
    # `required` has no default on purpose -- for a plugin it defaults to true to catch a
    # manifest typo, but a host subscription is a literal in code you can see.
    seen = {"tilt": 0, "brightness": 0, "last_tilt": None}

    def on_tilt(scalar, meta):
        seen["tilt"] += 1
        seen["last_tilt"] = scalar.value

    def on_brightness(scalar, meta):
        seen["brightness"] += 1

    app.subscribe("example.body_tilt", on_tilt, required=False)
    app.subscribe("example.brightness", on_brightness, required=False)

    print(f"\nplaying {video.name}"
          + ("  (synthetic pose — the clip has no person in it)" if synthetic_pose else "")
          + ("\ntwo windows should open. ESC in the overlay stops the run.\n"
             if display else "\nheadless: no windows.\n"))

    app.run()   # returns on ESC, Ctrl-C, or the end of the file

    print(f"\n{seen['brightness']} brightness readings, {seen['tilt']} tilt readings", end="")
    if seen["last_tilt"] is not None:
        print(f" (last {seen['last_tilt']:+.1f} deg)")
    else:
        print()
    print("\nfinal plugin states:")
    for pid, state in sorted(app.states.items()):
        detail = f"  ({state.detail.splitlines()[0][:64]})" if state.detail else ""
        print(f"  {pid:<24}{state.state}{detail}")


if __name__ == "__main__":
    # REQUIRED. climb-cv starts each plugin in its own process using 'spawn', which re-imports
    # this file in every child -- so anything at module scope would run again there. Without
    # this guard climb-cv raises with an explanation rather than letting processes multiply.
    # (In a Jupyter notebook there is no __main__ file to re-import, so a bare call is fine.)
    main()
