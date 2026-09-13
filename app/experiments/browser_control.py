#!/usr/bin/env python3
"""Disposable Linux experiment; never connect this fixture to real profiles.

Run in the app image with --network none, SYS_ADMIN, and uid 1000. The fixture
reports its own properties through HTTP; manual modes never attach a debugger.
Only synthetic input, cookies and localStorage are used. No Google sign-in is
attempted, and no property/feature is patched to conceal browser automation.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from aiohttp import ClientSession, web
from browser_preferences import prepare_profile
from worker import CDP, load_engine


MODES = ('pipe_app', 'port_app', 'manual_app', 'manual_fullscreen')
ASCII = 'Fixture-Only! "quotes" \\slash & symbols + % ^ ~ @'
UNICODE = 'Fixture café — Ελληνικά 日本語 🔐'
HTML = '''<!doctype html><meta charset="utf-8"><title>Browser control fixture</title>
<style>
  :root {color-scheme:light dark} body {font:24px sans-serif;margin:32px}
  input {display:block;width:1000px;height:48px;margin:24px 0;font:24px sans-serif}
  button {font:24px sans-serif;padding:12px} pre {font:18px monospace}
</style>
<h1>Disposable browser control experiment</h1>
<input id="entry" aria-label="Synthetic text" autocomplete="off" autofocus>
<button id="clicker">Count a click</button><pre id="status"></pre>
<script>
const field=document.querySelector('#entry'), button=document.querySelector('#clicker');
const previousStorage=localStorage.getItem('fixture')==='retained';
const previousCookie=document.cookie.split('; ').includes('fixture=retained');
localStorage.setItem('fixture','retained');
document.cookie='fixture=retained; Max-Age=86400; SameSite=Strict; Path=/';
const documentId=crypto.randomUUID(); let clicks=0;
button.onclick=()=>clicks++;
function point(el) {const r=el.getBoundingClientRect();
  return {x:Math.round((r.x+r.width/2)*devicePixelRatio),
          y:Math.round((r.y+r.height/2)*devicePixelRatio)}}
function report() {
  const data={document_id:documentId,path:location.pathname,webdriver:navigator.webdriver,
    dark:matchMedia('(prefers-color-scheme: dark)').matches,
    zoom_percent:Math.round(devicePixelRatio*100),width:innerWidth,height:innerHeight,
    value:field.value,clicks,previous_storage:previousStorage,previous_cookie:previousCookie,
    input_point:point(field),button_point:point(button)};
  document.querySelector('#status').textContent=JSON.stringify({...data,value:'[synthetic]'},null,2);
  fetch('/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).catch(()=>{});
}
setInterval(report,100); report();
</script>'''


class Fixture:
    def __init__(self):
        self.latest = {}

    async def page(self, _request):
        return web.Response(text=HTML, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def report(self, request):
        self.latest = await request.json()
        return web.Response(text='ok')

    async def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.latest):
                return self.latest.copy()
            await asyncio.sleep(.1)
        return None


class PortCDP:
    """Experimental loopback transport, deliberately not added to the app."""
    def __init__(self, client, port):
        self.client, self.port = client, port
        self.sequence = 100
        self.ws = None

    async def connect(self):
        async with self.client.get(f'http://127.0.0.1:{self.port}/json/version') as response:
            address = (await response.json())['webSocketDebuggerUrl']
        self.ws = await self.client.ws_connect(address)

    async def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {'id': self.sequence, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        await self.ws.send_json(message)
        async with asyncio.timeout(8):
            while True:
                result = await self.ws.receive_json()
                if result.get('id') == self.sequence:
                    if 'error' in result:
                        raise RuntimeError('CDP command failed')
                    return result.get('result', {})


def xdo(env, *args, text=None):
    # Production-style stdin delivery: text never enters the process arguments.
    return subprocess.run(['xdotool', *args], env=env, input=text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=True, timeout=8).stdout


async def window_ready(env, browser):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if browser.poll() is not None:
            raise RuntimeError('browser_start_failed')
        try:
            ids = xdo(env, 'search', '--onlyvisible', '--class', '^DoubletakeBrowser$').split()
            if len(ids) == 1:
                window = ids[0]
                xdo(env, 'windowsize', window, '1920', '1080')
                xdo(env, 'windowmove', window, '0', '0')
                xdo(env, 'windowfocus', '--sync', window)
                return window
        except subprocess.CalledProcessError:
            pass
        await asyncio.sleep(.2)
    raise RuntimeError('browser_window_missing')


def point_click(env, point):
    # --sync waits for pointer movement and can hang when already at this point.
    xdo(env, 'mousemove', str(point['x']), str(point['y']), 'click', '1')


async def native_text(env, fixture, value):
    point_click(env, fixture.latest['input_point'])
    xdo(env, 'key', '--clearmodifiers', 'ctrl+a')
    xdo(env, 'type', '--clearmodifiers', '--delay', '1', '--file', '-', text=value)
    return bool(await fixture.wait(lambda r: r.get('value') == value, timeout=3))


async def native_navigate(env, fixture, url, path):
    xdo(env, 'key', '--clearmodifiers', 'ctrl+l')
    await asyncio.sleep(.2)
    xdo(env, 'type', '--clearmodifiers', '--delay', '1', '--file', '-', text=url)
    xdo(env, 'key', '--clearmodifiers', 'Return')
    return bool(await fixture.wait(lambda r: r.get('path') == path, timeout=4))


async def native_close(env, browser):
    xdo(env, 'key', '--clearmodifiers', 'ctrl+shift+w')
    for _ in range(60):
        if browser.poll() is not None:
            return browser.returncode == 0
        await asyncio.sleep(.1)
    return False


async def run_mode(mode, fixture, base_url, client, evidence):
    engine = load_engine()
    outcome = {'mode': mode}
    browser = display = cdp = None
    fds = []
    with tempfile.TemporaryDirectory(prefix='browser-control-') as temporary:
        root = Path(temporary)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        state = root / 'state'
        state.mkdir(mode=0o700)
        start_path, next_path = f'/{mode}/start', f'/{mode}/next'
        args = engine.arguments(['--url', base_url + start_path, '--setup', '--state-dir', str(state),
                                 '--browser', '/opt/browser-app/acceleration.py'])
        config = vars(args).copy()
        config.update(executables=engine.dependencies(args), no_browser_sandbox=False)
        bootstrap = runtime / 'launch.html'
        bootstrap.write_text(f'<meta http-equiv="refresh" content="0;url={base_url}{start_path}">')
        prepare_profile(state / 'profile')
        try:
            display, env = engine.start_display(config, runtime)
            command = engine.browser_command(config, bootstrap)
            if mode != 'pipe_app':
                command.remove('--remote-debugging-pipe')
            if mode == 'port_app':
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    port = listener.getsockname()[1]
                command += [f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1']
            if mode == 'manual_fullscreen':
                command = [part for part in command if part != '--kiosk' and not part.startswith('--app=')]
                command += ['--start-fullscreen', bootstrap.as_uri()]
            outcome['flags'] = [flag for flag in command[1:] if flag.startswith('--') and not flag.startswith(('--user-data-dir=', '--app='))]

            async def launch():
                nonlocal browser, cdp, fds
                if mode == 'pipe_app':
                    browser, command_fd, response_fd = engine.start_browser(config, bootstrap, env)
                    fds = [command_fd, response_fd]
                    cdp = CDP(command_fd, response_fd)
                else:
                    browser = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, start_new_session=True)
                window = await window_ready(env, browser)
                if not await fixture.wait(lambda r: r.get('path') == start_path, timeout=20):
                    raise RuntimeError('fixture_not_loaded')
                if mode == 'port_app':
                    cdp = PortCDP(client, port)
                    await cdp.connect()
                return window

            async def release_browser():
                nonlocal cdp, fds
                if isinstance(cdp, CDP):
                    cdp.close()
                elif isinstance(cdp, PortCDP) and cdp.ws:
                    await cdp.ws.close()
                cdp = None
                engine.stop(browser)
                for fd in fds:
                    os.close(fd)
                fds = []

            fixture.latest = {}
            window = await launch()
            # Observe after debugger attachment too, with the fixture's own JS.
            await asyncio.sleep(.5)
            first = fixture.latest.copy()
            outcome['initial'] = {key: first[key] for key in ('webdriver', 'dark', 'zoom_percent', 'width', 'height')}
            outcome['native_ascii'] = await native_text(env, fixture, ASCII)
            outcome['native_unicode'] = await native_text(env, fixture, UNICODE)
            point_click(env, fixture.latest['button_point'])
            outcome['native_mouse'] = bool(await fixture.wait(lambda r: r.get('clicks') == 1))
            if cdp:
                targets = await cdp.call('Target.getTargets')
                page = next(t for t in targets['targetInfos'] if t['type'] == 'page')
                attached = await cdp.call('Target.attachToTarget', {'targetId': page['targetId'], 'flatten': True})
                session = attached['sessionId']
                point_click(env, fixture.latest['input_point'])
                xdo(env, 'key', '--clearmodifiers', 'ctrl+a')
                await cdp.call('Input.insertText', {'text': UNICODE}, session)
                outcome['cdp_unicode'] = bool(await fixture.wait(lambda r: r.get('value') == UNICODE))
                await cdp.call('Page.navigate', {'url': base_url + next_path}, session)
                outcome['cdp_navigation'] = bool(await fixture.wait(lambda r: r.get('path') == next_path))
                await cdp.call('Page.navigate', {'url': base_url + start_path}, session)
                if not await fixture.wait(lambda r: r.get('path') == start_path):
                    raise RuntimeError('cdp_return_failed')
                await asyncio.sleep(.3)
                outcome['webdriver_after_cdp'] = fixture.latest['webdriver']
            outcome['native_navigation'] = await native_navigate(env, fixture, base_url + next_path, next_path)
            if not outcome['native_navigation'] and mode == 'manual_fullscreen':
                # Chrome may disable the address bar while full screen. Measure
                # the native exit/navigate/restore sequence instead of masking it.
                xdo(env, 'key', '--clearmodifiers', 'F11')
                outcome['native_exit_fullscreen'] = bool(await fixture.wait(lambda r: r.get('height') != 900))
                outcome['native_navigation_with_toolbar'] = await native_navigate(env, fixture, base_url + next_path, next_path)
                xdo(env, 'key', '--clearmodifiers', 'F11')
                outcome['native_restore_fullscreen'] = bool(await fixture.wait(lambda r: r.get('width') == 1600 and r.get('height') == 900))
            if outcome['native_navigation'] or outcome.get('native_navigation_with_toolbar'):
                xdo(env, 'key', '--clearmodifiers', 'alt+Left')
                outcome['native_back'] = bool(await fixture.wait(lambda r: r.get('path') == start_path))
                xdo(env, 'key', '--clearmodifiers', 'alt+Right')
                outcome['native_forward'] = bool(await fixture.wait(lambda r: r.get('path') == next_path))
            document_id = fixture.latest['document_id']
            xdo(env, 'key', '--clearmodifiers', 'ctrl+r')
            outcome['native_reload'] = bool(await fixture.wait(lambda r: r.get('document_id') != document_id))
            capture = subprocess.run(['gst-launch-1.0', '-q', 'ximagesrc', f'xid={window}',
                                      'num-buffers=1', '!', 'videoconvert', '!', 'pngenc', '!',
                                      'filesink', f'location={evidence / (mode + ".png")}'],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            outcome['x11_capture'] = capture.returncode == 0
            outcome['native_clean_close'] = await native_close(env, browser)
            if not outcome['native_clean_close'] and cdp:
                with contextlib.suppress(RuntimeError, OSError, asyncio.TimeoutError):
                    await cdp.call('Browser.close')
                    await asyncio.to_thread(browser.wait, timeout=8)
            await release_browser()
            preferences = json.loads((state / 'profile' / 'Default' / 'Preferences').read_text())
            outcome['profile_exit_type'] = preferences.get('profile', {}).get('exit_type')
            fixture.latest = {}
            await launch()
            await asyncio.sleep(.3)
            outcome['reopened'] = {key: fixture.latest[key] for key in (
                'webdriver', 'dark', 'zoom_percent', 'width', 'height', 'previous_storage', 'previous_cookie')}
            await native_close(env, browser)
            await release_browser()
        except Exception as error:
            # There are no real accounts or private pages here. Keep errors
            # bounded anyway, and preserve results from the other modes.
            outcome['error'] = type(error).__name__ + ': ' + str(error)[:160]
        finally:
            if isinstance(cdp, CDP):
                cdp.close()
            elif isinstance(cdp, PortCDP) and cdp.ws:
                await cdp.ws.close()
            engine.stop(browser)
            for fd in fds:
                os.close(fd)
            engine.stop(display)
    return outcome


async def main():
    os.umask(0o077)
    if os.getuid() == 0:
        raise RuntimeError('Run as uid 1000 with the Chrome sandbox enabled')
    evidence = Path('/evidence')
    evidence.mkdir(exist_ok=True)
    fixture = Fixture()
    app = web.Application()
    app.router.add_post('/report', fixture.report)
    app.router.add_get('/{tail:.*}', fixture.page)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    results = {'chrome': subprocess.check_output(['google-chrome', '--version'], text=True).strip(),
               'real_google_signin_tested': False, 'real_tv_tested': False,
               'real_profiles_used': False, 'sandbox_disabled': False, 'modes': []}
    try:
        async with ClientSession() as client:
            for mode in MODES:
                result = await run_mode(mode, fixture, f'http://127.0.0.1:{port}', client, evidence)
                results['modes'].append(result)
                (evidence / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
                print(json.dumps(result), flush=True)
    finally:
        await runner.cleanup()
    if any('error' in result for result in results['modes']):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
