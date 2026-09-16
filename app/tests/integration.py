#!/usr/bin/env python3
"""Linux CI: installed container, real video, VNC input and AirPlay packets.

All pages, storage and receivers here are synthetic. No home credentials or
real receivers are available to this test. Chrome inside the app is sandboxed.
"""
import http.server
import contextlib
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

import fullscreen_motion as motion

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('fixture', ROOT / 'scripts/browser-smoke.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
CONTROL = os.environ.get('DOUBLETAKE_TEST_CONTROL', 'native')
ARTIFACTS = ROOT / 'artifacts/app' / CONTROL
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
    motion_clip = b''
    motion_metrics = {}
    page_requests = []

    def do_GET(self):
        if self.path in ('/', '/motion', '/second'):
            Fixture.page_requests = (Fixture.page_requests + [self.path])[-32:]
        if self.path in ('/motion', '/motion.mp4'):
            body = motion.PAGE if self.path == '/motion' else Fixture.motion_clip
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/motion' else 'video/mp4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/input-status':
            # Synthetic fixture only: acknowledge actual remote input before
            # the preview client changes focus or closes its VNC connection.
            body = json.dumps({'clicked': Fixture.clicked, 'typed': Fixture.typed}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/motion-metrics':
            Fixture.motion_metrics = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(204)
            self.end_headers()
        elif self.path == '/input':
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
    fixture.PAGE = fixture.PAGE.replace(b'<video muted autoplay', b'<video autoplay')
    fixture.PAGE = fixture.PAGE.replace(b'profileToken,hadProfile,hadCookie', b'''profileToken,hadProfile,hadCookie,path:location.pathname,webdriver:navigator.webdriver,
      css_width:innerWidth,css_height:innerHeight,zoom:Math.round(devicePixelRatio*100),dark:matchMedia('(prefers-color-scheme: dark)').matches''')
    fixture.PAGE += b'''<input id="entry" style="position:fixed;left:32px;top:550px;width:350px;height:40px;font-size:24px" placeholder="Test remote keyboard">
<button style="position:fixed;left:32px;top:615px;width:250px;height:45px;font-size:24px" onclick="fetch('/input',{method:'POST',body:JSON.stringify({clicked:true,typed:document.querySelector('#entry').value})})">Test remote click</button>'''
    receivers = []
    with tempfile.TemporaryDirectory(prefix='app-integration-') as directory:
        work = Path(directory)
        state = work / 'state'
        state.mkdir()
        subprocess.run(['sudo', 'chown', '1000:1000', str(state)], check=True)
        clip = work / 'clip.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2', '-movflags', '+faststart', str(clip)], check=True)
        Fixture.clip = clip.read_bytes()
        capture_path = work / 'synthetic-received-video.bin'
        if CONTROL == 'native':
            motion_clip = work / 'motion.mp4'
            motion.create_clip(motion_clip)
            Fixture.motion_clip = motion_clip.read_bytes()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        receiver_ports = [fixture.free_port(), fixture.free_port()]
        logs = [ARTIFACTS / 'receiver-alac.log', ARTIFACTS / 'receiver-aac-eld.log']
        def packets(index, kind='video'):
            return max([int(v) for v in re.findall(kind+r'=(\d+)/', logs[index].read_text())] or [0])
        def frames(index):
            return max([int(v) for v in re.findall(r'video_frames=(\d+)', logs[index].read_text())] or [0])
        try:
            for index, (port, log, profile) in enumerate(zip(receiver_ports, logs, ['uxplay', 'airserver'])):
                with log.open('w') as output:
                    capture_args = ['-video-capture', str(capture_path)] if CONTROL == 'native' and index == 0 else []
                    receivers.append(subprocess.Popen([str(ROOT / 'bin/doubletake-test-receiver'), '-listen', f'127.0.0.1:{port}', '-profile', profile, '-stats-interval', '200ms', *capture_args], stdout=output, stderr=subprocess.STDOUT))
                fixture.wait_for(lambda: 'listening' in log.read_text(), 10, 'synthetic receiver')
            subprocess.run(['docker', 'run', '-d', '--name', 'doubletake-integration', '--init', '--cap-add', 'SYS_ADMIN', '--network', 'host', '--user', '1000:1000', '-e', 'HOME=/home/browser', '-e', 'DOUBLETAKE_BROWSER_CONTROL='+CONTROL, '-e', 'DOUBLETAKE_DISPLAY_BACKEND=xvnc', '-v', f'{state}:/data/doubletake', '-v', f'{ROOT / "app/tests"}:/testsource:ro', '--entrypoint', 'python3', 'doubletake-app', '-u', '-B', '/opt/browser-app/server.py', '--development', '--port', str(PORT)], check=True)
            def ready():
                try:
                    return api('/api/state')
                except (OSError, RuntimeError):
                    return None
            csrf = fixture.wait_for(ready, 15, 'app API')['csrf']
            page = api('/api/settings/pages', {'name': 'Live dashboard', 'url': f'http://127.0.0.1:{server.server_port}/'})
            tv = api('/api/settings/tvs', {'name': 'Test TV', 'host': '127.0.0.1', 'port': receiver_ports[0]})
            tv2 = api('/api/settings/tvs', {'name': 'Second TV', 'host': '127.0.0.1', 'port': receiver_ports[1]})
            api('/api/action/open', {'page_id': page['id']})
            fixture.wait_for(lambda: fixture.Fixture.metrics.get('updates', 0) > 15 and fixture.Fixture.metrics.get('moving', 0) >= 3, 30, 'sandboxed live browser')
            token = fixture.Fixture.metrics['profileToken']
            assert api('/api/state')['runtime']['tv_id'] is None, 'Open started a sender'
            diagnostics = api('/api/diagnostics', {})
            expected_display = {'width':1920, 'height':1080, 'fps':30}
            if CONTROL == 'diagnostic':
                expected_display.update({
                                              'css_width':1920, 'css_height':1080,
                                              'page_zoom_percent':100, 'prefers_dark':True})
                assert any(v['width'] == 640 and v['decoded_frames'] > 0 for v in diagnostics['videos'])
            assert diagnostics['display'] == expected_display, diagnostics['display']
            assert diagnostics['display_server']['backend'] == 'xvnc', diagnostics['display_server']
            assert diagnostics['display_server']['dri3'] is False, diagnostics['display_server']
            metrics = fixture.Fixture.metrics
            assert metrics['webdriver'] == (CONTROL == 'diagnostic'), metrics
            assert (metrics['css_width'],metrics['css_height'],metrics['zoom'],metrics['dark']) == (1920,1080,100,True)
            assert not diagnostics['video_engine_active'], 'CI unexpectedly reports GPU activity'
            assert diagnostics['audio_enabled'] and diagnostics['audio']['ready'], diagnostics
            audio_checks = json.loads(subprocess.check_output(['docker','exec','doubletake-integration','python3','-B','/testsource/audio_runtime.py'], text=True, timeout=20))
            (ARTIFACTS/'audio-runtime.json').write_text(json.dumps(audio_checks,indent=2))
            if CONTROL == 'native':
                boundary_checks = json.loads(subprocess.check_output(['docker','exec','doubletake-integration','python3','-B','/testsource/native_runtime.py'], text=True))
                (ARTIFACTS/'native-runtime.json').write_text(json.dumps(boundary_checks,indent=2))
            subprocess.run(['node', str(ROOT / 'app/tests/preview.cjs'), BASE, str(ARTIFACTS), f'http://127.0.0.1:{server.server_port}'], check=True, timeout=60)
            fixture.wait_for(lambda: Fixture.clicked and Fixture.typed == 'keyboard worksP@ss "quotes" \\ $ & <tag> café 🔑', 10, 'Unicode password paste and VNC mouse')
            ids = [tv['id'], tv2['id']]
            api('/api/action/cast', {'page_id': page['id'], 'tv_ids': ids})
            def both_sending():
                current = api('/api/state')['runtime']['receivers']
                return set(current) == set(ids) and all(item['state'] == 'sending' for item in current.values())
            fixture.wait_for(both_sending, 40, 'two simultaneous AirPlay receivers')
            fixture.wait_for(lambda: all(frames(i) >= 60 and packets(i, 'audio_rtp') >= 300 for i in range(2)), 40, 'video frames and audio RTP at both receivers')
            assert all(value['audio'] == 'active' for value in api('/api/state')['runtime']['receivers'].values())
            before_fps = [frames(i) for i in range(2)]
            started = time.monotonic()
            time.sleep(8)
            elapsed = time.monotonic() - started
            measured_fps = [round((frames(i)-before_fps[i])/elapsed, 2) for i in range(2)]
            assert all(24 <= rate <= 36 for rate in measured_fps), measured_fps
            assert fixture.Fixture.metrics['videoWidth'] == 640
            fullscreen_checks = None
            if CONTROL == 'native':
                fullscreen = api('/api/settings/pages', {'name':'Synthetic full-screen video', 'url':f'http://127.0.0.1:{server.server_port}/motion'})
                api('/api/action/open', {'page_id':fullscreen['id']})
                fixture.wait_for(lambda: Fixture.motion_metrics.get('presented', 0) >= 60, 15, 'full-screen source warm-up')
                assert (Fixture.motion_metrics['width'], Fixture.motion_metrics['height']) == (1920,1080)
                first_frame = motion.frame_count(capture_path)
                browser_before = dict(Fixture.motion_metrics)
                started_motion = time.monotonic()
                time.sleep(12)
                browser_after = dict(Fixture.motion_metrics)
                motion_elapsed = time.monotonic() - started_motion
                assert both_sending(), 'Full-screen motion stopped a receiver'
                fullscreen_checks = motion.analyze_capture(capture_path, first_frame, work)
                fullscreen_checks.update({
                    'browser_presented_fps':round((browser_after['presented']-browser_before['presented'])/motion_elapsed,2),
                    'browser_dropped_frames':browser_after['dropped']-browser_before['dropped'],
                    'source_resolution':[1920,1080], 'two_senders_active':True,
                    'apple_tv_presentation_and_av_sync_verified':False})
                (ARTIFACTS/'fullscreen-motion.json').write_text(json.dumps(fullscreen_checks,indent=2))
                assert fullscreen_checks['browser_presented_fps'] >= 24, fullscreen_checks
                fixture.Fixture.metrics = {}
                api('/api/action/open', {'page_id':page['id']})
                fixture.wait_for(lambda: fixture.Fixture.metrics.get('path') == '/', 10, 'return from full-screen fixture')
            second = api('/api/settings/pages', {'name':'Second page', 'url':f'http://127.0.0.1:{server.server_port}/second'})
            before_packets = [packets(i) for i in range(2)]
            api('/api/action/open', {'page_id':second['id']})
            fixture.wait_for(lambda: fixture.Fixture.metrics.get('path') == '/second', 10, 'native URL navigation')
            assert both_sending(), 'Navigation changed the receivers'
            fixture.wait_for(lambda: all(packets(i) > before_packets[i]+30 for i in range(2)), 15, 'uninterrupted senders across navigation')
            for action, path in [('back','/'), ('forward','/second')]:
                api('/api/action/browser', {'action':action})
                fixture.wait_for(lambda: fixture.Fixture.metrics.get('path') == path, 10, action)
            api('/api/action/browser', {'action':'reload'})
            assert fixture.Fixture.metrics['webdriver'] == (CONTROL == 'diagnostic')
            # A per-device stop must not interrupt the other receiver or browser.
            api('/api/action/stop', {'tv_id':tv['id']})
            assert set(api('/api/state')['runtime']['receivers']) == {tv2['id']}
            remaining_packets = packets(1)
            fixture.wait_for(lambda: packets(1) > remaining_packets+30, 10, 'second receiver survives first Stop')
            # Reconnect one while the other stays up, then fail that process.
            before_rejoin_frames = [frames(i) for i in range(2)]
            before_rejoin_audio = [packets(i, 'audio_rtp') for i in range(2)]
            api('/api/action/cast', {'page_id':second['id'], 'tv_ids':ids})
            fixture.wait_for(both_sending, 30, 'rejoin while other receiver sends')
            fixture.wait_for(lambda: all(frames(i) > before_rejoin_frames[i]+30 and
                                        packets(i, 'audio_rtp') > before_rejoin_audio[i]+100 for i in range(2)),
                             20, 'fresh video and audio after receiver rejoin')
            assert all(value['audio'] == 'active' for value in api('/api/state')['runtime']['receivers'].values())
            fixture.stop(receivers[0])
            fixture.wait_for(lambda: api('/api/state')['runtime']['receivers'][tv['id']]['state'] == 'error', 45, 'targeted receiver failure')
            assert api('/api/state')['runtime']['receivers'][tv2['id']]['state'] == 'sending'
            remaining_packets = packets(1)
            fixture.wait_for(lambda: packets(1) > remaining_packets+30, 10, 'second receiver survives first failure')
            api('/api/action/stop', {})
            assert api('/api/state')['runtime']['browser'] == 'ready', 'Stop closed browser'
            api('/api/action/close', {})
            fixture.Fixture.metrics = {}
            api('/api/action/open', {'page_id': page['id']})
            fixture.wait_for(lambda: fixture.Fixture.metrics.get('updates', 0) > 10, 20, 'reopened browser')
            assert fixture.Fixture.metrics['profileToken'] == token
            assert fixture.Fixture.metrics['hadProfile'] and fixture.Fixture.metrics['hadCookie']
            assert tuple(fixture.Fixture.metrics[key] for key in ('css_width','css_height','zoom','dark')) == (1920,1080,100,True)
            assert api('/api/diagnostics', {})['display'] == diagnostics['display'], 'Display defaults changed on reopen'
            api('/api/action/close', {})
            processes = subprocess.check_output(['docker', 'top', 'doubletake-integration', '-eo', 'pid,comm'], text=True)
            assert not any(name in processes for name in ['chrome', 'Xvfb', 'Xvnc', 'x11vnc', 'doubletake', 'pulseaudio']), processes
            result = {'control_mode':CONTROL, 'webdriver':fixture.Fixture.metrics['webdriver'],
                      'native_navigation_and_history':True, 'sender_survives_navigation':True,
                      'sandbox_enabled': True, 'video_decoded': True, 'video_diagnostics': diagnostics, 'live_websocket_updates': fixture.Fixture.metrics['updates'], 'interactive_keyboard_and_mouse': True,
                      'masked_clipboard_paste_preserves_unicode_and_punctuation': True, 'paste_dialog_cleared': True,
                      'browser_version': subprocess.check_output(['docker', 'exec', 'doubletake-integration', 'google-chrome', '--version'], text=True).strip(),
                      'airplay_video_packets': [packets(i) for i in range(2)],
                      'airplay_audio_packets': [packets(i,'audio') for i in range(2)],
                      'airplay_audio_rtp_packets': [packets(i,'audio_rtp') for i in range(2)],
                      'received_video_fps':measured_fps,
                      'fullscreen_motion':fullscreen_checks,
                      'two_receivers_simultaneous':True, 'targeted_stop_and_failure_isolated':True,
                      'private_browser_audio':audio_checks, 'profile_and_cookie_retained': True,
                      'stop_preserves_browser': True, 'close_cleans_processes': True, 'real_apple_tv_tested': False}
            (ARTIFACTS / 'result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
        except Exception:
            # Synthetic-only navigation evidence separates a missing request
            # from a page which loaded but failed to report browser metrics.
            (ARTIFACTS/'fixture-state.json').write_text(json.dumps({
                'page_requests': Fixture.page_requests,
                'dashboard_metrics': {key:value for key,value in fixture.Fixture.metrics.items()
                                      if key in {'path','updates','moving','videoTime','videoWidth','paused','readyState','mediaError'}},
                'motion_metrics': Fixture.motion_metrics}, indent=2))
            if CONTROL == 'native':
                with contextlib.suppress(Exception):
                    inspection = subprocess.check_output(['docker','exec','doubletake-integration','python3','-B','/testsource/native_runtime.py','--inspect'],text=True,timeout=15)
                    (ARTIFACTS/'native-ui.json').write_text(inspection)
            with contextlib.suppress(Exception):
                print(json.dumps({'failure_runtime':api('/api/state')['runtime']}))
            with contextlib.suppress(Exception):
                print(json.dumps({'failure_diagnostics':api('/api/diagnostics', {})}))
            raise
        finally:
            # Only synthetic fixture logs are uploaded. Never copy this pattern
            # to a live app with user URLs, browser state, or authentication.
            with (ARTIFACTS / 'app.log').open('w') as output:
                subprocess.run(['docker', 'logs', 'doubletake-integration'], stdout=output, stderr=subprocess.STDOUT)
            subprocess.run(['docker', 'stop', '-t', '60', 'doubletake-integration'], check=False)
            subprocess.run(['docker', 'rm', 'doubletake-integration'], check=False)
            subprocess.run(['sudo', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', str(state)], check=True)
            for receiver in receivers:
                fixture.stop(receiver)
            server.shutdown()


if __name__ == '__main__':
    main()
