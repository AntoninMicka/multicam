import subprocess
import pytest


@pytest.fixture(scope='session')
def webm_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp('media') / 'fixture.webm'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'testsrc2=size=640x480:rate=30', '-t', '3', '-c:v', 'libvpx',
                    '-deadline', 'realtime', '-b:v', '8M', str(path)], check=True)
    return path.read_bytes()
