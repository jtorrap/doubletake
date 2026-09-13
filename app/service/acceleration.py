#!/usr/bin/python3
"""GPU setup and bounded diagnostics; never inspect page URLs or user input."""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def drm_nodes(directory=Path('/dev/dri')):
    nodes = []
    for path in sorted(directory.glob('*')):
        if re.fullmatch(r'(card|renderD)\d+', path.name):
            try:
                if stat.S_ISCHR(path.lstat().st_mode):
                    nodes.append(path)
            except OSError:
                pass
    return nodes


def drm_groups():
    # The host owns these devices. Grant only their existing groups, without
    # changing host permissions or inheriting unrelated root groups.
    return sorted({path.stat().st_gid for path in drm_nodes()})


def render_nodes():
    return [path for path in drm_nodes() if path.name.startswith('renderD')
            and os.access(path, os.R_OK | os.W_OK)]


def browser_flags(enabled, available):
    if not enabled:
        return ['--disable-accelerated-video-decode']
    if not available:
        return []
    return ['--use-gl=angle', '--use-angle=vulkan',
            '--enable-features=AcceleratedVideoDecoder,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE',
            '--ignore-gpu-blocklist']


def va_capabilities():
    nodes = render_nodes()
    result = {'render_nodes': [p.name for p in nodes], 'vaapi_ready': False, 'vaapi_profiles': []}
    if nodes:
        try:
            probe = subprocess.run(['vainfo', '--display', 'drm', '--device', str(nodes[0])],
                                   capture_output=True, text=True, timeout=5)
            result['vaapi_ready'] = probe.returncode == 0
            result['vaapi_profiles'] = sorted(set(re.findall(
                r'VAProfile([A-Za-z0-9_]+)\s*:\s*VAEntrypointVLD', probe.stdout)))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def gpu_info(value):
    gpu = value.get('gpu', {})
    attributes = gpu.get('auxAttributes', {})
    def safe_string(value):
        return re.sub(r'[^A-Za-z0-9 ()_.,:/+-]', '', str(value))[:240]
    return {
        'video_decode_feature': safe_string(gpu.get('featureStatus', {}).get('video_decode', 'unknown')),
        'renderer': safe_string(attributes.get('glRenderer', 'unknown')),
        'browser_profiles': sorted({safe_string(item.get('profile', ''))
                                    for item in gpu.get('videoDecoding', [])}),
    }


def video_engine_counters(proc=Path('/proc')):
    # Container-local, same-user processes only. Deduplicate duplicated DRM
    # file descriptors by device/client ID. No command lines or filenames leave.
    clients = {}
    for process in proc.glob('[0-9]*'):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for info in (process / 'fdinfo').iterdir():
                try:
                    content = info.read_text()
                except OSError:
                    continue
                client = re.search(r'^drm-client-id:\s*(\d+)', content, re.M)
                device = re.search(r'^drm-pdev:\s*([\w:.]+)', content, re.M)
                engines = re.findall(r'^drm-engine-video(?:-\d+)?:\s*(\d+) ns', content, re.M)
                if client and device and engines:
                    clients[(device[1], client[1])] = sum(map(int, engines))
        except OSError:
            continue
    return clients


def main():
    enabled = os.environ.get('DOUBLETAKE_HARDWARE_DECODING', 'true') == 'true'
    nodes = render_nodes()
    flags = browser_flags(enabled, bool(nodes))
    if enabled and nodes:
        flags.append('--render-node-override=' + str(nodes[0]))
    os.execvp('google-chrome', ['google-chrome', *flags, *sys.argv[1:]])


if __name__ == '__main__':
    main()
