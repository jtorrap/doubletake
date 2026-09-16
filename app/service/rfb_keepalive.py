"""Keep the private Xvnc Present clock active without requesting any pixels.

TigerVNC throttles Present MSC to 1 Hz when its own RFB desktop has no
authenticated client. x11vnc does not count as one. This local connection
authenticates, completes ClientInit, and stays idle; it never sends input,
clipboard content, or framebuffer requests, and never logs protocol data.
"""
import contextlib
import os
from pathlib import Path
import select
import socket
import stat
import struct
import threading
import time


def _read_exact(connection, count, deadline):
    output = bytearray()
    while len(output) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection.settimeout(remaining)
        part = connection.recv(count - len(output))
        if not part:
            raise EOFError()
        output.extend(part)
    return bytes(output)


def _encrypt_challenge(password, challenge):
    # RFB VncAuth reverses the bits of each password byte before DES. The
    # eight-byte password exists only in memory and the protected VNC file.
    from Cryptodome.Cipher import DES
    if len(password) != 8 or len(challenge) != 16:
        raise ValueError()
    key = bytes(int(f'{value:08b}'[::-1], 2) for value in password)
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)


def connect_keeper(path, password, expected_pid=None):
    """Authenticate a bounded, local-only RFB connection and return its socket."""
    connection = None
    try:
        path = Path(path)
        parent, endpoint = path.parent.lstat(), path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
                stat.S_IMODE(parent.st_mode) != 0o700 or
                not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or
                stat.S_IMODE(endpoint.st_mode) != 0o600):
            raise ValueError()
        deadline = time.monotonic() + 5
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(path))
        if hasattr(socket, 'SO_PEERCRED'):
            peer_pid, peer_uid, _ = struct.unpack('3i', connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            if peer_uid != os.getuid() or (expected_pid is not None and peer_pid != expected_pid):
                raise ValueError()
        version = _read_exact(connection, 12, deadline)
        if version != b'RFB 003.008\n':
            raise ValueError()
        connection.sendall(version)
        count = _read_exact(connection, 1, deadline)[0]
        if count != 1 or _read_exact(connection, count, deadline) != b'\x02':
            raise ValueError()
        connection.sendall(b'\x02')
        challenge = _read_exact(connection, 16, deadline)
        connection.sendall(_encrypt_challenge(password, challenge))
        password = b''
        if _read_exact(connection, 4, deadline) != b'\0\0\0\0':
            raise ValueError()
        connection.sendall(b'\x01')  # Shared ClientInit, never disconnect peers.
        initialization = _read_exact(connection, 24, deadline)
        name_size = struct.unpack('!I', initialization[20:24])[0]
        if name_size > 4096:
            raise ValueError()
        _read_exact(connection, name_size, deadline)
        connection.settimeout(None)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise RuntimeError('xvnc_keepalive_failed') from None


class RFBKeeper:
    """Own the connection until its display exits; fail closed if it disconnects."""
    def __init__(self, process, connection, stop_display):
        self._process = process
        self._connection = connection
        self._stop_display = stop_display
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._watch, name='xvnc-clock', daemon=True)
        self._thread.start()

    def _watch(self):
        try:
            while not self._stopped.is_set() and self._process.poll() is None:
                if select.select([self._connection], [], [], 0.2)[0]:
                    # No frame requests are sent. Discard unsolicited protocol
                    # data without interpreting or retaining page/clipboard data.
                    if not self._connection.recv(4096):
                        if not self._stopped.is_set() and self._process.poll() is None:
                            self._stop_display(self._process)
                        return
        except (OSError, ValueError):
            if not self._stopped.is_set() and self._process.poll() is None:
                self._stop_display(self._process)
        finally:
            self._connection.close()
            self._stopped.set()

    def close(self):
        self._stopped.set()
        with contextlib.suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        self._connection.close()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)


def start_keeper(process, path, password, stop_display):
    connection = connect_keeper(path, password, expected_pid=process.pid)
    try:
        return RFBKeeper(process, connection, stop_display)
    except BaseException:
        connection.close()
        raise
