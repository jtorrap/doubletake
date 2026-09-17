#!/usr/bin/env python3
"""Channel worker: no browser, X11, PulseAudio, MQTT or Supervisor credentials."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from model import identifier
from media_pipeline import MediaPipeline
from worker import Worker, emit


class MediaWorker(Worker):
    def __init__(self, config):
        super().__init__(config)
        self.channel = None
        self.had_sender = False
        self.control_in_flight = 0
        self.encoder = 'none'
        self.audio = os.environ.get('DOUBLETAKE_AUDIO', 'true') == 'true'

    async def start(self, runtime):
        self.environment = dict(os.environ)
        self.channel = MediaPipeline(self.config['source']['url'], self.config['quality'])
        self.media_socket = await self.channel.listen(runtime)
        await self.channel.prepare()
        emit('ready', media=True)

    async def command(self, value, *, retry_generation=None):
        # Stop removes the last sender before its process has finished exiting.
        # Keep the worker alive until commands() can acknowledge that operation.
        self.control_in_flight += 1
        try:
            return await self.channel_command(value)
        finally:
            self.control_in_flight -= 1

    async def channel_command(self, value):
        action = value['action']
        if action == 'cast':
            receiver = value['receiver']
            tv_id = identifier(receiver['id'])
            self.invalidate_senders(tv_id)
            generation = self.sender_generations[tv_id]
            await self.stop_sender(tv_id)
            if not self.sender_desired(tv_id, generation):
                return False
            directory = Path(self.config['receivers_dir']) / tv_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            command = [self.config.get('sender', 'doubletake'), '-target', receiver['host'],
                       '-port', str(receiver['port']), '-fps', '30', '-video-codec', 'h264',
                       '-creds', str(directory / 'airplay-credentials.json'), '-media-socket', self.media_socket]
            if not self.audio:
                command.append('-no-audio')
            # tsdemux buffers live broadcasts for about 700 ms. Leave headroom
            # for decoding and transport without changing either source clock.
            command += ['-target-latency-ms', str(self.target_latency_ms or 1000)]
            process = subprocess.Popen(command, env=self.environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            entry = {'process': process, 'buffer': '', 'state': 'starting', 'encoder': 'none',
                     'receiver': receiver, 'generation': generation, 'started': time.monotonic(),
                     'audio': 'starting' if self.audio else 'disabled'}
            self.had_sender = True
            self.senders[tv_id] = entry
            asyncio.get_running_loop().add_reader(process.stdout.fileno(), self.sender_output, tv_id, entry)
            return True
        if action == 'diagnostics':
            return {'source_kind': 'hdhomerun', 'receiver_count': len(self.senders),
                    'tuner_connections': int(self.channel.pipeline is not None),
                    'encoder_count': len(self.channel.branches), 'video_encoder': 'none',
                    'display': self.config['quality'], 'error': self.channel.error,
                    'receivers': {tv_id: {key: entry[key] for key in
                                  ('state', 'audio', 'audio_error_code', 'performance', 'audio_performance')
                                  if key in entry} for tv_id, entry in self.senders.items()}}
        if action not in {'pin', 'stop', 'close'}:
            raise ValueError('Browser controls are unavailable during channel playback')
        return await super().command(value)

    async def monitor(self):
        while not self.stop_event.is_set():
            if self.channel.health():
                emit('channel', state='error', code=self.channel.error_code)
                self.stop_event.set()
                return
            for tv_id, entry in list(self.senders.items()):
                if entry['process'].poll() is not None and self.senders.get(tv_id) is entry:
                    generation = entry['generation']
                    await self.stop_sender(tv_id)
                    # A receiver ending playback must never be reclaimed.
                    if self.sender_desired(tv_id, generation):
                        emit('airplay', tv_id=tv_id, state='error')
            if not self.control_in_flight and not self.senders and (self.had_sender or time.monotonic() - self.channel.started > 50):
                self.stop_event.set()
            await asyncio.sleep(0.25)

    async def close(self):
        self.invalidate_senders()
        await self.stop_sender()
        if self.channel:
            await self.channel.close()


async def main():
    os.umask(0o077)
    config = json.loads(Path(sys.argv[1]).read_text())
    worker = MediaWorker(config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, worker.stop_event.set)
    with tempfile.TemporaryDirectory(prefix='doubletake-channel-') as runtime:
        tasks = []
        try:
            await worker.start(runtime)
            tasks = [asyncio.create_task(worker.commands()), asyncio.create_task(worker.monitor())]
            await worker.stop_event.wait()
        except Exception:
            emit('fatal', stage='channel', code=worker.channel.error_code if worker.channel else 'startup')
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.close()


if __name__ == '__main__':
    asyncio.run(main())
