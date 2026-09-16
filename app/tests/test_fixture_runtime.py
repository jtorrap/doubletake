import os
from pathlib import Path
import socket
import tempfile
import unittest

from fixture_runtime import resolve_runtime


class FixtureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.proc, self.temporary = self.root/'proc', self.root/'tmp'
        self.proc.mkdir()
        self.temporary.mkdir()
        self.runtime = self.temporary/'doubletake-app-fixture'
        self.runtime.mkdir(mode=0o700)
        self.authority = self.runtime/'Xauthority'
        self.authority.write_bytes(b'synthetic')
        self.authority.chmod(0o600)
        sockets = self.temporary/'.X11-unix'
        sockets.mkdir()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(sockets/'X9'))
        self.process(10, 1, 'python3', ['python3', '-B', '/opt/browser-app/worker.py', '/data/worker.json'])
        self.process(11, 10, 'Xvfb', ['Xvfb', '-auth', str(self.authority)])

    def tearDown(self):
        self.socket.close()
        self.directory.cleanup()

    def process(self, pid, parent, name, command):
        process = self.proc/str(pid)
        process.mkdir(exist_ok=True)
        (process/'comm').write_text(name+'\n')
        (process/'status').write_text(f'Name:\t{name}\nPPid:\t{parent}\n')
        (process/'cmdline').write_bytes(b'\0'.join(os.fsencode(item) for item in command)+b'\0')

    def test_both_backends_resolve_the_same_private_worker_runtime(self):
        for backend in ['Xvfb', 'Xvnc']:
            with self.subTest(backend=backend):
                self.process(11, 10, backend, [backend, '-auth', str(self.authority)])
                resolved = resolve_runtime(self.proc, self.temporary)
                self.assertEqual(resolved, {'runtime':self.runtime,'worker_pid':10,
                    'display_pid':11,'backend':backend,'display':':9'})

    def test_unrelated_display_cannot_satisfy_worker_ownership(self):
        self.process(11, 99, 'Xvnc', ['Xvnc','-auth',str(self.authority)])
        with self.assertRaisesRegex(AssertionError, 'worker must own'):
            resolve_runtime(self.proc, self.temporary)

    def test_public_authority_is_rejected(self):
        self.authority.chmod(0o644)
        with self.assertRaises(AssertionError):
            resolve_runtime(self.proc, self.temporary)

    def test_stopped_worker_does_not_resolve_a_leftover_display(self):
        (self.proc/'10'/'comm').unlink()
        with self.assertRaisesRegex(AssertionError, 'exactly one browser worker'):
            resolve_runtime(self.proc, self.temporary)


if __name__ == '__main__':
    unittest.main()
