"""Resolve only the disposable fixture worker and its directly owned display."""
import os
from pathlib import Path
import re
import stat


def process_info(process):
    try:
        if process.stat().st_uid != os.getuid():
            return None
        command = (process / 'cmdline').read_bytes().split(b'\0')
        parent = re.search(r'^PPid:\s*(\d+)$', (process / 'status').read_text(), re.M)
        if not command[0] or not parent:
            return None
        return {'pid': int(process.name), 'parent': int(parent[1]),
                'comm': (process / 'comm').read_text().strip(), 'command': command}
    except (OSError, ValueError):
        return None


def resolve_runtime(proc=Path('/proc'), temporary=Path('/tmp')):
    processes = [info for process in proc.glob('[0-9]*') if (info := process_info(process))]
    workers = [info for info in processes if any(Path(os.fsdecode(argument)).name == 'worker.py'
                                                for argument in info['command'][1:] if argument)]
    assert len(workers) == 1, 'Fixture must own exactly one browser worker'
    worker = workers[0]
    displays = [info for info in processes if info['parent'] == worker['pid']
                and info['comm'] in {'Xvfb', 'Xvnc'}]
    assert len(displays) == 1, 'Fixture worker must own exactly one Xvfb or Xvnc display'
    server = displays[0]
    arguments = server['command']
    assert arguments.count(b'-auth') == 1, 'Fixture display must use private Xauthority'
    index = arguments.index(b'-auth')
    assert index + 1 < len(arguments), 'Fixture display is missing Xauthority'
    authority = Path(os.fsdecode(arguments[index+1]))
    runtime = authority.parent
    assert runtime.parent == temporary and runtime.name.startswith('doubletake-app-')
    assert authority.name == 'Xauthority' and not authority.is_symlink()
    assert runtime.is_dir() and not runtime.is_symlink() and runtime.stat().st_uid == os.getuid()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert authority.is_file() and authority.stat().st_uid == os.getuid()
    assert stat.S_IMODE(authority.stat().st_mode) == 0o600
    sockets = [path for path in (temporary / '.X11-unix').glob('X[0-9]*')
               if path.is_socket() and path.stat().st_uid == os.getuid()]
    assert len(sockets) == 1, 'Fixture must have exactly one owned X11 socket'
    return {'runtime': runtime, 'worker_pid': worker['pid'], 'display_pid': server['pid'],
            'backend': server['comm'], 'display': ':' + sockets[0].name[1:]}
