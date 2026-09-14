"""Worker-owned browser audio, with no host sound devices or network listener."""
import contextlib
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import time


SINK = 'doubletake_browser'
SAMPLE_RATE = 44100
CHANNELS = 2


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
        return {'ready': bool(self.ready and self.process and self.process.poll() is None),
                'source': 'browser', 'channels': CHANNELS, 'sample_rate': SAMPLE_RATE}

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
