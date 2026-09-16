import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import youtube_native_host as host


def frame(value):
    payload = json.dumps(value, ensure_ascii=False).encode()
    return struct.pack('=I', len(payload)) + payload


def exact_read(descriptor, size):
    result = bytearray()
    while len(result) < size:
        item = os.read(descriptor, size - len(result))
        if not item:
            raise EOFError('fixture closed')
        result.extend(item)
    return bytes(result)


class YouTubeNativeHostTests(unittest.TestCase):
    def setUp(self):
        self.chrome_read, self.chrome_write = os.pipe()
        self.output_read, self.output_write = os.pipe()
        self.relay_socket, self.worker = socket.socketpair()
        self.worker.settimeout(2)
        self.errors = []
        self.thread = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for descriptor in (self.chrome_write, self.chrome_read, self.output_read, self.output_write):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self.worker.close()
        self.relay_socket.close()
        if self.thread:
            self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive(), 'relay did not stop after peers closed')

    def start(self):
        def run():
            try:
                host.relay(self.chrome_read, self.output_write, self.relay_socket)
            except Exception as error:
                self.errors.append(type(error))
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def test_bidirectional_messages_fragmented_and_coalesced_then_chrome_eof(self):
        self.start()
        message = {'type': 'status', 'state': 'playing', 'fixture': 'déjà 🎬', 'launch_id': '1'}
        encoded = frame(message)
        os.write(self.chrome_write, encoded[:2])
        os.write(self.chrome_write, encoded[2:17])
        os.write(self.chrome_write, encoded[17:] + frame({'type': 'reply', 'id': '2', 'ok': True}))
        incoming = self.worker.makefile('rb')
        self.addCleanup(incoming.close)
        self.assertEqual(json.loads(incoming.readline()), message)
        self.assertEqual(json.loads(incoming.readline()), {'type': 'reply', 'id': '2', 'ok': True})
        command = {'id': '3', 'action': 'launch', 'mode': 'video', 'url': 'https://www.youtube.com/watch?v=fixture'}
        payload = json.dumps(command).encode() + b'\n'
        self.worker.sendall(payload[:4])
        self.worker.sendall(payload[4:] + b'{"id":"4","action":"cancel"}\n')
        for expected in (command, {'id': '4', 'action': 'cancel'}):
            length = struct.unpack('=I', exact_read(self.output_read, 4))[0]
            self.assertEqual(json.loads(exact_read(self.output_read, length)), expected)
        os.close(self.chrome_write)
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_worker_eof_stops_waiting_for_chrome(self):
        self.start()
        self.worker.close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_oversize_chrome_header_stops_before_waiting_for_payload(self):
        self.start()
        os.write(self.chrome_write, struct.pack('=I', host.MAX_MESSAGE + 1))
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [ValueError])

    def test_invalid_worker_object_is_not_sent_to_chrome(self):
        self.start()
        self.worker.sendall(b'["not an object"]\n')
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [ValueError])

    def test_json_bounds_and_nonstandard_or_duplicate_values_are_rejected(self):
        for payload in (b'', b'[]', b'null', b'{broken', b'{"x":NaN}', b'{"x":1e999}',
                        b'{"x":1,"x":2}', b'{"x":"' + b'a' * host.MAX_MESSAGE + b'"}'):
            with self.subTest(payload_size=len(payload)), self.assertRaises(ValueError):
                host.object_payload(payload)
        self.assertEqual(json.loads(host.object_payload(b'{"x":"escaped\\nline"}')), {'x': 'escaped\nline'})

    def test_origin_exact_match_required_and_main_failure_is_silent(self):
        identity = 'abcdefghijklmnopabcdefghijklmnop'
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / 'install.json'
            metadata.write_text(json.dumps({'extension_id': identity, 'version': '1.0.0'}))
            host.authorized_origin(['chrome-extension://' + identity + '/'], metadata)
            for arguments in ([], ['chrome-extension://' + identity], ['https://www.youtube.com/'],
                              ['chrome-extension://' + 'a' * 32 + '/'],
                              ['chrome-extension://' + identity + '/', 'unexpected']):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    host.authorized_origin(arguments, metadata)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(host, 'authorized_origin', side_effect=ValueError('private fixture')), \
             patch.object(host, 'connect_worker') as connect, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(host.main([], {}), 1)
            connect.assert_not_called()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), '')

    def test_stalled_output_times_out_and_restores_pipe_mode(self):
        original = os.get_blocking(self.output_write)
        with patch.object(host.select, 'select', return_value=([], [], [])):
            with self.assertRaises(TimeoutError):
                host.write_frame(self.output_write, b'{}')
        self.assertEqual(os.get_blocking(self.output_write), original)

    def test_private_unix_socket_required_and_connection_closes(self):
        # Short path also fits the stricter macOS Unix socket path limit.
        with tempfile.TemporaryDirectory(prefix='dtyt-', dir='/tmp') as directory:
            runtime = Path(directory)
            runtime.chmod(0o700)
            endpoint = runtime / 'youtube.sock'
            environment = {'XDG_RUNTIME_DIR': str(runtime), 'DOUBLETAKE_YOUTUBE_SOCKET': str(endpoint)}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(endpoint))
                endpoint.chmod(0o600)
                listener.listen(1)
                with host.connect_worker(environment):
                    peer, _ = listener.accept()
                with peer:
                    self.assertEqual(peer.recv(1), b'')
                endpoint.chmod(0o666)
                with self.assertRaisesRegex(ValueError, 'invalid_socket'):
                    host.connect_worker(environment)
                endpoint.chmod(0o600)
                runtime.chmod(0o755)
                with self.assertRaisesRegex(ValueError, 'invalid_socket'):
                    host.connect_worker(environment)
                runtime.chmod(0o700)
                for value in ('http://127.0.0.1:80', 'relative.sock', str(runtime.parent / 'other.sock')):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        host.connect_worker(dict(environment, DOUBLETAKE_YOUTUBE_SOCKET=value))
