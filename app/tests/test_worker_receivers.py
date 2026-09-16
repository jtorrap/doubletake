"""Worker process ownership and asynchronous receiver event isolation."""
import asyncio
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import worker as worker_module


ONE, TWO = '1' * 16, '2' * 16


class Sender:
    def __init__(self, returncode=None):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, 'rb', buffering=0)
        self.stdin = io.BytesIO()
        self.returncode = returncode

    def poll(self):
        return self.returncode


class WorkerReceivers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.processes = []
        self.stopped = []

        def stop(process):
            self.stopped.append(process)
            process.returncode = 0

        self.engine = SimpleNamespace(stop=stop, sender_command=Mock(return_value=['sender', '-no-audio']))
        with patch.object(worker_module, 'load_engine', return_value=self.engine):
            self.worker = worker_module.Worker({'receivers_dir': self.directory.name})
        self.worker.engine_config = {}
        self.worker.environment = {'FIXTURE': 'private environment'}
        self.worker.window = 42
        alive = SimpleNamespace(poll=lambda: None)
        self.worker.browser = self.worker.display = self.worker.vnc = alive
        self.events = []
        self.emitter = patch.object(worker_module, 'emit', side_effect=lambda kind, **values: self.events.append({'type': kind, **values}))
        self.emitter.start()

    async def asyncTearDown(self):
        self.worker.stop_event.set()
        self.emitter.stop()
        loop = asyncio.get_running_loop()
        for process in self.processes:
            if not process.stdout.closed:
                loop.remove_reader(process.stdout.fileno())
                process.stdout.close()
            process.stdin.close()
            os.close(process.write_fd)
        self.directory.cleanup()

    def sender(self, *, returncode=None):
        process = Sender(returncode)
        self.processes.append(process)
        return process

    def entry(self, tv_id, *, state='sending', returncode=None):
        entry = {'process': self.sender(returncode=returncode), 'buffer': '', 'state': state}
        self.worker.senders[tv_id] = entry
        return entry

    async def until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 2)

    async def test_targeted_stop_does_not_touch_peer_or_accept_old_output(self):
        first, second = self.entry(ONE), self.entry(TWO)
        await self.worker.command({'action': 'stop', 'tv_id': ONE})
        self.assertEqual(self.stopped, [first['process']])
        self.assertTrue(first['process'].stdout.closed)
        self.assertTrue(first['process'].stdin.closed)
        self.assertIs(self.worker.senders[TWO], second)
        self.assertIsNone(second['process'].poll())
        with patch.object(worker_module.os, 'read') as read:
            self.worker.sender_output(ONE, first)
            read.assert_not_called()
        self.assertEqual(self.events, [])
        await self.worker.command({'action': 'stop'})
        self.assertEqual(self.stopped, [first['process'], second['process']])
        self.assertEqual(self.worker.senders, {})

    async def test_partial_audio_error_after_video_ready_is_preserved_and_sanitized(self):
        first, second = self.entry(ONE, state='starting'), self.entry(TWO)
        os.write(first['process'].write_fd, b'mirror session ready\nwarning: audio capt')
        self.worker.sender_output(ONE, first)
        self.assertEqual(self.events, [{'type': 'airplay', 'tv_id': ONE, 'state': 'sending'}])
        os.write(first['process'].write_fd, b'ure failed: SYNTHETIC_PRIVATE_DETAIL\n')
        self.worker.sender_output(ONE, first)
        self.assertEqual(self.events[-1], {'type': 'audio', 'tv_id': ONE, 'state': 'error'})
        self.assertEqual(first['state'], 'sending')
        self.assertEqual(first['audio'], 'error')
        self.assertIs(self.worker.senders[TWO], second)
        self.assertNotIn('audio', second)
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', repr(self.events))
        os.write(first['process'].write_fd, b'audio capture started\n')
        self.worker.sender_output(ONE, first)
        self.assertEqual(first['audio'], 'error')
        self.assertEqual(len(self.events), 2)

    async def test_unterminated_pin_prompt_and_audio_availability_are_per_receiver(self):
        first, second = self.entry(ONE, state='starting'), self.entry(TWO, state='starting')
        os.write(first['process'].write_fd, b'Enter PIN for fixture: ')
        self.worker.sender_output(ONE, first)
        self.assertEqual(self.events, [{'type': 'airplay', 'tv_id': ONE, 'state': 'pairing'}])
        self.assertEqual(second['state'], 'starting')
        os.write(second['process'].write_fd, b'mirror session ready\naudio disabled (receiver did not provide audio ports)\n')
        self.worker.sender_output(TWO, second)
        self.assertEqual(self.events[-1], {'type': 'audio', 'tv_id': TWO, 'state': 'unavailable'})
        self.assertEqual(first['state'], 'pairing')
        self.assertEqual(second['state'], 'sending')

    async def test_retry_owns_new_process_and_only_its_credential_directory(self):
        first, second = self.entry(ONE), self.entry(TWO)
        replacement = self.sender()
        receiver = {'id': ONE, 'host': '127.0.0.1', 'port': 7000}
        with patch.object(worker_module.subprocess, 'Popen', return_value=replacement) as launch:
            await self.worker.command({'action': 'cast', 'receiver': receiver})
        self.assertEqual(self.stopped, [first['process']])
        self.assertIs(self.worker.senders[TWO], second)
        self.assertIs(self.worker.senders[ONE]['process'], replacement)
        config = self.engine.sender_command.call_args.args[0]
        self.assertEqual(config['state_dir'], str(Path(self.directory.name) / ONE))
        self.assertEqual(config['target'], receiver['host'])
        self.assertEqual(launch.call_args.kwargs['env'], self.worker.environment)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        with patch.object(worker_module.os, 'read') as read:
            self.worker.sender_output(ONE, first)
            read.assert_not_called()
        self.assertEqual(self.worker.senders[ONE]['state'], 'starting')

    async def test_pairing_code_writes_only_explicit_waiting_receiver(self):
        first, second = self.entry(ONE, state='pairing'), self.entry(TWO, state='pairing')
        for fields in [{}, {'tv_id': '3' * 16}, {'tv_id': ONE, 'value': 'line\nEnter'}]:
            with self.assertRaises(RuntimeError):
                await self.worker.command({'action': 'pin', 'value': '1234', **fields})
        self.assertEqual(first['process'].stdin.getvalue(), b'')
        self.assertEqual(second['process'].stdin.getvalue(), b'')
        await self.worker.command({'action': 'pin', 'tv_id': TWO, 'value': '1234'})
        self.assertEqual(second['process'].stdin.getvalue(), b'1234\n')
        self.assertEqual(first['process'].stdin.getvalue(), b'')
        self.assertEqual(first['state'], 'pairing')
        self.assertEqual(second['state'], 'starting')
        with self.assertRaises(RuntimeError):
            await self.worker.command({'action': 'pin', 'tv_id': TWO, 'value': '5678'})

    async def test_receiver_failure_keeps_healthy_sender_and_browser_running(self):
        failed, healthy = self.entry(ONE, returncode=1), self.entry(TWO)
        monitor = asyncio.create_task(self.worker.monitor())
        try:
            await self.until(lambda: bool(self.events))
            self.assertEqual(self.stopped, [failed['process']])
            self.assertEqual(self.events, [{'type': 'airplay', 'tv_id': ONE, 'state': 'error', 'code': 'receiver_session_ended'}])
            self.assertIs(self.worker.senders[TWO], healthy)
            self.assertFalse(self.worker.stop_event.is_set())
        finally:
            self.worker.stop_event.set()
            await monitor

    async def test_old_failure_cleanup_cannot_report_error_against_retry(self):
        failed, healthy = self.entry(ONE, returncode=1), self.entry(TWO)
        loop = asyncio.get_running_loop()
        cleanup_started = asyncio.Event()
        release_cleanup = threading.Event()

        def slow_stop(process):
            self.stopped.append(process)
            if process is failed['process']:
                loop.call_soon_threadsafe(cleanup_started.set)
                if not release_cleanup.wait(2):
                    raise RuntimeError('Fixture cleanup release missing')
            process.returncode = 0

        self.engine.stop = slow_stop
        monitor = asyncio.create_task(self.worker.monitor())
        try:
            await asyncio.wait_for(cleanup_started.wait(), 2)
            replacement = self.sender()
            with patch.object(worker_module.subprocess, 'Popen', return_value=replacement):
                await self.worker.command({'action': 'cast', 'receiver': {'id': ONE, 'host': '127.0.0.1', 'port': 7000}})
            release_cleanup.set()
            await self.until(lambda: failed['process'].stdout.closed)
            self.assertIs(self.worker.senders[ONE]['process'], replacement)
            self.assertIs(self.worker.senders[TWO], healthy)
            self.assertFalse(any(event.get('tv_id') == ONE and event['state'] == 'error' for event in self.events))
        finally:
            release_cleanup.set()
            self.worker.stop_event.set()
            await monitor

    async def assert_stop_cancels_pending_hardware_fallback(self, stop_all):
        failed, healthy = self.entry(ONE, returncode=1), self.entry(TWO)
        self.worker.encoder = 'vaapi'
        self.worker.sender_generations[ONE] = 1
        failed.update(encoder='vaapi', generation=1, started=time.monotonic(),
                      receiver={'id': ONE, 'host': '127.0.0.1', 'port': 7000})
        loop = asyncio.get_running_loop()
        cleanup_started, release_cleanup = asyncio.Event(), threading.Event()

        def slow_stop(process):
            self.stopped.append(process)
            if process is failed['process']:
                loop.call_soon_threadsafe(cleanup_started.set)
                if not release_cleanup.wait(2):
                    raise RuntimeError('Fixture cleanup release missing')
            process.returncode = 0

        self.engine.stop = slow_stop
        with patch.object(worker_module.subprocess, 'Popen') as launch:
            monitor = asyncio.create_task(self.worker.monitor())
            try:
                await asyncio.wait_for(cleanup_started.wait(), 2)
                # The monitor has already removed ONE; an absent entry still
                # represents a desired connection until this explicit Stop.
                self.assertNotIn(ONE, self.worker.senders)
                await self.worker.command({'action': 'stop', **({} if stop_all else {'tv_id': ONE})})
                release_cleanup.set()
                await self.until(lambda: failed['process'].stdout.closed)
                launch.assert_not_called()
                self.assertNotIn(ONE, self.worker.senders)
                self.assertFalse(any(event.get('tv_id') == ONE for event in self.events))
                if stop_all:
                    self.assertEqual(self.worker.senders, {})
                else:
                    self.assertIs(self.worker.senders[TWO], healthy)
            finally:
                release_cleanup.set()
                self.worker.stop_event.set()
                await monitor

    async def test_stop_during_failed_encoder_cleanup_prevents_automatic_reconnect(self):
        await self.assert_stop_cancels_pending_hardware_fallback(False)

    async def test_stop_all_invalidates_removed_sender_awaiting_encoder_fallback(self):
        await self.assert_stop_cancels_pending_hardware_fallback(True)

    async def test_hardware_start_failure_retries_software_only_once(self):
        failed, healthy = self.entry(ONE, returncode=1), self.entry(TWO)
        self.worker.encoder = 'vaapi'
        self.worker.sender_generations[ONE] = 1
        failed.update(encoder='vaapi', generation=1, started=time.monotonic(),
                      receiver={'id': ONE, 'host': '127.0.0.1', 'port': 7000})
        replacement = self.sender()
        with patch.object(worker_module.subprocess, 'Popen', return_value=replacement) as launch:
            monitor = asyncio.create_task(self.worker.monitor())
            try:
                await self.until(lambda: self.worker.senders.get(ONE, {}).get('process') is replacement)
                self.assertEqual(self.engine.sender_command.call_args.args[0]['hwaccel'], 'none')
                self.assertIs(self.worker.senders[TWO], healthy)
                replacement.returncode = 1
                await self.until(lambda: any(event.get('tv_id') == ONE and event['state'] == 'error' for event in self.events))
                self.assertEqual(launch.call_count, 1)
                self.assertIs(self.worker.senders[TWO], healthy)
                self.assertFalse(monitor.done())
            finally:
                self.worker.stop_event.set()
                await monitor


if __name__ == '__main__':
    unittest.main()
