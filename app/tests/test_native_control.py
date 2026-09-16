import contextlib
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import native_control as native


class NativeControls(unittest.TestCase):
    def test_hidden_toolbar_cannot_authorize_address_input(self):
        class Node(list):
            def __init__(self, role, name='', showing=True, focused=False, editable=False, children=()):
                super().__init__(children)
                self.role, self.name = role, name
                self.flags = {key for key, enabled in [('showing',showing),('focused',focused),('editable',editable)] if enabled}
            def getRole(self): return self.role
            def __bool__(self): return True
            def getState(self): return SimpleNamespace(contains=lambda flag: flag in self.flags)
            def get_process_id(self): return 42
            def clear_cache(self): pass
        entry = Node('entry','Address and search bar',focused=True,editable=True)
        toolbar = Node('toolbar',showing=False,children=[entry])
        app = Node('app',children=[toolbar])
        spi = SimpleNamespace(Registry=SimpleNamespace(getDesktop=lambda index:[app]),
            ROLE_TOOL_BAR='toolbar',ROLE_DOCUMENT_WEB='web',ROLE_DOCUMENT_FRAME='doc',ROLE_EMBEDDED='embedded',
            STATE_SHOWING='showing',STATE_FOCUSED='focused',STATE_EDITABLE='editable')
        glib = SimpleNamespace(MainContext=SimpleNamespace(default=lambda:SimpleNamespace(pending=lambda:False)))
        with patch.dict(sys.modules, {'pyatspi':spi, 'gi.repository':SimpleNamespace(GLib=glib)}):
            ui = native.ChromeUI(42)
            self.assertIsNone(ui.address(focused=True))
            toolbar.flags.add('showing')
            self.assertIs(ui.address(focused=True), entry)

    def test_launch_has_no_debugging_or_automation_flag(self):
        engine = Mock()
        engine.browser_command.return_value = ['chrome', '--remote-debugging-pipe', '--kiosk', '--app=file:///fixture', '--user-data-dir=/private/profile', '--force-device-scale-factor=1']
        command = native.browser_command(engine, {}, Path('/fixture'))
        self.assertNotIn('--remote-debugging-pipe', command)
        self.assertNotIn('--kiosk', command)
        self.assertIn('--user-data-dir=/private/profile', command)
        self.assertIn('--start-fullscreen', command)
        self.assertFalse(any(value.startswith(('--headless','--enable-automation','--remote-debugging-port')) for value in command))

    def test_paste_preserves_selection_and_never_submits(self):
        with patch.object(native, 'xdo', side_effect=lambda *args, **kw: '42' if args[0]=='getwindowpid' else '7' if args[0]=='getwindowfocus' else '') as xdo, patch.object(native, 'preview_readonly') as guard:
            native.perform({'action':'insert_text','pid':42,'window':7,'value':'Unicode 🔐 & quotes'})
            typed = [c for c in xdo.call_args_list if c.args[0] == 'type']
            self.assertEqual(len(typed), 1)
            self.assertEqual(typed[0].kwargs['text'], 'Unicode 🔐 & quotes')
            self.assertEqual(typed[0].args[typed[0].args.index('--delay')+1], '6')
            self.assertEqual(typed[0].kwargs['timeout'], 30)
            self.assertNotIn('Unicode', repr(typed[0].args))
            self.assertFalse(any('Return' in c.args or 'ctrl+a' in c.args for c in xdo.call_args_list))
            self.assertEqual([c.args[0] for c in guard.call_args_list], [True, False])

    def test_maximum_unicode_paste_is_not_truncated_or_put_in_arguments(self):
        text = 'é🔐' * 2048
        with patch.object(native, 'xdo', side_effect=lambda *args, **kw: '42' if args[0]=='getwindowpid' else '7' if args[0]=='getwindowfocus' else '') as xdo, patch.object(native, 'preview_readonly'):
            native.perform({'action':'insert_text','pid':42,'window':7,'value':text})
            typed = [call for call in xdo.call_args_list if call.args[0] == 'type']
            self.assertEqual(len(typed), 1)
            self.assertEqual(typed[0].kwargs['text'], text)
            self.assertNotIn('é', repr(typed[0].args))

    def test_unconfirmed_address_bar_does_not_receive_url_or_enter(self):
        with patch.object(native, 'xdo', side_effect=lambda *args, **kw: '42' if args[0]=='getwindowpid' else '7' if args[0]=='getwindowfocus' else '') as xdo, patch.object(native, 'preview_readonly') as guard, patch.object(native, 'ChromeUI') as ui, patch.object(native, 'frozen_frame', return_value=contextlib.nullcontext()), patch.object(native, 'wait_for', side_effect=RuntimeError('unconfirmed')):
            ui.return_value.address.return_value = None
            with self.assertRaises(RuntimeError):
                native.perform({'action':'navigate','pid':42,'window':7,'url':'https://example.com/'})
            self.assertFalse(any(c.args[0] == 'type' or 'Return' in c.args for c in xdo.call_args_list))
            self.assertEqual([c.args[0] for c in guard.call_args_list], [True, False])

    def test_navigation_rejects_inline_history_completion_before_enter(self):
        with patch.object(native, 'xdo', side_effect=lambda *args, **kw: '42' if args[0]=='getwindowpid' else '7' if args[0]=='getwindowfocus' else '') as xdo, patch.object(native, 'preview_readonly'), patch.object(native, 'ChromeUI') as ui, patch.object(native, 'frozen_frame', return_value=contextlib.nullcontext()), patch.object(native, 'wait_for'), patch.object(native.time, 'sleep'):
            ui.return_value.address.return_value = object()
            native.perform({'action':'navigate','pid':42,'window':7,'url':'https://example.com/'})
            keys = [call.args[-1] for call in xdo.call_args_list if call.args[0] == 'key']
            self.assertEqual(keys, ['ctrl+l', 'Delete', 'Return', 'F11'])
            self.assertEqual([call.kwargs.get('text') for call in xdo.call_args_list if call.args[0] == 'type'], ['https://example.com/'])

    def test_wrong_window_never_enables_or_types_controls(self):
        with patch.object(native, 'xdo', return_value='99') as xdo, patch.object(native, 'preview_readonly') as guard:
            with self.assertRaises(RuntimeError):
                native.perform({'action':'insert_text','pid':42,'window':7,'value':'fixture'})
            guard.assert_not_called()
            self.assertEqual(xdo.call_count, 1)
