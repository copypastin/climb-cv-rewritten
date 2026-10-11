import cv2
import numpy as np
import time

canvas = np.zeros((480, 640, 3), dtype=np.uint8)
start = time.monotonic()
for _ in range(100):
    cv2.imshow("test", canvas)
    cv2.waitKey(1)
end = time.monotonic()
print(f"FPS: {100 / (end - start)}")
