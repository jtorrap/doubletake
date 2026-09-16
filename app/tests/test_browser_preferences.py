import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from browser_preferences import prepare_profile


class BrowserPreferences(unittest.TestCase):
    def test_default_zoom_preserves_existing_profile_and_site_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Default' / 'Preferences'
            path.parent.mkdir()
            original = {'profile': {'fixture': 'retained'}, 'partition': {
                'default_zoom_level': {'x': 1.0, 'other_partition': 2.0},
                'per_host_zoom_levels': {'x': {'example.test': {'zoom_level': 2.0}}}}}
            path.write_text(json.dumps(original))
            prepare_profile(directory)
            original['partition']['default_zoom_level']['x'] = 0.0
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            prepare_profile(directory)
            self.assertEqual(json.loads(path.read_text()), original)

    def test_new_profile_and_invalid_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_profile(directory)
            path = Path(directory) / 'Default' / 'Preferences'
            self.assertEqual(json.loads(path.read_text())['partition']['default_zoom_level']['x'], 0.0)
            for content in ['{broken', '[]', '{"partition":null}', '{"partition":{"default_zoom_level":[]}}']:
                path.write_text(content)
                with self.assertRaises(ValueError):
                    prepare_profile(directory)
                self.assertEqual(path.read_text(), content)
