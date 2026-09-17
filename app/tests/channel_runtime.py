"""Linux synthetic MPEG-TS to two real AirPlay protocol fixtures.

Run inside the app image with ffmpeg and doubletake-test-receiver available.
No real channels, receiver addresses, credentials or browser profiles are used.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys
import tempfile
import time
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from media_worker import MediaWorker
from media_pipeline import MediaPipeline
from fullscreen_motion import analyze_capture, frame_count


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def until(predicate, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(.1)
    raise AssertionError(label)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        active, requests = set(), []
        filters = ['drawbox=x=0:y=0:w=640:h=96:color=black:t=fill', 'drawbox=x=16:y=24:w=24:h=48:color=white:t=fill']
        filters += [f"drawbox=x={64+40*bit}:y=24:w=24:h=48:color=white:t=fill:enable='bitand(n,{1<<bit})'" for bit in range(12)]
        async def stream(request):
            if request.match_info['channel'] in ('805', '807'):
                code = request.match_info['channel']
                return web.Response(status=503, headers={'X-HDHomeRun-Error': code + ' Fixture error'})
            response = web.StreamResponse(headers={'Content-Type':'video/mp2t'})
            await response.prepare(request)
            process = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-re', '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                '-vf', ','.join(filters), '-c:v', 'mpeg2video', '-b:v', '6M', '-g', '15', '-bf', '0', '-threads', '2',
                '-c:a', 'ac3', '-ac', '2', '-f', 'mpegts', 'pipe:1',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            requests.append(request.path)
            active.add(process)
            try:
                while chunk := await process.stdout.read(32768):
                    await response.write(chunk)
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                active.discard(process)
            return response

        app = web.Application()
        app.router.add_get('/auto/v{channel}', stream)
        runner = web.AppRunner(app)
        await runner.setup()
        source_port = free_port()
        await web.TCPSite(runner, '127.0.0.1', source_port).start()
        workers, receivers, handles, logs = [], [], [], []
        try:
            # A 503 alone does not mean tuner exhaustion. Only the tuner's
            # specific busy response may release our current stream to retry.
            for code, expected in [('805', 'busy'), ('807', 'no_media')]:
                failed = MediaPipeline(f'http://127.0.0.1:{source_port}/auto/v{code}',
                                       {'width':1920,'height':1080,'fps':30,'bitrate':8000})
                try:
                    try:
                        await failed.prepare()
                        raise AssertionError('Unavailable channel unexpectedly started')
                    except ValueError:
                        assert failed.error_code == expected, (code, failed.error_code)
                finally:
                    await failed.close()
            ports = [free_port(), free_port(), free_port()]
            for index, profile in enumerate(('uxplay', 'uxplay', 'airserver')):
                log = work / f'receiver{index}.log'
                handle = log.open('w')
                handles.append(handle)
                logs.append(log)
                receivers.append(await asyncio.create_subprocess_exec('doubletake-test-receiver',
                    '-listen', f'127.0.0.1:{ports[index]}', '-profile', profile, '-stats-interval', '200ms',
                    *(['-video-capture',str(work / 'video.bin')] if index == 0 else []),
                    stdout=handle, stderr=asyncio.subprocess.STDOUT))
            await until(lambda: all('listening' in p.read_text() for p in logs), 5, 'receiver readiness')
            config = {'source': {'url': f'http://127.0.0.1:{source_port}/auto/v2.1'},
                      'quality': {'width':1920,'height':1080,'fps':30,'bitrate':8000},
                      'receivers_dir': str(work / 'receivers')}
            worker = MediaWorker(config)
            workers.append(worker)
            private_output = []
            original_line = worker.sender_line
            def line(tv_id, entry, value):
                private_output.append(value)
                original_line(tv_id, entry, value)
            worker.sender_line = line
            await worker.start(work)
            targets = [{'id':str(i+1)*16,'host':'127.0.0.1','port':p} for i,p in enumerate(ports)]
            def packets(index, field):
                values = re.findall(r'\b' + field + r'=(\d+)', logs[index].read_text())
                return int(values[-1]) if values else 0
            await worker.command({'action':'cast','receiver':targets[0]})
            try:
                await until(lambda: packets(0,'video_frames') > 90 and packets(0,'audio_rtp') > 100, 25, 'first TV media')
            except AssertionError:
                print('\n'.join(private_output[-40:]))
                if worker.channel.pipeline:
                    bus = worker.channel.pipeline.get_bus()
                    while message := bus.pop_filtered(worker.channel.Gst.MessageType.ERROR | worker.channel.Gst.MessageType.WARNING):
                        print(message.parse_error() if message.type == worker.channel.Gst.MessageType.ERROR else message.parse_warning())
                raise
            before = packets(0,'video_frames')
            await worker.command({'action':'cast','receiver':targets[1]})
            await until(lambda: packets(1,'video_frames') > 60 and packets(1,'audio_rtp') > 100, 20, 'late second TV media')
            assert packets(0,'video_frames') > before + 30
            assert len(requests) == 1 and len(active) == 1, (requests,len(active))
            assert len(worker.channel.branches) == 1, worker.channel.branches.keys()
            first_frame = frame_count(work / 'video.bin')
            await asyncio.sleep(12)
            motion = await asyncio.to_thread(analyze_capture, work / 'video.bin', first_frame, work)
            await worker.command({'action':'cast','receiver':targets[2]})
            await until(lambda: packets(2,'video_frames') > 60 and packets(2,'audio_rtp') > 100, 20, 'smaller AAC-ELD TV')
            assert len(worker.channel.branches) == 2 and len(requests) == 1
            assert all(entry.get('audio') == 'active' for entry in worker.senders.values())
            before = packets(1,'video_frames')
            await worker.command({'action':'stop','tv_id':targets[0]['id']})
            await until(lambda: packets(1,'video_frames') > before + 60, 10, 'independent Stop')
            assert len(requests) == 1
            result = {'tuner_error_classification':True, 'one_tuner_connection':True, 'shared_encoder':True, 'late_join_preserved_first':True,
                      'independent_stop':True, 'motion':motion, 'different_canvas_same_tuner':True, 'video_frames':[packets(i,'video_frames') for i in range(3)],
                      'audio_rtp':[packets(i,'audio_rtp') for i in range(3)]}
            await worker.close()
            await until(lambda:not active, 10, 'last TV releases tuner')
            result['tuner_released'] = True
            print(json.dumps(result, indent=2))
        finally:
            if sys.exc_info()[0] and 'private_output' in locals():
                print('\n'.join(private_output[-45:]))
            for worker in workers:
                await worker.close()
            for receiver in receivers:
                if receiver.returncode is None:
                    receiver.terminate()
                await receiver.wait()
            for handle in handles:
                handle.close()
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
