"""Strict allowlist for numeric sender telemetry, never raw sender logs."""
import math


def video_stats(value):
    if not isinstance(value, dict) or value.get('version') != 1 or value.get('codec') not in {'h264', 'hevc'}:
        return None
    limits = {'window_ms': 600000, 'frames': 100000, 'sent_fps': 1000,
              'source_samples': 100000, 'source_age_mean_ms': 600000,
              'source_age_max_ms': 600000, 'write_mean_ms': 600000,
              'write_max_ms': 600000, 'late_frames': 100000, 'playout_lead_ms': 600000}
    result = {'version': 1, 'codec': value['codec']}
    for key, maximum in limits.items():
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= maximum:
            return None
        result[key] = number
    if result['window_ms'] <= 0 or result['late_frames'] > result['source_samples'] or result['source_samples'] > result['frames']:
        return None
    return result
