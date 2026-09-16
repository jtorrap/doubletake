import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import fullscreen_motion as motion


class FullscreenMotionTests(unittest.TestCase):
    def analyze(self, ids, arrivals=None):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            capture = work / 'synthetic.bin'
            codec = bytes.fromhex('0142001effe100046742001e0100026880')
            frame = bytes.fromhex('000000026580')
            rows = []
            with capture.open('wb') as output:
                output.write(b'DTVID001')
                output.write(struct.pack('>BQQI', 1, 0, 0, len(codec)) + codec)
                for index, value in enumerate(ids):
                    arrival = arrivals[index] if arrivals is not None else index / 30
                    output.write(struct.pack('>BQQI', 0, index << 32, round(arrival*1e9), len(frame)) + frame)
                    row = bytearray(640)
                    row[28] = 255
                    for bit in range(12):
                        row[76+40*bit] = 235 if value & (1 << bit) else 16
                    rows.append(row)
            decoded = types.SimpleNamespace(stdout=b''.join(rows))
            with mock.patch.object(motion.subprocess, 'run', return_value=decoded):
                return motion.analyze_capture(capture, 0, work)

    def test_fresh_well_paced_30fps_passes(self):
        result = self.analyze(list(range(360)))
        self.assertEqual(result['distinct_fps'], 30)
        self.assertEqual(result['duplicate_fraction'], 0)

    def test_30_encoded_fps_with_15_fresh_fps_fails(self):
        with self.assertRaises(AssertionError):
            self.analyze([index // 2 for index in range(360)])

    def test_30_average_fps_delivered_in_one_second_bursts_fails(self):
        with self.assertRaises(AssertionError):
            self.analyze(list(range(360)), [index // 30 for index in range(360)])

    def test_marker_needs_distinct_white_and_black_references(self):
        self.assertIsNone(motion.marker_id(bytes(640)))


if __name__ == '__main__':
    unittest.main()
