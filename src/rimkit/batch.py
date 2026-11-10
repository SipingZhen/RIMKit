"""Resumable BVH retargeting with separately published motions and videos."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from rimkit import __version__
from rimkit.api import Retargeter
from rimkit.config.schema import RunConfig
from rimkit.exceptions import ArtifactError
from rimkit.mujoco.model import MujocoModel
from rimkit.pipeline.events import EventSink, PipelineEvent, PipelineEventType
from rimkit.pipeline.state import PipelineStage
from rimkit.render import render_motion_preview
from rimkit.render.contact import PreviewContactState
from rimkit.robots.registry import get_robot

# Bump when the BVH retargeting policy changes so older cached motions cannot
# silently pass for results of the revised adapter.
_ADAPTER_REVISION = 3


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, record: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".record-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(source: Path, destination: Path, expected_sha256: str) -> None:
    """Publish a complete file without replacing any existing user data."""
    if destination.exists():
        if _digest(destination) == expected_sha256:
            return
        raise ArtifactError(f"Existing output differs from the cached result: {destination}")
    descriptor, name = tempfile.mkstemp(dir=destination.parent, prefix=".rimkit-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        if _digest(temporary) != expected_sha256:
            raise ArtifactError(f"Output checksum changed while copying {source}")
        temporary.chmod(0o644)
        # Same-filesystem hard linking creates the destination atomically and
        # fails if another writer has already created it.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _render_saved_motion(
    motion: Path,
    video: Path,
    *,
    robot_id: str,
    name: str,
    width: int,
    height: int,
    event_sink: EventSink,
) -> None:
    """Render the exported joint layout, including its stored contact labels."""
    with np.load(motion, allow_pickle=False) as archive:
        if str(archive["robot_id"].item()) != robot_id:
            raise ArtifactError("Cached motion belongs to a different robot.")
        exported_qpos = np.asarray(archive["qpos"], dtype=np.float64)
        joint_names = tuple(np.asarray(archive["joint_names"]).astype(str).tolist())
        model = MujocoModel.from_robot(robot_id)
        qpos = np.empty_like(exported_qpos)
        qpos[:, :7] = exported_qpos[:, :7]
        qpos[:, model.get_qpos_indices(joint_names)] = exported_qpos[:, 7:]
        seconds = np.asarray(archive["timestamps_s"], dtype=np.float64)
        fps = float(archive["fps"])
        contacts = PreviewContactState(
            fps=fps,
            seconds=seconds,
            labels=archive["contact_labels"],
            confidence=archive["contact_confidence"],
            availability=archive["contact_availability"],
            flight=archive["flight_labels"],
            segment_ranges=np.asarray([[0, len(seconds)]], dtype=np.int64),
            segment_boundaries=np.asarray([0.0, 1.0]),
            contact_source=str(archive["contact_source"].item()),
            hand_contact_source=str(archive["hand_contact_source"].item()),
        )

    def progress(current: int, total: int) -> None:
        event_sink.emit(
            PipelineEvent(
                event_type=PipelineEventType.PROGRESS,
                stage=PipelineStage.RENDERING,
                job_id=name,
                robot_id=robot_id,
                current=current,
                total=total,
                message="Rendering saved motion.",
            )
        )

    event_sink.emit(
        PipelineEvent(
            event_type=PipelineEventType.STAGE_STARTED,
            stage=PipelineStage.RENDERING,
            job_id=name,
            robot_id=robot_id,
            message="Rendering saved motion; the NPZ is already published.",
        )
    )
    render_motion_preview(
        robot_id=robot_id,
        qpos=qpos,
        fps=fps,
        motion_name=name,
        contacts=contacts,
        video_path=video,
        thumbnail_path=None,
        width=width,
        height=height,
        source_provider="bvh",
        progress=progress,
    )


def run_bvh_batch(
    input_dir: Path,
    *,
    motion_dir: Path,
    video_dir: Path,
    config: RunConfig,
    width: int,
    height: int,
    event_sink: EventSink,
) -> int:
    source_dir = input_dir.expanduser().resolve()
    motion_dir = motion_dir.expanduser().resolve()
    video_dir = video_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise ArtifactError(f"BVH input directory does not exist: {source_dir}")
    if motion_dir == video_dir:
        raise ArtifactError("Motion and video output directories must be different.")
    if width < 320 or height < 240 or width % 2 or height % 2:
        raise ArtifactError("Video dimensions must be even and at least 320x240.")
    sources = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() == ".bvh")
    if not sources:
        raise ArtifactError(f"No BVH files found in {source_dir}.")
    if len({p.stem for p in sources}) != len(sources):
        raise ArtifactError("BVH filenames must have unique stems for flat batch output.")
    motion_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    robot = get_robot(config.robot)
    retargeter: Retargeter | None = None
    failed: list[tuple[Path, str]] = []
    completed = skipped = 0
    for index, source in enumerate(sources, 1):
        print(f"[{index}/{len(sources)}] {source.name}", flush=True)
        motion_out = motion_dir / f"{source.stem}.npz"
        video_out = video_dir / f"{source.stem}.mp4"
        entry = motion_dir / ".rimkit" / source.stem
        try:
            entry.mkdir(parents=True, exist_ok=True)
            with (entry / "lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ArtifactError("This BVH is being processed by another batch.") from exc
                request = {
                    "schema_version": 1,
                    "adapter_revision": _ADAPTER_REVISION,
                    "core_version": __version__,
                    "source_sha256": _digest(source),
                    "robot": robot.robot_id,
                    "model_sha256": robot.model_sha256,
                    "fps_override": config.fps,
                    "backend": config.backend,
                }
                request_path = entry / "request.json"
                bundle = entry / "bundle"
                if request_path.exists():
                    if json.loads(request_path.read_text()) != request:
                        raise ArtifactError(
                            "Cached input or configuration differs; use a new motion directory."
                        )
                elif motion_out.exists() or video_out.exists() or bundle.exists():
                    raise ArtifactError(
                        "Existing output has no matching batch record; use a new output directory."
                    )
                else:
                    _write_json(request_path, request)
                was_complete = motion_out.exists() and video_out.exists()
                if not bundle.exists():
                    if retargeter is None:
                        retargeter = Retargeter(robot.robot_id, config)
                    retargeter.run(
                        source,
                        bundle,
                        save_stages=False,
                        render_video=False,
                        width=width,
                        height=height,
                        event_sink=event_sink,
                    )
                manifest = json.loads((bundle / "manifest.json").read_text())
                if (
                    manifest["source_motion"]["sha256"] != request["source_sha256"]
                    or manifest["robot"]["id"] != robot.robot_id
                ):
                    raise ArtifactError("Cached manifest does not match this input and robot.")
                cached_motion = bundle / "final" / "robot_motion.npz"
                motion_hash = str(manifest["artifacts"]["final_motion"]["sha256"])
                if _digest(cached_motion) != motion_hash:
                    raise ArtifactError("Cached motion checksum does not match its manifest.")
                _publish(cached_motion, motion_out, motion_hash)
                print(f"  motion saved: {motion_out}", flush=True)
                for warning in manifest.get("warnings", []):
                    print(f"  warning: {warning}", file=sys.stderr, flush=True)
                settings = {"motion_sha256": motion_hash, "width": width, "height": height}
                video_key = hashlib.sha256(str(video_out).encode()).hexdigest()
                video_record = entry / f"video-{video_key}.json"
                if video_out.exists():
                    record = json.loads(video_record.read_text()) if video_record.exists() else {}
                    if record.get("settings") != settings or record.get("sha256") != _digest(
                        video_out
                    ):
                        raise ArtifactError(
                            "Existing video has different settings or checksum; "
                            "use a new video directory."
                        )
                else:
                    with tempfile.TemporaryDirectory(prefix="render-", dir=entry) as temporary:
                        cached_video = Path(temporary) / "video.mp4"
                        _render_saved_motion(
                            cached_motion,
                            cached_video,
                            robot_id=robot.robot_id,
                            name=source.stem,
                            width=width,
                            height=height,
                            event_sink=event_sink,
                        )
                        video_hash = _digest(cached_video)
                        _write_json(video_record, {"settings": settings, "sha256": video_hash})
                        _publish(cached_video, video_out, video_hash)
                if was_complete:
                    skipped += 1
                    print("  verified existing pair; skipped", flush=True)
                else:
                    completed += 1
                    print(f"  video saved: {video_out}", flush=True)
        except Exception as exc:
            failed.append((source, str(exc)))
            print(f"  failed: {exc}", file=sys.stderr, flush=True)
    print(f"Completed: {completed}; skipped: {skipped}; failed: {len(failed)}", flush=True)
    for source, reason in failed:
        print(f"  {source.name}: {reason}", file=sys.stderr)
    return 1 if failed else 0
