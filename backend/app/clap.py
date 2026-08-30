import re
import statistics
import subprocess
from pathlib import Path


PTS = re.compile(r"pts_time:([0-9.]+)")
YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def detect_flash(path: Path, expected_seconds: float, radius: float = 1.5) -> dict:
    start = max(0.0, expected_seconds - radius)
    duration = radius * 2
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", f"trim=start={start}:duration={duration},setpts=PTS-STARTPTS,fps=20,scale=64:-1,signalstats,metadata=print:file=-",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        return {"status": "error", "detail": result.stderr.strip()[-500:]}
    samples: list[tuple[float, float]] = []
    current_time: float | None = None
    for line in result.stdout.splitlines():
        pts = PTS.search(line)
        if pts:
            current_time = float(pts.group(1)) + start
        average = YAVG.search(line)
        if average and current_time is not None:
            samples.append((current_time, float(average.group(1))))
    if len(samples) < 4:
        return {"status": "not_found", "detail": "Nedostatek dekódovaných snímků."}
    candidates: list[tuple[float, float]] = []
    for index in range(3, len(samples)):
        baseline = statistics.median(value for _, value in samples[max(0, index - 6):index])
        candidates.append((samples[index][1] - baseline, samples[index][0]))
    increase, timestamp = max(candidates)
    return {
        "status": "detected" if increase >= 8 else "low_confidence",
        "flash_seconds": round(timestamp, 4),
        "brightness_jump": round(increase, 3),
        "confidence": round(max(0.0, min(1.0, increase / 64)), 3),
    }
