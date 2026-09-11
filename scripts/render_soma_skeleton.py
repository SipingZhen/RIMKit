#!/usr/bin/env python3
"""Render a Kimodo SOMA77 source motion as a headless skeleton video.

Example:
    conda run -n rimkit python scripts/render_soma_skeleton.py \
        examples/motions/kimodo/soma_rp_v11/stand_walk_run_stop.npz \
        --output runs/source-visualizations/stand_walk_run_stop/soma_skeleton.mp4 \
        --thumbnail runs/source-visualizations/stand_walk_run_stop/soma_skeleton.png

This is a source-motion diagnostic renderer.  It deliberately renders the
SOMA77 joint hierarchy rather than a skinned human mesh, so it needs only
RIMKit's ``video`` extra and can run without an OpenGL display.
"""

from __future__ import annotations

import argparse
import importlib
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rimkit.motion import SOMA77_JOINT_INDEX, SOMA77_JOINT_PARENTS, load_soma_motion


BACKGROUND = (20, 25, 33)
GRID_COLOR = (55, 64, 76)
LEFT_COLOR = (72, 161, 255)
RIGHT_COLOR = (255, 105, 117)
CENTER_COLOR = (236, 239, 244)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Kimodo SOMA77 .npz motion file")
    parser.add_argument("--output", required=True, type=Path, help="Output .mp4 path")
    parser.add_argument("--thumbnail", type=Path, help="Optional middle-frame .png path")
    parser.add_argument("--fps", type=float, help="Override the source motion FPS")
    parser.add_argument("--stride", type=int, default=1, help="Render every Nth source frame")
    parser.add_argument("--width", type=int, default=1280, help="Even output width in pixels")
    parser.add_argument("--height", type=int, default=720, help="Even output height in pixels")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output files")
    return parser.parse_args()


def _require_video_runtime() -> tuple[ModuleType, ModuleType, ModuleType]:
    try:
        imageio = importlib.import_module("imageio.v2")
        importlib.import_module("imageio_ffmpeg")
        image = importlib.import_module("PIL.Image")
        image_draw = importlib.import_module("PIL.ImageDraw")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "This script requires RIMKit's video extra. Install with: "
            'python -m pip install -e ".[video]"'
        ) from exc
    return imageio, image, image_draw


def _unit(vector: NDArray[np.float64]) -> NDArray[np.float64]:
    length = float(np.linalg.norm(vector))
    if length < 1e-12:
        raise ValueError("Camera vectors must not have zero length.")
    return vector / length


def _camera(points: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return an orthonormal world-to-camera basis and a root-following eye."""

    hips = points[SOMA77_JOINT_INDEX["Hips"]]
    target = hips + np.array([0.0, 0.0, 0.82])
    eye = hips + np.array([3.0, 5.0, 1.55])
    forward = _unit(target - eye)
    right = _unit(np.cross(forward, np.array([0.0, 0.0, 1.0])))
    up = _unit(np.cross(right, forward))
    return np.stack((right, up, forward)), eye


def _project(
    points: NDArray[np.float64],
    *,
    basis: NDArray[np.float64],
    eye: NDArray[np.float64],
    width: int,
    height: int,
) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    camera = (points - eye) @ basis.T
    depth = camera[:, 2]
    visible = depth > 0.1
    safe_depth = np.maximum(depth, 0.1)
    focal_length = float(min(width, height)) * 1.65
    screen = np.empty((len(points), 2), dtype=np.float64)
    screen[:, 0] = width * 0.53 + focal_length * camera[:, 0] / safe_depth
    screen[:, 1] = height * 0.45 - focal_length * camera[:, 1] / safe_depth
    return screen, visible


def _joint_color(name: str) -> tuple[int, int, int]:
    if name.startswith("Left"):
        return LEFT_COLOR
    if name.startswith("Right"):
        return RIGHT_COLOR
    return CENTER_COLOR


def _draw_grid(
    draw: Any,
    *,
    root: NDArray[np.float64],
    basis: NDArray[np.float64],
    eye: NDArray[np.float64],
    width: int,
    height: int,
) -> None:
    values = np.arange(-2.0, 2.01, 0.25)
    for axis in range(2):
        for offset in values:
            if axis == 0:
                line = np.array(
                    [[root[0] + offset, root[1] - 2.0, 0.0], [root[0] + offset, root[1] + 2.0, 0.0]]
                )
            else:
                line = np.array(
                    [[root[0] - 2.0, root[1] + offset, 0.0], [root[0] + 2.0, root[1] + offset, 0.0]]
                )
            projected, visible = _project(
                line, basis=basis, eye=eye, width=width, height=height
            )
            if bool(np.all(visible)):
                draw.line([tuple(projected[0]), tuple(projected[1])], fill=GRID_COLOR, width=1)


def _render_frame(
    points: NDArray[np.float64],
    *,
    frame_index: int,
    frame_count: int,
    seconds: float,
    width: int,
    height: int,
    image: ModuleType,
    image_draw: ModuleType,
) -> Any:
    canvas = image.new("RGB", (width, height), BACKGROUND)
    draw = image_draw.Draw(canvas)
    basis, eye = _camera(points)
    screen, visible = _project(points, basis=basis, eye=eye, width=width, height=height)
    _draw_grid(
        draw,
        root=points[SOMA77_JOINT_INDEX["Hips"]],
        basis=basis,
        eye=eye,
        width=width,
        height=height,
    )

    for child_index, (child_name, parent_name) in enumerate(SOMA77_JOINT_PARENTS):
        if parent_name is None:
            continue
        parent_index = SOMA77_JOINT_INDEX[parent_name]
        if visible[child_index] and visible[parent_index]:
            draw.line(
                [tuple(screen[parent_index]), tuple(screen[child_index])],
                fill=_joint_color(child_name),
                width=3,
            )

    for joint_index, (name, _) in enumerate(SOMA77_JOINT_PARENTS):
        if visible[joint_index]:
            x, y = screen[joint_index]
            radius = 4 if "Hand" not in name else 2
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=_joint_color(name),
            )

    draw.rounded_rectangle((24, 22, 350, 91), radius=10, fill=(13, 17, 23))
    draw.text((42, 37), "SOMA77 source skeleton", fill=CENTER_COLOR)
    draw.text(
        (42, 61),
        f"frame {frame_index + 1}/{frame_count}   {seconds:5.2f} s",
        fill=(180, 190, 204),
    )
    return canvas


def _validate_paths(args: argparse.Namespace) -> tuple[Path, Path | None]:
    if args.stride < 1:
        raise SystemExit("--stride must be at least 1.")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        raise SystemExit("--width and --height must be positive even numbers for H.264 output.")
    output = args.output.expanduser().resolve()
    thumbnail = None if args.thumbnail is None else args.thumbnail.expanduser().resolve()
    if output.suffix.lower() != ".mp4":
        raise SystemExit("--output must end in .mp4.")
    if thumbnail is not None and thumbnail.suffix.lower() != ".png":
        raise SystemExit("--thumbnail must end in .png.")
    if thumbnail == output:
        raise SystemExit("--output and --thumbnail must be different files.")
    for path in (output, thumbnail):
        if path is not None and path.exists() and not args.overwrite:
            raise SystemExit(f"Refusing to overwrite existing file: {path} (pass --overwrite to replace it)")
    return output, thumbnail


def main() -> None:
    args = _parse_args()
    output, thumbnail = _validate_paths(args)
    imageio, image, image_draw = _require_video_runtime()
    try:
        motion = load_soma_motion(args.input, fps_override=args.fps)
    except Exception as exc:
        raise SystemExit(f"Could not load SOMA77 motion: {exc}") from exc

    frame_indices = np.arange(0, motion.frame_count, args.stride, dtype=np.int64)
    output.parent.mkdir(parents=True, exist_ok=True)
    if thumbnail is not None:
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
    output_fps = motion.fps / args.stride
    with tempfile.TemporaryDirectory(prefix="rimkit-soma-render-", dir=output.parent) as temporary:
        temporary_path = Path(temporary)
        temporary_video = temporary_path / output.name
        writer = imageio.get_writer(
            temporary_video,
            fps=output_fps,
            codec="libx264",
            macro_block_size=1,
            ffmpeg_log_level="error",
        )
        thumbnail_image: Any | None = None
        middle_index = len(frame_indices) // 2
        try:
            for output_index, source_index in enumerate(frame_indices):
                frame = _render_frame(
                    motion.posed_joints[source_index],
                    frame_index=int(source_index),
                    frame_count=motion.frame_count,
                    seconds=float(motion.seconds[source_index]),
                    width=args.width,
                    height=args.height,
                    image=image,
                    image_draw=image_draw,
                )
                writer.append_data(np.asarray(frame))
                if output_index == middle_index:
                    thumbnail_image = frame.copy()
        finally:
            writer.close()
        temporary_video.replace(output)
        if thumbnail is not None and thumbnail_image is not None:
            temporary_thumbnail = temporary_path / thumbnail.name
            thumbnail_image.save(temporary_thumbnail)
            temporary_thumbnail.replace(thumbnail)

    print(f"frames: {len(frame_indices)}/{motion.frame_count}")
    print(f"fps: {output_fps:g}")
    print(f"video: {output}")
    if thumbnail is not None:
        print(f"thumbnail: {thumbnail}")


if __name__ == "__main__":
    main()
