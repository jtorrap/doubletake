"""Inspect only a disposable Linux fixture's audio, retaining no PCM samples.

The integration page must be playing an unmuted synthetic audio track. Never
run this fixture inspector against a real browser profile or live HA app.
"""
import array
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, '/opt/browser-app')
from audio import SINK, SAMPLE_RATE, CHANNELS


def command(environment, *arguments):
    return subprocess.check_output(['pactl', *arguments], env=environment,
                                   stderr=subprocess.DEVNULL, text=True, timeout=5)


def sample_monitor(environment):
    """Read at most two seconds of PCM, return aggregate energy and discard it."""
    process = subprocess.Popen(['gst-launch-1.0', '-q', 'pulsesrc', 'device=' + SINK + '.monitor',
        '!', f'audio/x-raw,format=S16LE,rate={SAMPLE_RATE},channels={CHANNELS}',
        '!', 'fdsink', 'fd=1', 'sync=false'], env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    samples = bytearray()
    limit = 2 * SAMPLE_RATE * CHANNELS * 2
    deadline = time.monotonic() + 8
    try:
        while len(samples) < limit and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.25)
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), min(16384, limit - len(samples)))
            if not chunk:
                break
            samples.extend(chunk)
        assert len(samples) == limit, 'Private browser monitor did not deliver PCM'
        pcm = array.array('h', samples)
        if sys.byteorder != 'little':
            pcm.byteswap()
        rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
        peak = max(abs(value) for value in pcm)
        # A real non-silent track is required; an idle null sink still produces
        # correctly formatted silent PCM and must not satisfy the assertion.
        assert rms > 100 and peak > 1000, 'Private browser monitor is silent'
        return {'sample_seconds': 2, 'rms': round(rms, 2), 'peak': peak,
                'non_silent_browser_audio': True}
    finally:
        samples.clear()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        process.stdout.close()


def check_unix_sockets(rows, inodes, expected):
    # Linux copies a UNIX listener's address onto accepted server sockets.
    # /proc/net/unix therefore repeats the pathname for Chrome connections;
    # only the Flags field's __SO_ACCEPTCON bit identifies a listener.
    owned = [row for row in rows if len(row) >= 7 and row[6] in inodes]
    listeners = [row for row in owned if int(row[3], 16) & 0x10000]
    named = [row for row in owned if len(row) == 8]
    report = {
        'unix_listeners': len(listeners),
        'private_socket_rows': sum(row[7] == expected for row in named),
        'unexpected_named_sockets': sum(row[7] != expected for row in named),
        'abstract_sockets': sum(row[7].startswith('@') for row in named),
        'expected_private_listener': len(listeners) == 1 and len(listeners[0]) == 8
                                     and listeners[0][7] == expected
                                     and int(listeners[0][4], 16) == 1,
    }
    assert report['expected_private_listener'] and not report['unexpected_named_sockets'], (
        'PulseAudio UNIX socket audit failed: ' + json.dumps(report, sort_keys=True))
    return report


def main():
    runtimes = list(Path('/tmp').glob('doubletake-app-*'))
    assert len(runtimes) == 1, 'Fixture must own exactly one browser runtime'
    directory = runtimes[0] / 'audio'
    sock = directory / 'native'
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert sock.is_socket() and sock.stat().st_uid == os.getuid()
    for name in ['cookie', 'client.conf', 'startup.pa']:
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    environment = {key: value for key, value in os.environ.items() if not key.startswith('PULSE_')}
    environment.update(PULSE_SERVER='unix:' + str(sock), PULSE_COOKIE=str(directory / 'cookie'),
                       PULSE_CLIENTCONFIG=str(directory / 'client.conf'))
    modules = json.loads(command(environment, '-f', 'json', 'list', 'modules'))
    assert sorted(item['name'] for item in modules) == ['module-native-protocol-unix', 'module-null-sink'], 'Unexpected audio device or transport module'
    assert command(environment, 'get-default-sink').strip() == SINK
    assert command(environment, 'get-default-source').strip() == SINK + '.monitor'
    sinks = json.loads(command(environment, '-f', 'json', 'list', 'sinks'))
    sources = json.loads(command(environment, '-f', 'json', 'list', 'sources'))
    assert len(sinks) == len(sources) == 1
    assert sinks[0]['name'] == SINK and sources[0]['name'] == SINK + '.monitor'
    inputs = json.loads(command(environment, '-f', 'json', 'list', 'sink-inputs'))
    assert any(item['sink'] == sinks[0]['index'] and
               item.get('properties', {}).get('application.process.binary') in {'chrome', 'google-chrome', 'chromium'}
               for item in inputs), 'Chrome is not connected to the private sink'
    processes = []
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if (process / 'comm').read_text().strip() == 'pulseaudio':
                processes.append(process)
        except FileNotFoundError:
            pass  # Unrelated short-lived Chrome processes may exit mid-listing.
    assert len(processes) == 1
    inodes = set()
    for fd in (processes[0] / 'fd').iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue  # A completed pactl connection can close during the audit.
        if link.startswith('socket:['):
            inodes.add(link[8:-1])
    network_inodes = set()
    for name in ['tcp', 'tcp6', 'udp', 'udp6', 'raw', 'raw6']:
        network_inodes.update(line.split()[9] for line in (Path('/proc/net') / name).read_text().splitlines()[1:])
    assert not inodes & network_inodes, 'PulseAudio owns an IP network socket'
    unix = [line.split(maxsplit=7) for line in Path('/proc/net/unix').read_text().splitlines()[1:]]
    socket_report = check_unix_sockets(unix, inodes, str(sock))
    print(json.dumps({'private_filesystem_socket': True, 'cookie_authentication': True,
                      'no_host_devices_or_network_listener': True,
                      'browser_connected_to_private_sink': True,
                      'socket_audit': socket_report, **sample_monitor(environment)}))


if __name__ == '__main__':
    main()
