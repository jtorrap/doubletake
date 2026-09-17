"""Channel worker shutdown must not race its final control reply."""
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from media_worker import MediaWorker


class ChannelWorkerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_last_stop_can_finish_before_idle_monitor_exits(self):
        with patch('worker.load_engine', return_value=SimpleNamespace()):
            worker = MediaWorker({})
        worker.channel = SimpleNamespace(health=lambda: None, started=time.monotonic())
        worker.senders['1' * 16] = {}
        worker.had_sender = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_stop(tv_id):
            worker.senders.pop(tv_id)
            entered.set()
            await release.wait()

        worker.stop_sender = slow_stop
        stopping = asyncio.create_task(worker.command({'action': 'stop', 'tv_id': '1' * 16}))
        await entered.wait()
        monitor = asyncio.create_task(worker.monitor())
        try:
            # The old monitor exited on its first iteration, cancelling the
            # command before commands() could send its reply to the controller.
            await asyncio.sleep(.3)
            self.assertFalse(worker.stop_event.is_set())
            self.assertFalse(stopping.done())
            release.set()
            await stopping
            await asyncio.wait_for(monitor, 1)
            self.assertTrue(worker.stop_event.is_set())
        finally:
            release.set()
            worker.stop_event.set()
            await asyncio.gather(stopping, monitor)

    async def test_diagnostics_never_include_sender_output_or_credentials(self):
        with patch('worker.load_engine', return_value=SimpleNamespace()):
            worker = MediaWorker({'quality': {'width': 1920, 'height': 1080}})
        worker.channel = SimpleNamespace(pipeline=object(), branches={}, error=None)
        worker.senders['1' * 16] = {'audio': 'error', 'audio_error_code': 'capture_late',
                                  'buffer': 'PRIVATE', 'receiver': {'secret': 'PRIVATE'},
                                  'performance': {'sent_fps': 30}}
        result = await worker.command({'action': 'diagnostics'})
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['receivers']['1' * 16]['audio_error_code'], 'capture_late')


if __name__ == '__main__':
    unittest.main()
