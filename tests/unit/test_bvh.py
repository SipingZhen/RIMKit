"""Coverage for the LAFAN BVH input contract."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from rimkit.exceptions import MotionValidationError
from rimkit.motion.soma_joints import SOMA77_JOINT_INDEX
from rimkit.motion.source import load_source_motion, validate_source_motion
from rimkit.motion.source_frame import SOMA_PELVIS_LOCAL_ALIGNMENT
from rimkit.robots.profiles import get_dmr_profile
from rimkit.robots.profiles.fpa import get_fpa_profile
from rimkit.stages.dmr import build_base_orientation_targets


def fixture() -> str:
    # Distinct nonzero offsets and rotations exercise channel ordering and FK.
    groups = {
        "Hips": ["LeftUpLeg", "RightUpLeg", "Spine"],
        "LeftUpLeg": ["LeftLeg"],
        "LeftLeg": ["LeftFoot"],
        "LeftFoot": ["LeftToe"],
        "RightUpLeg": ["RightLeg"],
        "RightLeg": ["RightFoot"],
        "RightFoot": ["RightToe"],
        "Spine": ["Spine1"],
        "Spine1": ["Spine2"],
        "Spine2": ["Neck", "LeftShoulder", "RightShoulder"],
        "Neck": ["Head"],
        "LeftShoulder": ["LeftArm"],
        "LeftArm": ["LeftForeArm"],
        "LeftForeArm": ["LeftHand"],
        "RightShoulder": ["RightArm"],
        "RightArm": ["RightForeArm"],
        "RightForeArm": ["RightHand"],
    }
    names = []

    def joint(name, root=False):
        names.append(name)
        text = ("ROOT " if root else "JOINT ") + name + "\n{\nOFFSET 10 0 0\n"
        text += (
            "CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation\n"
            if root
            else "CHANNELS 3 Zrotation Yrotation Xrotation\n"
        )
        text += "".join(joint(child) for child in groups.get(name, []))
        return text + "}\n"

    hierarchy = joint("Hips", True)
    first = [100, 200, 300, 90, 0, 0] + [0] * (3 * (len(names) - 1))
    second = [110, 200, 300, 90, 0, 0] + [0] * (3 * (len(names) - 1))
    return (
        "HIERARCHY\n"
        + hierarchy
        + "MOTION\nFrames: 2\nFrame Time: 0.02\n"
        + "\n".join(" ".join(map(str, row)) for row in (first, second))
        + "\n"
    )


class BvhTests(unittest.TestCase):
    def test_fk_scale_frame_time_and_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "walk.BVH"
            path.write_text(fixture())
            source = load_source_motion(path)
            self.assertEqual(source.summary.provider, "bvh")
            self.assertEqual(source.summary.frame_count, 2)
            self.assertEqual(source.summary.fps, 50)
            self.assertEqual(validate_source_motion(path, fps_override=25).fps, 25)
            hips = source.motion.posed_joints[:, SOMA77_JOINT_INDEX["Hips"]]
            np.testing.assert_allclose(hips, [[1, -3, 2], [1.1, -3, 2]], atol=1e-10)
            hip = source.motion.posed_joints[0, SOMA77_JOINT_INDEX["LeftLeg"]]
            np.testing.assert_allclose(hip - hips[0], [0, 0, 0.1], atol=1e-10)

    def test_bad_data_and_missing_joint_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.bvh"
            path.write_text(fixture().replace("LeftToe", "OtherToe"))
            with self.assertRaisesRegex(MotionValidationError, "missing joints"):
                validate_source_motion(path)
            path.write_text(fixture() + " 1")
            with self.assertRaisesRegex(MotionValidationError, "invalid number"):
                validate_source_motion(path)


if __name__ == "__main__":
    unittest.main()


def test_n2_bvh_turn_is_continuous_across_180_degrees(tmp_path: Path) -> None:
    path = tmp_path / "turn.bvh"
    path.write_text(fixture())
    motion = load_source_motion(path).motion
    count = 721
    yaw = np.linspace(0, 2 * np.pi, count)
    canonical = Rotation.from_euler("z", yaw).as_matrix()
    rotations = np.repeat(motion.global_rot_mats[:1], count, axis=0)
    rotations[:, 0] = canonical @ SOMA_PELVIS_LOCAL_ALIGNMENT.T
    motion = replace(
        motion,
        summary=replace(motion.summary, frame_count=count, fps=30),
        seconds=np.arange(count) / 30,
        posed_joints=np.repeat(motion.posed_joints[:1], count, axis=0),
        global_rot_mats=rotations,
    )
    profile = get_dmr_profile("n2", source_provider="bvh")
    filtered = build_base_orientation_targets(
        motion,
        smooth_time=profile.pelvis_orientation_smooth_time,
        smoothing_mode=profile.orientation_smoothing_mode,
    )
    steps = Rotation.from_matrix(np.swapaxes(filtered[:-1], 1, 2) @ filtered[1:]).magnitude()
    errors = Rotation.from_matrix(np.swapaxes(canonical, 1, 2) @ filtered).magnitude()
    assert np.rad2deg(steps).max() < 0.6
    assert np.rad2deg(errors).max() < 0.5
    assert get_dmr_profile("n2").orientation_smoothing_mode == "rotvec_legacy"
    assert profile.ankle_orientation_mode == "none"
    assert not profile.hand_orientation_enabled


def test_n2_bvh_grounding_covers_single_support_without_ankle_tasks() -> None:
    profile = get_fpa_profile("n2", source_provider="bvh")
    assert profile.post_ground_root_lower_support_mode == "any"
    assert profile.post_ground_dual_support_lower_max == 0.03
    assert not profile.use_profile_ankle_orientation
    assert get_fpa_profile("n2").post_ground_root_lower_support_mode == "double"


def test_bvh_contact_inference_preserves_airborne_interval(tmp_path: Path) -> None:
    from rimkit.motion.contacts import build_contact_schedule

    path = tmp_path / "jump.bvh"
    path.write_text(fixture())
    motion = load_source_motion(path).motion
    count = 180
    positions = np.zeros((count, 77, 3))
    positions[:, 0, 2] = 1
    for side, y in (("Left", 0.1), ("Right", -0.1)):
        positions[:, SOMA77_JOINT_INDEX[side + "Leg"]] = [0, y, 1]
        positions[:, SOMA77_JOINT_INDEX[side + "Foot"]] = [0, y, 0.05]
        positions[:, SOMA77_JOINT_INDEX[side + "ToeBase"]] = [0.15, y, 0]
    lift = np.zeros(count)
    lift[60:91] = 0.3 * np.sin(np.linspace(0, np.pi, 31))
    positions[:, :, 2] += lift[:, None]
    motion = replace(
        motion,
        summary=replace(motion.summary, frame_count=count, fps=30),
        seconds=np.arange(count) / 30,
        posed_joints=positions,
        global_rot_mats=np.repeat(motion.global_rot_mats[:1], count, axis=0),
    )
    contacts = build_contact_schedule(motion, infer_source_contacts=True)
    assert contacts.flight_label is not None
    assert contacts.flight_label[75]
    assert contacts.left_confidence[75] == contacts.right_confidence[75] == 0
    assert contacts.left_contact_label[10] and contacts.right_contact_label[150]


@pytest.mark.parametrize("fps", [0, -1, float("nan"), float("inf"), 1001])
def test_bvh_rejects_invalid_fps(tmp_path: Path, fps: float) -> None:
    path = tmp_path / "walk.bvh"
    path.write_text(fixture())
    with pytest.raises(MotionValidationError):
        validate_source_motion(path, fps_override=fps)
