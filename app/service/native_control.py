"""Bounded native Chrome controls on the worker's private X11/session bus.

Input arrives only on stdin. Never print page content, accessibility trees,
process arguments, passwords or URLs. No browser debugging channel is used.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from model import browser_text, page_url

STAGE = 'validate'


def browser_command(engine, config, bootstrap):
    command = [part for part in engine.browser_command(config, bootstrap)
               if part not in {'--remote-debugging-pipe', '--kiosk'} and not part.startswith('--app=')]
    return command + ['--start-fullscreen', '--force-renderer-accessibility', '--lang=en-US', bootstrap.as_uri()]


def xdo(*args, text=None):
    return subprocess.run(['xdotool', *map(str, args)], input=text, text=True,
                          capture_output=True, check=True, timeout=25).stdout.strip()


def preview_readonly(enabled):
    result = subprocess.run(['x11vnc', '-display', os.environ['DISPLAY'], '-auth', os.environ['XAUTHORITY'],
                             '-R', 'viewonly' if enabled else 'noviewonly', '-Q', 'viewonly'],
                            capture_output=True, text=True, timeout=8)
    if result.returncode or f'ans=viewonly:{int(enabled)}' not in result.stdout:
        raise RuntimeError('preview_input_guard_failed')


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise RuntimeError('native_control_state_unconfirmed')


class ChromeUI:
    def __init__(self, pid):
        import pyatspi
        self.spi = pyatspi
        self.pid = pid

    def address(self, focused=False):
        spi = self.spi
        # Inspect only the native browser UI, never descend into web documents.
        queue = []
        for app in spi.Registry.getDesktop(0):
            if app and app.get_process_id() == self.pid:
                queue.append((app, False))
        visited = 0
        while queue and visited < 1000:
            node, toolbar = queue.pop(0)
            visited += 1
            try:
                role = node.getRole()
                if role in {spi.ROLE_DOCUMENT_WEB, spi.ROLE_DOCUMENT_FRAME, spi.ROLE_EMBEDDED}:
                    continue
                toolbar = toolbar or role == spi.ROLE_TOOL_BAR
                states = node.getState()
                if toolbar and node.name == 'Address and search bar' and states.contains(spi.STATE_EDITABLE):
                    if states.contains(spi.STATE_SHOWING) and (not focused or states.contains(spi.STATE_FOCUSED)):
                        return node
                queue.extend((child, toolbar) for child in node if child)
            except Exception:
                continue
        return None


@contextlib.contextmanager
def frozen_frame(window_id):
    from Xlib import X, display
    connection = display.Display()
    cover = pixmap = gc = None
    try:
        window = connection.create_resource_object('window', window_id)
        geometry = window.get_geometry()
        pixmap = window.create_pixmap(geometry.width, geometry.height, geometry.depth)
        gc = pixmap.create_gc(subwindow_mode=X.IncludeInferiors)
        pixmap.copy_area(gc, window, 0, 0, geometry.width, geometry.height, 0, 0)
        cover = window.create_window(0, 0, geometry.width, geometry.height, 0,
                                     X.CopyFromParent, background_pixmap=pixmap, override_redirect=True)
        cover.map()
        cover.configure(stack_mode=X.Above)
        connection.sync()
        yield
    finally:
        if cover:
            cover.destroy()
        if gc:
            gc.free()
        if pixmap:
            pixmap.free()
        connection.sync()
        connection.close()


def perform(value):
    global STAGE
    action, window, pid = value['action'], int(value['window']), int(value['pid'])
    if action not in {'insert_text', 'navigate', 'back', 'forward', 'reload', 'close'}:
        raise ValueError('unknown_native_action')
    text = browser_text(value['value']) if action == 'insert_text' else None
    url = page_url(value['url']) if action == 'navigate' else None
    if int(xdo('getwindowpid', window)) != pid:
        raise RuntimeError('browser_window_changed')
    STAGE = 'preview'
    preview_readonly(True)
    try:
        STAGE = 'focus'
        # Discard held VNC modifiers before generating our own input. The VNC
        # guard applies to all connected clients, not only the current UI tab.
        xdo('keyup', 'Control_L', 'Control_R', 'Shift_L', 'Shift_R', 'Alt_L', 'Alt_R', 'Super_L', 'Super_R')
        xdo('windowfocus', '--sync', window)
        if int(xdo('getwindowfocus')) != window:
            raise RuntimeError('browser_focus_unconfirmed')
        if action == 'insert_text':
            STAGE = 'text'
            xdo('type', '--clearmodifiers', '--delay', '1', '--file', '-', text=text)
        elif action == 'navigate':
            ui = ChromeUI(pid)
            STAGE = 'freeze'
            with frozen_frame(window):
                STAGE = 'address'
                if not ui.address():
                    xdo('key', '--clearmodifiers', 'F11')
                wait_for(ui.address)
                xdo('key', '--clearmodifiers', 'ctrl+l')
                wait_for(lambda: ui.address(focused=True))
                STAGE = 'navigate'
                xdo('type', '--clearmodifiers', '--delay', '1', '--file', '-', text=url)
                # The focused native address bar is the only place Enter is
                # generated. Paste never presses Enter or changes selection.
                if not ui.address(focused=True):
                    raise RuntimeError('address_focus_changed')
                xdo('key', '--clearmodifiers', 'Return')
                time.sleep(.3)
                STAGE = 'restore'
                xdo('key', '--clearmodifiers', 'F11')
                wait_for(lambda: not ui.address())
                # Let Chrome's full-screen notice finish before capture resumes.
                time.sleep(4)
        else:
            xdo('key', '--clearmodifiers', {'back':'alt+Left', 'forward':'alt+Right',
                                          'reload':'ctrl+r', 'close':'ctrl+shift+w'}[action])
    finally:
        text = url = None
        preview_readonly(False)


def main():
    value = {}
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('native_input_too_large')
        value = json.loads(raw)
        raw = None
        perform(value)
        print('{"ok":true}')
    except Exception:
        # Fixed code only: subprocess exceptions contain their input arguments.
        print(json.dumps({'ok':False,'code':'native_control_failed','stage':STAGE}))
        return 1
    finally:
        value.clear()
    return 0


if __name__ == '__main__':
    sys.exit(main())
