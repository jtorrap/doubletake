import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from aiohttp.test_utils import TestClient, TestServer
from hdhomerun import ChannelCatalog, normalize_device, local_ipv4, valid_reply, discovery_packet
from model import Store, discovery
from mqtt_bridge import MQTTBridge
from server import create_app
from session import Session

DEVICE = '10ABCDEF'
HOST = '192.168.1.123'
TV1 = {'id':'1'*16, 'name':'One', 'host':'192.168.1.201', 'port':7000}
TV2 = {'id':'2'*16, 'name':'Two', 'host':'192.168.1.202', 'port':7000}
SOURCE = {'kind':'hdhomerun','device_id':DEVICE,'channel':'2.1','label':'2.1 Test','url':f'http://{HOST}:5004/auto/v2.1'}


def device():
    return normalize_device(HOST, {'DeviceID':DEVICE,'DeviceAuth':'PRIVATE-FIXTURE','FriendlyName':'HDHomeRun'}, [
        {'GuideNumber':number,'GuideName':name,'VideoCodec':video,'AudioCodec':audio,'DRM':drm,
         'URL':f'http://{HOST}:5004/auto/v{number}'} for number,name,video,audio,drm in [
             ('2.1','Test','MPEG2','AC3',0), ('4.1','Other','H264','AC3',0),
             ('102.1','ATSC 3','HEVC','AC4',0), ('9.1','Protected','MPEG2','AC3',1)]])


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_is_additive_and_never_keeps_device_auth_or_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            tv = store.put('tvs', {'name':'TV','host':HOST})
            before = store.path.read_bytes()
            original = discovery(store)
            catalog = ChannelCatalog(directory)
            with patch('hdhomerun.discover_hosts', return_value={HOST}), patch('hdhomerun.fetch_device', AsyncMock(return_value=device())):
                await catalog.refresh()
            self.assertEqual(store.path.read_bytes(), before)
            saved = catalog.path.read_text()
            self.assertNotIn('PRIVATE-FIXTURE', saved)
            self.assertNotIn('http:', saved)
            self.assertEqual(len(catalog.options()), 2)
            self.assertEqual(catalog.path.stat().st_mode & 0o777, 0o600)
            choices = discovery(store, channels=catalog)
            self.assertTrue(original.keys() <= choices.keys())
            select = next(value for key,value in choices.items() if '/select/' in key)
            self.assertFalse(select['retain'])
            self.assertIn('select_channel', select['command_template'])
            option = select['options'][0]
            catalog.select(tv['id'], option)
            self.assertEqual(ChannelCatalog(directory).selected(tv['id']), (DEVICE,'2.1'))
            catalog.favorite(DEVICE,'2.1',True)
            self.assertEqual(ChannelCatalog(directory).data['favorites'],[DEVICE+':2.1'])
            self.assertEqual((await catalog.source(DEVICE,'2.1'))['url'],SOURCE['url'])
            for number in ('9.1','102.1','999.9'):
                with self.assertRaises(ValueError):
                    await catalog.source(DEVICE,number)

    async def test_stale_device_identity_and_untrusted_streams_are_rejected(self):
        for host in ('8.8.8.8','127.0.0.1','169.254.169.254','0.0.0.0','224.0.0.1','::1'):
            with self.assertRaises(ValueError):
                local_ipv4(host)
        self.assertFalse(valid_reply(discovery_packet()))
        for url in ('file:///etc/passwd', 'http://127.0.0.1:5004/auto/v2.1',
                    f'http://{HOST}:5004/tuner0/v2.1', f'http://{HOST}:5004/auto/v2.1?auth=secret'):
            value = normalize_device(HOST, {'DeviceID':DEVICE}, [{'GuideNumber':'2.1','URL':url}])
            self.assertEqual(value['channels'],[])
        with tempfile.TemporaryDirectory() as directory:
            catalog = ChannelCatalog(directory)
            catalog.data['devices'] = [device()]
            replacement = {**device(),'id':'10AAAAAA'}
            with patch('hdhomerun.fetch_device', AsyncMock(return_value=replacement)):
                with self.assertRaises(ValueError):
                    await catalog.source(DEVICE,'2.1')
            self.assertEqual(catalog.data['devices'][0]['id'], DEVICE)


class ChannelSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.operations = []
        self.starts = 0

    async def adopt_channel(self, source):
        identity = (source['device_id'], source['channel'])
        if identity != self.source_identity:
            self.operations.append(('source',source['channel']))
            self.starts += 1
            self.set_receivers({})
            self.process = SimpleNamespace(returncode=None)
            self.source_identity = identity
            self.update(source_kind='hdhomerun',channel={'state':'starting'})

    async def request(self, action, **fields):
        self.operations.append((action, fields))

    async def close_worker(self):
        self.operations.append(('close',{}))
        self.process = None


class ChannelLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_same_source_join_exact_selection_switch_stop_and_no_reclaim(self):
        session = ChannelSession('/unused', {})
        await session.channel(SOURCE,[TV1])
        await session.channel(SOURCE,[TV2],replace_receivers=False)
        self.assertEqual(session.starts,1)
        self.assertEqual(set(session.runtime['receivers']),{TV1['id'],TV2['id']})
        session.operations.clear()
        await session.channel({**SOURCE,'channel':'4.1'},[TV2])
        self.assertEqual(session.operations[0],('stop',{'tv_id':TV1['id']}))
        self.assertEqual(session.operations[1],('source','4.1'))
        self.assertEqual(set(session.runtime['receivers']),{TV2['id']})
        session.receiver_event({'tv_id':TV2['id'],'state':'error'})
        await session.channel(SOURCE,[TV1],replace_receivers=False)
        self.assertEqual(set(session.runtime['receivers']),{TV1['id']})
        await session.stop(TV1['id'])
        self.assertIsNone(session.process)
        self.assertEqual(session.runtime['channel']['state'],'stopped')
        session.receiver_event({'tv_id':TV1['id'],'state':'sending'})
        self.assertEqual(session.runtime['receivers'],{})

    async def test_failed_warmup_keeps_current_source_and_busy_releases_only_own_worker(self):
        session = Session('/unused',{})
        session.update(source_kind='hdhomerun', source_label='Original')
        session.process = SimpleNamespace(returncode=None)
        session.prepare_channel = AsyncMock(side_effect=ValueError('channel_unavailable'))
        session.close_worker = AsyncMock()
        with self.assertRaises(ValueError):
            await session.adopt_channel(SOURCE)
        session.close_worker.assert_not_awaited()
        self.assertEqual(session.runtime['source_label'],'Original')
        session.prepare_channel = AsyncMock(side_effect=[ValueError('busy'), ValueError('channel_unavailable')])
        with self.assertRaises(ValueError):
            await session.adopt_channel(SOURCE)
        session.close_worker.assert_awaited_once()
        self.assertEqual(session.prepare_channel.await_count,2)


class ChannelAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name,{},development=True,session_factory=ChannelSession)
        self.tv = self.app['store'].put('tvs',{'name':'One','host':TV1['host']})
        self.app['channels'].data['devices'] = [device()]
        self.app['channels'].checked[DEVICE] = time.monotonic()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_csrf_and_complete_validation_before_playback(self):
        valid = {'device_id':DEVICE, 'channel':'2.1', 'tv_ids':[self.tv['id']]}
        self.assertEqual((await self.client.post('/api/action/channel',json=valid)).status,403)
        self.client.session.headers['X-Doubletake-CSRF'] = self.app['csrf']
        for invalid in [{**valid,'channel':'9.1'}, {**valid,'tv_ids':[self.tv['id'],'0'*16]},
                        {**valid,'url':'http://example.com'}, {**valid,'tv_ids':[]},
                        {**valid,'tv_ids':[self.tv['id']]*2}]:
            self.assertEqual((await self.client.post('/api/action/channel',json=invalid)).status,400)
        self.assertEqual(self.app['session'].operations,[])
        self.assertEqual((await self.client.post('/api/action/channel',json=valid)).status,200)
        state = await (await self.client.get('/api/state')).json()
        self.assertNotIn('/auto/v',json.dumps(state))
        self.assertNotIn('PRIVATE-FIXTURE',json.dumps(state))

    async def test_retained_channel_and_selection_commands_are_ignored(self):
        accepted = []
        async def command(tv, value):
            accepted.append(value)
        bridge = MQTTBridge(self.app['store'],{'username':'fixture','password':'fixture'},
                            self.directory.name,command,self.app['session'].state,channels=self.app['channels'])
        for action in ('channel','select_channel'):
            message = SimpleNamespace(topic=f'{bridge.base}/{self.tv["id"]}/command', payload=json.dumps({'action':action}).encode(), retain=True)
            bridge._message(None,None,message)
        await asyncio.sleep(.01)
        self.assertEqual(accepted,[])


if __name__ == '__main__':
    unittest.main()
