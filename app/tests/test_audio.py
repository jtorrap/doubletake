import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from audio import (BrowserAudio, SINK, CHANNELS, SAMPLE_RATE, DIAGNOSTIC_SECONDS,
                   _playback_status, _sample_levels)
from audio_runtime import check_unix_sockets


class BrowserAudioTests(unittest.TestCase):
    def test_unix_listener_audit_allows_accepted_connections_at_same_private_path(self):
        expected = '/tmp/private/audio/native'
        rows = [line.split(maxsplit=7) for line in [
            '00000000: 00000002 00000000 00010000 0001 01 101 ' + expected,
            '00000000: 00000003 00000000 00000000 0001 03 102 ' + expected,
            '00000000: 00000003 00000000 00000000 0001 03 103 ' + expected,
            '00000000: 00000003 00000000 00000000 0001 03 104',
            '00000000: 00000002 00000000 00010000 0001 01 999 /other/process/socket',
        ]]
        report = check_unix_sockets(rows, {'101', '102', '103', '104'}, expected)
        self.assertEqual(report['unix_listeners'], 1)
        self.assertEqual(report['private_socket_rows'], 3)
        self.assertEqual(report['unexpected_named_sockets'], 0)

    def test_unix_listener_audit_rejects_exposed_or_additional_endpoints(self):
        expected = '/tmp/private/audio/native'
        private = ('00000000: 00000002 00000000 00010000 0001 01 101 ' + expected).split()
        for extra in [
            '00000000: 00000002 00000000 00010000 0001 01 102 @exposed',
            '00000000: 00000002 00000000 00010000 0001 01 102 /unintended/socket',
            '00000000: 00000002 00000000 00010000 0001 01 102',
            '00000000: 00000003 00000000 00000000 0001 03 102 @exposed',
            '00000000: 00000003 00000000 00000000 0001 03 102 /unintended/socket with spaces',
        ]:
            with self.subTest(extra=extra), self.assertRaises(AssertionError):
                check_unix_sockets([private, extra.split(maxsplit=7)], {'101', '102'}, expected)
        with self.assertRaises(AssertionError):
            check_unix_sockets([], set(), expected)

    def test_private_sink_environment_replaces_inherited_audio_endpoints(self):
        with tempfile.TemporaryDirectory() as runtime:
            base = {'DISPLAY': ':10', 'PULSE_SERVER': 'tcp:host:4713',
                    'PULSE_SCRIPT': '/host/default.pa', 'PULSE_SINK': 'host-microphone'}
            audio = BrowserAudio(runtime, base)
            command = audio._prepare()
            self.assertEqual(base['PULSE_SERVER'], 'tcp:host:4713')
            self.assertEqual(audio.environment['DISPLAY'], ':10')
            self.assertEqual(audio.environment['PULSE_SERVER'], 'unix:' + str(audio.socket))
            self.assertEqual(audio.environment['PULSE_SINK'], SINK)
            self.assertNotIn('PULSE_SCRIPT', audio.environment)
            self.assertEqual(stat.S_IMODE(audio.directory.stat().st_mode), 0o700)
            for name in ['cookie', 'client.conf', 'startup.pa']:
                self.assertEqual(stat.S_IMODE((audio.directory / name).stat().st_mode), 0o600)
            self.assertEqual((audio.directory / 'cookie').stat().st_size, 256)
            script = (audio.directory / 'startup.pa').read_text()
            modules = [line.split()[1] for line in script.splitlines() if line.startswith('load-module ')]
            self.assertEqual(modules, ['module-null-sink', 'module-native-protocol-unix'])
            self.assertIn('auth-cookie-enabled=1 auth-anonymous=0', script)
            self.assertIn('--disallow-module-loading=yes', command)
            self.assertIn('-n', command)
            self.assertIn('autospawn = no', (audio.directory / 'client.conf').read_text())

    def test_readiness_requires_owned_socket_and_both_default_devices(self):
        with tempfile.TemporaryDirectory() as runtime:
            audio = BrowserAudio(runtime, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/private/browser-bus'})
            process = Mock(pid=4567)
            process.poll.return_value = None
            listener = socket.socket(socket.AF_UNIX)
            def launch(*_args, **_kwargs):
                listener.bind(str(audio.socket))
                return process
            try:
                with patch('audio.subprocess.Popen', side_effect=launch) as popen, patch.object(audio, '_default', side_effect=[SINK, SINK + '.monitor']) as readback:
                    environment = audio.start()
                self.assertEqual([call.args[0] for call in readback.call_args_list], ['sink', 'source'])
                self.assertEqual(environment['PULSE_SOURCE'], SINK + '.monitor')
                self.assertEqual(environment['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/private/browser-bus')
                self.assertEqual(popen.call_args.kwargs['env']['DBUS_SESSION_BUS_ADDRESS'],
                                 'unix:path=' + str(audio.directory / 'no-dbus'))
                with patch('audio._playback_status', return_value={'status': 'ok'}), patch('audio._sample_levels', return_value={'status': 'ok'}):
                    self.assertTrue(audio.diagnostics()['ready'])
                    self.assertNotIn(str(audio.directory), str(audio.diagnostics()))
                process.poll.return_value = 1
                self.assertFalse(audio.diagnostics()['ready'])
            finally:
                listener.close()

    def test_wrong_endpoint_fails_and_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as runtime:
            audio = BrowserAudio(runtime, {})
            process = Mock(pid=4567)
            process.poll.return_value = None
            def launch(*_args, **_kwargs):
                audio.socket.write_text('not a socket')
                return process
            with patch('audio.subprocess.Popen', side_effect=launch), patch('audio.os.killpg') as kill:
                with self.assertRaisesRegex(RuntimeError, '^browser_audio_unavailable$'):
                    audio.start()
            kill.assert_called_once_with(4567, signal.SIGTERM)
            process.wait.assert_called_once_with(timeout=5)
            self.assertIsNone(audio.process)
            self.assertFalse(audio.diagnostics()['ready'])

    def test_close_escalates_only_owned_process_group_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as runtime:
            audio = BrowserAudio(runtime, {})
            process = audio.process = Mock(pid=4567)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired('pulseaudio', 5), 0]
            with patch('audio.os.killpg') as kill:
                audio.close()
                audio.close()
            self.assertEqual([call.args for call in kill.call_args_list],
                             [(4567, signal.SIGTERM), (4567, signal.SIGKILL)])

    def test_level_sample_reports_silence_and_signal_without_retaining_pcm(self):
        for amplitude in [0, 16384, -32768]:
            with self.subTest(amplitude=amplitude):
                process = Mock(pid=7890)
                process.poll.return_value = None
                environment = {'PULSE_SERVER': 'unix:/private/native', 'PULSE_COOKIE': '/private/cookie'}
                byte_count = 0
                def read(_fd, count):
                    nonlocal byte_count
                    byte_count += count
                    return struct.pack('<h', amplitude) * (count // 2)
                with patch('audio.subprocess.Popen', return_value=process) as popen, \
                        patch('audio.select.select', return_value=([process.stdout], [], [])), \
                        patch('audio.os.read', side_effect=read), patch('audio.os.killpg') as kill:
                    result = _sample_levels(environment)
                self.assertEqual(result, {'status': 'ok', 'sample_seconds': DIAGNOSTIC_SECONDS,
                                         'rms': abs(amplitude) / 32768,
                                         'peak': abs(amplitude) / 32768,
                                         'non_silent': amplitude != 0})
                self.assertEqual(byte_count, int(DIAGNOSTIC_SECONDS * SAMPLE_RATE) * CHANNELS * 2)
                self.assertEqual(popen.call_args.kwargs['env'], environment)
                self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
                self.assertTrue(popen.call_args.kwargs['start_new_session'])
                self.assertIn('device=' + SINK + '.monitor', popen.call_args.args[0])
                self.assertNotIn('/private', json.dumps(result))
                kill.assert_called_once_with(7890, signal.SIGTERM)
                process.stdout.close.assert_called_once()

    def test_level_sample_timeout_reaps_only_owned_sampler(self):
        process = Mock(pid=7890)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('sampler', 0.5), 0]
        with patch('audio.subprocess.Popen', return_value=process), \
                patch('audio.time.monotonic', side_effect=[0, 0, 4]), \
                patch('audio.select.select', return_value=([], [], [])), \
                patch('audio.os.killpg') as kill:
            self.assertEqual(_sample_levels({}), {'status': 'timeout'})
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(7890, signal.SIGTERM), (7890, signal.SIGKILL)])
        self.assertEqual([call.kwargs for call in process.wait.call_args_list],
                         [{'timeout': 0.5}, {'timeout': 0.5}])
        process.stdout.close.assert_called_once()

    def test_level_sample_failure_returns_no_exception_or_partial_audio(self):
        process = Mock(pid=7890)
        process.poll.return_value = 1
        with patch('audio.subprocess.Popen', return_value=process), \
                patch('audio.select.select', return_value=([process.stdout], [], [])), \
                patch('audio.os.read', side_effect=[b'private media', b'']):
            self.assertEqual(_sample_levels({}), {'status': 'unavailable'})
        process.stdout.close.assert_called_once()
        with patch('audio.subprocess.Popen', side_effect=OSError('private path or credentials')):
            self.assertEqual(_sample_levels({}), {'status': 'unavailable'})

    def test_playback_metadata_contains_only_aggregate_owned_sink_values(self):
        secret = 'private media title, URL, profile and credentials'
        properties = {'application.process.binary': 'chrome', 'media.name': secret}
        datasets = [
            [{'name': 'host-device', 'index': 9, 'monitor_source': 19, 'description': secret},
             {'name': SINK, 'index': 1, 'monitor_source': 11, 'mute': True,
              'volume': {'front-left': {'value': 32768}, 'front-right': {'value': 65536}},
              'properties': {'private': secret}}],
            [{'sink': 1, 'mute': False, 'corked': False, 'properties': properties},
             {'sink': 1, 'mute': True, 'corked': True, 'properties': properties},
             {'sink': 1, 'mute': False, 'corked': False, 'properties': {'application.process.binary': 'other'}},
             {'sink': 9, 'mute': False, 'corked': False, 'properties': properties}],
            [{'source': 11, 'properties': {'private': secret}}, {'source': 19}],
        ]
        responses = [subprocess.CompletedProcess([], 0, json.dumps(data)) for data in datasets]
        environment = {'PULSE_SERVER': 'unix:/private/native'}
        with patch('audio.subprocess.run', side_effect=responses) as run:
            result = _playback_status(environment)
        self.assertEqual(result, {'status': 'ok', 'sink_muted': True, 'sink_volume_percent': 100.0,
                                 'browser_streams': 2, 'browser_streams_muted': 1,
                                 'browser_streams_corked': 1, 'browser_streams_running': 1,
                                 'capture_streams': 1})
        self.assertNotIn(secret, json.dumps(result))
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env'], environment)
            self.assertEqual(call.kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(call.kwargs['timeout'], 1)

    def test_playback_metadata_failure_does_not_expose_output(self):
        for output in ['private unparseable text', '{}', '["private"]',
                       '[{"name":"other-server"}]', 'x' * 262145]:
            with self.subTest(output=output[:30]), patch('audio.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertEqual(_playback_status({}), {'status': 'unavailable'})
        with patch('audio.subprocess.run', side_effect=subprocess.TimeoutExpired('private endpoint', 1)):
            self.assertEqual(_playback_status({}), {'status': 'unavailable'})

    def test_diagnostics_requires_exact_owned_private_endpoint_and_serializes_samples(self):
        with tempfile.TemporaryDirectory() as runtime:
            audio = BrowserAudio(runtime, {})
            audio._prepare()
            audio.process = Mock()
            audio.process.poll.return_value = None
            audio.ready = True
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(audio.socket))
                with patch('audio._playback_status', return_value={'status': 'ok'}) as playback, \
                        patch('audio._sample_levels', return_value={'status': 'ok', 'non_silent': True}) as levels:
                    report = audio.diagnostics()
                    self.assertTrue(report['levels']['non_silent'])
                    self.assertEqual(levels.call_args.args[0], audio.environment)
                    self.assertIsNot(levels.call_args.args[0], audio.environment)
                    self.assertNotIn(runtime, json.dumps(report))
                    playback.reset_mock()
                    levels.reset_mock()
                    with audio._diagnostic_lock:
                        self.assertEqual(audio.diagnostics()['levels']['status'], 'busy')
                    audio.environment['PULSE_SERVER'] = 'tcp:host:4713'
                    self.assertEqual(audio.diagnostics()['levels']['status'], 'unavailable')
                    playback.assert_not_called()
                    levels.assert_not_called()
                    audio.environment['PULSE_SERVER'] = 'unix:' + str(audio.socket)
                    (audio.directory / 'cookie').chmod(0o644)
                    self.assertEqual(audio.diagnostics()['levels']['status'], 'unavailable')
                    playback.assert_not_called()
                    levels.assert_not_called()

    def test_close_during_metadata_does_not_start_a_new_sampler(self):
        with tempfile.TemporaryDirectory() as runtime:
            audio = BrowserAudio(runtime, {})
            audio._prepare()
            audio.process = Mock()
            audio.process.poll.return_value = None
            audio.ready = True
            def stop(_environment):
                audio.ready = False
                return {'status': 'ok'}
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(audio.socket))
                with patch('audio._playback_status', side_effect=stop), patch('audio._sample_levels') as levels:
                    self.assertEqual(audio.diagnostics()['levels']['status'], 'unavailable')
                    levels.assert_not_called()


if __name__ == '__main__':
    unittest.main()
