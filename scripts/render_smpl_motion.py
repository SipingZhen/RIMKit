#!/usr/bin/env python3
"""Render a standard SMPL-family parameter motion as a headless mesh video.

The script intentionally does not ship an SMPL body model.  Obtain the model
files under the relevant licence and pass their directory explicitly.

Examples:
    # Standard SMPL 72-D poses stored in motion.npz as poses/trans/betas.
    conda run -n rimkit python scripts/render_smpl_motion.py motion.npz \
        --model-dir /path/to/smpl_models --model-type smpl \
        --output runs/source-visualizations/motion/smpl.mp4 \
        --thumbnail runs/source-visualizations/motion/smpl.png

    # SMPL-X parameters stored as explicit global_orient/body_pose fields.
    conda run -n rimkit python scripts/render_smpl_motion.py motion.npz \
        --model-dir /path/to/smplx_models --model-type smplx \
        --output runs/source-visualizations/motion/smplx.mp4

Supported input is a non-pickle ``.npz`` archive.  It may contain a combined
``poses``/``pose`` array, or explicit SMPL-X-style parameter arrays.  The
script accepts ``transl``, ``trans``, or ``translation`` for root translation,
and an optional scalar ``fps``.  It renders using Pillow rather than OpenGL.
"""

from __future__ import annotations

import argparse
import importlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray


BACKGROUND = (20, 25, 33)
GRID_COLOR = (55, 64, 76)
MESH_COLOR = np.array((87, 176, 238), dtype=np.float64)
LIGHT_DIRECTION = np.array((-0.4, -0.5, 0.75), dtype=np.float64)


@dataclass(frozen=True)
class SmplParameters:
    """Per-frame SMPL-family inputs normalized to axis-angle arrays."""

    frame_count: int
    fps: float
    global_orient: NDArray[np.float64]
    body_pose: NDArray[np.float64]
    transl: NDArray[np.float64]
    betas: NDArray[np.float64]
    left_hand_pose: NDArray[np.float64] | None = None
    right_hand_pose: NDArray[np.float64] | None = None
    jaw_pose: NDArray[np.float64] | None = None
    leye_pose: NDArray[np.float64] | None = None
    reye_pose: NDArray[np.float64] | None = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="SMPL-family parameter .npz")
    parser.add_argument("--model-dir", required=True, type=Path, help="Licensed SMPL model directory")
    parser.add_argument("--model-type", choices=("smpl", "smplh", "smplx"), default="smpl")
    parser.add_argument("--gender", choices=("neutral", "male", "female"), default="neutral")
    parser.add_argument("--output", required=True, type=Path, help="Output .mp4 path")
    parser.add_argument("--thumbnail", type=Path, help="Optional middle-frame .png path")
    parser.add_argument("--fps", type=float, help="Override the archive FPS")
    parser.add_argument("--stride", type=int, default=1, help="Render every Nth source frame")
    parser.add_argument("--face-stride", type=int, default=4, help="Draw every Nth mesh triangle")
    parser.add_argument("--width", type=int, default=1280, help="Even output width in pixels")
    parser.add_argument("--height", type=int, default=720, help="Even output height in pixels")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output files")
    return parser.parse_args()


def _require_runtime() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType, ModuleType]:
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
    try:
        smplx = importlib.import_module("smplx")
        torch = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "SMPL rendering also requires smplx and PyTorch. Install with: "
            "python -m pip install smplx torch"
        ) from exc
    return imageio, image, image_draw, smplx, torch


def _numeric(archive: Any, name: str, *, required: bool = False) -> NDArray[np.float64] | None:
    if name not in archive.files:
        if required:
            raise ValueError(f"Missing required SMPL parameter: {name}")
        return None
    value = np.asarray(archive[name])
    if value.dtype.hasobject or not np.issubdtype(value.dtype, np.number):
        raise ValueError(f"{name} must be a real numeric array, not an object array.")
    if np.issubdtype(value.dtype, np.complexfloating) or not np.isfinite(value).all():
        raise ValueError(f"{name} contains non-real, NaN, or infinite values.")
    return value.astype(np.float64, copy=False)


def _frames_by_width(values: NDArray[np.float64], *, name: str, width: int) -> NDArray[np.float64]:
    if values.ndim == 3 and values.shape[-1] == 3:
        values = values.reshape(values.shape[0], -1)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.ndim != 2 or values.shape[1] != width:
        raise ValueError(f"{name} must have shape (T, {width}) or (T, {width // 3}, 3); found {values.shape}.")
    return values


def _broadcast(values: NDArray[np.float64], *, frame_count: int, name: str) -> NDArray[np.float64]:
    if len(values) == frame_count:
        return values
    if len(values) == 1:
        return np.broadcast_to(values, (frame_count, values.shape[1])).copy()
    raise ValueError(f"{name} has {len(values)} frames, but the motion has {frame_count}.")


def _combined_pose(archive: Any) -> NDArray[np.float64] | None:
    for name in ("poses", "pose", "theta"):
        candidate = _numeric(archive, name)
        if candidate is not None:
            if candidate.ndim == 3 and candidate.shape[-1] == 3:
                candidate = candidate.reshape(candidate.shape[0], -1)
            if candidate.ndim == 1:
                candidate = candidate.reshape(1, -1)
            if candidate.ndim != 2:
                raise ValueError(f"{name} must be a two-dimensional pose array; found {candidate.shape}.")
            return candidate
    return None


def _optional_pose(
    archive: Any, name: str, *, frame_count: int, width: int
) -> NDArray[np.float64] | None:
    value = _numeric(archive, name)
    if value is None:
        return None
    return _broadcast(_frames_by_width(value, name=name, width=width), frame_count=frame_count, name=name)


def _load_parameters(path: Path, *, model_type: str, fps_override: float | None) -> SmplParameters:
    input_path = path.expanduser().resolve()
    if input_path.suffix.lower() != ".npz":
        raise ValueError("SMPL input must be a .npz archive; pickle-based files are intentionally unsupported.")
    if not input_path.is_file():
        raise ValueError(f"SMPL motion file does not exist: {input_path}")
    with np.load(input_path, allow_pickle=False) as archive:
        combined = _combined_pose(archive)
        global_orient = _numeric(archive, "global_orient")
        body_pose = _numeric(archive, "body_pose")
        body_width = 69 if model_type == "smpl" else 63
        if combined is not None:
            expected_width = {"smpl": 72, "smplh": 156, "smplx": 165}[model_type]
            if combined.shape[1] != expected_width:
                raise ValueError(
                    f"Combined pose for {model_type} must have {expected_width} values; found {combined.shape[1]}."
                )
            frame_count = len(combined)
            global_orient = combined[:, :3] if global_orient is None else global_orient
            body_pose = combined[:, 3 : 3 + body_width] if body_pose is None else body_pose
        elif global_orient is not None and body_pose is not None:
            frame_count = max(len(np.atleast_2d(global_orient)), len(np.atleast_2d(body_pose)))
        else:
            raise ValueError(
                "Provide poses/pose/theta, or both global_orient and body_pose in the input archive."
            )

        assert global_orient is not None and body_pose is not None
        global_orient = _broadcast(
            _frames_by_width(global_orient, name="global_orient", width=3),
            frame_count=frame_count,
            name="global_orient",
        )
        body_pose = _broadcast(
            _frames_by_width(body_pose, name="body_pose", width=body_width),
            frame_count=frame_count,
            name="body_pose",
        )

        translation = next(
            (_numeric(archive, name) for name in ("transl", "trans", "translation") if name in archive.files),
            None,
        )
        if translation is None:
            transl = np.zeros((frame_count, 3), dtype=np.float64)
        else:
            transl = _broadcast(
                _frames_by_width(translation, name="translation", width=3),
                frame_count=frame_count,
                name="translation",
            )
        beta_values = _numeric(archive, "betas")
        if beta_values is None:
            betas = np.zeros((frame_count, 10), dtype=np.float64)
        else:
            if beta_values.ndim == 1:
                beta_values = beta_values.reshape(1, -1)
            if beta_values.ndim != 2 or beta_values.shape[1] < 1:
                raise ValueError(f"betas must have shape (B,) or (T, B); found {beta_values.shape}.")
            betas = _broadcast(beta_values, frame_count=frame_count, name="betas")
        archive_fps = _numeric(archive, "fps")
        if fps_override is None and archive_fps is not None:
            if archive_fps.size != 1:
                raise ValueError("fps must be a scalar.")
            fps = float(archive_fps.reshape(-1)[0])
        else:
            fps = 30.0 if fps_override is None else float(fps_override)

        extras: dict[str, NDArray[np.float64] | None] = {
            "left_hand_pose": None,
            "right_hand_pose": None,
            "jaw_pose": None,
            "leye_pose": None,
            "reye_pose": None,
        }
        if model_type in {"smplh", "smplx"}:
            hand_offset = 3 + body_width
            if combined is not None:
                extras["left_hand_pose"] = combined[:, hand_offset : hand_offset + 45]
                extras["right_hand_pose"] = combined[:, hand_offset + 45 : hand_offset + 90]
            for name in ("left_hand_pose", "right_hand_pose"):
                explicit = _optional_pose(archive, name, frame_count=frame_count, width=45)
                if explicit is not None:
                    extras[name] = explicit
        if model_type == "smplx":
            if combined is not None:
                extras["jaw_pose"] = combined[:, 66:69]
                extras["leye_pose"] = combined[:, 69:72]
                extras["reye_pose"] = combined[:, 72:75]
                extras["left_hand_pose"] = combined[:, 75:120]
                extras["right_hand_pose"] = combined[:, 120:165]
            for name in ("jaw_pose", "leye_pose", "reye_pose"):
                explicit = _optional_pose(archive, name, frame_count=frame_count, width=3)
                if explicit is not None:
                    extras[name] = explicit
    if not np.isfinite(fps) or not 0.0 < fps <= 1000.0:
        raise ValueError(f"fps must be in (0, 1000], found {fps}.")
    return SmplParameters(
        frame_count=frame_count,
        fps=fps,
        global_orient=global_orient,
        body_pose=body_pose,
        transl=transl,
        betas=betas,
        **extras,
    )


def _unit(vector: NDArray[np.float64]) -> NDArray[np.float64]:
    length = float(np.linalg.norm(vector))
    if length < 1e-12:
        raise ValueError("Camera vectors must not have zero length.")
    return vector / length


def _z_up(vertices: NDArray[np.float64]) -> NDArray[np.float64]:
    """Rotate the common SMPL Y-up convention into the renderer's Z-up world."""

    return vertices[:, (0, 2, 1)] * np.array((1.0, -1.0, 1.0))


def _project_mesh(
    vertices: NDArray[np.float64], *, width: int, height: int
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    root = vertices[0]
    target = root + np.array((0.0, 0.0, 0.82))
    eye = root + np.array((3.0, 5.0, 1.55))
    forward = _unit(target - eye)
    right = _unit(np.cross(forward, np.array((0.0, 0.0, 1.0))))
    up = _unit(np.cross(right, forward))
    basis = np.stack((right, up, forward))
    camera = (vertices - eye) @ basis.T
    depth = camera[:, 2]
    focal_length = float(min(width, height)) * 1.65
    screen = np.empty((len(vertices), 2), dtype=np.float64)
    safe_depth = np.maximum(depth, 0.1)
    screen[:, 0] = width * 0.53 + focal_length * camera[:, 0] / safe_depth
    screen[:, 1] = height * 0.45 - focal_length * camera[:, 1] / safe_depth
    return screen, depth, basis, eye


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
    focal_length = float(min(width, height)) * 1.65
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
            camera = (line - eye) @ basis.T
            if np.any(camera[:, 2] <= 0.1):
                continue
            screen = np.empty((2, 2), dtype=np.float64)
            screen[:, 0] = width * 0.53 + focal_length * camera[:, 0] / camera[:, 2]
            screen[:, 1] = height * 0.45 - focal_length * camera[:, 1] / camera[:, 2]
            draw.line([tuple(screen[0]), tuple(screen[1])], fill=GRID_COLOR, width=1)


def _render_frame(
    vertices: NDArray[np.float64],
    faces: NDArray[np.int64],
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
    screen, depth, basis, eye = _project_mesh(vertices, width=width, height=height)
    _draw_grid(draw, root=vertices[0], basis=basis, eye=eye, width=width, height=height)

    face_vertices = vertices[faces]
    camera_vertices = (face_vertices - eye) @ basis.T
    normals = np.cross(camera_vertices[:, 1] - camera_vertices[:, 0], camera_vertices[:, 2] - camera_vertices[:, 0])
    normal_lengths = np.linalg.norm(normals, axis=1)
    face_depth = depth[faces].mean(axis=1)
    visible = np.all(depth[faces] > 0.1, axis=1) & (normal_lengths > 1e-12) & (normals[:, 2] < 0.0)
    light = _unit(LIGHT_DIRECTION)
    world_normals = np.cross(face_vertices[:, 1] - face_vertices[:, 0], face_vertices[:, 2] - face_vertices[:, 0])
    world_normals /= np.maximum(np.linalg.norm(world_normals, axis=1, keepdims=True), 1e-12)
    brightness = np.clip(0.30 + 0.70 * (world_normals @ light), 0.15, 1.0)
    for face_index in np.argsort(face_depth[visible])[::-1]:
        selected = np.flatnonzero(visible)[face_index]
        color = tuple(np.clip(MESH_COLOR * brightness[selected], 0, 255).astype(np.uint8))
        draw.polygon([tuple(point) for point in screen[faces[selected]]], fill=color)

    draw.rounded_rectangle((24, 22, 365, 91), radius=10, fill=(13, 17, 23))
    draw.text((42, 37), "SMPL source mesh", fill=(236, 239, 244))
    draw.text(
        (42, 61),
        f"frame {frame_index + 1}/{frame_count}   {seconds:5.2f} s",
        fill=(180, 190, 204),
    )
    return canvas


def _model_kwargs(parameters: SmplParameters, frame_index: int, torch: ModuleType) -> dict[str, Any]:
    values: dict[str, Any] = {
        "global_orient": torch.as_tensor(parameters.global_orient[frame_index : frame_index + 1], dtype=torch.float32),
        "body_pose": torch.as_tensor(parameters.body_pose[frame_index : frame_index + 1], dtype=torch.float32),
        "transl": torch.as_tensor(parameters.transl[frame_index : frame_index + 1], dtype=torch.float32),
        "betas": torch.as_tensor(parameters.betas[frame_index : frame_index + 1], dtype=torch.float32),
        "return_verts": True,
    }
    for name in ("left_hand_pose", "right_hand_pose", "jaw_pose", "leye_pose", "reye_pose"):
        value = getattr(parameters, name)
        if value is not None:
            values[name] = torch.as_tensor(value[frame_index : frame_index + 1], dtype=torch.float32)
    return values


def _validate_paths(args: argparse.Namespace) -> tuple[Path, Path | None]:
    if args.stride < 1 or args.face_stride < 1:
        raise SystemExit("--stride and --face-stride must both be at least 1.")
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
    model_dir = args.model_dir.expanduser().resolve()
    if not model_dir.is_dir():
        raise SystemExit(f"--model-dir is not an existing directory: {model_dir}")
    try:
        parameters = _load_parameters(args.input, model_type=args.model_type, fps_override=args.fps)
    except Exception as exc:
        raise SystemExit(f"Could not load SMPL parameters: {exc}") from exc
    imageio, image, image_draw, smplx, torch = _require_runtime()
    try:
        model = smplx.create(
            str(model_dir),
            model_type=args.model_type,
            gender=args.gender,
            num_betas=int(parameters.betas.shape[1]),
            use_pca=False,
        )
        model.eval()
    except Exception as exc:
        raise SystemExit(
            f"Could not create {args.model_type} model from {model_dir}: {exc}\n"
            "Check that this is the licensed SMPL model root expected by smplx."
        ) from exc

    faces = np.asarray(model.faces, dtype=np.int64)[:: args.face_stride]
    if len(faces) == 0:
        raise SystemExit("No mesh faces remain after --face-stride.")
    frame_indices = np.arange(0, parameters.frame_count, args.stride, dtype=np.int64)
    output.parent.mkdir(parents=True, exist_ok=True)
    if thumbnail is not None:
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
    output_fps = parameters.fps / args.stride
    with tempfile.TemporaryDirectory(prefix="rimkit-smpl-render-", dir=output.parent) as temporary:
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
            with torch.no_grad():
                for output_index, source_index in enumerate(frame_indices):
                    result = model(**_model_kwargs(parameters, int(source_index), torch))
                    vertices = result.vertices[0].detach().cpu().numpy().astype(np.float64, copy=False)
                    frame = _render_frame(
                        _z_up(vertices),
                        faces,
                        frame_index=int(source_index),
                        frame_count=parameters.frame_count,
                        seconds=float(source_index) / parameters.fps,
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

    print(f"frames: {len(frame_indices)}/{parameters.frame_count}")
    print(f"fps: {output_fps:g}")
    print(f"video: {output}")
    if thumbnail is not None:
        print(f"thumbnail: {thumbnail}")


if __name__ == "__main__":
    main()
