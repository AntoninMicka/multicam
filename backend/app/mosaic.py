from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path


class MosaicError(RuntimeError):
    pass


def grid_size(count: int) -> tuple[int, int]:
    if count < 1:
        raise MosaicError("Mosaic needs at least one video")
    columns = math.ceil(math.sqrt(count))
    return columns, math.ceil(count / columns)


def mosaic_filter(count: int, tile_width: int = 640, tile_height: int = 360) -> tuple[str, str]:
    columns, rows = grid_size(count)
    filters = []
    inputs = []
    layout = []
    for index in range(count):
        filters.append(
            f"[{index}:v]scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[v{index}]"
        )
        inputs.append(f"[v{index}]")
        layout.append(f"{index % columns * tile_width}_{index // columns * tile_height}")
    # Blank cells are unnecessary: xstack uses the calculated canvas occupied
    # by actual inputs and therefore avoids adding synthetic video sources.
    if count == 1:
        filters.append("[v0]null[outv]")
    else:
        filters.append(
            f"{''.join(inputs)}xstack=inputs={count}:layout={'|'.join(layout)}:fill=black[outv]"
        )
    return ";".join(filters), f"{columns * tile_width}x{rows * tile_height}"


def render_mosaic(
    sources: list[Path],
    sync_points: list[float | None],
    destination: Path,
    audio_input_index: int | None,
) -> Path:
    if len(sources) != len(sync_points) or not sources:
        raise MosaicError("Mosaic source metadata is inconsistent")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source, sync_point in zip(sources, sync_points, strict=True):
        if sync_point is not None and sync_point > 0:
            command.extend(["-ss", f"{sync_point:.6f}"])
        command.extend(["-i", str(source)])
    filter_graph, _ = mosaic_filter(len(sources))
    command.extend([
        "-filter_complex", filter_graph,
        "-map", "[outv]",
    ])
    if audio_input_index is not None:
        command.extend(["-map", f"{audio_input_index}:a:0?"])
    command.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-shortest", "-f", "mp4", str(temporary),
    ])
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise MosaicError(result.stderr.strip()[-1000:] or "FFmpeg did not create the mosaic")
    os.replace(temporary, destination)
    return destination
