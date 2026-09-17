"""Broadcast segment offsets must not become an extra AirPlay clock delay."""
from pathlib import Path
import struct
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
except (ImportError, ValueError):
    Gst = None


@unittest.skipIf(Gst is None, 'GStreamer is tested in the Linux app image')
class BroadcastClock(unittest.TestCase):
    def test_nonzero_segment_time_preserves_pipeline_running_time(self):
        from media_pipeline import TIMESTAMPED_RTP
        Gst.init(None)
        # Audio can retain a broadcast segment origin; x264 uses a large PTS
        # origin of its own. Both must map to the same three-second instant.
        cases = [(10_000_000_000, 13_000_000_000, 1_000_000_000, 12_000_000_000),
                 (3_600_000_000_000_000, 1_024_000_000, 0, 3_600_003_000_000_000)]
        for start, stream_time, base, pts in cases:
            with self.subTest(start=start):
                pipeline = Gst.parse_launch(
                    'appsrc name=input is-live=true format=time handle-segment-change=true '
                    f'! {TIMESTAMPED_RTP} ! appsink name=output sync=false async=false')
                clock = Gst.SystemClock.obtain()
                pipeline.use_clock(clock)
                pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
                pipeline.set_base_time(clock.get_time() - 3_250_000_000)
                try:
                    pipeline.set_state(Gst.State.PLAYING)
                    caps = Gst.Caps.from_string('application/x-rtp,media=audio,clock-rate=44100,'
                                               'encoding-name=L16,channels=2,payload=96')
                    segment = Gst.Segment.new()
                    segment.init(Gst.Format.TIME)
                    segment.start, segment.time, segment.base = start, stream_time, base
                    packet = struct.pack('!BBHII', 0x80, 96, 1, 0, 1234) + b'\x00\x00'
                    buffer = Gst.Buffer.new_wrapped(packet)
                    buffer.pts = pts
                    buffer.duration = 8_000_000
                    sample = Gst.Sample.new(buffer, caps, segment, None)
                    running = segment.to_running_time(Gst.Format.TIME, pts)
                    self.assertEqual(running, 3_000_000_000)
                    expected = time.time() + (pipeline.get_base_time() + running - clock.get_time()) / 1e9
                    self.assertEqual(pipeline.get_by_name('input').emit('push-sample', sample), Gst.FlowReturn.OK)
                    output = pipeline.get_by_name('output').emit('try-pull-sample', 2 * Gst.SECOND)
                    self.assertIsNotNone(output)
                    data = output.get_buffer().extract_dup(0, output.get_buffer().get_size())[2:]
                    offset = 12 + 4 * (data[0] & 15)
                    self.assertEqual(struct.unpack('!H', data[offset:offset+2])[0], 0xabac)
                    timestamp = struct.unpack('!Q', data[offset+4:offset+12])[0]
                    actual = (timestamp >> 32) + (timestamp & 0xffffffff) / 2**32 - 2208988800
                    self.assertAlmostEqual(actual, expected, delta=.025)
                finally:
                    pipeline.set_state(Gst.State.NULL)


if __name__ == '__main__':
    unittest.main()
