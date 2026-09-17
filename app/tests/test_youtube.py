"""YouTube launch boundaries, source identity, and shared TV selection."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from aiohttp.test_utils import TestClient, TestServer
from model import Store, discovery
from mqtt_bridge import MQTTBridge
from server import create_app
from youtube import launch, video_url
from test_multi_tv import RecordingSession


VIDEO = 'https://www.youtube.com/watch?v=AbCdEf12_-3'
OTHER = 'https://www.youtube.com/watch?v=XyZaBc45_-6'
PAGE = {'id': 'page', 'name': 'Dashboard', 'url': 'https://example.com/'}


class YouTubeValidation(unittest.TestCase):
    def test_supported_video_links_normalize_without_tracking_or_playlist_side_effects(self):
        for url in [VIDEO + '&si=tracking&list=private&feature=shared',
                    'http://youtu.be/AbCdEf12_-3?si=tracking',
                    'https://m.youtube.com/shorts/AbCdEf12_-3',
                    'https://www.youtube.com/live/AbCdEf12_-3/',
                    'https://www.youtube.com/embed/AbCdEf12_-3?autoplay=0',
                    'https://WWW.YOUTUBE.COM:443/watch?v=AbCdEf12_-3']:
            with self.subTest(url=url):
                self.assertEqual(video_url(url), VIDEO)
        for suffix in ['&t=90', '&t=1m30s', '&start=90', '#t=90s', '&t=90&start=90']:
            self.assertEqual(video_url(VIDEO + suffix), VIDEO + '&t=90s')
        self.assertEqual(launch('watch_later'), {'mode': 'watch_later', 'resume': True})
        self.assertEqual(launch('watch_later', resume=False)['resume'], False)

    def test_untrusted_hosts_ambiguous_ids_and_invalid_times_are_rejected(self):
        for value in [None, '', 'javascript:alert(1)', 'file:///private',
                      'https://youtube.com.evil/watch?v=AbCdEf12_-3',
                      'https://youtube.com@evil.test/watch?v=AbCdEf12_-3',
                      'https://user:password@youtube.com/watch?v=AbCdEf12_-3',
                      'https://youtube.com:8443/watch?v=AbCdEf12_-3',
                      'https://www.youtube.com/redirect?q=' + VIDEO,
                      'https://www.youtube.com/playlist?list=WL',
                      'https://www.youtube.com/watch?v=short', VIDEO + '&v=XyZaBc45_-6',
                      VIDEO + '&t=-1', VIDEO + '&t=1.5', VIDEO + '&t=9000000',
                      VIDEO + '&t=5&t=6', VIDEO + '&t=5&start=6', VIDEO + '&t=5#t=6',
                      VIDEO + '&t=', VIDEO + '\n', VIDEO + '&si=' + 'x' * 4096]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    video_url(value)
        for kwargs in [{'mode': 'other'}, {'mode': 'watch_later', 'url': VIDEO},
                       {'mode': 'video'}, {'mode': 'watch_later', 'resume': 1},
                       {'mode': 'watch_later', 'resume': 'true'}]:
            with self.assertRaises(ValueError):
                launch(**kwargs)


class YouTubeLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_play_resumes_paused_but_mqtt_join_preserves_it(self):
        session = RecordingSession('/unused', {})
        await session.youtube('video', url=VIDEO, receivers=[{'id': 'one'}])
        session.youtube_event({'launch_id': session.youtube_launch_id, 'mode': 'video', 'state': 'paused'})
        session.commands.clear()
        await session.youtube('video', url=VIDEO, receivers=[{'id': 'two'}], replace_receivers=False)
        self.assertEqual(session.commands, [('cast', {'receiver': {'id': 'two'}})])
        self.assertEqual(session.runtime['youtube']['state'], 'paused')
        session.commands.clear()
        await session.youtube('video', url=VIDEO)
        self.assertEqual([name for name, _ in session.commands], ['youtube_cancel', 'youtube'])
        self.assertEqual(session.runtime['youtube']['state'], 'loading')
        self.assertEqual(set(session.runtime['receivers']), {'one', 'two'})

    async def test_distinct_sources_navigate_and_same_source_joins_without_restart(self):
        session = RecordingSession('/unused', {})
        await session.youtube('video', url=VIDEO, receivers=[{'id': 'one'}])
        first_id = session.youtube_launch_id
        session.youtube_event({'launch_id': first_id, 'mode': 'video', 'state': 'playing'})
        session.commands.clear()
        await session.youtube('video', url=VIDEO + '&si=ignored', receivers=[{'id': 'two'}], replace_receivers=False)
        self.assertEqual(session.commands, [('cast', {'receiver': {'id': 'two'}})])
        self.assertEqual(set(session.runtime['receivers']), {'one', 'two'})
        session.commands.clear()
        await session.youtube('video', url=OTHER)
        self.assertEqual([name for name, _ in session.commands], ['youtube_cancel', 'youtube'])
        self.assertEqual(session.commands[-1][1]['url'], OTHER)
        self.assertGreater(session.youtube_launch_id, first_id)
        self.assertEqual(set(session.runtime['receivers']), {'one', 'two'})
        self.assertIsNone(session.runtime['page_id'])
        self.assertEqual(session.runtime['source_label'], 'YouTube')
        self.assertNotIn('youtube.com', json.dumps(session.state()))

    async def test_exact_selection_stops_removed_receiver_before_new_source(self):
        session = RecordingSession('/unused', {})
        await session.open(PAGE, {'id': 'one'})
        await session.open(PAGE, {'id': 'two'}, preserve_view=True)
        session.commands.clear()
        await session.youtube('watch_later', receivers=[{'id': 'two'}])
        self.assertEqual([name for name, _ in session.commands], ['stop', 'youtube'])
        self.assertEqual(session.commands[0][1], {'tv_id': 'one'})
        self.assertEqual(session.commands[1][1], {'mode': 'watch_later', 'resume': True, 'launch_id': 1})
        self.assertEqual(set(session.runtime['receivers']), {'two'})
        self.assertEqual(session.runtime['source_label'], 'Watch Later')
        before = list(session.commands)
        with self.assertRaises(ValueError):
            await session.youtube('video', url='https://evil.test/', receivers=[{'id': 'other'}])
        self.assertEqual(session.commands, before)

    async def test_normal_navigation_cancels_mode_and_late_events_are_ignored(self):
        session = RecordingSession('/unused', {})
        await session.youtube('watch_later')
        first_id = session.youtube_launch_id
        session.youtube_event({'launch_id': first_id, 'mode': 'watch_later', 'state': 'playing', 'error': 'private upstream text'})
        self.assertEqual(session.runtime['youtube'], {'mode': 'watch_later', 'state': 'playing'})
        await session.youtube('watch_later', resume=False)
        second_id = session.youtube_launch_id
        session.youtube_event({'launch_id': first_id, 'mode': 'watch_later', 'state': 'finished'})
        self.assertEqual(session.runtime['youtube']['state'], 'loading')
        session.youtube_event({'launch_id': second_id, 'mode': 'watch_later', 'state': 'error', 'error': 'private error with URL'})
        self.assertNotIn('private', json.dumps(session.state()))
        session.commands.clear()
        await session.browser_action('back')
        self.assertEqual([name for name, _ in session.commands], ['youtube_cancel', 'back'])
        session.youtube_event({'launch_id': second_id, 'mode': 'watch_later', 'state': 'playing'})
        self.assertIsNone(session.runtime['youtube'])
        await session.youtube('video', url=VIDEO)
        session.commands.clear()
        await session.open(PAGE)
        self.assertEqual([name for name, _ in session.commands], ['youtube_cancel', 'navigate'])
        self.assertEqual(session.runtime['source_label'], 'Dashboard')
        self.assertEqual(session.runtime['page_id'], PAGE['id'])
        await session.close()
        self.assertIsNone(session.runtime['youtube'])


class YouTubeAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bridges = []
        bridges = self.bridges

        class Bridge:
            connected = True

            def __init__(self, store, credentials, directory, on_command, state, **kwargs):
                self.command = on_command
                bridges.append(self)

            def start(self): pass
            def publish_state(self): pass
            def refresh(self): pass
            async def stop(self): pass

        self.bridge_patch = patch('server.MQTTBridge', Bridge)
        self.bridge_patch.start()
        self.app = create_app(self.directory.name, {'mqtt': {'fixture': True}}, development=True, session_factory=RecordingSession)
        self.store, self.session = self.app['store'], self.app['session']
        self.tvs = [self.store.put('tvs', {'name': name, 'host': '127.0.0.1'}) for name in ('One', 'Two')]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.client.session.headers['X-Doubletake-CSRF'] = self.app['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        self.bridge_patch.stop()
        self.directory.cleanup()

    async def test_invalid_source_or_complete_selection_has_no_side_effects(self):
        await self.session.open(PAGE, self.tvs[0])
        self.session.commands.clear()
        for fields in [
            {'mode': 'video', 'url': VIDEO, 'tv_ids': [self.tvs[0]['id'], '0' * 16]},
            {'mode': 'video', 'url': VIDEO, 'tv_ids': [self.tvs[0]['id']] * 2},
            {'mode': 'video', 'url': VIDEO, 'tv_ids': []},
            {'mode': 'video', 'url': VIDEO, 'tv_ids': None},
            {'mode': 'video', 'url': VIDEO, 'tv_ids': [None]},
            {'mode': 'video', 'url': 'https://evil.test/', 'tv_ids': [self.tvs[1]['id']]},
            {'mode': 'watch_later', 'url': VIDEO}, {'mode': 'watch_later', 'resume': 'true'},
            {'mode': 'watch_later', 'tv_id': self.tvs[1]['id']},
        ]:
            with self.subTest(fields=fields):
                self.assertEqual((await self.client.post('/api/action/youtube', json=fields)).status, 400)
                self.assertEqual(self.session.commands, [])
                self.assertEqual(set(self.session.runtime['receivers']), {self.tvs[0]['id']})

    async def test_http_omission_preserves_and_mqtt_is_additive(self):
        response = await self.client.post('/api/action/youtube', json={'mode': 'video', 'url': VIDEO})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.session.runtime['receivers'], {})
        await self.bridges[0].command(self.tvs[0]['id'], {'action': 'youtube', 'url': VIDEO})
        self.assertEqual(set(self.session.runtime['receivers']), {self.tvs[0]['id']})
        await self.bridges[0].command(self.tvs[1]['id'], {'action': 'watch_later'})
        self.assertEqual(set(self.session.runtime['receivers']), {tv['id'] for tv in self.tvs})
        self.assertEqual(self.session.runtime['source_label'], 'Watch Later')
        response = await self.client.post('/api/action/youtube', json={'mode': 'video', 'url': OTHER, 'tv_ids': [self.tvs[1]['id']]})
        self.assertEqual(response.status, 200)
        self.assertEqual(set(self.session.runtime['receivers']), {self.tvs[1]['id']})
        self.assertNotIn('youtube.com', await response.text())
        self.assertNotIn('youtube.com', self.store.path.read_text())


class YouTubeMQTT(unittest.IsolatedAsyncioTestCase):
    async def test_launch_entities_do_not_echo_url_and_retained_commands_stay_ignored(self):
        with tempfile.TemporaryDirectory() as directory, patch('mqtt_bridge.mqtt.Client') as factory:
            store = Store(directory)
            tv = store.put('tvs', {'name': 'TV', 'host': '127.0.0.1'})
            configs = discovery(store)
            text = next(value for topic, value in configs.items() if topic.startswith('homeassistant/text/'))
            self.assertEqual((text['name'], text['max'], text['retain'], text['optimistic']), ('Play YouTube URL', 255, False, False))
            self.assertEqual(text['value_template'], "{{ '' }}")
            self.assertIn('| tojson', text['command_template'])
            button = next(value for value in configs.values() if value['name'] == 'Play Watch Later')
            self.assertEqual(json.loads(button['payload_press']), {'action': 'watch_later', 'resume': True})
            commands = []

            async def command(tv_id, payload):
                commands.append((tv_id, payload))

            state = {'source_label': 'Watch Later', 'page_id': None, 'receivers': {tv['id']: {'state': 'sending'}}}
            bridge = MQTTBridge(store, {'username': 'fixture', 'password': 'fixture'}, directory, command, lambda: state)
            bridge.connected = True
            bridge.publish_state()
            published = factory.return_value.publish.call_args.args
            self.assertEqual(json.loads(published[1]), {'state': 'sending', 'page': 'Watch Later'})
            for action in [{'action': 'youtube', 'url': VIDEO}, {'action': 'watch_later', 'resume': True}]:
                message = SimpleNamespace(topic=f'{bridge.base}/{tv["id"]}/command', payload=json.dumps(action).encode(), retain=True)
                bridge._message(None, None, message)
                await asyncio.sleep(0)
                self.assertEqual(commands, [])
            message.retain = False
            bridge._message(None, None, message)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(commands, [(tv['id'], {'action': 'watch_later', 'resume': True})])


if __name__ == '__main__':
    unittest.main()
