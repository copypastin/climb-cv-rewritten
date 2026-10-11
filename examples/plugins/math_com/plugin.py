import dataclasses
import numpy as np
from climbcv import Plugin, subscribe

@dataclasses.dataclass(frozen=True)
class CoMFrame:
    frame_seq: int
    t_capture_ns: int
    image_x: float
    image_y: float
    world_x: float
    world_y: float
    world_z: float

# MediaPipe 33-point indices
NOSE = 0
L_SHOULDER, R_SHOULDER = 11, 12
L_HIP, R_HIP = 23, 24
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28

# Biological mass distribution (approximate percentages)
WEIGHTS = {
    "head": 0.08,
    "trunk": 0.50,
    "upper_arm_l": 0.03,
    "upper_arm_r": 0.03,
    "forearm_l": 0.02,
    "forearm_r": 0.02,
    "thigh_l": 0.10,
    "thigh_r": 0.10,
    "calf_l": 0.06,
    "calf_r": 0.06,
}

class CenterOfMass(Plugin):
    def setup(self) -> None:
        self.log.info("Center of Mass calculator ready")

    @subscribe("pose.smoothed")
    def on_pose(self, pose, meta) -> None:
        if pose is None or pose.image is None or pose.world is None:
            return

        img = pose.image
        world = pose.world

        def midpoint(p1, p2, arr):
            return (arr[p1, 1:] + arr[p2, 1:]) / 2.0

        def segment_com(p1, p2, arr):
            # Approximate segment CoM as the midpoint of the joints
            return (arr[p1, 1:] + arr[p2, 1:]) / 2.0

        segments_img = {
            "head": img[NOSE, 1:],
            "trunk": midpoint(L_SHOULDER, R_HIP, img),  # rough trunk center
            "upper_arm_l": segment_com(L_SHOULDER, L_ELBOW, img),
            "upper_arm_r": segment_com(R_SHOULDER, R_ELBOW, img),
            "forearm_l": segment_com(L_ELBOW, L_WRIST, img),
            "forearm_r": segment_com(R_ELBOW, R_WRIST, img),
            "thigh_l": segment_com(L_HIP, L_KNEE, img),
            "thigh_r": segment_com(R_HIP, R_KNEE, img),
            "calf_l": segment_com(L_KNEE, L_ANKLE, img),
            "calf_r": segment_com(R_KNEE, R_ANKLE, img),
        }

        segments_world = {
            "head": world[NOSE, 1:],
            "trunk": midpoint(L_SHOULDER, R_HIP, world),
            "upper_arm_l": segment_com(L_SHOULDER, L_ELBOW, world),
            "upper_arm_r": segment_com(R_SHOULDER, R_ELBOW, world),
            "forearm_l": segment_com(L_ELBOW, L_WRIST, world),
            "forearm_r": segment_com(R_ELBOW, R_WRIST, world),
            "thigh_l": segment_com(L_HIP, L_KNEE, world),
            "thigh_r": segment_com(R_HIP, R_KNEE, world),
            "calf_l": segment_com(L_KNEE, L_ANKLE, world),
            "calf_r": segment_com(R_KNEE, R_ANKLE, world),
        }

        com_img = np.zeros(3)
        com_world = np.zeros(3)
        total_weight = 0.0

        for key, weight in WEIGHTS.items():
            com_img += segments_img[key] * weight
            com_world += segments_world[key] * weight
            total_weight += weight

        com_img /= total_weight
        com_world /= total_weight

        self.publish("metrics.com", CoMFrame(
            frame_seq=pose.frame_seq,
            t_capture_ns=pose.t_capture_ns,
            image_x=float(com_img[0]),
            image_y=float(com_img[1]),
            world_x=float(com_world[0]),
            world_y=float(com_world[1]),
            world_z=float(com_world[2]),
        ))
