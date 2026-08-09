"""core.pose_mediapipe — one skeleton per frame, via MediaPipe PoseLandmarker.

Note what this plugin does NOT contain: no process, no queue, no model pickling, no
`model_path` parameter threaded through a worker entry point. The model is built in `setup()`,
in the process that uses it, which is the whole point of Decision #15.
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path

import numpy as np

from climbcv import Plugin, subscribe
from climbcv.contracts import PoseFrame

_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/1/pose_landmarker_full.task"
)
_TOPOLOGY = "mediapipe.pose.33"


class MediaPipePose(Plugin):
    def setup(self) -> None:
        try:
            import mediapipe as mp
        except ImportError:
            # Declaring it in `requires` is informational; this is what the user actually
            # sees, and it names the interpreter so they install into the right one.
            import sys

            self.unavailable(
                f"mediapipe is not installed in {sys.executable}. Install it with:\n"
                f"    {sys.executable} -m pip install mediapipe"
            )
            return

        self._mp = mp
        model = self._model_path()
        base = mp.tasks.BaseOptions
        vision = mp.tasks.vision

        # CPU by default, GPU opt-in. Probing GPU first looks better and is not safe: on this
        # machine the GPU delegate CREATES successfully, reports "ready on GPU", and then dies
        # inside mediapipe::Image::ConvertToGpu during inference -- a native abort, which no
        # except clause can catch. The framework would contain it and restart, forever. A
        # create-time probe cannot detect an inference-time failure, so the honest default is
        # the one that works everywhere.
        delegate_name = str(self.config.get("delegate", "cpu")).lower()
        order = (
            [base.Delegate.GPU, base.Delegate.CPU]
            if delegate_name == "gpu"
            else [base.Delegate.CPU]
        )

        last_error: Exception | None = None
        for delegate in order:
            try:
                options = vision.PoseLandmarkerOptions(
                    base_options=base(model_asset_path=str(model), delegate=delegate),
                    running_mode=vision.RunningMode.VIDEO,
                    min_pose_detection_confidence=float(
                        self.config.get("min_pose_detection_confidence", 0.5)
                    ),
                    min_tracking_confidence=float(
                        self.config.get("min_tracking_confidence", 0.5)
                    ),
                )
                self._landmarker = vision.PoseLandmarker.create_from_options(options)
                self.log.info("pose landmarker ready on %s", delegate.name)
                break
            except Exception as exc:  # noqa: BLE001 — delegate probing is best-effort
                last_error = exc
                self.log.info("%s delegate unavailable (%s); trying the next one",
                              delegate.name, exc)
        else:
            raise RuntimeError(f"no usable MediaPipe delegate: {last_error}")

        self._last_ts_ms = -1

    def _model_path(self) -> Path:
        configured = self.config.get("model_path")
        if configured:
            path = Path(configured)
            if not path.is_file():
                self.unavailable(f"model_path {path} does not exist")
            return path

        # data_dir belongs to this plugin and is created before setup() runs. Downloading into
        # the plugin's own directory would break under a read-only install, and the bundled
        # root always is read-only.
        cached = Path(self.data_dir) / "pose_landmarker_full.task"
        if not cached.is_file():
            self.log.info("downloading the pose model to %s (about 5 MB, once)", cached)
            try:
                urllib.request.urlretrieve(_MODEL_URL, cached)  # noqa: S310
            except Exception as exc:  # noqa: BLE001
                self.unavailable(
                    f"could not download the pose model ({exc}). Set a local file instead:\n"
                    '    [plugins."core.pose_mediapipe"]\n'
                    '    model_path = "/path/to/pose_landmarker_lite.task"'
                )
        return cached

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        # MediaPipe's VIDEO mode requires strictly increasing timestamps. Frames may be
        # conflated upstream, so derive the timestamp from the frame rather than a counter.
        ts_ms = max(frame.t_capture_ns // 1_000_000, self._last_ts_ms + 1)
        self._last_ts_ms = ts_ms

        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB, data=frame.as_rgb()
        )
        result = self._landmarker.detect_for_video(image, ts_ms)
        if not result.pose_world_landmarks:
            return  # no estimate is not an error, and publishing NaNs would poison every
            # subscriber silently — the contract refuses non-finite values for that reason.

        world = _to_array(result.pose_world_landmarks[0])
        img = (
            _to_array(result.pose_landmarks[0])
            if result.pose_landmarks else None
        )
        self.publish("pose.raw", PoseFrame(
            frame_seq=frame.seq,
            t_capture_ns=frame.t_capture_ns,
            topology=_TOPOLOGY,
            mirrored=frame.mirrored,   # carried so a pose-only subscriber can tell hands apart
            world=world,
            image=img,
            smoothed=False,
        ))

    def teardown(self) -> None:
        landmarker = getattr(self, "_landmarker", None)
        if landmarker is not None:
            landmarker.close()


def _to_array(landmarks) -> np.ndarray:
    """(visibility, x, y, z) per landmark, float32 — the column order the contract states."""
    out = np.empty((len(landmarks), 4), dtype=np.float32)
    for i, lm in enumerate(landmarks):
        out[i] = (
            getattr(lm, "visibility", 1.0), lm.x, lm.y, lm.z,
        )
    return np.nan_to_num(out, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
