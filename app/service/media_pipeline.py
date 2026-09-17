"""One tuner/decode clock, shared H.264 branches and timestamped stereo PCM.

Only the isolated worker imports GStreamer. The controller supplies a resolved
HDHomeRun URL; no arbitrary URL or GStreamer syntax is accepted from a client.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import time
from hdhomerun import ChannelError


class MediaPipeline:
    def __init__(self, url, quality, *, gst=None):
        if gst is None:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            gst = Gst
        self.Gst = gst
        gst.init(None)
        self.url, self.quality = url, quality
        self.pipeline = self.server = None
        self.branches = {}
        self.clients = {}
        self.tasks = set()
        self.error = None
        self.error_code = None
        self.decoded_video = 0
        self.last_audio = 0
        self.last_video = 0
        self.started = 0
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def listen(self, runtime):
        self.socket = str(Path(runtime) / 'channel.sock')
        self.server = await asyncio.start_unix_server(self.subscribe, self.socket, limit=1024)
        os.chmod(self.socket, 0o600)
        return self.socket

    def canvas(self, width, height):
        if type(width) is not int or type(height) is not int or not 0 <= width <= 8192 or not 0 <= height <= 8192:
            raise ValueError('Invalid receiver canvas')
        return (min(width or 1280, self.quality['width']) & ~1,
                min(height or 720, self.quality['height']) & ~1)

    def sample(self, sink, key):
        sample = sink.emit('pull-sample')
        if sample is None:
            return self.Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        # GStreamer calls on a streaming thread. The bounded asyncio queues and
        # socket writers run on the worker loop; one slow TV cannot stall peers.
        self.loop.call_soon_threadsafe(self.publish, key, data)
        return self.Gst.FlowReturn.OK

    def publish(self, key, data):
        if self.closed:
            return
        if key == 'audio':
            self.last_audio = time.monotonic()
        else:
            self.last_video = time.monotonic()
        for queue, writer in tuple(self.clients.get(key, {}).items()):
            if queue.full():
                # Never truncate an encoded frame or lose PCM silently.
                writer.close()
            else:
                queue.put_nowait(data)

    def start(self):
        if self.pipeline:
            return
        Gst = self.Gst
        # Set the URI as a property, never interpolate it into pipeline syntax.
        self.pipeline = Gst.parse_launch(
            'uridecodebin name=source '
            'source. ! queue max-size-time=1000000000 max-size-bytes=0 max-size-buffers=0 '
            '! video/x-raw ! deinterlace mode=auto fields=top ! videoconvert '
            '! videorate ! video/x-raw,framerate=30/1 ! tee name=video allow-not-linked=true '
            '! queue ! fakesink name=clock sync=true async=false signal-handoffs=true '
            'source. ! queue max-size-time=1000000000 max-size-bytes=0 max-size-buffers=0 '
            '! audio/x-raw ! audioconvert ! audioresample '
            '! audio/x-raw,rate=44100,channels=2,format=S16BE,layout=interleaved '
            '! rtpL16pay pt=96 mtu=60000 timestamp-offset=0 perfect-rtptime=true max-ptime=8000000 '
            '! rtponviftimestamp ntp-offset=-1 set-e-bit=false set-t-bit=false ! rtpstreampay '
            '! appsink name=audio emit-signals=true sync=true async=false max-buffers=32 drop=false')
        source = self.pipeline.get_by_name('source')
        source.set_property('uri', self.url)
        source.connect('source-setup', self.configure_source)
        self.pipeline.get_by_name('audio').connect('new-sample', self.sample, 'audio')
        self.pipeline.get_by_name('clock').connect('handoff', self.decoded)
        self.started = time.monotonic()

    def decoded(self, *_args):
        self.decoded_video += 1
        self.last_video = time.monotonic()

    async def prepare(self):
        """Tune and confirm decoded A/V before taking over any selected TV."""
        self.start()
        self.pipeline.set_state(self.Gst.State.PLAYING)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.health():
                raise ValueError('Channel could not start')
            if self.decoded_video >= 2 and self.last_audio:
                return
            await asyncio.sleep(.1)
        self.error_code = 'no_media'
        raise ValueError('Channel did not provide video and audio')

    @staticmethod
    def configure_source(_decode, source):
        # No transparent reconnect/redirect can consume another tuner or send
        # the request to a host outside the validated local device.
        for name, value in [('timeout', 8), ('retries', 0), ('automatic-redirect', False), ('is-live', True)]:
            if source.find_property(name):
                source.set_property(name, value)

    def branch(self, key):
        if key in self.branches:
            return
        if len(self.branches) >= 4:
            raise ValueError('Too many different receiver sizes')
        width, height = key
        if width < 160 or height < 90:
            raise ValueError('Unsupported receiver canvas')
        self.start()
        bitrate = min(self.quality.get('bitrate', 8000), 4500 if height <= 720 else 8000)
        # Bookworm's VA encoder rewrites PTS. Use timestamp-preserving x264
        # until hardware timing is validated, keeping A/V on the broadcast clock.
        branch = self.Gst.parse_bin_from_description(
            'queue max-size-time=250000000 max-size-bytes=0 max-size-buffers=0 '
            '! videoscale add-borders=true '
            f'! video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1,format=I420 '
            f'! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} key-int-max=30 bframes=0 '
            '! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au '
            '! rtph264pay pt=96 mtu=60000 aggregate-mode=none timestamp-offset=0 seqnum-offset=0 '
            '! rtponviftimestamp ntp-offset=-1 set-e-bit=false set-t-bit=false ! rtpstreampay '
            '! appsink name=output emit-signals=true sync=true async=false max-buffers=32 drop=false', True)
        branch.get_by_name('output').connect('new-sample', self.sample, key)
        self.pipeline.add(branch)
        pad = self.pipeline.get_by_name('video').request_pad_simple('src_%u')
        if pad.link(branch.get_static_pad('sink')) != self.Gst.PadLinkReturn.OK:
            raise RuntimeError('Could not connect shared encoder')
        self.branches[key] = (branch, pad)
        branch.sync_state_with_parent()
        if self.pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Could not start channel')

    async def subscribe(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        queue = asyncio.Queue(maxsize=128)
        key = None
        peer_closed = None
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if request.get('kind') == 'video':
                key = self.canvas(request.get('width'), request.get('height'))
                self.branch(key)
            elif request.get('kind') == 'audio' and self.pipeline:
                key = 'audio'
            else:
                raise ValueError('Unknown media subscription')
            self.clients.setdefault(key, {})[queue] = writer
            writer.write(b'\x01')
            await writer.drain()
            peer_closed = asyncio.create_task(reader.read(1))
            while not self.closed and not writer.is_closing():
                packet = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait([packet, peer_closed], return_when=asyncio.FIRST_COMPLETED)
                    if peer_closed in done:
                        break
                    writer.write(packet.result())
                    await asyncio.wait_for(writer.drain(), 2)
                finally:
                    packet.cancel()
                    await asyncio.gather(packet, return_exceptions=True)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            if key in self.clients:
                self.clients[key].pop(queue, None)
            if peer_closed:
                peer_closed.cancel()
                await asyncio.gather(peer_closed, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.tasks.discard(task)

    def health(self):
        if not self.pipeline:
            return None
        bus = self.pipeline.get_bus()
        types = self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS | self.Gst.MessageType.ELEMENT
        while message := bus.pop_filtered(types):
            if message.type == self.Gst.MessageType.ELEMENT:
                structure = message.get_structure()
                if structure and structure.get_name() == 'http-headers':
                    headers = structure.get_value('response-headers')
                    if headers:
                        for index in range(headers.n_fields()):
                            field = headers.nth_field_name(index)
                            if field.lower() == 'x-hdhomerun-error':
                                code = (headers.get_string(field) or '')[:3]
                                self.error_code = {'805':'busy','804':'busy','807':'no_media',
                                                   '811':'protected','801':'unknown'}.get(code,'stream')
                                self.error = ChannelError(self.error_code).safe_message
            else:
                self.error_code = self.error_code or 'stream'
                self.error = ChannelError(self.error_code).safe_message
        if not self.error and time.monotonic() - (self.last_video or self.started) > 15:
            self.error_code = 'no_media'
            self.error = ChannelError(self.error_code).safe_message
        return self.error

    async def close(self):
        self.closed = True
        if self.server:
            self.server.close()
        for group in self.clients.values():
            for writer in group.values():
                writer.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        if self.pipeline:
            await asyncio.to_thread(self.pipeline.set_state, self.Gst.State.NULL)
            self.pipeline = None
        self.branches.clear()
