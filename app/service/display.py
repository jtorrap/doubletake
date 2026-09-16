"""Private X11 display with optional render-node acceleration.

Xvnc supplies DRI3 without a physical display or DRM master. Its required RFB
endpoint is a private authenticated Unix socket; the existing x11vnc preview
and native input guard continue to own interaction. No profile data is touched.
"""
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import subprocess
import sys

from acceleration import render_nodes
from rfb_keepalive import start_keeper


def _capabilities(environment):
    # Run the Xlib client in the selected display environment without changing
    # this worker's process-global DISPLAY/XAUTHORITY or exposing either value.
    query = ('from Xlib import display; d=display.Display(); '
             'r=d.query_extension("DRI3"); '
             'print("1" if r and r.present else "0"); d.close()')
    dri3 = False
    renderer = 'unknown'
    try:
        result = subprocess.run([sys.executable, '-B', '-c', query], env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=5)
        dri3 = result.returncode == 0 and result.stdout.strip() == '1'
    except (OSError, subprocess.TimeoutExpired):
        pass
    if shutil.which('glxinfo'):
        try:
            result = subprocess.run(['glxinfo', '-B'], env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=5)
            if result.returncode == 0:
                match = re.search(r'^OpenGL renderer string:\s*([^\r\n]+)', result.stdout, re.M)
                if match:
                    renderer = re.sub(r'[^A-Za-z0-9 ()_.,:/+-]', '', match[1])[:240]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'dri3': dri3, 'gl_renderer': renderer}


def _private_vnc_password(path):
    # VNC passwords use eight bytes. The socket and password file are also
    # protected by the worker's private 0700 runtime and their own 0600 modes.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        password = secrets.token_urlsafe(6).encode('ascii')
        result = subprocess.run(['tigervncpasswd', '-f'],
                                input=password + b'\n',
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=True, timeout=5)
        if not isinstance(result.stdout, bytes) or len(result.stdout) != 8:
            raise RuntimeError('xvnc_password_failed')
        with os.fdopen(fd, 'wb') as output:
            fd = None
            output.write(result.stdout)
        return password
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _xvnc_command(binary, config, display_fd, authority, node):
    width, height = config['width'], config['height']
    if (type(width) is not int or type(height) is not int or
            not 320 <= width <= 3840 or not 240 <= height <= 2160 or width % 2 or height % 2):
        raise ValueError('invalid_display_geometry')
    return [binary, '-displayfd', str(display_fd), '-geometry', f'{width}x{height}',
            '-depth', '24', '-nolisten', 'tcp', '-auth', str(authority), '-noreset',
            '-rfbport', '-1', '-rfbunixpath', str(authority.parent / 'xvnc-rfb'),
            '-rfbunixmode', '0600', '-SecurityTypes', 'VncAuth',
            '-PasswordFile', str(authority.parent / 'xvnc.pass'),
            '-rendernode', str(node) if node is not None else '',
            '-desktop', 'Doubletake Browser']


def _start_xvnc(config, runtime, engine, binary, node):
    environment = engine.isolated_environment(os.environ, runtime)
    authority = Path(runtime) / 'Xauthority'
    password = Path(runtime) / 'xvnc.pass'
    socket_path = Path(runtime) / 'xvnc-rfb'
    if os.path.lexists(password) or os.path.lexists(socket_path):
        raise FileExistsError('display_runtime_not_empty')
    # The runtime is already private and owned by this worker. Exclusive create
    # prevents overwriting another display's authorization if a caller errs.
    fd = os.open(authority, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    cookie = secrets.token_hex(16)
    process = None
    keeper = None
    rfb_password = b''
    password_created = False
    read_fd = write_fd = None

    def authorize(display_name):
        subprocess.run([config['executables']['xauth'], '-f', str(authority), 'source', '-'],
                       input=f'add {display_name} MIT-MAGIC-COOKIE-1 {cookie}\n', text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=5)

    try:
        rfb_password = _private_vnc_password(password)
        password_created = True
        # Xserver accepts the cookie before displayfd chooses the final number.
        # Then add the client-side entry for the allocated private display.
        authorize(':0')
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(_xvnc_command(binary, config, write_fd, authority, node),
                                   env=environment, pass_fds=(write_fd,),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(write_fd)
        write_fd = None
        if not select.select([read_fd], [], [], 10)[0]:
            raise RuntimeError('xvnc_start_failed')
        number = os.read(read_fd, 32).strip()
        if not number.isdigit() or len(number) > 5 or process.poll() is not None:
            raise RuntimeError('xvnc_start_failed')
        display_name = ':' + number.decode('ascii')
        authorize(display_name)
        environment.update(DISPLAY=display_name, XAUTHORITY=str(authority))
        # Xvnc slows Present/vblank to 1 Hz without an authenticated RFB client.
        # Keep one idle private client; x11vnc remains the only UI transport.
        keeper = start_keeper(process, socket_path, rfb_password, engine.stop)
        rfb_password = b''
        process._doubletake_rfb_keeper = keeper
        capabilities = _capabilities(environment)
        if node is not None and not capabilities['dri3']:
            raise RuntimeError('xvnc_dri3_unavailable')
        return process, environment, {'backend': 'xvnc', **capabilities,
                                      'render_node': node.name if node is not None else None,
                                      'fallback': None}
    except BaseException:
        # Stop the whole owned display group before permitting an Xvfb retry.
        if keeper is not None:
            keeper.close()
        engine.stop(process)
        authority.unlink(missing_ok=True)
        if password_created:
            password.unlink(missing_ok=True)
        if process is not None:
            socket_path.unlink(missing_ok=True)
        raise
    finally:
        cookie = ''
        rfb_password = b''
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def start_display(config, runtime, engine):
    """Return (owned process, child environment, bounded capabilities).

    Auto uses GPU-backed Xvnc where available and fully cleans up failed starts
    before falling back. Forced xvnc exercises the same backend on CPU-only CI,
    with DRI3 explicitly disabled; its failures never silently become Xvfb.
    """
    backend = os.environ.get('DOUBLETAKE_DISPLAY_BACKEND', 'auto')
    if backend not in {'auto', 'xvnc', 'xvfb'}:
        raise ValueError('invalid_display_backend')
    fallback = None
    if backend != 'xvfb':
        binary = shutil.which('Xvnc')
        nodes = render_nodes()
        if binary and (nodes or backend == 'xvnc'):
            try:
                return _start_xvnc(config, runtime, engine, binary, nodes[0] if nodes else None)
            except FileExistsError:
                # A pre-existing authority belongs to an unknown display. A
                # fallback must not rewrite it or attach to that display.
                raise RuntimeError('display_runtime_not_empty') from None
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                if backend == 'xvnc':
                    raise RuntimeError('xvnc_start_failed') from None
                fallback = 'xvnc_start_failed'
        elif backend == 'xvnc':
            raise RuntimeError('xvnc_unavailable')
        else:
            fallback = 'xvnc_unavailable' if not binary else 'no_render_node'
    process, environment = engine.start_display(config, runtime)
    return process, environment, {'backend': 'xvfb', **_capabilities(environment),
                                  'render_node': None, 'fallback': fallback}
