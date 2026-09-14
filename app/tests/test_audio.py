import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from audio import BrowserAudio, SINK
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


if __name__ == '__main__':
    unittest.main()
