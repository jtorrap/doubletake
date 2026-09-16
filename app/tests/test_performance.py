import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from performance import video_stats
from worker import Worker


class Performance(unittest.TestCase):
    def setUp(self):
        self.stats = dict(version=1, codec='h264', window_ms=5000, frames=150,
                          sent_fps=30, source_samples=150, source_age_mean_ms=22,
                          source_age_max_ms=40, write_mean_ms=1, write_max_ms=3,
                          late_frames=0, playout_lead_ms=75)

    def test_only_finite_bounded_metrics_cross_worker_boundary(self):
        self.assertEqual(video_stats({**self.stats, 'secret': 'private'}), self.stats)
        for field in ['sent_fps', 'source_age_mean_ms', 'write_max_ms']:
            for invalid in [float('nan'), float('inf'), -1, 'private', True, None]:
                self.assertIsNone(video_stats({**self.stats, field: invalid}))
        for invalid in [{}, {'codec': 'private'}, {'late_frames': 151}, {'source_samples': 151}, {'window_ms': 0}]:
            value = invalid if not invalid else {**self.stats, **invalid}
            self.assertIsNone(video_stats(value))

    def test_raw_output_is_not_forwarded_as_metrics(self):
        worker = Worker.__new__(Worker)
        entry = {'encoder': 'vaapi'}
        with patch('worker.emit') as emit:
            worker.sender_line('fixture', entry, 'DOUBLETAKE_VIDEO_STATS ' + json.dumps({**self.stats, 'secret': 'private'}))
            emit.assert_called_once_with('performance', tv_id='fixture', data=self.stats, encoder='vaapi')
            emit.reset_mock()
            worker.sender_line('fixture', entry, 'DOUBLETAKE_VIDEO_STATS private')
            worker.sender_line('fixture', entry, 'DOUBLETAKE_VIDEO_STATS ' + json.dumps({**self.stats, 'sent_fps': 'private'}))
            emit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
