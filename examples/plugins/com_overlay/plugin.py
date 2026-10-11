import cv2
import numpy as np
from climbcv import Plugin, subscribe, every

class CoMOverlay(Plugin):
    def setup(self) -> None:
        self._window = str(self.config.get("window_name", "Center of Mass"))
        self._radius = int(self.config.get("radius", 15))
        cv2.namedWindow(self._window, cv2.WINDOW_NORMAL)
        self.log.info("CoM overlay ready")

    @subscribe("frame")
    def on_frame(self, frame, meta) -> None:
        if not hasattr(self, "_frames"):
            self._frames = {}
        self._frames[frame.seq] = frame
        if len(self._frames) > 30:
            del self._frames[min(self._frames.keys())]
        self._latest_frame = frame

    @every(1 / 60)
    def draw(self) -> None:
        if not hasattr(self, "_latest_frame"):
            cv2.waitKey(1)
            return

        com = self.latest("metrics.com")
        if com is not None and hasattr(self, "_frames") and com.frame_seq in self._frames:
            frame = self._frames[com.frame_seq]
        else:
            frame = self._latest_frame

        com_seq = com.frame_seq if com else -1
        if getattr(self, "_last_drawn_state", None) == (frame.seq, com_seq):
            cv2.waitKey(1)
            return
        self._last_drawn_state = (frame.seq, com_seq)

        if not hasattr(self, "_canvas") or self._canvas.shape != frame.pixels.shape:
            self._canvas = np.empty_like(frame.pixels)

        if frame.color == "rgb":
            cv2.cvtColor(frame.pixels, cv2.COLOR_RGB2BGR, dst=self._canvas)
        else:
            np.copyto(self._canvas, frame.pixels)

        canvas = self._canvas
        h, w = canvas.shape[:2]

        if com is not None:
            cx = int(com.image_x * w)
            cy = int(com.image_y * h)
            
            # Draw a cool target reticle
            color = (0, 255, 255) # Yellow
            cv2.circle(canvas, (cx, cy), self._radius, color, 2)
            cv2.circle(canvas, (cx, cy), 2, (0, 0, 255), -1) # Red center dot
            
            # Crosshairs
            cv2.line(canvas, (cx - self._radius - 5, cy), (cx - 5, cy), color, 2)
            cv2.line(canvas, (cx + 5, cy), (cx + self._radius + 5, cy), color, 2)
            cv2.line(canvas, (cx, cy - self._radius - 5), (cx, cy - 5), color, 2)
            cv2.line(canvas, (cx, cy + 5), (cx, cy + self._radius + 5), color, 2)

            cv2.putText(canvas, "CoM", (cx + self._radius + 10, cy - self._radius),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        cv2.imshow(self._window, canvas)
        cv2.waitKey(1)
