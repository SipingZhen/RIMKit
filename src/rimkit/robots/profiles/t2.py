"""Booster Robotics T2 DMR profile."""

from dataclasses import replace

from rimkit.robots.joi.body import get_body_joi_mapping
from rimkit.robots.profiles.g1 import G1_DMR_PROFILE

T2_JOI_BODY_NAMES = get_body_joi_mapping("t2")

T2_DMR_PROFILE = replace(
    G1_DMR_PROFILE,
    robot_id="t2",
    qpos_dim=38,
    joi_bodies=T2_JOI_BODY_NAMES,
    joi_anchor_reference_keys={"base": ("lp", "rp")},
    # Keep waist yaw in the primary shoulder-position solve; the post torso
    # pass controls only roll and pitch.
    waist_joint_tokens=("waist_pitch", "waist_roll"),
    torso_orientation_joi_key="torso",
    left_ankle_orientation_joi_key="lsole",
    right_ankle_orientation_joi_key="rsole",
)

__all__ = ["T2_DMR_PROFILE", "T2_JOI_BODY_NAMES"]
