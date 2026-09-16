import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
from latency import target_latency_ms
from session import worker_environment


class LatencyConfiguration(unittest.TestCase):
    def test_option_and_environment_values_have_the_same_bounded_range(self):
        for value in [0, 250, 2000]:
            self.assertEqual(target_latency_ms(value), value)
            self.assertEqual(target_latency_ms(str(value)), value)
        for invalid in [True, False, 0.5, -1, 2001, None, [], {}, '-1', '2001',
                        '0.5', ' 250', 'private', '２５０', '1\n', '100000']:
            with self.assertRaisesRegex(ValueError, '^invalid_target_latency_ms$'):
                target_latency_ms(invalid)

    def test_latency_reaches_worker_without_controller_secrets(self):
        with patch.dict(os.environ, {'DOUBLETAKE_TARGET_LATENCY_MS': '250',
                                     'SUPERVISOR_TOKEN': 'private', 'MQTT_PASSWORD': 'private'}, clear=True):
            self.assertEqual(worker_environment(), {'DOUBLETAKE_TARGET_LATENCY_MS': '250'})


if __name__ == '__main__':
    unittest.main()
