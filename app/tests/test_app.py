import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from aiohttp.test_utils import TestClient, TestServer
from model import Store, discovery
from mqtt_bridge import MQTTBridge
from server import create_app
from session import Session


class Storage(unittest.TestCase):
    def test_names_are_editable_without_changing_device_or_button_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            tv = store.put('tvs', {'name': 'TV', 'host': '127.0.0.1'})
            page = store.put('pages', {'name': 'Home', 'url': 'https://example.com/'})
            first = discovery(store)
            store.put('tvs', {**tv, 'name': 'Basement'}, tv['id'])
            store.put('pages', {**page, 'name': 'Dashboard'}, page['id'])
            self.assertEqual(first.keys(), discovery(Store(directory)).keys())
            self.assertTrue(all('url' not in json.dumps(v) or 'support_url' in json.dumps(v) for v in first.values()))
            self.assertNotIn('example.com', json.dumps(first))
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_bad_input_and_corrupt_storage_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            for url in ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:password@host/', 'https://host:bad/']:
                with self.assertRaises(ValueError):
                    store.put('pages', {'name': 'Page', 'url': url})
            store.path.write_text('{broken')
            with self.assertRaises(RuntimeError):
                Store(directory)
            self.assertEqual(store.path.read_text(), '{broken')


class SimulatedSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = []
        self.starts = 0

    async def ensure(self, page):
        if self.runtime['browser'] != 'ready':
            self.starts += 1
            self.process = SimpleNamespace(returncode=None)
            self.update(browser='ready')
            return True
        return False

    async def request(self, action, **fields):
        self.commands.append((action, fields))

    async def close_worker(self):
        self.process = None


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_browser_switches_senders_and_inactive_stop_is_ignored(self):
        session = SimulatedSession('/unused', {})
        page = {'id': 'page', 'url': 'https://example.com/'}
        await session.open(page, {'id': 'tv1'})
        await session.open(page, {'id': 'tv2'})
        self.assertEqual(session.starts, 1)
        self.assertEqual([v[1]['receiver']['id'] for v in session.commands if v[0] == 'cast'], ['tv1', 'tv2'])
        count = len(session.commands)
        await session.stop('tv1')
        self.assertEqual(len(session.commands), count)
        await session.stop('tv2')
        self.assertEqual(session.runtime['browser'], 'ready')
        self.assertIsNone(session.runtime['tv_id'])

    async def test_ingress_and_csrf_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            production = TestClient(TestServer(create_app(directory, {}, session_factory=SimulatedSession)))
            await production.start_server()
            try:
                self.assertEqual((await production.get('/api/state')).status, 403)
                self.assertEqual((await production.get('/healthz')).status, 200)
            finally:
                await production.close()
            client = TestClient(TestServer(create_app(directory, {}, development=True, session_factory=SimulatedSession)))
            await client.start_server()
            try:
                state = await (await client.get('/api/state')).json()
                body = {'name': 'Test', 'url': 'https://example.com/'}
                self.assertEqual((await client.post('/api/settings/pages', json=body)).status, 403)
                client.session.headers['X-Doubletake-CSRF'] = state['csrf']
                self.assertEqual((await client.post('/api/settings/tvs', json={'name': 'TV', 'host': '127.0.0.1'})).status, 200)
                response = await client.post('/api/settings/pages', json=body)
                self.assertEqual(response.status, 200)
                page = await response.json()
                self.assertEqual((await client.post('/api/action/open', json={'page_id': page['id']})).status, 200)
                pasted = 'private fixture 🔑'
                self.assertEqual((await client.post('/api/action/insert_text', json={'value':pasted})).status, 200)
                self.assertNotIn(pasted, await (await client.get('/api/state')).text())
                for value in ['', None, 'x'*4097, 'line\nEnter']:
                    self.assertEqual((await client.post('/api/action/insert_text', json={'value':value})).status, 400)
                with self.assertRaises(Exception):
                    await client.ws_connect('/ws/preview', protocols=['binary'])
                self.assertEqual((await client.post('/api/action/pin', json={'value': '1234'})).status, 400)
            finally:
                await client.close()

    async def test_ui_cast_preserves_interaction_but_mqtt_reopens_saved_url(self):
        session = SimulatedSession('/unused', {})
        page = {'id': 'page', 'url': 'https://example.com/'}
        await session.open(page)
        await session.open(page, {'id': 'tv'}, preserve_view=True)
        self.assertNotIn('navigate', [action for action, _ in session.commands])
        await session.open(page, {'id': 'tv'})
        self.assertEqual(session.commands[-1], ('navigate', {'url': page['url']}))

    async def test_mqtt_ignores_retained_commands_and_cleans_only_owned_topics(self):
        with tempfile.TemporaryDirectory() as directory, patch('mqtt_bridge.mqtt.Client') as factory:
            store = Store(directory)
            tv = store.put('tvs', {'name': 'TV', 'host': '127.0.0.1'})
            page = store.put('pages', {'name': 'Page', 'url': 'https://example.com/'})
            commands = []
            async def command(tv_id, payload):
                commands.append((tv_id, payload))
            bridge = MQTTBridge(store, {'host': 'localhost', 'username': 'fixture', 'password': 'fixture'}, directory, command, lambda: {})
            bridge.connected = True
            bridge.refresh()
            owned = set(discovery(store))
            bridge.previous.add('homeassistant/button/unrelated/config')
            store.delete('pages', page['id'])
            factory.return_value.publish.reset_mock()
            bridge.refresh()
            erased = {call.args[0] for call in factory.return_value.publish.call_args_list if call.args[1] == b''}
            self.assertEqual(erased, owned - discovery(store).keys())
            message = SimpleNamespace(topic=f'{bridge.base}/{tv["id"]}/command', payload=b'{"action":"stop"}', retain=True)
            bridge._message(None, None, message)
            await asyncio.sleep(0.01)
            self.assertEqual(commands, [])
            message.retain = False
            bridge._message(None, None, message)
            await asyncio.sleep(0.01)
            self.assertEqual(commands, [(tv['id'], {'action': 'stop'})])


if __name__ == '__main__':
    unittest.main()
