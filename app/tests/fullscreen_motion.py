"""Synthetic CI-only full-screen motion source and received-picture analysis.

The frame number is burned into the compressed source video, so a fresh JS
overlay or repeated encoder output cannot disguise dropped browser pictures.
Exported media stays in the integration test's temporary directory.
"""
import math
import struct
import subprocess

PAGE = b'''<!doctype html><meta charset="utf-8"><title>Synthetic full-screen motion</title>
<style>html,body{margin:0;overflow:hidden;background:black}video{position:fixed;inset:0;width:100vw;height:100vh;object-fit:fill}</style>
<video autoplay playsinline src="/motion.mp4"></video>
<script>
const v=document.querySelector('video');let presented=0;
function frame(now,meta){presented=meta.presentedFrames;v.requestVideoFrameCallback(frame)}
v.requestVideoFrameCallback(frame);
setInterval(()=>{const q=v.getVideoPlaybackQuality();fetch('/motion-metrics',{method:'POST',body:JSON.stringify({
 presented,total:q.totalVideoFrames,dropped:q.droppedVideoFrames,time:v.currentTime,
 width:v.videoWidth,height:v.videoHeight,css_width:innerWidth,css_height:innerHeight})})},250);
</script>'''


def create_clip(path):
    # The white reference and twelve binary cells have wide centers that remain
    # unambiguous through two lossy encodes. ID 0..719 never wraps in the sample.
    filters = ['drawbox=x=0:y=0:w=640:h=96:color=black:t=fill',
               'drawbox=x=16:y=24:w=24:h=48:color=white:t=fill']
    filters += [f"drawbox=x={64+40*bit}:y=24:w=24:h=48:color=white:t=fill:enable='bitand(n,{1<<bit})'"
                for bit in range(12)]
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=30',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                    '-t', '24', '-vf', ','.join(filters), '-c:v', 'libx264',
                    '-preset', 'veryfast', '-threads', '2', '-crf', '20',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2',
                    '-movflags', '+faststart', str(path)], check=True, timeout=120)


def read_capture(path):
    """Snapshot complete records; the final record can still be being written."""
    records = []
    with path.open('rb') as source:
        assert source.read(8) == b'DTVID001', 'Wrong test video capture format'
        while header := source.read(21):
            if len(header) != 21:
                break
            kind, pts, arrival, size = struct.unpack('>BQQI', header)
            assert size <= 32 * 1024 * 1024, 'Oversized test video record'
            payload = source.read(size)
            if len(payload) != size:
                break
            records.append((kind, pts, arrival / 1e9, payload))
    return records


def frame_count(path):
    return sum(record[0] == 0 for record in read_capture(path))


def _codec_nals(payload):
    assert len(payload) >= 7 and payload[0] == 1 and payload[4] & 3 == 3, 'Expected H.264 avcC'
    offset = 6
    for count in (payload[5] & 31, None):
        if count is None:
            count, offset = payload[offset], offset + 1
        for _ in range(count):
            size = int.from_bytes(payload[offset:offset+2], 'big')
            offset += 2
            assert size and offset + size <= len(payload), 'Truncated avcC'
            yield payload[offset:offset+size]
            offset += size


def _frame_nals(payload):
    offset = 0
    while offset < len(payload):
        assert offset + 4 <= len(payload), 'Truncated AVCC NAL length'
        size = int.from_bytes(payload[offset:offset+4], 'big')
        offset += 4
        assert size and offset + size <= len(payload), 'Truncated AVCC frame'
        yield payload[offset:offset+size]
        offset += size


def marker_id(row):
    white, black = row[28], row[52]
    if white - black < 120:
        return None
    threshold = (white + black) / 2
    return sum(1 << bit for bit in range(12) if row[76+40*bit] > threshold)


def _percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def analyze_capture(path, first_frame, work):
    records = read_capture(path)
    frames = [record for record in records if record[0] == 0]
    assert len(frames) > first_frame + 100, 'Too few received pictures for motion analysis'
    encoded = work / 'received-motion.h264'
    with encoded.open('wb') as output:
        for kind, _, _, payload in records:
            if kind not in (0, 1):
                continue
            if kind == 0:
                output.write(b'\x00\x00\x00\x01\x09\xf0')  # Access-unit delimiter.
            for nal in _codec_nals(payload) if kind == 1 else _frame_nals(payload):
                output.write(b'\x00\x00\x00\x01' + nal)
    decoded = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                              '-threads', '2', '-i', str(encoded),
                              '-vf', 'format=gray,crop=640:1:0:48',
                              '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                             check=True, capture_output=True, timeout=60).stdout
    assert len(decoded) == len(frames) * 640, ('Decoded frame count differs from received access units', len(decoded)//640, len(frames))
    ids = [marker_id(decoded[i*640:(i+1)*640]) for i in range(first_frame, len(frames))]
    arrivals = [record[2] for record in frames[first_frame:]]
    assert all(value is not None for value in ids), 'Received picture lost the source frame marker'
    assert all(a <= b for a, b in zip(ids, ids[1:])), 'Source picture IDs moved backwards'
    changes = [0] + [index for index in range(1, len(ids)) if ids[index] != ids[index-1]]
    elapsed = arrivals[-1] - arrivals[0]
    fresh_fps = (len(changes) - 1) / elapsed
    gaps = [b-a for a, b in zip(arrivals, arrivals[1:])]
    changes_at = [arrivals[index] for index in changes] + [arrivals[-1]]
    freezes = [b-a for a, b in zip(changes_at, changes_at[1:])]
    result = {'seconds': round(elapsed, 3), 'received_frames': len(ids),
              'distinct_source_frames': len(changes), 'distinct_fps': round(fresh_fps, 2),
              'duplicate_fraction': round(1-len(changes)/len(ids), 4),
              'p95_arrival_gap_ms': round(_percentile(gaps, .95)*1000, 2),
              'max_arrival_gap_ms': round(max(gaps)*1000, 2),
              'max_freeze_ms': round(max(freezes)*1000, 2),
              'source_frame_first': ids[0], 'source_frame_last': ids[-1]}
    assert elapsed >= 10, result
    assert fresh_fps >= 24, result
    assert max(freezes) <= .200, result
    assert _percentile(gaps, .95) <= .070 and max(gaps) <= .250, result
    return result
