import contextlib
import importlib.util
import io
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from rfb_keepalive import connect_keeper, RFBKeeper, _encrypt_challenge


class PrivateRFBTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rfb'
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.addCleanup(self.listener.close)
        self.received = []
        self.release = threading.Event()
        self.server_error = []

    def serve(self, security=b'\x01\x02', result=b'\0\0\0\0'):
        try:
            with self.listener.accept()[0] as connection:
                connection.settimeout(2)
                for part in [b'RFB ', b'003.008\n']:
                    connection.sendall(part)
                self.received.append(connection.recv(12))
                connection.sendall(security)
                if security != b'\x01\x02':
                    return
                self.received.append(connection.recv(1))
                connection.sendall(bytes(range(16)))
                self.received.append(connection.recv(16))
                connection.sendall(result)
                if result != b'\0\0\0\0':
                    return
                self.received.append(connection.recv(1))
                name = b'Synthetic display'
                connection.sendall(struct.pack('!HH', 1920, 1080) + bytes(16) +
                                   struct.pack('!I', len(name)) + name)
                self.release.wait(2)
                connection.settimeout(0.05)
                try:
                    self.received.append(connection.recv(1))
                except socket.timeout:
                    pass
        except Exception as error:
            self.server_error.append(type(error).__name__)

    def start_server(self, **kwargs):
        thread = threading.Thread(target=self.serve, kwargs=kwargs, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.release.set(), thread.join(timeout=3)))
        return thread

    def test_authenticates_locally_then_stays_idle_without_logging(self):
        thread = self.start_server()
        output = io.StringIO()
        with patch('rfb_keepalive._encrypt_challenge', return_value=b'R' * 16) as encrypt, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            connection = connect_keeper(self.path, b'password', expected_pid=os.getpid())
            self.addCleanup(connection.close)
            self.release.set()
            thread.join(timeout=3)
        encrypt.assert_called_once_with(b'password', bytes(range(16)))
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.received, [b'RFB 003.008\n', b'\x02', b'R' * 16, b'\x01'])
        self.assertEqual(self.server_error, [])

    def test_rejects_unauthenticated_security_type(self):
        self.start_server(security=b'\x01\x01')
        with self.assertRaisesRegex(RuntimeError, '^xvnc_keepalive_failed$'):
            connect_keeper(self.path, b'password', expected_pid=os.getpid())

    def test_rejects_authentication_failure_with_fixed_error(self):
        self.start_server(result=b'\0\0\0\x01')
        with patch('rfb_keepalive._encrypt_challenge', return_value=b'R' * 16):
            with self.assertRaisesRegex(RuntimeError, '^xvnc_keepalive_failed$'):
                connect_keeper(self.path, b'password', expected_pid=os.getpid())

    def test_rejects_socket_that_is_not_private(self):
        self.path.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, '^xvnc_keepalive_failed$'):
            connect_keeper(self.path, b'password')

    @unittest.skipUnless(importlib.util.find_spec('Cryptodome'), 'Debian crypto dependency tested in image')
    def test_vnc_des_known_vector(self):
        self.assertEqual(_encrypt_challenge(b'password', bytes(range(16))).hex(),
                         'b866924125c8eebb9debc1db61c538e2')

    def test_keeper_closes_after_owned_display_exits(self):
        parent, peer = socket.socketpair()
        self.addCleanup(peer.close)
        process, stop = Mock(), Mock()
        process.poll.return_value = None
        keeper = RFBKeeper(process, parent, stop)
        self.addCleanup(keeper.close)
        process.poll.return_value = 0
        peer.settimeout(1)
        self.assertEqual(peer.recv(1), b'')
        stop.assert_not_called()
        self.assertTrue(keeper._stopped.wait(1))

    def test_unexpected_disconnect_stops_only_owned_display(self):
        parent, peer = socket.socketpair()
        process = Mock()
        process.poll.return_value = None
        stopped = threading.Event()
        stop = Mock(side_effect=lambda _process: stopped.set())
        keeper = RFBKeeper(process, parent, stop)
        self.addCleanup(keeper.close)
        peer.close()
        self.assertTrue(stopped.wait(1))
        stop.assert_called_once_with(process)

    def test_explicit_keeper_close_does_not_kill_display(self):
        parent, peer = socket.socketpair()
        self.addCleanup(peer.close)
        process, stop = Mock(), Mock()
        process.poll.return_value = None
        keeper = RFBKeeper(process, parent, stop)
        keeper.close()
        self.assertEqual(peer.recv(1), b'')
        stop.assert_not_called()
        self.assertFalse(keeper._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
