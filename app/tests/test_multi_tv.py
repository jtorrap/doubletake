"""Shared-browser fanout, per-TV isolation, and API selection boundaries."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from aiohttp.test_utils import TestClient, TestServer
from model import Store
from mqtt_bridge import MQTTBridge
from server import create_app
from session import Session


PAGE = {'id': 'page1', 'url': 'https://example.com/one'}
OTHER_PAGE = {'id': 'page2', 'url': 'https://example.com/two'}


class RecordingSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands, self.fail_tvs = [], set()
        self.starts = 0

    async def ensure(self, page):
        if self.runtime['browser'] == 'ready':
            return False
        self.starts += 1
        self.process = SimpleNamespace(returncode=None)
        self.update(browser='ready')
        return True

    async def request(self, action, **fields):
        self.commands.append((action, fields))
        if action == 'cast' and fields['receiver']['id'] in self.fail_tvs:
            raise ValueError('Synthetic connection failure')

    async def close_worker(self):
        self.process = None


class ReceiverLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_audio_failure_is_visible_without_misstating_video_or_other_tv(self):
        session = RecordingSession('/unused', {})
        await session.cast(PAGE, [{'id': 'one'}, {'id': 'two'}])
        session.audio_event({'tv_id': 'one', 'state': 'active'})
        session.receiver_event({'tv_id': 'one', 'state': 'sending'})
        session.audio_event({'tv_id': 'two', 'state': 'unavailable'})
        session.receiver_event({'tv_id': 'two', 'state': 'sending'})
        self.assertEqual(session.runtime['receivers']['one']['audio'], 'active')
        self.assertEqual(session.runtime['receivers']['two']['audio'], 'unavailable')
        session.audio_event({'tv_id': 'one', 'state': 'error'})
        self.assertEqual(session.runtime['receivers']['one']['state'], 'sending')
        self.assertEqual(session.runtime['receivers']['two']['audio'], 'unavailable')
        self.assertEqual(session.runtime['airplay'], 'sending')
        self.assertIsNone(session.runtime['error'])
        await session.stop('one')
        session.audio_event({'tv_id': 'one', 'state': 'active'})
        self.assertNotIn('one', session.runtime['receivers'])
        with patch.dict('os.environ', {'DOUBLETAKE_AUDIO': 'false'}):
            muted = RecordingSession('/unused', {})
        await muted.open(PAGE, {'id': 'muted'})
        self.assertFalse(muted.state()['audio_enabled'])
        self.assertEqual(muted.runtime['receivers']['muted']['audio'], 'disabled')

    async def test_failure_and_stop_are_isolated_and_late_events_do_not_restore_tv(self):
        session = RecordingSession('/unused', {})
        await session.open(PAGE, {'id': 'one'}, preserve_view=True)
        await session.open(PAGE, {'id': 'two'}, preserve_view=True)
        for tv_id in ('one', 'two'):
            session.receiver_event({'tv_id': tv_id, 'state': 'sending'})
        session.receiver_event({'tv_id': 'one', 'state': 'error', 'error': 'private upstream details'})
        self.assertEqual(session.runtime['receivers']['two']['state'], 'sending')
        self.assertEqual(session.runtime['airplay'], 'sending')
        self.assertIsNone(session.runtime['error'])
        self.assertNotIn('private', json.dumps(session.state()))
        state = session.state()
        state['receivers']['two']['state'] = 'error'
        self.assertEqual(session.runtime['receivers']['two']['state'], 'sending')
        await session.stop('one')
        self.assertEqual(session.commands[-1], ('stop', {'tv_id': 'one'}))
        session.receiver_event({'tv_id': 'one', 'state': 'sending'})
        session.receiver_event({'state': 'pairing'})
        self.assertEqual(set(session.runtime['receivers']), {'two'})
        self.assertEqual(session.runtime['browser'], 'ready')
        await session.stop()
        self.assertEqual(session.commands[-1], ('stop', {}))
        self.assertEqual(session.runtime['receivers'], {})
        self.assertEqual(session.runtime['airplay'], 'idle')

    async def test_selected_set_reconciles_without_reopening_view_or_existing_sender(self):
        session = RecordingSession('/unused', {})
        await session.cast(PAGE, [{'id': 'one'}, {'id': 'two'}])
        session.commands.clear()
        await session.cast(PAGE, [{'id': 'two'}, {'id': 'three'}])
        self.assertEqual(session.commands, [('stop', {'tv_id': 'one'}), ('cast', {'receiver': {'id': 'three'}})])
        session.commands.clear()
        await session.cast(OTHER_PAGE, [{'id': 'two'}, {'id': 'three'}])
        self.assertEqual(session.commands, [('navigate', {'url': OTHER_PAGE['url']})])
        self.assertEqual(session.runtime['page_id'], OTHER_PAGE['id'])
        self.assertEqual(session.starts, 1)

    async def test_failed_add_does_not_prevent_other_selected_receivers(self):
        session = RecordingSession('/unused', {})
        session.fail_tvs.add('one')
        with self.assertRaises(ValueError):
            await session.cast(PAGE, [{'id': 'one'}, {'id': 'two'}])
        self.assertEqual(session.runtime['receivers']['one']['state'], 'error')
        self.assertEqual(session.runtime['receivers']['two']['state'], 'starting')
        self.assertEqual([fields['receiver']['id'] for action, fields in session.commands if action == 'cast'], ['one', 'two'])
        session.fail_tvs.clear()
        session.commands.clear()
        await session.cast(PAGE, [{'id': 'one'}, {'id': 'two'}])
        self.assertEqual(session.commands, [('cast', {'receiver': {'id': 'one'}})])

    async def test_pairing_requires_an_unambiguous_target(self):
        session = RecordingSession('/unused', {})
        await session.cast(PAGE, [{'id': 'one'}, {'id': 'two'}])
        for tv_id in ('one', 'two'):
            session.receiver_event({'tv_id': tv_id, 'state': 'pairing'})
        session.commands.clear()
        with self.assertRaises(ValueError):
            await session.pin('1234')
        with self.assertRaises(ValueError):
            await session.pin('1234', 'unknown')
        self.assertEqual(session.commands, [])
        await session.pin('1234', 'two')
        self.assertEqual(session.commands, [('pin', {'tv_id': 'two', 'value': '1234'})])
        self.assertEqual(session.runtime['receivers']['one']['state'], 'pairing')
        await session.pin('5678')
        self.assertEqual(session.commands[-1], ('pin', {'tv_id': 'one', 'value': '5678'}))
        await session.close()
        self.assertEqual(session.runtime['receivers'], {})
        self.assertEqual(session.runtime['browser'], 'closed')


class APISelections(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, {}, development=True, session_factory=RecordingSession)
        self.store, self.session = self.app['store'], self.app['session']
        self.page = self.store.put('pages', {'name': 'Page', 'url': PAGE['url']})
        self.tvs = [self.store.put('tvs', {'name': name, 'host': f'127.0.0.{index}'}) for index, name in enumerate(('One', 'Two', 'Three'), 1)]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.client.session.headers['X-Doubletake-CSRF'] = self.app['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def cast(self, **fields):
        return await self.client.post('/api/action/cast', json={'page_id': self.page['id'], **fields})

    async def test_invalid_complete_selection_has_no_partial_side_effects(self):
        first, second = [tv['id'] for tv in self.tvs[:2]]
        self.assertEqual((await self.cast(tv_ids=[first, second])).status, 200)
        before = self.session.state()
        self.session.commands.clear()
        for fields in [
            {'tv_ids': []}, {'tv_ids': None}, {'tv_ids': first},
            {'tv_ids': [first, first]}, {'tv_ids': [first, None]},
            {'tv_ids': [first, {}]}, {'tv_ids': [first, '0' * 16]},
            {'tv_ids': [first], 'tv_id': second}, {'tv_id': '0' * 16},
            {'tv_ids': [first], 'page_id': '0' * 16},
        ]:
            with self.subTest(fields=fields):
                self.assertEqual((await self.cast(**fields)).status, 400)
                self.assertEqual(self.session.commands, [])
                self.assertEqual(self.session.state(), before)
        self.assertEqual((await self.cast(tv_id=second)).status, 200)
        self.assertEqual(set(self.session.runtime['receivers']), {second})

    async def test_targeted_stop_edit_rename_delete_keep_other_receiver(self):
        first, second = self.tvs[:2]
        await self.cast(tv_ids=[first['id'], second['id']])
        self.session.commands.clear()
        response = await self.client.put(f"/api/settings/tvs/{first['id']}", json={**first, 'name': 'Renamed'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.session.commands, [])
        response = await self.client.put(f"/api/settings/tvs/{first['id']}", json={**first, 'host': '127.0.0.11'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.session.commands, [('stop', {'tv_id': first['id']})])
        self.assertEqual(set(self.session.runtime['receivers']), {second['id']})
        self.assertEqual((await self.cast(tv_ids=[first['id'], second['id']])).status, 200)
        response = await self.client.post('/api/action/stop', json={'tv_id': second['id']})
        self.assertEqual(response.status, 200)
        self.assertEqual(set(self.session.runtime['receivers']), {first['id']})
        self.assertEqual((await self.client.delete(f"/api/settings/tvs/{first['id']}")).status, 200)
        self.assertEqual(self.session.runtime['receivers'], {})
        self.assertEqual(self.session.runtime['browser'], 'ready')

    async def test_api_pairing_target_validation_and_page_delete_closes_all(self):
        first, second = [tv['id'] for tv in self.tvs[:2]]
        await self.cast(tv_ids=[first, second])
        for tv_id in (first, second):
            self.session.receiver_event({'tv_id': tv_id, 'state': 'pairing'})
        self.session.commands.clear()
        for fields in [{'value': '1234'}, {'value': '1234', 'tv_id': '0' * 16}]:
            self.assertEqual((await self.client.post('/api/action/pin', json=fields)).status, 400)
        self.assertEqual(self.session.commands, [])
        self.assertEqual((await self.client.post('/api/action/pin', json={'tv_id': second, 'value': '1234'})).status, 200)
        self.assertEqual(self.session.commands[-1], ('pin', {'tv_id': second, 'value': '1234'}))
        self.assertEqual((await self.client.delete(f"/api/settings/pages/{self.page['id']}")).status, 200)
        self.assertEqual(self.session.runtime['browser'], 'closed')
        self.assertEqual(self.session.runtime['receivers'], {})


class MQTTFanout(unittest.IsolatedAsyncioTestCase):
    async def test_each_device_publishes_own_connection_and_shared_page(self):
        with tempfile.TemporaryDirectory() as directory, patch('mqtt_bridge.mqtt.Client') as factory:
            store = Store(directory)
            tvs = [store.put('tvs', {'name': name, 'host': '127.0.0.1'}) for name in ('One', 'Two', 'Idle')]
            page = store.put('pages', {'name': 'Page', 'url': PAGE['url']})
            runtime = {'page_id': page['id'], 'receivers': {tvs[0]['id']: {'state': 'sending', 'error': None}, tvs[1]['id']: {'state': 'error', 'error': 'Connection ended'}}}
            bridge = MQTTBridge(store, {'username': 'fixture', 'password': 'fixture'}, directory, None, lambda: runtime)
            bridge.connected = True
            bridge.publish_state()
            published = {call.args[0]: json.loads(call.args[1]) for call in factory.return_value.publish.call_args_list}
            self.assertEqual(published[f"{bridge.base}/{tvs[0]['id']}/state"], {'state': 'sending', 'page': 'Page'})
            self.assertEqual(published[f"{bridge.base}/{tvs[1]['id']}/state"], {'state': 'error', 'page': 'Page'})
            self.assertEqual(published[f"{bridge.base}/{tvs[2]['id']}/state"], {'state': 'idle', 'page': 'None'})

    async def test_mqtt_joins_current_view_and_page_change_affects_all(self):
        bridges = []

        class TestBridge:
            connected = False

            def __init__(self, store, credentials, directory, on_command, state):
                self.command = on_command
                bridges.append(self)

            def start(self): pass
            def publish_state(self): pass
            async def stop(self): pass

        with tempfile.TemporaryDirectory() as directory, patch('server.MQTTBridge', TestBridge):
            app = create_app(directory, {'mqtt': {'fixture': True}}, development=True, session_factory=RecordingSession)
            store, session = app['store'], app['session']
            first, second = [store.put('tvs', {'name': name, 'host': '127.0.0.1'}) for name in ('One', 'Two')]
            page, other = [store.put('pages', {'name': name, 'url': url}) for name, url in [('Page', PAGE['url']), ('Other', OTHER_PAGE['url'])]]
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                await session.open(page, first)
                session.commands.clear()
                await bridges[0].command(second['id'], {'action': 'cast', 'page_id': page['id']})
                self.assertEqual(session.commands, [('cast', {'receiver': second})])
                self.assertEqual(set(session.runtime['receivers']), {first['id'], second['id']})
                session.commands.clear()
                await bridges[0].command(second['id'], {'action': 'cast', 'page_id': other['id']})
                self.assertEqual(session.commands, [('navigate', {'url': other['url']})])
                self.assertEqual(session.runtime['page_id'], other['id'])
                await bridges[0].command(first['id'], {'action': 'stop'})
                self.assertEqual(set(session.runtime['receivers']), {second['id']})
            finally:
                await client.close()


if __name__ == '__main__':
    unittest.main()
