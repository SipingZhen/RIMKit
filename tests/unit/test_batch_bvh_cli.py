"""Durability and resume coverage for BVH batch conversion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

import rimkit.batch as batch
from rimkit.cli.main import main
from rimkit.mujoco.model import MujocoModel
from rimkit.pipeline.events import NullEventSink


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Mock, Mock]:
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "walk.bvh").write_text("BVH source")

    def retarget(source: Path, bundle: Path, **kwargs: Any) -> None:
        assert not kwargs["render_video"]
        (bundle / "final").mkdir(parents=True)
        (bundle / "final/robot_motion.npz").write_bytes(b"complete motion")
        manifest = {
            "source_motion": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
            "robot": {"id": "n2"},
            "artifacts": {
                "final_motion": {
                    "sha256": hashlib.sha256(b"complete motion").hexdigest(),
                }
            },
            "warnings": ["contact labels are estimated"],
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest))

    def render(motion: Path, video: Path, **kwargs: Any) -> None:
        assert motion.read_bytes() == b"complete motion"
        video.write_bytes(b"complete video")

    run = Mock(side_effect=retarget)
    render_mock = Mock(side_effect=render)
    monkeypatch.setattr(batch, "Retargeter", Mock(return_value=Mock(run=run)))
    monkeypatch.setattr(batch, "_render_saved_motion", render_mock)
    args = [
        "batch-bvh",
        str(inputs),
        "--robot",
        "n2",
        "--motion-dir",
        str(tmp_path / "motions"),
        "--video-dir",
        str(tmp_path / "videos"),
    ]
    return args, run, render_mock


def test_verified_pairs_are_skipped(job: tuple[list[str], Mock, Mock], tmp_path: Path) -> None:
    args, run, render = job
    assert main(args) == 0
    assert main(args) == 0
    assert run.call_count == render.call_count == 1
    assert (tmp_path / "motions/walk.npz").read_bytes() == b"complete motion"
    assert (tmp_path / "videos/walk.mp4").read_bytes() == b"complete video"
    assert (tmp_path / "motions/.rimkit/walk/bundle/manifest.json").exists()


def test_video_failure_preserves_motion_and_retry_only_renders(
    job: tuple[list[str], Mock, Mock],
    tmp_path: Path,
) -> None:
    args, run, render = job
    successful_render = render.side_effect
    render.side_effect = RuntimeError("renderer unavailable")
    assert main(args) == 1
    assert (tmp_path / "motions/walk.npz").exists()
    assert not (tmp_path / "videos/walk.mp4").exists()
    render.side_effect = successful_render
    assert main(args) == 0
    assert run.call_count == 1
    assert render.call_count == 2


def test_interrupted_render_preserves_motion_and_can_resume(
    job: tuple[list[str], Mock, Mock],
    tmp_path: Path,
) -> None:
    args, run, render = job
    successful_render = render.side_effect
    render.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        main(args)
    assert (tmp_path / "motions/walk.npz").read_bytes() == b"complete motion"
    assert not (tmp_path / "videos/walk.mp4").exists()
    render.side_effect = successful_render
    assert main(args) == 0
    assert run.call_count == 1


def test_keyboard_interrupt_during_publish_leaves_no_partial_output(
    job: tuple[list[str], Mock, Mock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, run, render = job
    original_copy = batch.shutil.copyfileobj

    def interrupt(source: Any, destination: Any) -> None:
        destination.write(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(batch.shutil, "copyfileobj", interrupt)
    with pytest.raises(KeyboardInterrupt):
        main(args)
    assert not (tmp_path / "motions/walk.npz").exists()
    assert not list((tmp_path / "motions").glob(".rimkit-*.tmp"))
    monkeypatch.setattr(batch.shutil, "copyfileobj", original_copy)
    assert main(args) == 0
    assert run.call_count == 1
    assert render.call_count == 1


def test_modified_input_or_output_is_not_silently_reused(
    job: tuple[list[str], Mock, Mock],
    tmp_path: Path,
) -> None:
    args, run, _ = job
    assert main(args) == 0
    motion = tmp_path / "motions/walk.npz"
    motion.write_bytes(b"corrupt")
    assert main(args) == 1
    assert motion.read_bytes() == b"corrupt"
    (tmp_path / "input/walk.bvh").write_text("different input")
    assert main(args) == 1
    assert run.call_count == 1


def test_failure_continues_to_other_sources(
    job: tuple[list[str], Mock, Mock],
    tmp_path: Path,
) -> None:
    args, run, _ = job
    (tmp_path / "input/broken.BVH").write_text("bad input")
    retarget = run.side_effect

    def maybe_fail(source: Path, bundle: Path, **kwargs: Any) -> None:
        if source.stem == "broken":
            raise ValueError("invalid skeleton")
        retarget(source, bundle, **kwargs)

    run.side_effect = maybe_fail
    assert main(args) == 1
    assert (tmp_path / "motions/walk.npz").exists()
    assert (tmp_path / "videos/walk.mp4").exists()


def test_bad_video_settings_fail_before_retarget(job: tuple[list[str], Mock, Mock]) -> None:
    args, run, _ = job
    assert main([*args, "--width", "321"]) == 2
    run.assert_not_called()


def test_saved_motion_render_restores_joint_order_and_contact_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = MujocoModel.from_robot("n2")
    model.reset()
    qpos = np.repeat(model.get_qpos()[None], 2, axis=0)
    qpos[:, 7:] = np.arange(qpos.shape[1] - 7) * 0.01
    names = tuple(reversed(model.qpos_joint_names))
    exported = np.concatenate((qpos[:, :7], qpos[:, model.get_qpos_indices(names)]), axis=1)
    motion = tmp_path / "saved.npz"
    np.savez(
        motion,
        robot_id="n2",
        qpos=exported,
        joint_names=names,
        timestamps_s=np.arange(2) / 30,
        fps=30.0,
        contact_labels=np.zeros((2, 4), dtype=bool),
        contact_confidence=np.zeros((2, 4)),
        contact_availability=np.array([True, True, False, False]),
        flight_labels=np.ones(2, dtype=bool),
        contact_source="bvh_inferred",
        hand_contact_source="unavailable",
    )
    render = Mock()
    monkeypatch.setattr(batch, "render_motion_preview", render)
    batch._render_saved_motion(
        motion,
        tmp_path / "video.mp4",
        robot_id="n2",
        name="saved",
        width=320,
        height=240,
        event_sink=NullEventSink(),
    )
    kwargs = render.call_args.kwargs
    np.testing.assert_array_equal(kwargs["qpos"], qpos)
    np.testing.assert_array_equal(kwargs["contacts"].segment_boundaries, [0, 1])
    assert kwargs["contacts"].flight.all()
