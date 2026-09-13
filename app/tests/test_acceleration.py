import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from acceleration import browser_flags, drm_nodes, drm_groups, video_engine_counters
from worker import Worker


class Acceleration(unittest.TestCase):
    def test_only_real_drm_devices_grant_groups(self):
        def node(name, mode, gid):
            return SimpleNamespace(name=name, lstat=lambda: SimpleNamespace(st_mode=mode),
                                   stat=lambda: SimpleNamespace(st_gid=gid))
        render = node('renderD128', stat.S_IFCHR, 107)
        card = node('card0', stat.S_IFCHR, 44)
        directory = Mock()
        directory.glob.return_value = [render, card, node('renderD129', stat.S_IFLNK, 0),
                                       node('card1', stat.S_IFREG, 0), node('other', stat.S_IFCHR, 0)]
        # Glob is sorted by pathlib in production; fake nodes need ordering.
        with patch('acceleration.sorted', side_effect=lambda value: list(value)):
            self.assertEqual(drm_nodes(directory), [render, card])
        with patch('acceleration.drm_nodes', return_value=[render, card, render]):
            self.assertEqual(drm_groups(), [44, 107])

    def test_software_fallback_and_sandbox_preserved(self):
        self.assertEqual(browser_flags(True, False), [])
        self.assertEqual(browser_flags(False, True), ['--disable-accelerated-video-decode'])
        flags = browser_flags(True, True)
        self.assertIn('--use-angle=gl', flags)
        self.assertFalse(any('sandbox' in flag for flag in flags))

    def test_video_counters_deduplicate_shared_file_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, fd, client, ns in [('10','4',1,700), ('10','5',1,700), ('11','8',2,300)]:
                info = proc / pid / 'fdinfo' / fd
                info.parent.mkdir(parents=True, exist_ok=True)
                info.write_text(f'drm-pdev: 0000:00:02.0\ndrm-client-id: {client}\ndrm-engine-video: {ns} ns\ndrm-engine-render: 9000 ns\n')
            self.assertEqual(sum(video_engine_counters(proc).values()), 1000)

    def test_media_diagnostics_do_not_retain_page_data(self):
        worker = Worker.__new__(Worker)
        worker.media = {}
        worker.media_event({'method':'Media.playerPropertiesChanged', 'params':{
            'playerId':'1','properties':[
                {'name':'kFrameUrl','value':'https://private.example/secret'},
                {'name':'kVideoDecoderName','value':'GpuVideoDecoder'},
                {'name':'kIsPlatformVideoDecoder','value':'true'}]}})
        self.assertEqual(worker.media, {'1': {'decoder':'GpuVideoDecoder','platform_decoder':True}})
        self.assertNotIn('secret', json.dumps(worker.media))


if __name__ == '__main__':
    unittest.main()
