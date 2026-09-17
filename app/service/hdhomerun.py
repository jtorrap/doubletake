"""Local, read-only HDHomeRun discovery and channel catalog.

DeviceAuth and supplied stream URLs never enter persistent/public state. We
accept only local devices and their /auto/v<channel> streams on port 5004.
"""
import asyncio
import copy
import ipaddress
import json
from pathlib import Path
import re
import socket
import struct
import time
from urllib.parse import urlsplit
import zlib
from aiohttp import ClientSession, ClientTimeout
from model import atomic_json, identifier, receiver_host

DEVICE = re.compile(r'^[0-9A-F]{8}$')
CHANNEL = re.compile(r'^[0-9]{1,5}(?:\.[0-9]{1,3})?$')


class ChannelError(ValueError):
    messages = {
        'busy': 'All HDHomeRun tuners are in use. Stop another stream and try again.',
        'no_media': 'The HDHomeRun received no video for this channel. Try another channel or check reception.',
        'protected': 'This channel requires content protection and cannot play here.',
        'unknown': 'The HDHomeRun no longer has this channel. Refresh the lineup.',
        'stream': 'Channel playback could not continue. Check reception and try again.',
        'startup': 'The HDHomeRun channel could not start. Check the device and try again.',
        'cancelled': 'The channel change was cancelled.',
    }
    def __init__(self, code='startup'):
        self.code = code if code in self.messages else 'startup'
        self.safe_message = self.messages[self.code]
        super().__init__(self.safe_message)


def local_ipv4(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
        raise ValueError('Use a local HDHomeRun address')
    return str(address)


def discovery_packet():
    payload = b'\x01\x04' + struct.pack('>I', 1) + b'\x02\x04' + b'\xff' * 4
    packet = struct.pack('>HH', 2, len(payload)) + payload
    return packet + struct.pack('<I', zlib.crc32(packet))


def valid_reply(packet):
    return (8 <= len(packet) <= 1460 and struct.unpack('>HH', packet[:4]) == (3, len(packet) - 8)
            and struct.unpack('<I', packet[-4:])[0] == zlib.crc32(packet[:-4]))


def discover_hosts():
    found = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        sock.sendto(discovery_packet(), ('255.255.255.255', 65001))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(found) < 8:
            try:
                packet, (host, port) = sock.recvfrom(1461)
                if port == 65001 and valid_reply(packet):
                    found.add(local_ipv4(host))
            except (socket.timeout, ValueError):
                continue
    return found


async def resolve_host(value):
    host = receiver_host(value)
    answers = await asyncio.get_running_loop().getaddrinfo(host, 80, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return local_ipv4(answers[0][4][0])


def normalize_device(host, metadata, lineup):
    device_id = str(metadata.get('DeviceID', '')).upper()
    if not DEVICE.fullmatch(device_id) or not isinstance(lineup, list) or len(lineup) > 1000:
        raise ValueError('Invalid HDHomeRun response')
    channels = {}
    for item in lineup:
        number = item.get('GuideNumber')
        if not isinstance(number, str) or not CHANNEL.fullmatch(number):
            continue
        url = urlsplit(item.get('URL', ''))
        # Strictly validate the manufacturer-provided mapping, then reconstruct
        # it from the verified address. No redirect, auth, query or tuner lock.
        if (url.scheme != 'http' or url.hostname != host or url.port != 5004 or
                url.path != '/auto/v' + number or url.query or url.fragment or url.username or url.password):
            continue
        video, audio = str(item.get('VideoCodec', '')).upper(), str(item.get('AudioCodec', '')).upper()
        protected = bool(item.get('DRM')) or 'drm' in str(item.get('Tags', '')).lower().split(',')
        reason = 'Protected channel' if protected else (
            'ATSC 3.0 is not supported yet' if video == 'HEVC' or audio == 'AC4' else (
                '' if video in {'MPEG2', 'H264'} and audio in {'AC3', 'MPEG', 'AAC'} else 'Unsupported or unknown channel format'))
        name = ''.join(c for c in str(item.get('GuideName', number)) if ord(c) >= 32)[:80]
        channels[number] = {'number': number, 'name': name, 'supported': not reason, 'reason': reason,
                            'video_codec': video, 'audio_codec': audio}
    return {'id': device_id, 'host': host, 'name': str(metadata.get('FriendlyName', 'HDHomeRun'))[:80],
            'model': str(metadata.get('ModelNumber', ''))[:40], 'channels': list(channels.values())}


async def fetch_device(host):
    async with ClientSession(timeout=ClientTimeout(total=8), trust_env=False) as client:
        values = []
        for path in ('discover.json', 'lineup.json'):
            async with client.get(f'http://{host}/{path}', allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('HDHomeRun is unavailable')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(raw) + len(chunk) > 1024 * 1024:
                        raise ValueError('HDHomeRun response too large')
                    raw.extend(chunk)
                values.append(json.loads(raw))
        return normalize_device(host, *values)


class ChannelCatalog:
    def __init__(self, directory):
        self.path = Path(directory) / 'channels.json'
        self.data = {'version': 1, 'devices': [], 'favorites': [], 'selected': {}}
        self.checked = {}
        self.lock = asyncio.Lock()
        if self.path.exists():
            value = json.loads(self.path.read_text())
            if value.get('version') != 1 or not isinstance(value.get('devices'), list):
                raise RuntimeError('Channel settings could not be read; the original file is preserved')
            self.data = value

    def public(self):
        return copy.deepcopy(self.data)

    def save(self):
        atomic_json(self.path, self.data)

    async def refresh(self, host=None):
        async with self.lock:
            hosts = {await resolve_host(host)} if host else {device['host'] for device in self.data['devices']}
            if not host:
                with_socket = await asyncio.to_thread(discover_hosts)
                hosts.update(with_socket)
            results = await asyncio.gather(*(fetch_device(value) for value in sorted(hosts)[:8]), return_exceptions=True)
            found = {item['id']: item for item in self.data['devices']}
            successful = 0
            for value in results:
                if isinstance(value, dict):
                    found[value['id']] = value
                    self.checked[value['id']] = time.monotonic()
                    successful += 1
            if host and not successful:
                raise ValueError('HDHomeRun could not be reached')
            self.data['devices'] = list(found.values())[:8]
            self.save()
            return self.public()

    def get(self, device_id, number, *, supported=True):
        if not isinstance(device_id, str) or not DEVICE.fullmatch(device_id) or not isinstance(number, str) or not CHANNEL.fullmatch(number):
            raise ValueError('Choose a channel')
        device = next((d for d in self.data['devices'] if d['id'] == device_id), None)
        channel = next((c for c in (device or {}).get('channels', []) if c['number'] == number), None)
        if not channel or (supported and not channel['supported']):
            raise ValueError('Channel is unavailable or unsupported')
        return device, channel

    async def source(self, device_id, number):
        device, _ = self.get(device_id, number)
        if time.monotonic() - self.checked.get(device_id, -1000) > 60:
            # Recheck the lineup before taking over any TV. No tuner is opened.
            fresh = await fetch_device(device['host'])
            if fresh['id'] != device_id:
                raise ValueError('The HDHomeRun address now belongs to another device')
            self.data['devices'] = [fresh if d['id'] == device_id else d for d in self.data['devices']]
            self.checked[device_id] = time.monotonic()
            self.save()
        device, channel = self.get(device_id, number)
        return {'kind': 'hdhomerun', 'device_id': device_id, 'channel': number,
                'label': number + ' ' + channel['name'], 'url': f"http://{device['host']}:5004/auto/v{number}"}

    def options(self):
        return {f"{c['number']} {c['name']} · {d['id']}": (d['id'], c['number'])
                for d in self.data['devices'] for c in d['channels'] if c['supported']}

    def select(self, tv_id, option):
        identifier(tv_id)
        if option not in self.options():
            raise ValueError('Choose an available channel')
        self.data['selected'][tv_id] = option
        self.save()

    def selected(self, tv_id):
        return self.options().get(self.data['selected'].get(tv_id))

    def favorite(self, device_id, number, enabled):
        self.get(device_id, number, supported=False)
        if type(enabled) is not bool:
            raise ValueError('Invalid favorite')
        key = device_id + ':' + number
        favorites = set(self.data['favorites'])
        (favorites.add if enabled else favorites.discard)(key)
        self.data['favorites'] = sorted(favorites)
        self.save()
