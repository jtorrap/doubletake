import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from performance import video_stats, audio_stats
from session import Session
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
            for invalid in [float('nan'), float('inf'), -1, 10**1000, 'private', True, None]:
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


class AudioPerformance(unittest.TestCase):
    def setUp(self):
        self.stats = dict(version=1, codec='aac_eld', window_ms=5000,
                          captured_frames=459, captured_fps=91.8,
                          sent_frames=450, sent_fps=90, packets_sent=900,
                          stale_dropped=7, startup_dropped=4, clock_rebases=1,
                          source_samples=459, capture_age_mean_ms=12.5,
                          capture_age_max_ms=95, playout_lead_ms=85)

    def test_audio_fields_are_numeric_bounded_and_strip_unknown_data(self):
        self.assertEqual(audio_stats({**self.stats, 'secret': 'private'}), self.stats)
        for field in self.stats.keys() - {'version', 'codec'}:
            for invalid in [float('nan'), float('inf'), -1, 600001, 10**1000, 'private', True, None]:
                self.assertIsNone(audio_stats({**self.stats, field: invalid}), (field, invalid))
        for invalid in [{}, {'codec': 'private'}, {'version': True}, {'version': 1.0},
                        {'captured_frames': 1.5}, {'source_samples': 460}, {'window_ms': 0}]:
            self.assertIsNone(audio_stats({**self.stats, **invalid}) if invalid else audio_stats({}))
        # Capture/send/drop can straddle a five-second report boundary.
        self.assertIsNotNone(audio_stats({**self.stats, 'sent_frames': 460, 'stale_dropped': 460}))

    def test_audio_marker_does_not_leak_raw_output_or_set_video_metrics(self):
        worker = Worker.__new__(Worker)
        entry = {'performance': {'sent_fps': 30}}
        with patch('worker.emit') as emit:
            worker.sender_line('fixture', entry, 'DOUBLETAKE_AUDIO_STATS ' + json.dumps({**self.stats, 'secret': 'private'}))
            emit.assert_called_once_with('audio_performance', tv_id='fixture', data=self.stats)
            self.assertEqual(entry['audio_performance'], self.stats)
            self.assertEqual(entry['performance'], {'sent_fps': 30})
            emit.reset_mock()
            worker.sender_line('fixture', entry, 'DOUBLETAKE_AUDIO_STATS Enter private')
            worker.sender_line('fixture', entry, 'DOUBLETAKE_AUDIO_STATS ' + json.dumps({**self.stats, 'codec': 'Enter private'}))
            emit.assert_not_called()

    def test_session_revalidates_audio_metrics_and_ignores_removed_tv(self):
        session = Session('/unused', {})
        session.runtime['receivers'] = {'one': {'state': 'sending'}, 'two': {'state': 'sending'}}
        session.audio_performance_event({'tv_id': 'one', 'data': {**self.stats, 'secret': 'private'}})
        self.assertEqual(session.state()['receivers']['one']['audio_performance'], self.stats)
        self.assertNotIn('audio_performance', session.state()['receivers']['two'])
        session.audio_performance_event({'tv_id': 'one', 'data': {**self.stats, 'sent_fps': 'private'}})
        self.assertEqual(session.state()['receivers']['one']['audio_performance'], self.stats)
        session.runtime['receivers'].pop('one')
        session.audio_performance_event({'tv_id': 'one', 'data': self.stats})
        self.assertNotIn('one', session.state()['receivers'])


if __name__ == '__main__':
    unittest.main()
