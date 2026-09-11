"""Noetix Robotics N2 18-DOF DMR profile."""

from dataclasses import replace

from rimkit.robots.joi.body import get_body_joi_mapping
from rimkit.robots.profiles.t1 import T1_DMR_PROFILE

N2_JOI_BODY_NAMES = get_body_joi_mapping("n2")

N2_DMR_PROFILE = replace(
    T1_DMR_PROFILE,
    robot_id="n2",
    qpos_dim=25,
    joi_bodies=N2_JOI_BODY_NAMES,
    joi_anchor_reference_keys={"base": ("lp", "rp")},
    wrist_joint_tokens=(),
    waist_joint_tokens=(),
    ankle_orientation_mode="none",
    ankle_orientation_stage="none",
    ankle_orientation_axes=(),
    left_ankle_orientation_joi_key=None,
    right_ankle_orientation_joi_key=None,
    optimize_toe_dmr=False,
    torso_orientation_joi_key="torso",
    hand_orientation_enabled=False,
)

__all__ = ["N2_DMR_PROFILE", "N2_JOI_BODY_NAMES"]
