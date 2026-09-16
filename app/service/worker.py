#!/usr/bin/env python3
"""One isolated browser with independently owned AirPlay senders.

stdout is a bounded JSON event channel, not a log. Never forward browser output
or raw AirPlay messages to it. This process has no MQTT/Supervisor credentials.
"""
import asyncio
import contextlib
import html
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from model import browser_text, identifier
from performance import video_stats
from audio import BrowserAudio
from acceleration import gpu_info, va_capabilities, video_engine_counters, encoding_probe, process_cpu_counters
from browser_preferences import prepare_profile
from native_control import browser_command as native_browser_command
from native_control import accessibility_address
from display import start_display


def load_engine():
    loader = importlib.machinery.SourceFileLoader("browser_engine", os.environ.get("DOUBLETAKE_LAUNCHER", "/opt/doubletake/launcher.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


class CDP:
    def __init__(self, command_fd, response_fd, on_event=lambda _value: None):
        self.command_fd, self.response_fd = command_fd, response_fd
        self.loop = asyncio.get_running_loop()
        self.buffer = b""
        self.sequence = 100
        self.pending = {}
        self.on_event = on_event
        self.loop.add_reader(response_fd, self.read)

    def read(self):
        try:
            chunk = os.read(self.response_fd, 65536)
            if not chunk:
                self.close()
                return
            self.buffer += chunk
            if len(self.buffer) > 2 * 1024 * 1024:
                self.close()
                return
            while b"\0" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\0", 1)
                value = json.loads(raw)
                if "method" in value:
                    self.on_event(value)
                future = self.pending.pop(value.get("id"), None)
                if future and not future.done():
                    if "error" in value:
                        future.set_exception(RuntimeError("Browser command failed"))
                    else:
                        future.set_result(value.get("result", {}))
        except (OSError, ValueError):
            self.close()

    async def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = self.loop.create_future()
        self.pending[self.sequence] = future
        try:
            os.write(self.command_fd, json.dumps(message).encode() + b"\0")
            return await asyncio.wait_for(future, 8)
        finally:
            self.pending.pop(message["id"], None)

    def close(self):
        self.loop.remove_reader(self.response_fd)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Browser connection ended"))
        self.pending.clear()


class Worker:
    def __init__(self, config):
        self.config = config
        self.engine = load_engine()
        self.browser = self.display = self.vnc = None
        self.senders = {}
        self.audio = None
        self.bus = None
        self.command_fd = self.response_fd = None
        self.cdp = None
        self.environment = None
        self.window = None
        self.display_info = {}
        self.page_session = None
        self.stop_event = asyncio.Event()
        self.stage = "dependencies"
        self.media = {}
        self.encoder = None
        self.encoder_fallback = False
        # Desired connection generations outlive entries being cleaned up.
        # An explicit Stop must invalidate an automatic retry even after the
        # old sender was popped from self.senders and while stop() is awaiting.
        self.sender_generations = {}
        self.native = config.get('control_mode', 'native') == 'native'

    def media_event(self, value):
        if value.get('method') != 'Media.playerPropertiesChanged':
            return
        params = value.get('params', {})
        player = params.get('playerId')
        if player not in self.media and len(self.media) >= 16:
            self.media.pop(next(iter(self.media)))
        properties = self.media.setdefault(player, {})
        for item in params.get('properties', []):
            name, content = item.get('name'), item.get('value')
            if name == 'kVideoDecoderName' and content in {
                'GpuVideoDecoder', 'VaapiVideoDecoder', 'FFmpegVideoDecoder',
                'VpxVideoDecoder', 'Dav1dVideoDecoder', 'MojoVideoDecoder'}:
                properties['decoder'] = content
            elif name == 'kIsPlatformVideoDecoder' and content in {'true', 'false'}:
                properties['platform_decoder'] = content == 'true'

    async def diagnostics(self):
        capabilities = await asyncio.to_thread(va_capabilities)
        info = gpu_info(await self.cdp.call('SystemInfo.getInfo')) if self.cdp else {}
        before = video_engine_counters()
        cpu_before = process_cpu_counters()
        started = time.monotonic_ns()
        await asyncio.sleep(1)
        after = video_engine_counters()
        cpu_after = process_cpu_counters()
        elapsed = time.monotonic_ns() - started
        common = before.keys() & after.keys()
        video_ns = sum(max(0, after[key] - before[key]) for key in common)
        cpu = {}
        for key in cpu_before.keys() & cpu_after.keys():
            cpu[key[1]] = cpu.get(key[1], 0) + max(0, cpu_after[key] - cpu_before[key])
        cpu = {role: round(ticks / os.sysconf('SC_CLK_TCK') / (elapsed / 1e9) * 100, 1) for role, ticks in cpu.items()}
        # Read only video dimensions/playback counters, including HA shadow DOM.
        # The fixed expression returns no page text, URLs, cookies, or inputs.
        expression = '''(() => {
          const videos = [], visit = root => {
            for (const element of root.querySelectorAll('*')) {
              if (element.tagName === 'VIDEO') {
                const q = element.getVideoPlaybackQuality();
                videos.push({width:element.videoWidth,height:element.videoHeight,
                  ready_state:element.readyState,paused:element.paused,
                  decoded_frames:q.totalVideoFrames,dropped_frames:q.droppedVideoFrames,
                  error_code:element.error?.code || null});
              }
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          }; visit(document); return {videos:videos.slice(0,16),
            display:{css_width:innerWidth,css_height:innerHeight,
              page_zoom_percent:Math.round(devicePixelRatio*100),
              prefers_dark:matchMedia('(prefers-color-scheme: dark)').matches}};
        })()'''
        videos = await self.cdp.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, self.page_session) if self.cdp else {}
        page = videos.get('result', {}).get('value', {})
        return {**capabilities, **info,
                'control_mode': 'native' if self.native else 'diagnostic',
                'browser_inspection_available': not self.native,
                'last_control_error': getattr(self, 'native_error', None),
                'display': {'width': self.engine_config['width'], 'height': self.engine_config['height'],
                            'fps': self.engine_config['fps'], **page.get('display', {})},
                'display_server': self.display_info,
                'hardware_decoding_enabled': os.environ.get('DOUBLETAKE_HARDWARE_DECODING', 'true') == 'true',
                'audio_enabled': self.audio is not None,
                'audio': self.audio.diagnostics() if self.audio else {'ready': False},
                'receiver_count': len(self.senders),
                'cpu_percent_of_one_core': cpu,
                'video_encoder': self.encoder or 'not_started',
                'encoder_fallback': self.encoder_fallback,
                'video_engine_scope': 'browser decoding and sender encoding combined',
                'senders': {key: {'encoder': entry.get('encoder', 'none'), **entry.get('performance', {})} for key, entry in self.senders.items()},
                'video_engine_observable': bool(common),
                'video_engine_active': video_ns > 0,
                'video_engine_busy_percent': round(video_ns / elapsed * 100, 2) if common else None,
                'players': [p for p in self.media.values() if p],
                'videos': page.get('videos', [])}

    async def start(self, runtime):
        args = self.engine.arguments(["--url", self.config["url"], "--setup", "--state-dir", self.config["profile_dir"]])
        args.browser = self.config.get("browser")
        self.engine_config = vars(args).copy()
        self.engine_config.update(self.config["quality"])
        self.engine_config["state_dir"] = self.config["profile_dir"]
        self.engine_config["executables"] = self.engine.dependencies(args)
        self.engine_config["executables"]["sender"] = self.engine.executable(self.config.get("sender", "doubletake"))
        # No production sandbox-disable switch is accepted by this worker.
        self.engine_config["no_browser_sandbox"] = False
        Path(self.config["profile_dir"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stage = "display"
        self.display, self.environment, self.display_info = await asyncio.to_thread(start_display, self.engine_config, runtime, self.engine)
        if self.native:
            # A filesystem socket inside the private container/runtime avoids
            # abstract UNIX sockets shared by HA host-network applications.
            address = 'unix:path=' + str(Path(runtime) / 'session-bus')
            self.bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--address='+address, '--print-address=1'],
                    env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            ready = await asyncio.wait_for(asyncio.to_thread(self.bus.stdout.readline), 5)
            if not ready.decode().startswith(address):
                raise RuntimeError('private_bus_unavailable')
            self.environment['DBUS_SESSION_BUS_ADDRESS'] = ready.decode().strip()
            self.environment['ACCESSIBILITY_ENABLED'] = '1'
            self.environment['AT_SPI_BUS_ADDRESS'] = await asyncio.to_thread(accessibility_address, self.environment)
        if os.environ.get('DOUBLETAKE_AUDIO', 'true') == 'true':
            self.stage = 'audio'
            self.audio = BrowserAudio(runtime, self.environment)
            self.environment = await asyncio.to_thread(self.audio.start)
        bootstrap = Path(runtime) / "launch.html"
        bootstrap.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=' + html.escape(self.config["url"], quote=True) + '">')
        self.stage = "browser"
        prepare_profile(Path(self.config['profile_dir']) / 'profile')
        if self.native:
            self.browser = subprocess.Popen(native_browser_command(self.engine, self.engine_config, bootstrap),
                                            env=self.environment, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            self.browser, self.command_fd, self.response_fd = self.engine.start_browser(self.engine_config, bootstrap, self.environment)
            self.cdp = CDP(self.command_fd, self.response_fd, self.media_event)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise RuntimeError("startup_cancelled")
            if self.browser.poll() is not None:
                raise RuntimeError("browser_start_failed")
            found = subprocess.run([self.engine_config["executables"]["xdotool"], "search", "--onlyvisible", "--class", "^DoubletakeBrowser$"], env=self.environment, capture_output=True, text=True, timeout=3)
            ids = found.stdout.split()
            if len(ids) == 1 and ids[0].isdigit():
                self.window = int(ids[0])
                break
            await asyncio.sleep(0.2)
        if not self.window:
            raise RuntimeError("browser_window_missing")
        for operation in [["windowsize", str(self.window), str(self.engine_config["width"]), str(self.engine_config["height"])], ["windowmove", str(self.window), "0", "0"]]:
            subprocess.run([self.engine_config["executables"]["xdotool"], *operation], env=self.environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        self.stage = "browser control"
        if self.cdp:
            targets = await self.cdp.call("Target.getTargets")
            page = next(t for t in targets["targetInfos"] if t["type"] == "page")
            attached = await self.cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
            self.page_session = attached["sessionId"]
            with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
                await self.cdp.call('Media.enable', session=self.page_session)
        # This file and the control channel remain private; no VNC password
        # appears in process arguments, logs, MQTT, or persistent settings.
        self.stage = "preview"
        password = secrets.token_urlsafe(6)
        password_file = Path(runtime) / "vnc-password"
        password_file.write_text(password + "\n")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.vnc = subprocess.Popen([self.engine_config["executables"]["vnc"], "-display", self.environment["DISPLAY"], "-auth", self.environment["XAUTHORITY"], "-localhost", "-rfbport", str(port), "-passwdfile", str(password_file), "-forever", "-shared", "-noxdamage", "-quiet"], env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            if self.vnc.poll() is not None:
                raise RuntimeError("preview_start_failed")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                banner = await asyncio.wait_for(reader.readexactly(12), 2)
                writer.close()
                await writer.wait_closed()
                if banner.startswith(b"RFB "):
                    break
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("preview_start_failed")
        emit("ready", port=port, password=password)

    async def stop_sender(self, tv_id=None):
        if tv_id is None:
            await asyncio.gather(*(self.stop_sender(key) for key in list(self.senders)))
            return
        entry = self.senders.pop(tv_id, None)
        if not entry:
            return
        process = entry['process']
        asyncio.get_running_loop().remove_reader(process.stdout.fileno())
        try:
            await asyncio.to_thread(self.engine.stop, process)
        finally:
            process.stdout.close()
            process.stdin.close()
            entry['buffer'] = ''

    def invalidate_senders(self, tv_id=None):
        keys = {tv_id} if tv_id is not None else self.senders.keys() | self.sender_generations.keys()
        for key in keys:
            self.sender_generations[key] = self.sender_generations.get(key, 0) + 1

    def sender_desired(self, tv_id, generation):
        return not self.stop_event.is_set() and self.sender_generations.get(tv_id, 0) == generation

    def sender_output(self, tv_id, entry):
        # A queued callback from an old connection must never update its retry.
        if self.senders.get(tv_id) is not entry:
            return
        process = entry['process']
        try:
            chunk = os.read(process.stdout.fileno(), 8192)
        except OSError:
            return
        if not chunk:
            asyncio.get_running_loop().remove_reader(process.stdout.fileno())
            return
        lines = (entry['buffer'] + chunk.decode('utf-8', errors='replace'))[-16384:].split('\n')
        entry['buffer'] = lines.pop()
        for line in lines:
            self.sender_line(tv_id, entry, line)
        # Prompts have no newline. Retain other partial lines so a ready event
        # cannot discard an audio status split across reads.
        if 'Enter ' in entry['buffer']:
            self.sender_line(tv_id, entry, entry['buffer'])
            entry['buffer'] = ''

    def sender_line(self, tv_id, entry, line):
        # Only fixed status markers leave the sender's private output channel.
        # URLs, credentials and raw errors never reach the UI or logs.
        if line.startswith('DOUBLETAKE_VIDEO_STATS '):
            try:
                stats = video_stats(json.loads(line[len('DOUBLETAKE_VIDEO_STATS '):]))
            except (ValueError, TypeError):
                stats = None
            if stats:
                entry['performance'] = stats
                emit('performance', tv_id=tv_id, data=stats, encoder=entry.get('encoder', 'none'))
            return
        if 'Enter ' in line:
            entry['state'] = 'pairing'
            emit('airplay', tv_id=tv_id, state='pairing')
        elif 'mirror session ready' in line:
            entry['state'] = 'sending'
            emit('airplay', tv_id=tv_id, state='sending')
        audio = None
        if 'warning: audio capture failed:' in line or 'audio streaming error:' in line:
            audio = 'error'
        elif 'audio disabled (receiver did not provide audio ports)' in line:
            audio = 'unavailable'
        elif 'audio capture started' in line and entry.get('audio') != 'error':
            audio = 'active'
        if audio and audio != entry.get('audio'):
            entry['audio'] = audio
            emit('audio', tv_id=tv_id, state=audio)

    async def native_command(self, action, **fields):
        process = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).with_name('native_control.py')),
                    env=self.environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(
                {'action':action, 'window':self.window, 'pid':self.browser.pid, **fields}).encode()), 35)
            result = json.loads(output)
            if not result.get('ok'):
                self.native_error = result.get('stage') if result.get('stage') in {'validate','preview','focus','text','address','freeze','navigate','restore'} else 'control'
                raise RuntimeError('native_control_failed')
            self.native_error = None
        finally:
            fields.clear()
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            # Also recover after helper timeout/cancellation. The X11 cover is
            # owned by that helper connection and disappears when it exits.
            recovery = await asyncio.create_subprocess_exec('x11vnc', '-display', self.environment['DISPLAY'],
                '-auth', self.environment['XAUTHORITY'], '-R', 'noviewonly', '-Q', 'viewonly',
                env=self.environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                answer, _ = await asyncio.wait_for(recovery.communicate(), 8)
                if recovery.returncode or b'ans=viewonly:0' not in answer:
                    self.stop_event.set()
            finally:
                if recovery.returncode is None:
                    recovery.kill()
                    await recovery.wait()
                    self.stop_event.set()

    async def command(self, value, *, retry_generation=None):
        action = value["action"]
        if self.native and action in {'navigate', 'back', 'forward', 'reload', 'insert_text'}:
            fields = {'url':value['url']} if action == 'navigate' else {'value':value['value']} if action == 'insert_text' else {}
            return await self.native_command(action, **fields)
        if action == "diagnostics":
            return await self.diagnostics()
        elif action == "navigate":
            self.media.clear()
            self.engine.arguments(["--url", value["url"], "--setup"])
            result = await self.cdp.call("Page.navigate", {"url": value["url"]}, self.page_session)
            if result.get("errorText"):
                raise RuntimeError("page_load_failed")
        elif action in {"back", "forward"}:
            history = await self.cdp.call("Page.getNavigationHistory", session=self.page_session)
            index = history["currentIndex"] + (-1 if action == "back" else 1)
            if 0 <= index < len(history["entries"]):
                await self.cdp.call("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]}, self.page_session)
        elif action == "reload":
            await self.cdp.call("Page.reload", session=self.page_session)
        elif action == "insert_text":
            # Browser-native text insertion preserves Unicode and punctuation.
            # It neither uses a clipboard nor sends Enter or other key actions.
            await self.cdp.call("Input.insertText", {"text": browser_text(value["value"])}, self.page_session)
        elif action == "cast":
            receiver = value["receiver"]
            tv_id = identifier(receiver['id'])
            if retry_generation is None:
                self.invalidate_senders(tv_id)
            generation = self.sender_generations.get(tv_id, 0) if retry_generation is None else retry_generation
            if not self.sender_desired(tv_id, generation):
                return False
            await self.stop_sender(tv_id)
            if not self.sender_desired(tv_id, generation):
                return False
            if self.encoder is None:
                enabled = os.environ.get('DOUBLETAKE_HARDWARE_ENCODING', 'true') == 'true'
                available = enabled and await asyncio.to_thread(encoding_probe, self.engine_config, self.environment)
                if not self.sender_desired(tv_id, generation):
                    return False
                self.encoder = 'vaapi' if available else 'none'
                self.encoder_fallback = enabled and not available
            config = {**self.engine_config, "target": receiver["host"], "port": receiver["port"], "state_dir": str(Path(self.config["receivers_dir"]) / receiver["id"]), "pair": False}
            config['hwaccel'] = 'none' if value.get('software_retry') else self.encoder
            Path(config["state_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            command = self.engine.sender_command(config, self.window)
            if self.audio:
                command = [part for part in command if part != '-no-audio']
            process = subprocess.Popen(command, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            entry = {'process': process, 'buffer': '', 'state': 'starting',
                     'encoder': config['hwaccel'], 'receiver': receiver,
                     'generation': generation,
                     'started': time.monotonic(),
                     'audio': 'starting' if self.audio else 'disabled'}
            self.senders[tv_id] = entry
            asyncio.get_running_loop().add_reader(process.stdout.fileno(), self.sender_output, tv_id, entry)
            return True
        elif action == "pin":
            pin = value["value"]
            entry = self.senders.get(value.get('tv_id'))
            if not entry or entry['state'] != 'pairing' or entry['process'].poll() is not None or not isinstance(pin, str) or not 1 <= len(pin) <= 128 or any(ord(c) < 32 for c in pin):
                raise RuntimeError("pairing_not_waiting")
            entry['process'].stdin.write(pin.encode() + b'\n')
            entry['process'].stdin.flush()
            entry['state'] = 'starting'
        elif action == "stop":
            self.invalidate_senders(value.get('tv_id'))
            await self.stop_sender(value.get('tv_id'))
        elif action == "close":
            self.invalidate_senders()
            self.stop_event.set()
        else:
            raise RuntimeError("unknown_command")

    async def monitor(self):
        while not self.stop_event.is_set():
            owned = [self.browser, self.display, self.vnc]
            if self.audio:
                owned.append(self.audio.process)
            if any(process.poll() is not None for process in owned):
                emit("fatal", code="browser_session_ended")
                self.stop_event.set()
                return
            for tv_id, entry in list(self.senders.items()):
                if entry['process'].poll() is not None and self.senders.get(tv_id) is entry:
                    generation = entry.get('generation', self.sender_generations.get(tv_id, 0))
                    await self.stop_sender(tv_id)
                    if not self.sender_desired(tv_id, generation):
                        continue
                    # A successful probe cannot cover every real receiver's
                    # negotiated caps. Retry software once if VAAPI dies during
                    # startup; leave the shared browser and other TVs intact.
                    if tv_id not in self.senders and entry.get('encoder') == 'vaapi' and time.monotonic() - entry['started'] < 20 and not entry.get('performance'):
                        self.encoder_fallback = True
                        try:
                            retried = await self.command({'action': 'cast', 'receiver': entry['receiver'], 'software_retry': True}, retry_generation=generation)
                        except Exception:
                            retried = False
                        if retried:
                            emit('airplay', tv_id=tv_id, state='starting')
                            continue
                    # Stop/retry can run while process cleanup awaits. Ignore
                    # the old exit if a replacement connection already exists.
                    if tv_id not in self.senders and self.sender_desired(tv_id, generation):
                        emit('airplay', tv_id=tv_id, state='error', code='receiver_session_ended')
            await asyncio.sleep(0.25)

    async def commands(self):
        # 4096 Unicode characters can occupy 49152 JSON-escaped bytes.
        reader = asyncio.StreamReader(limit=65536)
        await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        while not self.stop_event.is_set():
            try:
                raw = await reader.readline()
                if not raw:
                    self.stop_event.set()
                    return
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, OSError):
                emit("fatal", code="invalid_control_message")
                self.stop_event.set()
                return
            try:
                result = await self.command(value)
                emit("reply", id=value["id"], ok=True, data=result)
            except Exception:
                emit("reply", id=value.get("id"), ok=False, code="browser_command_failed")
            finally:
                # Do not retain the last password/PIN in the idle command loop.
                value.clear()

    async def close(self):
        await self.stop_sender()
        if self.native and self.browser and self.browser.poll() is None and self.window:
            with contextlib.suppress(Exception):
                await self.native_command('close')
                await asyncio.to_thread(self.browser.wait, timeout=8)
        self.engine.stop(self.vnc)
        if self.cdp:
            with contextlib.suppress(Exception):
                await self.cdp.call("Browser.close")
            self.cdp.close()
        if self.native:
            self.engine.stop(self.browser)
        else:
            self.engine.close_browser(self.browser, self.command_fd)
        for fd in [self.command_fd, self.response_fd]:
            if fd is not None:
                os.close(fd)
        self.engine.stop(self.display)
        self.engine.stop(self.bus)
        if self.audio:
            await asyncio.to_thread(self.audio.close)


async def main():
    os.umask(0o077)
    config = json.loads(Path(sys.argv[1]).read_text())
    worker = Worker(config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, worker.stop_event.set)
    with tempfile.TemporaryDirectory(prefix="doubletake-app-") as runtime:
        tasks = []
        try:
            await worker.start(runtime)
            tasks = [asyncio.create_task(worker.commands()), asyncio.create_task(worker.monitor())]
            await worker.stop_event.wait()
        except Exception:
            emit("fatal", code="browser_start_failed", stage=worker.stage)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
