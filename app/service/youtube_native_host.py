#!/usr/bin/python3
"""Bounded Chrome-native messaging relay to the owned browser worker socket."""
import json
import os
from pathlib import Path
import re
import select
import selectors
import socket
import stat
import struct
import sys


MAX_MESSAGE = 32768
INSTALL_METADATA = Path('/opt/browser-app/youtube-extension-install.json')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('invalid_message')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('invalid_message')


def object_payload(payload):
    if not 0 < len(payload) <= MAX_MESSAGE:
        raise ValueError('invalid_message')
    value = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('invalid_message')
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(result) > MAX_MESSAGE:
        raise ValueError('invalid_message')
    return result


def authorized_origin(arguments, metadata_path=INSTALL_METADATA):
    path = Path(metadata_path)
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError('invalid_origin')
    metadata = json.loads(path.read_text())
    extension_id = metadata.get('extension_id') if isinstance(metadata, dict) else None
    if (not isinstance(extension_id, str) or not re.fullmatch('[a-p]{32}', extension_id)
            or len(arguments) != 1 or arguments[0] != 'chrome-extension://' + extension_id + '/'):
        raise ValueError('invalid_origin')


def connect_worker(environment):
    runtime = Path(environment.get('XDG_RUNTIME_DIR', ''))
    endpoint = Path(environment.get('DOUBLETAKE_YOUTUBE_SOCKET', ''))
    if (not runtime.is_absolute() or not endpoint.is_absolute()
            or runtime.is_symlink() or endpoint.is_symlink()
            or endpoint.parent != runtime or len(os.fsencode(endpoint)) > 103):
        raise ValueError('invalid_socket')
    for path, required_mode, kind in ((runtime, 0o700, stat.S_ISDIR), (endpoint, 0o600, stat.S_ISSOCK)):
        details = path.stat()
        if not kind(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != required_mode:
            raise ValueError('invalid_socket')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(5)
        connection.connect(str(endpoint))
        if hasattr(socket, 'SO_PEERCRED'):
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid():
                raise ValueError('invalid_socket')
        return connection
    except BaseException:
        connection.close()
        raise


def relay(input_fd, output_fd, connection):
    """Forward JSON objects; either peer closing ends the owned host process."""
    chrome = bytearray()
    worker = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(input_fd, selectors.EVENT_READ, 'chrome')
        selector.register(connection, selectors.EVENT_READ, 'worker')
        while True:
            for key, _ in selector.select():
                if key.data == 'chrome':
                    chunk = os.read(input_fd, 4096)
                    if not chunk:
                        if chrome:
                            raise ValueError('incomplete_message')
                        return
                    chrome.extend(chunk)
                    while len(chrome) >= 4:
                        size = struct.unpack('=I', chrome[:4])[0]
                        if not 0 < size <= MAX_MESSAGE:
                            raise ValueError('invalid_message')
                        if len(chrome) < size + 4:
                            break
                        payload = object_payload(bytes(chrome[4:size + 4]))
                        del chrome[:size + 4]
                        connection.sendall(payload + b'\n')
                else:
                    chunk = connection.recv(4096)
                    if not chunk:
                        if worker:
                            raise ValueError('incomplete_message')
                        return
                    worker.extend(chunk)
                    while b'\n' in worker:
                        line, _, remainder = worker.partition(b'\n')
                        worker = bytearray(remainder)
                        payload = object_payload(line)
                        write_frame(output_fd, payload)
                    if len(worker) > MAX_MESSAGE:
                        raise ValueError('invalid_message')


def write_frame(output_fd, payload):
    # A stalled Chrome must not retain its host indefinitely during shutdown.
    previous = os.get_blocking(output_fd)
    os.set_blocking(output_fd, False)
    try:
        frame = memoryview(struct.pack('=I', len(payload)) + payload)
        while frame:
            if not select.select([], [output_fd], [], 5)[1]:
                raise TimeoutError('output_stalled')
            try:
                written = os.write(output_fd, frame)
            except BlockingIOError:
                continue
            if written <= 0:
                raise OSError('output_closed')
            frame = frame[written:]
    finally:
        os.set_blocking(output_fd, previous)


def main(arguments=None, environment=None):
    try:
        authorized_origin(sys.argv[1:] if arguments is None else arguments)
        with connect_worker(os.environ if environment is None else environment) as connection:
            relay(sys.stdin.fileno(), sys.stdout.fileno(), connection)
        return 0
    except Exception:
        # Never emit raw JSON, browser URLs, errors, or environment values.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
