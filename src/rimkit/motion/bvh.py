"""LAFAN BVH parsing and conversion to the common source-motion contract."""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.spatial.transform import Rotation  # type: ignore[import-untyped]

from rimkit.exceptions import MotionValidationError
from rimkit.motion.soma import SOMA_JOINT_COUNT, SomaMotion, SomaMotionSummary, rotation_x
from rimkit.motion.soma_joints import SOMA77_JOINT_PARENTS
from rimkit.motion.source_frame import SOMA_PELVIS_LOCAL_ALIGNMENT

FloatArray = NDArray[np.float64]
_MAPPING = {
    "Hips": "Hips",
    "Spine1": "Spine",
    "Spine2": "Spine1",
    "Chest": "Spine2",
    "Neck1": "Neck",
    "Head": "Head",
    **{
        f"{side}{dst}": f"{side}{src}"
        for side in ("Left", "Right")
        for dst, src in (
            ("Shoulder", "Shoulder"),
            ("Arm", "Arm"),
            ("ForeArm", "ForeArm"),
            ("Hand", "Hand"),
            ("Leg", "UpLeg"),
            ("Shin", "Leg"),
            ("Foot", "Foot"),
            ("ToeBase", "Toe"),
        )
    },
}
_TOKEN = re.compile(r"[{}]|[^\s{}]+")


@dataclass(frozen=True)
class _Joint:
    name: str
    parent: int
    offset: FloatArray
    channels: tuple[str, ...]


def _parse_hierarchy(text: str) -> list[_Joint]:
    tokens: Iterator[str] = iter(_TOKEN.findall(text))
    joints: list[_Joint] = []
    names: set[str] = set()

    def pop(expected: str | None = None) -> str:
        try:
            value = next(tokens)
        except StopIteration as exc:
            raise MotionValidationError("Unexpected end of BVH hierarchy.") from exc
        if expected is not None and value.lower() != expected.lower():
            raise MotionValidationError(f"Expected {expected!r} in BVH, found {value!r}.")
        return value

    def parse_joint(parent: int, kind: str, depth: int) -> None:
        if depth > 128:
            raise MotionValidationError("BVH hierarchy is too deeply nested.")
        name = pop() if kind.lower() != "end" else None
        if name is None:
            pop("Site")
        pop("{")
        pop("OFFSET")
        try:
            offset = np.asarray([float(pop()) for _ in range(3)], dtype=np.float64)
        except ValueError as exc:
            raise MotionValidationError("Invalid BVH offset.") from exc
        if not np.isfinite(offset).all():
            raise MotionValidationError("BVH offset contains NaN or infinity.")
        index = -1
        if name is not None:
            if name in names:
                raise MotionValidationError(f"Duplicate BVH joint {name!r}.")
            names.add(name)
            pop("CHANNELS")
            expected_count = 6 if parent == -1 else 3
            try:
                count = int(pop())
            except ValueError as exc:
                raise MotionValidationError("Invalid BVH channel count.") from exc
            if count != expected_count:
                raise MotionValidationError("LAFAN BVH requires 6 root and 3 joint channels.")
            channels = tuple(pop() for _ in range(count))
            expected = {axis + "rotation" for axis in "XYZ"}
            if parent == -1:
                expected |= {axis + "position" for axis in "XYZ"}
            if set(channels) != expected:
                raise MotionValidationError(f"Unsupported LAFAN channels for {name}.")
            index = len(joints)
            joints.append(_Joint(name, parent, offset, channels))
        while True:
            token = pop()
            if token == "}":
                return
            if token.lower() not in ("joint", "end") or index == -1:
                raise MotionValidationError(f"Unexpected BVH hierarchy token {token!r}.")
            parse_joint(index, token, depth + 1)

    pop("HIERARCHY")
    pop("ROOT")
    parse_joint(-1, "ROOT", 0)
    if next(tokens, None) is not None:
        raise MotionValidationError("Unexpected data after BVH hierarchy.")
    missing = sorted(set(_MAPPING.values()) - names)
    if missing:
        raise MotionValidationError("LAFAN BVH missing joints: " + ", ".join(missing))
    expected_parents = {
        "Spine": "Hips",
        "Spine1": "Spine",
        "Spine2": "Spine1",
        "Neck": "Spine2",
        "Head": "Neck",
    }
    for side in ("Left", "Right"):
        expected_parents.update(
            {
                side + child: side + parent
                for child, parent in (
                    ("Leg", "UpLeg"),
                    ("Foot", "Leg"),
                    ("Toe", "Foot"),
                    ("Arm", "Shoulder"),
                    ("ForeArm", "Arm"),
                    ("Hand", "ForeArm"),
                )
            }
        )
        expected_parents[side + "UpLeg"] = "Hips"
        expected_parents[side + "Shoulder"] = "Spine2"
    by_name = {joint.name: joint for joint in joints}
    if joints[0].name != "Hips" or any(
        by_name[child].parent < 0 or joints[by_name[child].parent].name != parent
        for child, parent in expected_parents.items()
    ):
        raise MotionValidationError("BVH does not have the expected LAFAN hierarchy.")
    return joints


def load_bvh_motion(
    path: str | Path,
    *,
    fps_override: float | None = None,
    max_file_bytes: int = 2 * 1024 * 1024 * 1024,
    max_frames: int = 1_000_000,
) -> SomaMotion:
    """Load LAFAN's centimetre, Y-up BVH convention, preserving channel order."""
    path = Path(path).expanduser().resolve()
    try:
        if path.stat().st_size > max_file_bytes:
            raise MotionValidationError("BVH file exceeds the size limit.")
        raw = path.read_bytes()
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise MotionValidationError(f"Could not read BVH file: {exc}") from exc
    parts = re.split(r"(?im)^\s*MOTION\s*$", content, maxsplit=1)
    if len(parts) != 2:
        raise MotionValidationError("BVH is missing MOTION section.")
    joints = _parse_hierarchy(parts[0])
    match = re.match(r"\s*Frames\s*:\s*(\d+)\s+Frame\s+Time\s*:\s*([^\s]+)\s*", parts[1], re.I)
    if not match:
        raise MotionValidationError("Invalid BVH frame header.")
    try:
        count = int(match.group(1))
        frame_time = float(match.group(2))
        fps = float(fps_override) if fps_override is not None else 1.0 / frame_time
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise MotionValidationError("Invalid BVH frame time or FPS.") from exc
    if (
        not 1 <= count <= max_frames
        or not np.isfinite(frame_time)
        or frame_time <= 0
        or not np.isfinite(fps)
        or not 0 < fps <= 1000
    ):
        raise MotionValidationError(
            "Invalid BVH frame count or frame time; FPS must be in (0, 1000]."
        )
    width = sum(len(joint.channels) for joint in joints)
    try:
        # Parse directly into numeric storage rather than retaining a Python
        # float object and token string for every channel of the full clip.
        values = np.loadtxt(io.StringIO(parts[1][match.end() :]), dtype=np.float64, ndmin=2)
    except ValueError as exc:
        raise MotionValidationError(
            "BVH motion data has invalid number of values or tokens."
        ) from exc
    if values.shape != (count, width) or not np.isfinite(values).all():
        raise MotionValidationError(
            "BVH motion data has invalid number of values or non-finite values."
        )
    positions = np.empty((count, len(joints), 3))
    rotations = np.empty((count, len(joints), 3, 3))
    cursor = 0
    for n, joint in enumerate(joints):
        local_pos = np.broadcast_to(joint.offset, (count, 3)).copy()
        local_rot = np.broadcast_to(np.eye(3), (count, 3, 3)).copy()
        for channel in joint.channels:
            data = values[:, cursor]
            cursor += 1
            axis = "XYZ".index(channel[0])
            if channel.endswith("position"):
                local_pos[:, axis] = data
            else:
                vectors = np.zeros((count, 3))
                vectors[:, axis] = np.deg2rad(data)
                local_rot = local_rot @ Rotation.from_rotvec(vectors).as_matrix()
        if joint.parent < 0:
            positions[:, n] = local_pos
            rotations[:, n] = local_rot
        else:
            positions[:, n] = positions[:, joint.parent] + np.einsum(
                "tij,tj->ti", rotations[:, joint.parent], local_pos
            )
            rotations[:, n] = rotations[:, joint.parent] @ local_rot
    world = rotation_x(np.pi / 2)
    positions = np.einsum("ij,tnj->tni", world, positions * 0.01)
    rotations = np.einsum("ij,tnjk->tnik", world, rotations)
    if not np.isfinite(positions).all() or not np.isfinite(rotations).all():
        raise MotionValidationError("BVH forward kinematics produced non-finite transforms.")
    out_pos = np.empty((count, SOMA_JOINT_COUNT, 3))
    out_rot = np.empty((count, SOMA_JOINT_COUNT, 3, 3))
    source_index = {joint.name: n for n, joint in enumerate(joints)}
    soma_index: dict[str, int] = {}
    for n, (name, parent) in enumerate(SOMA77_JOINT_PARENTS):
        soma_index[name] = n
        if name in _MAPPING:
            source = source_index[_MAPPING[name]]
            out_pos[:, n] = positions[:, source]
            out_rot[:, n] = rotations[:, source]
        else:
            assert parent is not None
            out_pos[:, n] = out_pos[:, soma_index[parent]]
            out_rot[:, n] = out_rot[:, soma_index[parent]]
    # LAFAN pelvis +X points up and +Z points left: its anatomical forward,
    # left, up axes are +Y, +Z, +X. Compensate for the SOMA-only right multiply
    # in canonical_soma_pelvis_rotations, leaving the world frame unchanged.
    lafan_to_canonical = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    out_rot[:, 0] = out_rot[:, 0] @ lafan_to_canonical @ SOMA_PELVIS_LOCAL_ALIGNMENT.T
    summary = SomaMotionSummary(
        path,
        sha256(raw).hexdigest(),
        count,
        fps,
        count / fps,
        tuple(joint.name for joint in joints),
        None,
        (
            "BVH assumes LAFAN centimetres and Y-up coordinates; "
            "unmapped SOMA joints are approximated.",
        ),
    )
    return SomaMotion(
        summary, np.arange(count) / fps, out_pos, out_rot, None, world, 0.01, np.zeros(3), True
    )
