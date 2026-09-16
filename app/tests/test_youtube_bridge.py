"""Real private Unix sockets with a synthetic native-extension peer."""
import asyncio
import contextlib
import json
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import youtube_bridge as bridge_module
from youtube_bridge import YouTubeBridge
from worker import Worker


VIDEO = 'https://www.youtube.com/watch?v=AbCdEf12_-3'


class PrivateYouTubeBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Short paths also fit macOS's smaller sockaddr_un pathname limit.
        self.directory = tempfile.TemporaryDirectory(prefix='yt-bridge-', dir='/tmp')
        self.events, self.clients = [], []
        self.bridge = YouTubeBridge(self.events.append)
        self.path = await self.bridge.start(self.directory.name)
        self.loop = asyncio.get_running_loop()
        self.loop_errors = []
        self.previous_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        await self.bridge.close()
        for _, writer in self.clients:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        self.loop.set_exception_handler(self.previous_handler)
        self.directory.cleanup()
        self.assertEqual(self.loop_errors, [], 'Private socket task raised an unhandled error')

    async def connect(self, *, hello=True):
        reader, writer = await asyncio.open_unix_connection(self.path)
        self.clients.append((reader, writer))
        if hello:
            await self.send(writer, {'type': 'hello'})
            await asyncio.wait_for(self.bridge.connected.wait(), 1)
        return reader, writer

    async def send(self, writer, value):
        writer.write(json.dumps(value).encode() + b'\n')
        await writer.drain()

    async def message(self, reader):
        return json.loads(await asyncio.wait_for(reader.readline(), 1))

    async def wait_until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 1)

    async def reply(self, writer, command, *, ok=True):
        await self.send(writer, {'type': 'reply', 'id': command['id'], 'ok': ok})

    async def launch(self, reader, writer, *, launch_id=7, mode='video'):
        task = asyncio.create_task(self.bridge.play(mode, launch_id=launch_id, **({'url': VIDEO} if mode == 'video' else {})))
        command = await self.message(reader)
        await self.reply(writer, command)
        result = await task
        return command, result

    async def test_private_socket_permissions_handshake_and_connected_readback(self):
        self.assertEqual(stat.S_IMODE(Path(self.path).stat().st_mode), 0o600)
        self.assertTrue(stat.S_ISSOCK(Path(self.path).stat().st_mode))
        self.assertEqual({sock.family for sock in self.bridge.server.sockets}, {socket.AF_UNIX})
        self.assertFalse(self.bridge.connected.is_set())
        reader, writer = await self.connect(hello=False)
        await self.send(writer, {'type': 'not-hello'})
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertFalse(self.bridge.connected.is_set())
        reader, writer = await self.connect()
        self.assertTrue(self.bridge.connected.is_set())
        writer.close()
        await writer.wait_closed()
        await self.wait_until(lambda: not self.bridge.connected.is_set())
        self.assertIsNone(self.bridge.writer)

    async def test_launch_reply_and_status_export_only_safe_current_fields(self):
        reader, writer = await self.connect()
        task = asyncio.create_task(self.bridge.play('video', launch_id=7,
            url='https://youtu.be/AbCdEf12_-3?si=private-tracking&t=1m30s'))
        command = await self.message(reader)
        self.assertEqual(command, {'id': 1, 'action': 'launch', 'launch_id': 7, 'mode': 'video', 'url': VIDEO + '&t=90s', 'resume': True})
        # bool is an int subclass, but must not acknowledge request ID 1.
        await self.send(writer, {'type': 'reply', 'id': True, 'ok': True})
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        await self.reply(writer, command)
        self.assertEqual(await task, {'mode': 'video', 'state': 'loading'})
        for status in [
            {'launch_id': 6, 'mode': 'video', 'state': 'playing'},
            {'launch_id': True, 'mode': 'video', 'state': 'playing'},
            {'launch_id': 7, 'mode': 'watch_later', 'state': 'playing'},
            {'launch_id': 7, 'mode': 'video', 'state': 'private-state'},
        ]:
            await self.send(writer, {'type': 'status', **status})
        await self.send(writer, {'type': 'status', 'launch_id': 7, 'mode': 'video', 'state': 'playing',
                                 'url': VIDEO, 'video_id': 'private-id', 'playlist': ['private-history'], 'error': 'private message'})
        await self.wait_until(lambda: bool(self.events))
        self.assertEqual(self.events, [{'launch_id': 7, 'mode': 'video', 'state': 'playing'}])
        self.assertNotIn('private', json.dumps(self.events))
        self.assertEqual(self.bridge.pending, {})

    async def test_cancel_acknowledges_and_stale_events_cannot_restore_launch(self):
        await asyncio.wait_for(self.bridge.cancel(), 0.1)  # No peer or active launch.
        reader, writer = await self.connect()
        await self.launch(reader, writer, mode='watch_later')
        cancel = asyncio.create_task(self.bridge.cancel())
        command = await self.message(reader)
        self.assertEqual(command['action'], 'cancel')
        self.assertIsNone(self.bridge.active)
        await self.send(writer, {'type': 'status', 'launch_id': 7, 'mode': 'watch_later', 'state': 'playing'})
        await self.reply(writer, command)
        await cancel
        self.assertEqual(self.events, [])
        # Cancel remains an acknowledged no-op with an already connected peer.
        cancel = asyncio.create_task(self.bridge.cancel())
        command = await self.message(reader)
        await self.reply(writer, command)
        await cancel
        self.assertIsNone(self.bridge.active)

    async def test_second_client_cannot_replace_existing_connection(self):
        reader, writer = await self.connect()
        second_reader, second_writer = await self.connect(hello=False)
        await self.send(second_writer, {'type': 'hello'})
        self.assertEqual(await asyncio.wait_for(second_reader.read(), 1), b'')
        self.assertTrue(self.bridge.connected.is_set())
        command, result = await self.launch(reader, writer)
        self.assertEqual((command['action'], result['state']), ('launch', 'loading'))

    async def test_disconnect_rejects_pending_reply_and_reports_only_interaction_state(self):
        reader, writer = await self.connect()
        task = asyncio.create_task(self.bridge.play('video', launch_id=3, url=VIDEO))
        await self.message(reader)
        writer.close()
        await writer.wait_closed()
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            await task
        self.assertEqual(self.bridge.pending, {})
        self.assertIsNone(self.bridge.active)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertEqual(self.events, [{'launch_id': 3, 'mode': 'video', 'state': 'needs_interaction'}])

    async def test_missing_hello_and_connection_have_bounded_sanitized_timeouts(self):
        with patch.object(bridge_module, 'HELLO_TIMEOUT', 0.03):
            reader, _ = await self.connect(hello=False)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        with patch.object(bridge_module, 'CONNECT_TIMEOUT', 0.03):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                await self.bridge.play('watch_later', launch_id=1)
        self.assertIsNone(self.bridge.active)
        self.assertEqual(self.bridge.pending, {})

    async def test_hung_reply_closes_channel_before_retry_can_start(self):
        reader, writer = await self.connect()
        with patch.object(bridge_module, 'REPLY_TIMEOUT', 0.03):
            task = asyncio.create_task(self.bridge.play('video', launch_id=1, url=VIDEO))
            old = await self.message(reader)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                await task
        self.assertIsNone(self.bridge.active)
        self.assertEqual(self.bridge.pending, {})
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        await self.wait_until(lambda: self.bridge.writer is None)
        self.assertFalse(self.bridge.connected.is_set())
        reader, writer = await self.connect()
        await self.reply(writer, old)  # A stale ID still cannot acknowledge retry.
        _, result = await self.launch(reader, writer, launch_id=2)
        self.assertEqual(result['state'], 'loading')
        self.assertEqual(self.bridge.active['launch_id'], 2)

    async def test_unacknowledged_cancel_disconnects_peer_that_may_still_be_playing(self):
        reader, writer = await self.connect()
        await self.launch(reader, writer, mode='watch_later')
        with patch.object(bridge_module, 'REPLY_TIMEOUT', 0.03):
            task = asyncio.create_task(self.bridge.cancel())
            command = await self.message(reader)
            self.assertEqual(command['action'], 'cancel')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                await task
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        await self.wait_until(lambda: self.bridge.writer is None)
        self.assertIsNone(self.bridge.active)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertEqual(self.bridge.pending, {})

    async def test_oversized_and_malformed_input_disconnects_without_task_errors(self):
        for raw in [b'x' * (bridge_module.MAX_MESSAGE + 2), b'true\n',
                    b'[' * 1500 + b'0' + b']' * 1500 + b'\n',
                    b' ' * bridge_module.MAX_MESSAGE + b'{}\n']:
            with self.subTest(size=len(raw)):
                reader, writer = await self.connect()
                writer.write(raw)
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
                await self.wait_until(lambda: not self.bridge.connected.is_set())
        self.assertEqual(self.events, [])

    async def test_boolean_identifiers_and_boolean_like_ok_do_not_pass_type_checks(self):
        for launch_id in (True, False, 0, -1, '1'):
            with self.assertRaises(ValueError):
                await self.bridge.play('watch_later', launch_id=launch_id)
        with self.assertRaises(ValueError):
            await self.bridge.play('watch_later', launch_id=1, resume=1)
        self.assertIsNone(self.bridge.active)
        reader, writer = await self.connect()
        task = asyncio.create_task(self.bridge.play('watch_later', launch_id=1))
        command = await self.message(reader)
        await self.reply(writer, command, ok=1)
        with self.assertRaisesRegex(ValueError, 'launch failed'):
            await task
        self.assertEqual(self.bridge.pending, {})

    async def test_close_wakes_initial_connection_wait_and_removes_owned_socket(self):
        task = asyncio.create_task(self.bridge.play('watch_later', launch_id=1))
        await self.wait_until(lambda: self.bridge.active is not None)
        await self.bridge.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            await asyncio.wait_for(task, 0.5)
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(self.bridge.clients, set())
        self.assertEqual(self.bridge.pending, {})
        await self.bridge.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            await self.bridge.play('watch_later', launch_id=2)

    async def test_close_rejects_pending_reply_and_cancels_connection_tasks(self):
        reader, writer = await self.connect()
        task = asyncio.create_task(self.bridge.play('video', launch_id=1, url=VIDEO))
        await self.message(reader)
        await self.bridge.close()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(self.bridge.pending, {})
        self.assertEqual(self.bridge.clients, set())
        self.assertFalse(self.bridge.connected.is_set())
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')

    async def test_cancelling_ambiguous_written_request_revokes_connection(self):
        reader, writer = await self.connect()
        old = asyncio.create_task(self.bridge.play('watch_later', launch_id=1))
        await self.message(reader)
        old.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        await self.wait_until(lambda: self.bridge.writer is None)
        self.assertIsNone(self.bridge.active)
        self.assertEqual(self.bridge.pending, {})


class WorkerYouTubeRouting(unittest.IsolatedAsyncioTestCase):
    async def test_worker_uses_typed_extension_intent_and_explicit_cancel(self):
        worker = Worker.__new__(Worker)
        worker.youtube = AsyncMock()
        worker.youtube.play.return_value = {'mode': 'video', 'state': 'loading'}
        result = await worker.command({'action': 'youtube', 'mode': 'video', 'url': VIDEO, 'resume': False, 'launch_id': 9})
        worker.youtube.play.assert_awaited_once_with('video', launch_id=9, url=VIDEO, resume=False)
        self.assertEqual(result, {'mode': 'video', 'state': 'loading'})
        await worker.command({'action': 'youtube_cancel'})
        worker.youtube.cancel.assert_awaited_once_with()
        worker.youtube = None
        await worker.command({'action': 'youtube_cancel'})
        with self.assertRaises(ValueError):
            await worker.command({'action': 'youtube', 'mode': 'watch_later', 'launch_id': 10})


if __name__ == '__main__':
    unittest.main()
