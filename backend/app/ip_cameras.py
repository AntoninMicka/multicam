"""Backend-owned RTSP/HTTP cameras using the same verified upload pipeline."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .models import DeviceState, UploadCreate
from .storage_guard import check_storage


class IPCameraService:
    def __init__(self, store, uploads):
        self.store = store
        self.uploads = uploads
        self.jobs: dict[UUID, dict] = {}
        self.finishing: set[asyncio.Task] = set()

    def config_path(self) -> Path:
        return self.uploads.root / '.ip-cameras.json'

    def sources(self) -> dict:
        try:
            return json.loads(self.config_path().read_text())
        except FileNotFoundError:
            return {}

    def configure(self, device_id: UUID, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {'rtsp', 'http', 'https'} or not parsed.hostname:
            raise ValueError('IP kamera vyžaduje RTSP nebo HTTP(S) URL')
        check_storage()
        sources = self.sources()
        sources[str(device_id)] = url
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(sources))
        temporary.chmod(0o600)
        os.replace(temporary, path)

    @staticmethod
    def input_args(url: str) -> list[str]:
        args = ['-protocol_whitelist', 'http,https,tcp,tls,udp,rtp,rtsp,crypto']
        if urlsplit(url).scheme == 'rtsp':
            args += ['-rtsp_transport', 'tcp', '-timeout', '10000000']
        else:
            args += ['-rw_timeout', '10000000']
        return args + ['-i', url]

    async def arm(self, session_id: UUID) -> list[dict]:
        session = await self.store.get(session_id)
        if any(job['metadata']['session_id'] == str(session_id) for job in self.jobs.values()):
            raise ValueError('IP kamery ještě dokončují předchozí záznam')
        result = []
        for device_id, url in self.sources().items():
            if device_id not in session.devices:
                continue
            try:
                process = await asyncio.create_subprocess_exec(
                    'ffprobe', '-v', 'error', *self.input_args(url), '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_name,width,height', '-of', 'json',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    stdout, _ = await asyncio.wait_for(process.communicate(), 15)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
                streams = json.loads(stdout).get('streams', [])
                if process.returncode or not streams or not streams[0].get('width'):
                    raise ValueError('Kamera neposkytuje čitelný video stream')
                await self.store.set_device_state(session_id, UUID(device_id), DeviceState.ARMED)
                result.append({'device_id': device_id, 'status': 'ready'})
            except (OSError, ValueError, TimeoutError):
                await self.store.set_device_state(session_id, UUID(device_id), DeviceState.READY)
                result.append({'device_id': device_id, 'status': 'error', 'detail': 'IP kamera není dostupná nebo nemá čitelný video stream.'})
        return result

    async def start(self, session_id: UUID, take_id: UUID) -> None:
        session = await self.store.get(session_id)
        for device_id, url in self.sources().items():
            key = UUID(device_id)
            if device_id not in session.devices or key in self.jobs:
                continue
            check_storage()
            capture_id = uuid4()
            directory = self.uploads.root / str(session_id) / '.ip-camera-jobs' / str(capture_id)
            directory.mkdir(parents=True)
            path = directory / 'recording.mp4'
            metadata = {'capture_id': str(capture_id), 'session_id': str(session_id),
                        'device_id': device_id, 'take_id': str(take_id), 'state': 'recording'}
            (directory / 'job.json').write_text(json.dumps(metadata))
            log = (directory / 'ffmpeg.log').open('wb')
            try:
                process = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *self.input_args(url),
                    '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-c:a', 'aac',
                    '-movflags', '+frag_keyframe+empty_moov+default_base_moof', str(path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=log)
            except BaseException:
                log.close()
                metadata['state'] = 'failed'
                (directory / 'job.json').write_text(json.dumps(metadata))
                raise
            log.close()
            self.jobs[key] = {'process': process, 'path': path, 'metadata': metadata}
            await self.store.set_device_state(session_id, key, DeviceState.RECORDING)
            task = asyncio.create_task(self._watch(key, self.jobs[key]))
            self.finishing.add(task)
            task.add_done_callback(self.finishing.discard)

    async def _watch(self, device_id: UUID, job: dict) -> None:
        await job['process'].wait()
        meta, path = job['metadata'], job['path']
        session_id = UUID(meta['session_id'])
        try:
            if job['process'].returncode != 0 or not path.is_file() or not path.stat().st_size:
                raise ValueError('IP camera stream ended with an error')
            await self.store.set_device_state(session_id, device_id, DeviceState.STORED)
            timing = path.with_name('timing.jsonl')
            timing.write_text(json.dumps({'event': 'recording_started', 'recording_offset_ms': 0,
                                          'details': {'source': 'ip_camera', 'take_id': meta['take_id']}}) + '\n')
            for kind, source, mime in [('recording', path, 'video/mp4'), ('telemetry', timing, 'application/x-ndjson')]:
                digest = hashlib.sha256()
                with source.open('rb') as file:
                    for block in iter(lambda: file.read(1024 * 1024), b''):
                        digest.update(block)
                chunk_size = 1024 * 1024
                size = source.stat().st_size
                upload = await self.uploads.create(session_id, device_id, UploadCreate(
                    capture_id=UUID(meta['capture_id']), take_id=UUID(meta['take_id']), kind=kind,
                    file_name=source.name, mime_type=mime, size_bytes=size, sha256=digest.hexdigest(),
                    chunk_size=chunk_size, total_chunks=(size + chunk_size - 1) // chunk_size))
                with source.open('rb') as file:
                    for index in range(upload.total_chunks):
                        content = file.read(chunk_size)
                        await self.uploads.put_chunk(session_id, device_id, upload.upload_id, index, content, hashlib.sha256(content).hexdigest())
                await self.uploads.complete(session_id, device_id, upload.upload_id)
            await self.store.set_device_state(session_id, device_id, DeviceState.VERIFIED)
            meta['state'] = 'verified'
        except Exception as error:
            meta['state'] = 'failed'
            meta['error'] = type(error).__name__  # Do not leak URL credentials to the UI.
            await self.store.set_device_state(session_id, device_id, DeviceState.FAILED)
        finally:
            self.jobs.pop(device_id, None)
            path.with_name('job.json').write_text(json.dumps(meta))

    async def stop(self, session_id: UUID | None = None) -> None:
        for job in list(self.jobs.values()):
            if session_id and job['metadata']['session_id'] != str(session_id):
                continue
            process = job['process']
            if process.returncode is None:
                try:
                    process.stdin.write(b'q\n')
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), 15)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
