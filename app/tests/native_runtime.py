"""Linux-only checks against the disposable integration container's display."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/browser-app')
from native_control import accessibility_address, frozen_frame, preview_readonly, xdo
from Xlib import X, display
from fixture_runtime import process_info, resolve_runtime


def main():
    # The fixture owns exactly one worker and X display. Resolve Chrome through
    # its actual window, not inherited/rewritten child-process command lines.
    try:
        fixture = resolve_runtime()
    except AssertionError:
        if '--inspect' not in sys.argv:
            raise
        # Failed display startup may already have cleaned every owned process
        # and runtime. Do not obscure its primary failure with another trace.
        print(json.dumps({'inspection_unavailable':'fixture_runtime_not_ready'}))
        return
    runtime = fixture['runtime']
    os.environ.update(DISPLAY=fixture['display'], XAUTHORITY=str(runtime/'Xauthority'),
                      XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS='unix:path='+str(runtime/'session-bus'))
    os.environ['AT_SPI_BUS_ADDRESS'] = accessibility_address(os.environ)
    windows = xdo('search','--onlyvisible','--class','^DoubletakeBrowser$').split()
    assert len(windows) == 1
    process = Path('/proc') / xdo('getwindowpid', windows[0])
    info = process_info(process)
    assert info and info['parent'] == fixture['worker_pid'], 'Chrome window is not owned by the fixture worker'
    command = (process/'cmdline').read_bytes().split(b'\0')
    assert command[0]
    assert not any(part.startswith((b'--remote-debugging',b'--enable-automation',b'--headless')) for part in command)
    if '--inspect' in sys.argv:
        import pyatspi
        rows, queue = [], [(app,0) for app in pyatspi.Registry.getDesktop(0) if app]
        while queue and len(rows) < 150:
            node, depth = queue.pop(0)
            role = node.getRole()
            states = node.getState()
            if role in {pyatspi.ROLE_DOCUMENT_WEB, pyatspi.ROLE_DOCUMENT_FRAME, pyatspi.ROLE_EMBEDDED}:
                continue
            rows.append({'depth':depth,'role':node.getRoleName(),'name':node.name[:100],
                         'pid':node.get_process_id(),'showing':states.contains(pyatspi.STATE_SHOWING),
                         'editable':states.contains(pyatspi.STATE_EDITABLE),'focused':states.contains(pyatspi.STATE_FOCUSED)})
            queue.extend((child,depth+1) for child in node if child)
        print(json.dumps(rows))
        return
    assert os.environ['DBUS_SESSION_BUS_ADDRESS'].startswith('unix:path='+os.environ['XDG_RUNTIME_DIR']+'/')
    assert accessibility_address(os.environ).startswith('unix:path='+os.environ['XDG_RUNTIME_DIR']+'/')
    connection = display.Display()
    screen = connection.screen()
    window = screen.root.create_window(0,0,160,100,0,screen.root_depth,
                                      background_pixel=screen.white_pixel, override_redirect=True)
    try:
        window.map()
        connection.sync()
        before = window.get_image(0,0,160,100,X.ZPixmap,0xffffffff).data
        with frozen_frame(window.id):
            window.change_attributes(background_pixel=screen.black_pixel)
            window.clear_area()
            connection.sync()
            during = window.get_image(0,0,160,100,X.ZPixmap,0xffffffff).data
            assert during == before, 'Freeze did not retain the captured frame'
        connection.sync()
        after = window.get_image(0,0,160,100,X.ZPixmap,0xffffffff).data
        assert after != before, 'Capture did not resume after removing the cover'
        preview_readonly(True)
        preview_readonly(False)
    finally:
        window.destroy()
        connection.sync()
        connection.close()
    print(json.dumps({'no_debugging_channel':True,'private_filesystem_buses':True,
                      'capture_freeze_and_resume':True,'preview_guard_readback':True,
                      'owned_display_backend':fixture['backend']}))


if __name__ == '__main__':
    main()
