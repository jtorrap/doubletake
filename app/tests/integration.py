#!/usr/bin/env python3
"""Linux CI: installed container, real video, VNC input and AirPlay packets.

All pages, storage and receivers here are synthetic. No home credentials or
real receivers are available to this test. Chrome inside the app is sandboxed.
"""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('fixture', ROOT / 'scripts/browser-smoke.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
ARTIFACTS = ROOT / 'artifacts/app'
csrf = ''
PORT = fixture.free_port()
BASE = f'http://127.0.0.1:{PORT}'


def api(path, body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', 'X-Doubletake-CSRF': csrf})
    try:
        with urllib.request.urlopen(request, timeout=100) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'API failed {path}: {error.code} {error.read().decode()}') from None


class Fixture(fixture.Fixture):
    clicked = False
    typed = ''

    def do_POST(self):
        if self.path == '/input':
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            Fixture.clicked = bool(value.get('clicked'))
            Fixture.typed = value.get('typed', '')
            self.send_response(204)
            self.end_headers()
        else:
            super().do_POST()


def main():
    global csrf
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    fixture.PAGE += b'''<input id="entry" style="position:fixed;left:32px;top:550px;width:350px;height:40px;font-size:24px" placeholder="Test remote keyboard">
<button style="position:fixed;left:32px;top:615px;width:250px;height:45px;font-size:24px" onclick="fetch('/input',{method:'POST',body:JSON.stringify({clicked:true,typed:document.querySelector('#entry').value})})">Test remote click</button>'''
    receiver = None
    with tempfile.TemporaryDirectory(prefix='app-integration-') as directory:
        work = Path(directory)
        state = work / 'state'
        state.mkdir()
        subprocess.run(['sudo', 'chown', '1000:1000', str(state)], check=True)
        clip = work / 'clip.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=15', '-t', '4', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(clip)], check=True)
        Fixture.clip = clip.read_bytes()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        receiver_port = fixture.free_port()
        log = ARTIFACTS / 'receiver.log'
        try:
            with log.open('w') as output:
                receiver = subprocess.Popen([str(ROOT / 'bin/doubletake-test-receiver'), '-listen', f'127.0.0.1:{receiver_port}', '-profile', 'uxplay', '-stats-interval', '1s'], stdout=output, stderr=subprocess.STDOUT)
            fixture.wait_for(lambda: 'listening' in log.read_text(), 10, 'synthetic receiver')
            subprocess.run(['docker', 'run', '-d', '--name', 'doubletake-integration', '--init', '--cap-add', 'SYS_ADMIN', '--network', 'host', '--user', '1000:1000', '-e', 'HOME=/home/browser', '-v', f'{state}:/data/doubletake', '--entrypoint', 'python3', 'doubletake-app', '-u', '-B', '/opt/browser-app/server.py', '--development', '--port', str(PORT)], check=True)
            def ready():
                try:
                    return api('/api/state')
                except (OSError, RuntimeError):
                    return None
            csrf = fixture.wait_for(ready, 15, 'app API')['csrf']
            page = api('/api/settings/pages', {'name': 'Live dashboard', 'url': f'http://127.0.0.1:{server.server_port}/'})
            tv = api('/api/settings/tvs', {'name': 'Test TV', 'host': '127.0.0.1', 'port': receiver_port})
            api('/api/action/open', {'page_id': page['id']})
            fixture.wait_for(lambda: fixture.Fixture.metrics.get('updates', 0) > 15 and fixture.Fixture.metrics.get('moving', 0) >= 3, 30, 'sandboxed live browser')
            token = fixture.Fixture.metrics['profileToken']
            assert api('/api/state')['runtime']['tv_id'] is None, 'Open started a sender'
            subprocess.run(['node', str(ROOT / 'app/tests/preview.cjs'), BASE, str(ARTIFACTS)], check=True, timeout=60)
            fixture.wait_for(lambda: Fixture.clicked and Fixture.typed == 'keyboard works', 10, 'VNC keyboard and mouse')
            api('/api/action/cast', {'page_id': page['id'], 'tv_id': tv['id']})
            fixture.wait_for(lambda: api('/api/state')['runtime']['airplay'] == 'sending', 30, 'AirPlay readiness')
            fixture.wait_for(lambda: max([int(v) for v in re.findall(r'video=(\d+)/', log.read_text())] or [0]) >= 30, 30, 'AirPlay packets')
            assert fixture.Fixture.metrics['videoWidth'] == 640
            api('/api/action/stop', {})
            assert api('/api/state')['runtime']['browser'] == 'ready', 'Stop closed browser'
            api('/api/action/close', {})
            fixture.Fixture.metrics = {}
            api('/api/action/open', {'page_id': page['id']})
            fixture.wait_for(lambda: fixture.Fixture.metrics.get('updates', 0) > 10, 20, 'reopened browser')
            assert fixture.Fixture.metrics['profileToken'] == token
            assert fixture.Fixture.metrics['hadProfile'] and fixture.Fixture.metrics['hadCookie']
            api('/api/action/close', {})
            processes = subprocess.check_output(['docker', 'top', 'doubletake-integration', '-eo', 'comm'], text=True)
            assert not any(name in processes for name in ['chrome', 'Xvfb', 'x11vnc', 'doubletake']), processes
            result = {'sandbox_enabled': True, 'video_decoded': True, 'live_websocket_updates': fixture.Fixture.metrics['updates'], 'interactive_keyboard_and_mouse': True,
                      'airplay_video_packets': max(int(v) for v in re.findall(r'video=(\d+)/', log.read_text())), 'profile_and_cookie_retained': True,
                      'stop_preserves_browser': True, 'close_cleans_processes': True, 'real_apple_tv_tested': False}
            (ARTIFACTS / 'result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
        finally:
            # Only synthetic fixture logs are uploaded. Never copy this pattern
            # to a live app with user URLs, browser state, or authentication.
            with (ARTIFACTS / 'app.log').open('w') as output:
                subprocess.run(['docker', 'logs', 'doubletake-integration'], stdout=output, stderr=subprocess.STDOUT)
            subprocess.run(['docker', 'stop', '-t', '60', 'doubletake-integration'], check=False)
            subprocess.run(['docker', 'rm', 'doubletake-integration'], check=False)
            subprocess.run(['sudo', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', str(state)], check=True)
            fixture.stop(receiver)
            server.shutdown()


if __name__ == '__main__':
    main()
