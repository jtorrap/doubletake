"""Worker-owned browser audio, with no host sound devices or network listener."""
import array
import contextlib
import json
import math
import os
from pathlib import Path
import secrets
import select
import signal
import stat
import subprocess
import sys
import threading
import time


SINK = 'doubletake_browser'
SAMPLE_RATE = 44100
CHANNELS = 2
DIAGNOSTIC_SECONDS = 0.75
DIAGNOSTIC_TIMEOUT = 3


def _sample_levels(environment):
    """Read a bounded private-monitor sample; return only normalized energy.

    PCM never goes to disk, stderr, or a diagnostic result. An idle null sink
    emits valid zero samples, which are an OK sample with non_silent=False.
    """
    process = None
    samples = bytearray()
    pcm = array.array('h')
    limit = int(DIAGNOSTIC_SECONDS * SAMPLE_RATE) * CHANNELS * 2
    deadline = time.monotonic() + DIAGNOSTIC_TIMEOUT
    try:
        process = subprocess.Popen([
            'gst-launch-1.0', '-q', 'pulsesrc', 'device=' + SINK + '.monitor',
            '!', f'audio/x-raw,format=S16LE,rate={SAMPLE_RATE},channels={CHANNELS}',
            '!', 'fdsink', 'fd=1', 'sync=false',
        ], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        while len(samples) < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'status': 'timeout'}
            readable, _, _ = select.select([process.stdout], [], [], min(0.1, remaining))
            if not readable:
                continue
            chunk = os.read(process.stdout.fileno(), min(16384, limit - len(samples)))
            if not chunk:
                return {'status': 'unavailable'}
            samples.extend(chunk)
        pcm.frombytes(samples)
        if sys.byteorder != 'little':
            pcm.byteswap()
        peak = max(abs(value) for value in pcm)
        rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
        return {'status': 'ok', 'sample_seconds': DIAGNOSTIC_SECONDS,
                'rms': round(rms / 32768, 6), 'peak': round(peak / 32768, 6),
                'non_silent': peak != 0}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'status': 'unavailable'}
    finally:
        samples.clear()
        del pcm[:]
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
            if process.stdout is not None:
                process.stdout.close()


def _playback_status(environment):
    """Allowlist numeric/boolean metadata; never expose Pulse client properties."""
    def query(kind):
        result = subprocess.run(['pactl', '-f', 'json', 'list', kind],
                                env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                timeout=1, check=False)
        if result.returncode != 0 or len(result.stdout) > 262144:
            raise ValueError('audio_metadata_unavailable')
        data = json.loads(result.stdout)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError('audio_metadata_unavailable')
        return data

    try:
        sinks = [item for item in query('sinks') if item.get('name') == SINK]
        if len(sinks) != 1:
            return {'status': 'unavailable'}
        sink = sinks[0]
        if type(sink.get('index')) is not int or sink.get('monitor_source') != SINK + '.monitor':
            return {'status': 'unavailable'}
        # pactl identifies a sink's monitor by name, but capture streams refer
        # to it by source index. Resolve only our exact private monitor name.
        sources = [item for item in query('sources') if item.get('name') == SINK + '.monitor']
        if len(sources) != 1 or type(sources[0].get('index')) is not int:
            return {'status': 'unavailable'}
        monitor_index = sources[0]['index']
        inputs = [item for item in query('sink-inputs')
                  if item.get('sink') == sink['index']
                  and isinstance(item.get('properties'), dict)
                  and item['properties'].get('application.process.binary')
                  in {'chrome', 'google-chrome', 'chromium'}]
        outputs = [item for item in query('source-outputs')
                   if item.get('source') == monitor_index]
        volumes = [item.get('value') for item in sink.get('volume', {}).values()
                   if isinstance(item, dict) and type(item.get('value')) is int
                   and 0 <= item['value'] <= 0xffffffff]
        return {'status': 'ok',
                'sink_muted': sink.get('mute') if type(sink.get('mute')) is bool else None,
                'sink_volume_percent': round(max(volumes) / 65536 * 100, 1) if volumes else None,
                'browser_streams': len(inputs),
                'browser_streams_muted': sum(item.get('mute') is True for item in inputs),
                'browser_streams_corked': sum(item.get('corked') is True for item in inputs),
                'browser_streams_running': sum(item.get('corked') is False for item in inputs),
                'capture_streams': len(outputs)}
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return {'status': 'unavailable'}


class BrowserAudio:
    """A private PulseAudio null sink shared by Chrome and its AirPlay senders.

    Call start before launching Chrome, then give its returned environment to
    Chrome and every sender. Stop those clients before close. The caller owns
    the containing temporary runtime directory, which removes files on exit.
    """

    def __init__(self, runtime, environment):
        self.directory = Path(runtime) / 'audio'
        self.socket = self.directory / 'native'
        self.environment = {key: value for key, value in environment.items()
                            if not key.startswith('PULSE_')}
        self.process = None
        self.ready = False
        self._diagnostic_lock = threading.Lock()

    def _prepare(self):
        # Never reuse another server's state or authentication cookie. This
        # directory is new for each worker, even with a persistent Chrome profile.
        self.directory.mkdir(mode=0o700)
        (self.directory / 'state').mkdir(mode=0o700)
        cookie = self.directory / 'cookie'
        client = self.directory / 'client.conf'
        startup = self.directory / 'startup.pa'
        for path, content in [
            (cookie, secrets.token_bytes(256)),
            (client, b'autospawn = no\nenable-shm = no\n'),
        ]:
            with open(path, 'xb', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
                output.write(content)
        # Runtime paths are generated locally. Reject line/argument injection
        # instead of accepting arbitrary PulseAudio configuration through paths.
        if any(character.isspace() or character in '\\"\'' for character in str(self.directory)):
            raise RuntimeError('browser_audio_unavailable')
        script = '\n'.join([
            '.fail',
            f'load-module module-null-sink sink_name={SINK} rate={SAMPLE_RATE} channels={CHANNELS} format=s16le',
            f'load-module module-native-protocol-unix socket={self.socket} auth-cookie={cookie} auth-cookie-enabled=1 auth-anonymous=0',
            f'set-default-sink {SINK}',
            f'set-default-source {SINK}.monitor',
            '',
        ])
        with open(startup, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            output.write(script)
        self.environment.update({
            'PULSE_SERVER': 'unix:' + str(self.socket),
            'PULSE_SINK': SINK,
            'PULSE_SOURCE': SINK + '.monitor',
            'PULSE_COOKIE': str(cookie),
            'PULSE_CLIENTCONFIG': str(client),
            'PULSE_RUNTIME_PATH': str(self.directory),
            'PULSE_STATE_PATH': str(self.directory / 'state'),
        })
        return [
            'pulseaudio', '--daemonize=no', '--system=no', '--use-pid-file=no',
            '--exit-idle-time=-1', '--disallow-exit=yes',
            '--disallow-module-loading=yes', '--realtime=no', '--high-priority=no',
            '--log-target=stderr', '--log-level=error', '-n', '--file=' + str(startup),
        ]

    def _default(self, name):
        result = subprocess.run(['pactl', 'get-default-' + name], env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=2, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def start(self):
        if self.process is not None:
            raise RuntimeError('browser_audio_already_started')
        try:
            command = self._prepare()
            # PulseAudio's optional D-Bus server lookup is unnecessary with an
            # explicit PULSE_SERVER. An absent address can autolaunch a session
            # bus; point only this daemon at a nonexistent private path instead.
            # Chrome keeps its real accessibility bus in self.environment.
            daemon_environment = {**self.environment,
                'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(self.directory / 'no-dbus'),
                'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=' + str(self.directory / 'no-dbus')}
            self.process = subprocess.Popen(command, env=daemon_environment,
                                            stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            start_new_session=True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                if self.socket.exists():
                    node = self.socket.lstat()
                    if not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.getuid():
                        break
                    # Both readbacks must identify our null sink. Never fall
                    # back to an inherited/default system audio endpoint.
                    if self._default('sink') == SINK and self._default('source') == SINK + '.monitor':
                        self.ready = True
                        return dict(self.environment)
                time.sleep(0.05)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self.close()
        raise RuntimeError('browser_audio_unavailable') from None

    def diagnostics(self):
        """Blocking, on-demand diagnostics; run off the worker's event loop."""
        process = self.process
        result = {'ready': bool(self.ready and process and process.poll() is None),
                  'source': 'browser', 'channels': CHANNELS, 'sample_rate': SAMPLE_RATE,
                  'playback': {'status': 'unavailable'}, 'levels': {'status': 'unavailable'}}
        if not result['ready']:
            return result
        if not self._diagnostic_lock.acquire(blocking=False):
            result['levels'] = {'status': 'busy'}
            return result
        try:
            # Check the exact endpoint and private files again. In particular,
            # never allow a missing server to fall through to host audio.
            expected = {'PULSE_SERVER': 'unix:' + str(self.socket),
                        'PULSE_SINK': SINK, 'PULSE_SOURCE': SINK + '.monitor',
                        'PULSE_COOKIE': str(self.directory / 'cookie'),
                        'PULSE_CLIENTCONFIG': str(self.directory / 'client.conf')}
            if any(self.environment.get(key) != value for key, value in expected.items()):
                return result
            for path, kind, mode in [(self.directory, stat.S_ISDIR, 0o700),
                                     (self.socket, stat.S_ISSOCK, None),
                                     (self.directory / 'cookie', stat.S_ISREG, 0o600),
                                     (self.directory / 'client.conf', stat.S_ISREG, 0o600)]:
                node = path.lstat()
                if (node.st_uid != os.getuid() or not kind(node.st_mode)
                        or (mode is not None and stat.S_IMODE(node.st_mode) != mode)):
                    return result
            environment = dict(self.environment)
            # Count existing capture clients before opening our temporary one.
            result['playback'] = _playback_status(environment)
            if self.ready and self.process is process and process.poll() is None:
                result['levels'] = _sample_levels(environment)
            return result
        except OSError:
            return result
        finally:
            self._diagnostic_lock.release()

    def close(self):
        self.ready = False
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
