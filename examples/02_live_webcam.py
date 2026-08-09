"""Run the full pipeline against a LIVE webcam, with a host-owned overlay and a 3D pose plot.

    python3 examples/02_live_webcam.py            # camera 0
    python3 examples/02_live_webcam.py 1          # camera 1
    python3 examples/02_live_webcam.py --no-plot  # skip the 3D plot (the costliest window)

Rendering follows the original climb-cv `utils/rendering/exo_live.py`: MediaPipe's own
`draw_landmarks` for the skeleton, and the torso vector plus angle drawn over it.

The overlay is owned by the host rather than a plugin, mirroring the original's single loop.
`poll()` drains callbacks on the thread that calls it, so the drawing happens right here, on
the main thread, where a GUI is allowed to live.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cv2  # noqa: E402

from climbcv.app import ClimbCV  # noqa: E402
from climbcv.rendering import draw_body_tilt, draw_pose_overlay  # noqa: E402

HERE = Path(__file__).resolve().parent
WINDOW = "MediaPipe Pose Landmarker"

def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    device = int(args[0]) if args else 0
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
                # 640x480 deliberately: the pose model downscales internally, so extra pixels
                # cost time in read(), in the mirror flip and in every draw, and buy nothing.
                "width": 640, "height": 480,
            },
            "core.pose_mediapipe": {"delegate": "cpu"},
            "core.smooth_oneeuro": {"min_cutoff": 1.0, "beta": 0.3},
            "brightness": {"report_every_s": 10.0},
            # The overlay is host-owned here, so the plugin version stays off.
            "exo_live": {"enabled": False},
            "body_tilt": {"enabled": False},   # drawn inline, the way the original did
            "pose_plot": {"enabled": plot, "redraw_hz": 10, "limit_m": 1.0},
            "demo_pose": {"enabled": False},   # only one publisher of pose.raw may run
        },
    })

    state = {"frame": None, "pose": None, "lid": None, "poses": 0, "drawn": 0}

    def on_frame(frame, meta) -> None:
        state["frame"] = frame

    def on_pose(pose, meta) -> None:
        state["pose"] = pose
        state["poses"] += 1

    def on_lid(scalar, meta) -> None:
        state["lid"] = scalar.value

    app.subscribe("frame", on_frame, required=False)
    app.subscribe("pose.smoothed", on_pose, required=False, requires_topology="any")
    app.subscribe("device.lid_angle", on_lid, required=False)

    try:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        overlay = True
    except cv2.error as exc:
        print(f"overlay disabled: cannot open a window: {exc}")
        overlay = False

    print("starting — stand in front of the camera. ESC in the overlay (or Ctrl-C) stops.\n")
    app.start()

    fps, prev_t, last_status = 0.0, None, 0.0
    try:
        while True:
            # A small timeout rather than 0.0: polling in a hot spin burns a core for nothing
            # and leaves less CPU for the stages that are actually doing work.
            app.poll(0.005)

            # Newest frame, newest pose — the way the original worked. Holding frames back to
            # match a pose to the exact frame it came from delays the VIDEO by the pose
            # latency, which is worse than the sub-frame offset it removes.
            frame, pose = state["frame"], state["pose"]

            if overlay and frame is not None:
                canvas = frame.as_bgr()

                now = time.time()
                if pose is not None:
                    if prev_t:
                        fps = 1.0 / (now - prev_t) if now > prev_t else fps
                    prev_t = now
                    draw_pose_overlay(canvas, pose)

                cv2.putText(canvas, f"FPS: {int(fps)}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
                if state["lid"] is not None:
                    cv2.putText(canvas, f"Mac Camera Angle: {state['lid']:.0f}", (10, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
                else:
                    cv2.putText(canvas, "Mac Camera Angle: n/a", (10, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
                draw_body_tilt(canvas, pose, state["lid"])

                cv2.imshow(WINDOW, canvas)
                state["drawn"] += 1
                if cv2.waitKey(1) & 0xFF == 27:
                    break

            # Throttled to 5 Hz. A flushed terminal write on every pass of a fast loop is
            # thousands of writes a second, and it makes the whole loop stutter.
            if time.monotonic() - last_status > 0.2:
                last_status = time.monotonic()
                print(f"\rposes {state['poses']:<6} drawn {state['drawn']:<6} "
                      f"pose fps {fps:4.1f}   ", end="", flush=True)

            capture = app.states.get("core.capture")
            if capture is not None and capture.state in ("unavailable", "quarantined"):
                print(f"\n\ncapture is {capture.state}: {capture.detail}")
                break

    except KeyboardInterrupt:
        print("\n\nstopping…")
    finally:
        app.stop()
        if overlay:
            cv2.destroyAllWindows()
            cv2.waitKey(1)

    print(f"\n{state['poses']} poses, {state['drawn']} frames drawn")
    for pid, st in sorted(app.states.items()):
        detail = f"  ({st.detail.splitlines()[0][:60]})" if st.detail else ""
        print(f"  {pid:<24}{st.state}{detail}")


if __name__ == "__main__":
    # REQUIRED — 'spawn' re-imports this file in every child process.
    main()
