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
        if type(number) not in (int, float) or not 0 <= number <= maximum or not math.isfinite(number):
            return None
        result[key] = number
    if result['window_ms'] <= 0 or result['late_frames'] > result['source_samples'] or result['source_samples'] > result['frames']:
        return None
    return result


def audio_stats(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or
            value['version'] != 1 or value.get('codec') not in {'alac', 'aac_eld', 'unknown'}):
        return None
    counts = {'captured_frames', 'sent_frames', 'packets_sent', 'stale_dropped',
              'startup_dropped', 'clock_rebases', 'source_samples'}
    limits = {**{key: 500000 for key in counts},
              'window_ms': 600000, 'captured_fps': 1000, 'sent_fps': 1000,
              'capture_age_mean_ms': 600000, 'capture_age_max_ms': 600000,
              'playout_lead_ms': 600000}
    result = {'version': 1, 'codec': value['codec']}
    for key, maximum in limits.items():
        number = value.get(key)
        if (type(number) not in (int, float) or not 0 <= number <= maximum or
                not math.isfinite(number) or (key in counts and type(number) is not int)):
            return None
        result[key] = number
    if result['window_ms'] <= 0 or result['source_samples'] > result['captured_frames']:
        return None
    # A captured frame can be sent/dropped in the next reporting window, and
    # startup/stale drops overlap. Do not impose false sum/count equalities.
    return result
