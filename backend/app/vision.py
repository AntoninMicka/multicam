from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


DEFAULT_PROMPT = (
    "Popiš tento snímek z top-down kamery. Identifikuj scénu, viditelné osoby, "
    "kamery, další sledovatelné objekty, jejich činnost a nejistoty. Nevymýšlej "
    "identity ani přesné metrické souřadnice. Odpověz česky podle JSON schématu."
)


class VisionRequest(BaseModel):
    backend: Literal["prepare", "ollama", "comfyui"] = "prepare"
    model: str | None = Field(default=None, max_length=160)
    prompt: str = Field(default=DEFAULT_PROMPT, min_length=1, max_length=4000)
    sample_interval_s: float = Field(default=2.0, ge=0.25, le=60)
    max_frames: int = Field(default=12, ge=1, le=60)


class VisionError(RuntimeError):
    pass


FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string"},
        "activity": {"type": "string"},
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "class": {"type": "string"},
                    "description": {"type": "string"},
                    "approximate_image_region": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["class", "description", "approximate_image_region", "confidence"],
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scene", "activity", "objects", "uncertainties"],
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _frame_times(video: Path, interval: float, maximum: int) -> list[float]:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "frame=best_effort_timestamp_time", "-of", "json", str(video),
    ], capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise VisionError(result.stderr.strip()[-500:] or "FFprobe failed")
    try:
        candidates = [float(frame["best_effort_timestamp_time"]) for frame in json.loads(result.stdout)["frames"]]
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise VisionError("Video frame timestamps are unavailable") from error
    selected: list[float] = []
    for timestamp in candidates:
        if not selected or timestamp - selected[-1] >= interval:
            selected.append(timestamp)
            if len(selected) >= maximum:
                break
    return selected


def prepare_frames(video: Path, job_dir: Path, interval: float, maximum: int) -> list[dict]:
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, timestamp in enumerate(_frame_times(video, interval, maximum)):
        destination = frames_dir / f"frame-{index:04d}.jpg"
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp:.6f}",
            "-i", str(video), "-frames:v", "1", "-vf",
            "scale=1280:1280:force_original_aspect_ratio=decrease", "-q:v", "3", str(destination),
        ], capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0 or not destination.is_file():
            raise VisionError(result.stderr.strip()[-500:] or f"Could not extract frame at {timestamp}")
        frames.append({"index": index, "source_pts_s": round(timestamp, 6), "file": str(destination.relative_to(job_dir))})
    if not frames:
        raise VisionError("No video frames were extracted")
    return frames


def _local_url(env_name: str, default: str) -> str:
    url = os.environ.get(env_name, default).rstrip("/")
    parsed = urllib.parse.urlparse(url)
    allowed = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not allowed and os.environ.get("MULTICAM_ALLOW_REMOTE_ANALYSIS") != "1":
        raise VisionError(f"{env_name} must be a loopback HTTP URL")
    return url


def _json_request(url: str, payload: dict, timeout: int = 600) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise VisionError(f"Analysis service request failed: {error}") from error


def _get_json(url: str, timeout: int = 30) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise VisionError(f"Analysis service request failed: {error}") from error


def analyze_ollama(job_dir: Path, frames: list[dict], request: VisionRequest) -> dict:
    model = request.model or os.environ.get("MULTICAM_OLLAMA_MODEL")
    if not model:
        raise VisionError("Ollama model is not configured")
    endpoint = _local_url("MULTICAM_OLLAMA_URL", "http://127.0.0.1:11434")
    results = []
    for frame in frames:
        image = base64.b64encode((job_dir / frame["file"]).read_bytes()).decode()
        response = _json_request(f"{endpoint}/api/chat", {
            "model": model,
            "stream": False,
            "format": FRAME_SCHEMA,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": request.prompt, "images": [image]}],
        })
        content = response.get("message", {}).get("content", "")
        try:
            description: Any = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            description = {"unparsed_content": content}
        results.append({**frame, "description": description, "raw_response": response})
    return {"backend": "ollama", "model": model, "frames": results}


def _replace_placeholder(value: Any, image_name: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{INPUT_IMAGE}}", image_name)
    if isinstance(value, list):
        return [_replace_placeholder(item, image_name) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholder(item, image_name) for key, item in value.items()}
    return value


def _upload_comfy_image(endpoint: str, path: Path) -> dict:
    boundary = f"----multicam-{uuid4().hex}"
    content = path.read_bytes()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{endpoint}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise VisionError(f"ComfyUI image upload failed: {error}") from error


def submit_comfyui(job_dir: Path, frames: list[dict], request: VisionRequest) -> dict:
    workflow_path = os.environ.get("MULTICAM_COMFY_WORKFLOW")
    if not workflow_path:
        raise VisionError("MULTICAM_COMFY_WORKFLOW is not configured")
    try:
        workflow = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VisionError("ComfyUI API workflow cannot be read") from error
    endpoint = _local_url("MULTICAM_COMFY_URL", "http://127.0.0.1:8188")
    submissions = []
    for frame in frames:
        uploaded = _upload_comfy_image(endpoint, job_dir / frame["file"])
        image_name = uploaded.get("name")
        if not image_name:
            raise VisionError("ComfyUI did not return an uploaded image name")
        response = _json_request(f"{endpoint}/prompt", {
            "prompt": _replace_placeholder(workflow, image_name), "client_id": str(uuid4()),
        })
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise VisionError("ComfyUI did not return a prompt_id")
        history = None
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            candidate = _get_json(f"{endpoint}/history/{prompt_id}")
            if prompt_id in candidate:
                history = candidate[prompt_id]
                break
            time.sleep(1)
        if history is None:
            raise VisionError(f"ComfyUI prompt {prompt_id} did not finish in time")
        submissions.append({**frame, "uploaded": uploaded, "prompt_id": prompt_id, "history": history, "raw_response": response})
    return {"backend": "comfyui", "workflow": workflow_path, "submissions": submissions}


def run_vision_job(video: Path, job_dir: Path, request: VisionRequest, metadata: dict) -> None:
    started = datetime.now(timezone.utc).isoformat()
    _write_json(job_dir / "job.json", {**metadata, "status": "running", "started_at": started, "request": request.model_dump()})
    try:
        frames = prepare_frames(video, job_dir, request.sample_interval_s, request.max_frames)
        _write_json(job_dir / "frames.json", {"frames": frames})
        if request.backend == "ollama":
            result = analyze_ollama(job_dir, frames, request)
        elif request.backend == "comfyui":
            result = submit_comfyui(job_dir, frames, request)
        else:
            result = {"backend": "prepare", "frames": frames}
        _write_json(job_dir / "description.json", result)
        _write_json(job_dir / "job.json", {
            **metadata, "status": "completed", "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(), "request": request.model_dump(),
            "result_file": "description.json",
        })
    except Exception as error:
        _write_json(job_dir / "job.json", {
            **metadata, "status": "failed", "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(), "request": request.model_dump(),
            "error": str(error),
        })
