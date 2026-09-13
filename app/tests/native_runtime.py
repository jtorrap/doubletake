"""Linux-only checks against the disposable integration container's display."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/browser-app')
from native_control import accessibility_address, frozen_frame, preview_readonly
from Xlib import X, display


def main():
    candidates = []
    for process in Path('/proc').glob('[0-9]*'):
        try:
            command = (process/'cmdline').read_bytes().split(b'\0')
            if b'--class=DoubletakeBrowser' in command:
                candidates.append((process, command))
        except OSError:
            pass
    assert len(candidates) == 1
    process, command = candidates[0]
    assert not any(part.startswith((b'--remote-debugging',b'--enable-automation',b'--headless')) for part in command)
    environment = dict(part.split(b'=',1) for part in (process/'environ').read_bytes().split(b'\0') if b'=' in part)
    for name in ('DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','AT_SPI_BUS_ADDRESS'):
        os.environ[name] = environment[name.encode()].decode()
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
                      'capture_freeze_and_resume':True,'preview_guard_readback':True}))


if __name__ == '__main__':
    main()
