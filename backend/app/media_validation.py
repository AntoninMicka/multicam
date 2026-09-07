"""Independent structural verification after transport checksums have passed."""
import json
import subprocess
from pathlib import Path


class MediaValidationError(ValueError):
    code = 'media_validation_failed'


def webm_doctype(header: bytes) -> str:
    def vint(offset: int, size: bool) -> tuple[int, int]:
        if offset >= len(header) or not header[offset]:
            raise ValueError("Invalid EBML integer")
        width = 9 - header[offset].bit_length()
        end = offset + width
        if end > len(header):
            raise ValueError("Truncated EBML header")
        value = int.from_bytes(header[offset:end], "big")
        if size:
            value &= (1 << (7 * width)) - 1
        return value, end
    length, cursor = vint(4, True)
    end = cursor + length
    while cursor < min(end, len(header)):
        element, cursor = vint(cursor, False)
        size, cursor = vint(cursor, True)
        if cursor + size > min(end, len(header)):
            raise ValueError("Truncated EBML element")
        if element == 0x4282:
            return header[cursor:cursor + size].decode("ascii")
        cursor += size
    raise ValueError("Missing EBML DocType")


def validate_media(path: Path, mime_type: str) -> dict:
    expected = mime_type.lower().split(';', 1)[0].strip()
    if expected not in {'video/webm', 'video/mp4', 'video/quicktime'}:
        raise MediaValidationError(f'Nepodporovaný deklarovaný kontejner: {expected}')
    if expected == 'video/webm':
        with path.open('rb') as source:
            header = source.read(4096)
        if not header.startswith(bytes.fromhex('1a45dfa3')):
            raise MediaValidationError('WebM nemá úvodní EBML hlavičku; pravděpodobně chybí inicializační blok.')
        try:
            if webm_doctype(header) != 'webm':
                raise ValueError('Not WebM')
        except ValueError as error:
            raise MediaValidationError('EBML hlavička nedeklaruje platný kontejner WebM.') from error
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'stream=index,codec_name,codec_type,width,height',
            '-show_entries', 'format=format_name,duration', '-of', 'json', str(path),
        ], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaValidationError('Médium nelze ověřit: FFprobe není dostupné nebo vypršel časový limit.') from error
    if result.returncode != 0 or result.stderr.strip():
        raise MediaValidationError(f'FFprobe odmítlo záznam: {result.stderr.strip()[-800:]}')
    try:
        probe = json.loads(result.stdout)
        formats = set(probe.get('format', {}).get('format_name', '').split(','))
        allowed = {'webm'} if expected == 'video/webm' else {'mov', 'mp4'}
        if not formats.intersection(allowed):
            raise MediaValidationError(f'Kontejner neodpovídá MIME {expected}.')
        videos = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'video']
        if not any(stream.get('codec_name') not in {None, 'unknown'} and stream.get('width', 0) > 0 and stream.get('height', 0) > 0 for stream in videos):
            raise MediaValidationError('Soubor neobsahuje parsovatelný video stream.')
        return probe
    except (json.JSONDecodeError, TypeError) as error:
        raise MediaValidationError('FFprobe nevrátilo platná metadata média.') from error
