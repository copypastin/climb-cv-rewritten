"""core.smooth_oneeuro — One Euro filtering of pose landmarks.

The reference *transform* stage, and the plugin that motivated two design decisions:

* `provides_topology` accepts a list, because this stage publishes whatever topology it
  received. A filter that treats columns uniformly indexes no joints, so naming a single
  topology would be a guess — and the wrong guess quarantines the exclusive publisher of
  `pose.smoothed`, which ends the run.
* `passthrough` exists because `enabled = false` is not a translation of "don't smooth".
  Disabling an exclusive-topic publisher removes the topic, which fatally starves every
  required subscriber. Turning the filter off has to be a config option *of this plugin*.
"""

from __future__ import annotations

import numpy as np

from climbcv import Plugin, subscribe
from climbcv.contracts import PoseFrame


class _OneEuro:
    """One Euro filter over an (L, 4) landmark array, vectorised across all columns.

    Adaptive low-pass: the cutoff rises with speed, so slow movement is smoothed hard and
    fast movement is barely touched — which is what keeps a limb from lagging behind while
    still removing jitter when the climber is static.
    """

    def __init__(self, freq: float, min_cutoff: float, beta: float, d_cutoff: float) -> None:
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x: np.ndarray | None = None
        self._dx: np.ndarray | None = None
        self._t: float | None = None

    @staticmethod
    def _alpha(cutoff: np.ndarray | float, freq: float) -> np.ndarray | float:
        tau = 1.0 / (2.0 * np.pi * cutoff)
        te = 1.0 / freq
        return 1.0 / (1.0 + tau / te)

    def __call__(self, x: np.ndarray, t: float) -> np.ndarray:
        x = x.astype(np.float32, copy=True)
        if self._x is None:
            self._x, self._dx, self._t = x, np.zeros_like(x), t
            return x

        freq = self.freq
        if self._t is not None and t > self._t:
            freq = 1.0 / (t - self._t)
            freq = float(np.clip(freq, 1.0, 500.0))
        self._t = t

        dx = (x - self._x) * freq
        a_d = self._alpha(self.d_cutoff, freq)
        self._dx = a_d * dx + (1 - a_d) * self._dx

        cutoff = self.min_cutoff + self.beta * np.abs(self._dx)
        a = self._alpha(cutoff, freq)
        self._x = a * x + (1 - a) * self._x
        return self._x.astype(np.float32, copy=True)


class SmoothOneEuro(Plugin):
    def setup(self) -> None:
        self._passthrough = bool(self.config.get("passthrough", False))
        if self._passthrough:
            self.log.info(
                "passthrough is on: republishing pose.raw unchanged. The topic still has a "
                "publisher, which is why this is a config option rather than 'enabled = false' "
                "— disabling this plugin would remove pose.smoothed entirely and starve "
                "everything that requires it."
            )
        self._freq = float(self.config.get("freq", 30.0))
        self._min_cutoff = float(self.config.get("min_cutoff", 1.0))
        self._beta = float(self.config.get("beta", 0.0))
        self._d_cutoff = float(self.config.get("d_cutoff", 1.0))
        self._world: _OneEuro | None = None
        self._image: _OneEuro | None = None
        self._topology: str | None = None

    @subscribe("pose.raw")
    def on_pose(self, pose: PoseFrame, meta) -> None:
        if self._passthrough:
            self.publish("pose.smoothed", PoseFrame(
                frame_seq=pose.frame_seq, t_capture_ns=pose.t_capture_ns,
                topology=pose.topology, mirrored=pose.mirrored,
                world=pose.world, image=pose.image, smoothed=False,
            ))
            return

        # A topology change mid-run means the upstream publisher swapped models (MediaPipe's
        # GPU->CPU fallback does exactly this). The filter state has the wrong shape now, so
        # reset rather than broadcasting a mismatched array.
        if self._topology is not None and pose.topology != self._topology:
            self.log.info(
                "upstream topology changed %s -> %s; resetting the filter",
                self._topology, pose.topology,
            )
            self._world = self._image = None
        self._topology = pose.topology

        t = pose.t_capture_ns / 1e9
        if self._world is None:
            self._world = _OneEuro(self._freq, self._min_cutoff, self._beta, self._d_cutoff)
            self._image = _OneEuro(self._freq, self._min_cutoff, self._beta, self._d_cutoff)

        world = self._world(pose.world, t)
        image = self._image(pose.image, t) if pose.image is not None else None

        # Topology is copied, not asserted: this stage publishes whatever it received, which
        # is why its manifest lists several rather than claiming one.
        self.publish("pose.smoothed", PoseFrame(
            frame_seq=pose.frame_seq,
            t_capture_ns=pose.t_capture_ns,
            topology=pose.topology,
            mirrored=pose.mirrored,
            world=world,
            image=image,
            smoothed=True,
        ))
