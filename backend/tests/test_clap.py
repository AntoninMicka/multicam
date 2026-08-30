from pathlib import Path
from types import SimpleNamespace

from app.clap import detect_flash


def test_detect_flash_uses_brightness_jump(monkeypatch) -> None:
    output = "\n".join([
        "frame:0 pts:0 pts_time:0.00", "lavfi.signalstats.YAVG=20",
        "frame:1 pts:1 pts_time:0.05", "lavfi.signalstats.YAVG=21",
        "frame:2 pts:2 pts_time:0.10", "lavfi.signalstats.YAVG=20",
        "frame:3 pts:3 pts_time:0.15", "lavfi.signalstats.YAVG=90",
        "frame:4 pts:4 pts_time:0.20", "lavfi.signalstats.YAVG=22",
    ])
    monkeypatch.setattr("app.clap.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=output, stderr="",
    ))

    result = detect_flash(Path("video.webm"), expected_seconds=2.0)

    assert result["status"] == "detected"
    assert result["flash_seconds"] == 0.65
    assert result["brightness_jump"] == 70
