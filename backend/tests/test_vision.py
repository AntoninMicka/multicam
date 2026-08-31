import json
from pathlib import Path

from app.vision import VisionRequest, run_vision_job


def test_prepare_job_persists_frames_and_provenance(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "top.webm"
    video.write_bytes(b"video")
    job = tmp_path / "job"

    def fake_prepare(video_path: Path, job_dir: Path, interval: float, maximum: int) -> list[dict]:
        frames = job_dir / "frames"
        frames.mkdir(parents=True)
        (frames / "frame-0000.jpg").write_bytes(b"jpeg")
        return [{"index": 0, "source_pts_s": 1.25, "file": "frames/frame-0000.jpg"}]

    monkeypatch.setattr("app.vision.prepare_frames", fake_prepare)
    run_vision_job(video, job, VisionRequest(backend="prepare"), {"job_id": "test"})

    state = json.loads((job / "job.json").read_text())
    result = json.loads((job / "description.json").read_text())
    assert state["status"] == "completed"
    assert state["request"]["backend"] == "prepare"
    assert result["frames"][0]["source_pts_s"] == 1.25
