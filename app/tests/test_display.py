import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import display


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.config = {'width': 1920, 'height': 1080, 'executables': {'xauth': '/usr/bin/xauth'}}
        self.environment = {'DISPLAY': ':99', 'XAUTHORITY': '/private/authority'}
        self.engine = SimpleNamespace(
            isolated_environment=lambda _env, runtime: {'XDG_RUNTIME_DIR': str(runtime)},
            start_display=Mock(return_value=(Mock(), self.environment)), stop=Mock())
        self.node = Path('/dev/dri/renderD128')
        self.process = Mock()
        self.process.poll.return_value = None
        keeper_patch = patch.object(display, 'start_keeper', return_value=Mock())
        self.start_keeper = keeper_patch.start()
        self.addCleanup(keeper_patch.stop)

    def run_command(self, command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, b'12345678' if command[0] == 'tigervncpasswd' else '', '')

    def start_process(self, command, **_kwargs):
        os.write(int(command[command.index('-displayfd')+1]), b'12\n')
        return self.process

    def test_auto_uses_authenticated_gpu_display_with_private_rfb_socket(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'auto'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[self.node]), \
             patch.object(display.subprocess, 'Popen', side_effect=self.start_process) as popen, \
             patch.object(display.subprocess, 'run', side_effect=self.run_command) as run, \
             patch.object(display, '_capabilities', return_value={'dri3': True, 'gl_renderer': 'Mesa Intel'}):
            process, env, metadata = display.start_display(self.config, directory, self.engine)
            self.assertIs(process, self.process)
            self.assertEqual(env['DISPLAY'], ':12')
            authority = Path(env['XAUTHORITY'])
            self.assertEqual(authority.stat().st_mode & 0o777, 0o600)
            command = popen.call_args.args[0]
            for flag, value in [('-rfbport', '-1'), ('-nolisten', 'tcp'), ('-rendernode', str(self.node)),
                                ('-geometry', '1920x1080'), ('-auth', str(authority)),
                                ('-rfbunixpath', str(Path(directory) / 'xvnc-rfb')),
                                ('-rfbunixmode', '0600'), ('-SecurityTypes', 'VncAuth'),
                                ('-PasswordFile', str(Path(directory) / 'xvnc.pass'))]:
                self.assertEqual(command[command.index(flag)+1], value)
            self.assertNotIn('None', command)
            password = Path(directory) / 'xvnc.pass'
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)
            self.assertEqual(password.read_bytes(), b'12345678')
            password_call = run.call_args_list[0]
            self.assertEqual(password_call.args[0], ['tigervncpasswd', '-f'])
            self.assertEqual(len(password_call.kwargs['input']), 9)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertTrue(run.call_args_list[1].kwargs['input'].startswith('add :0 MIT-MAGIC-COOKIE-1 '))
            self.assertTrue(run.call_args_list[2].kwargs['input'].startswith('add :12 MIT-MAGIC-COOKIE-1 '))
            self.assertEqual(metadata, {'backend': 'xvnc', 'dri3': True, 'gl_renderer': 'Mesa Intel',
                                        'render_node': 'renderD128', 'fallback': None})
            self.engine.start_display.assert_not_called()
            keeper_args = self.start_keeper.call_args.args
            self.assertIs(keeper_args[0], process)
            self.assertEqual(keeper_args[1], Path(directory) / 'xvnc-rfb')
            self.assertEqual(keeper_args[2], password_call.kwargs['input'][:-1])
            self.assertIs(process._doubletake_rfb_keeper, self.start_keeper.return_value)

    def test_forced_xvnc_exercises_cpu_only_ci_without_auto_render_device(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'xvnc'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[]), \
             patch.object(display.subprocess, 'Popen', side_effect=self.start_process) as popen, \
             patch.object(display.subprocess, 'run', side_effect=self.run_command), \
             patch.object(display, '_capabilities', return_value={'dri3': False, 'gl_renderer': 'llvmpipe'}):
            _, _, metadata = display.start_display(self.config, directory, self.engine)
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('-rendernode')+1], '')
            self.assertEqual(metadata['backend'], 'xvnc')
            self.assertFalse(metadata['dri3'])
            self.engine.start_display.assert_not_called()

    def test_auto_cleans_failed_gpu_display_before_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'auto'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[self.node]), \
             patch.object(display.subprocess, 'Popen', side_effect=self.start_process), \
             patch.object(display.subprocess, 'run', side_effect=self.run_command), \
             patch.object(display, '_capabilities', return_value={'dri3': False, 'gl_renderer': 'unknown'}):
            def fallback(*_args):
                self.engine.stop.assert_called_once_with(self.process)
                self.assertFalse((Path(directory) / 'Xauthority').exists())
                self.assertFalse((Path(directory) / 'xvnc.pass').exists())
                return Mock(), self.environment
            self.engine.start_display.side_effect = fallback
            _, _, metadata = display.start_display(self.config, directory, self.engine)
            self.assertEqual(metadata['backend'], 'xvfb')
            self.assertEqual(metadata['fallback'], 'xvnc_start_failed')
            self.start_keeper.return_value.close.assert_called_once()

    def test_forced_xvnc_failure_does_not_hide_behind_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'xvnc'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[]), \
             patch.object(display.subprocess, 'Popen', return_value=self.process), \
             patch.object(display.subprocess, 'run', side_effect=self.run_command), \
             patch.object(display.select, 'select', return_value=([], [], [])):
            with self.assertRaisesRegex(RuntimeError, '^xvnc_start_failed$'):
                display.start_display(self.config, directory, self.engine)
            self.engine.stop.assert_called_once_with(self.process)
            self.engine.start_display.assert_not_called()
            self.assertFalse((Path(directory) / 'Xauthority').exists())

    def test_no_gpu_auto_retains_existing_display(self):
        with patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'auto'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[]), \
             patch.object(display, '_capabilities', return_value={'dri3': False, 'gl_renderer': 'unknown'}):
            _, env, metadata = display.start_display(self.config, '/unused', self.engine)
            self.assertIs(env, self.environment)
            self.assertEqual(metadata['fallback'], 'no_render_node')
            self.engine.start_display.assert_called_once()

    def test_capabilities_exposes_only_allowlisted_fields(self):
        with patch.object(display.shutil, 'which', return_value='/usr/bin/glxinfo'), \
             patch.object(display.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0, '1\n', 'private error'),
                 subprocess.CompletedProcess([], 0, 'secret-other-field\nOpenGL renderer string: Mesa Intel <ADL-N>\nprivate-tail', '')]) as run:
            self.assertEqual(display._capabilities(self.environment), {'dri3': True, 'gl_renderer': 'Mesa Intel ADL-N'})
            self.assertTrue(all(call.kwargs['env'] is self.environment for call in run.call_args_list))

    def test_invalid_backend_is_rejected_before_process_start(self):
        with patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'invalid'}):
            with self.assertRaisesRegex(ValueError, '^invalid_display_backend$'):
                display.start_display(self.config, '/unused', self.engine)
            self.engine.start_display.assert_not_called()

    def test_existing_authority_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'auto'}), \
             patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
             patch.object(display, 'render_nodes', return_value=[self.node]):
            authority = Path(directory) / 'Xauthority'
            authority.write_text('existing')
            with self.assertRaisesRegex(RuntimeError, '^display_runtime_not_empty$'):
                display.start_display(self.config, directory, self.engine)
            self.assertEqual(authority.read_text(), 'existing')
            self.engine.stop.assert_not_called()
            self.engine.start_display.assert_not_called()

    def test_invalid_password_output_is_removed_without_starting_display(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(display.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'invalid', b'')):
            password = Path(directory) / 'xvnc.pass'
            with self.assertRaisesRegex(RuntimeError, '^xvnc_password_failed$'):
                display._private_vnc_password(password)
            self.assertFalse(password.exists())

    def test_existing_password_and_socket_are_never_reused(self):
        for name in ['xvnc.pass', 'xvnc-rfb']:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'DOUBLETAKE_DISPLAY_BACKEND': 'auto'}), \
                 patch.object(display.shutil, 'which', return_value='/usr/local/bin/Xvnc'), \
                 patch.object(display, 'render_nodes', return_value=[self.node]):
                owned = Path(directory) / name
                owned.write_text('existing')
                with self.assertRaisesRegex(RuntimeError, '^display_runtime_not_empty$'):
                    display.start_display(self.config, directory, self.engine)
                self.assertEqual(owned.read_text(), 'existing')
                self.assertFalse((Path(directory) / 'Xauthority').exists())
                self.engine.start_display.assert_not_called()
