import base64
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
import zipfile

from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import youtube_extension_install as installer


def protobuf_fields(data):
    """Decode the length-delimited fields independently from the CRX writer."""
    offset = 0

    def integer():
        nonlocal offset
        result = shift = 0
        while True:
            item = data[offset]
            offset += 1
            result |= (item & 127) << shift
            if item < 128:
                return result
            shift += 7

    fields = {}
    while offset < len(data):
        tag = integer()
        if tag & 7 != 2:
            raise ValueError('unsupported fixture field')
        size = integer()
        fields[tag >> 3] = data[offset:offset + size]
        offset += size
    return fields


class YouTubeExtensionInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSA.generate(2048)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.manifest = {
            'manifest_version': 3, 'name': 'Doubletake YouTube', 'version': '1.0.0',
            'permissions': ['nativeMessaging'],
            'host_permissions': ['https://www.youtube.com/*'],
            'background': {'service_worker': 'background.js'},
            'content_scripts': [{'matches': ['https://www.youtube.com/*'], 'js': ['content.js']}],
        }
        self.save_manifest()
        (self.source / 'background.js').write_text('chrome.runtime.connectNative("com.doubletake.youtube");')
        (self.source / 'content.js').write_text('/* synthetic trusted fixture */')

    def save_manifest(self):
        (self.source / 'manifest.json').write_text(json.dumps(self.manifest))

    def install(self):
        return installer.install_extension(
            state_directory=self.root / 'state', source_directory=self.source,
            external_directory=self.root / 'external', native_directory=self.root / 'native',
            metadata_path=self.root / 'public' / 'youtube-extension-install.json',
            host_path='/opt/browser-app/youtube_native_host.py')

    def test_crx_signature_and_identity_match_chromium_format(self):
        crx, identity, version = installer.build_crx(self.source, self.key)
        self.assertEqual(crx[:4], b'Cr24')
        format_version, size = struct.unpack('<II', crx[4:12])
        self.assertEqual(format_version, 3)
        header = protobuf_fields(crx[12:12 + size])
        proof = protobuf_fields(header[2])
        signed = protobuf_fields(header[10000])
        archive = crx[12 + size:]
        public_key = RSA.import_key(proof[1])
        self.assertFalse(public_key.has_private())
        self.assertEqual(signed[1], hashlib.sha256(proof[1]).digest()[:16])
        expected_id = ''.join('abcdefghijklmnop'[int(letter, 16)] for letter in signed[1].hex())
        self.assertEqual(identity, expected_id)
        self.assertEqual(version, '1.0.0')
        signature_input = b'CRX3 SignedData\0' + struct.pack('<I', len(header[10000])) + header[10000] + archive
        pkcs1_15.new(public_key).verify(SHA256.new(signature_input), proof[2])
        with self.assertRaises(ValueError):
            pkcs1_15.new(public_key).verify(SHA256.new(signature_input[:-1] + bytes([signature_input[-1] ^ 1])), proof[2])
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertEqual(sorted(zipped.namelist()), ['background.js', 'content.js', 'manifest.json'])
            manifest = json.loads(zipped.read('manifest.json'))
            self.assertEqual(base64.b64decode(manifest['key']), proof[1])
            self.assertNotIn('PRIVATE KEY', zipped.read('manifest.json').decode())
        self.assertEqual(installer.build_crx(self.source, self.key)[0], crx)

    def test_install_persists_private_identity_and_updates_exact_origins(self):
        first = self.install()
        key_path = self.root / 'state' / 'youtube-signing' / 'key.pem'
        original_key = key_path.read_bytes()
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key_path.parent.stat().st_mode), 0o700)
        metadata = self.root / 'public' / 'youtube-extension-install.json'
        self.assertEqual(json.loads(metadata.read_text()), first)
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o644)
        host = json.loads((self.root / 'native' / 'com.doubletake.youtube.json').read_text())
        self.assertEqual(host['allowed_origins'], ['chrome-extension://' + first['extension_id'] + '/'])
        self.assertEqual(host['path'], '/opt/browser-app/youtube_native_host.py')
        self.assertEqual(host['type'], 'stdio')
        external = self.root / 'external' / (first['extension_id'] + '.json')
        registration = json.loads(external.read_text())
        self.assertEqual(registration['external_version'], '1.0.0')
        self.assertEqual(Path(registration['external_crx']), self.root / 'public' / 'youtube-extension.crx')
        original_package = Path(registration['external_crx']).read_bytes()
        self.assertEqual(self.install(), first)
        self.assertEqual(Path(registration['external_crx']).read_bytes(), original_package)
        self.manifest['version'] = '1.0.1'
        self.save_manifest()
        updated = self.install()
        self.assertEqual(updated['extension_id'], first['extension_id'])
        self.assertEqual(updated['version'], '1.0.1')
        self.assertEqual(key_path.read_bytes(), original_key)
        self.assertEqual(json.loads(external.read_text())['external_version'], '1.0.1')
        self.assertEqual(len(list((self.root / 'external').iterdir())), 1)

    def test_rejects_broader_permissions_versions_and_source_links(self):
        mutations = [
            ('host_permissions', ['<all_urls>']), ('permissions', ['nativeMessaging', 'storage', 'cookies']),
            ('externally_connectable', {'matches': ['https://www.youtube.com/*']}),
            ('content_scripts', [{'matches': ['https://www.youtube.com/*'], 'world': 'MAIN'}]),
            ('version', '0.0.0'), ('version', '1.65536'), ('version', '../1'),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                original = dict(self.manifest)
                self.manifest[key] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    installer.build_crx(self.source, self.key)
                self.manifest = original
        self.save_manifest()
        (self.source / 'linked.js').symlink_to(self.source / 'content.js')
        with self.assertRaisesRegex(ValueError, 'source_invalid'):
            installer.build_crx(self.source, self.key)

    def test_invalid_existing_key_and_symlink_are_preserved(self):
        signing = self.root / 'state' / 'youtube-signing'
        signing.mkdir(parents=True, mode=0o700)
        key = signing / 'key.pem'
        key.write_text('invalid private fixture')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(key.read_text(), 'invalid private fixture')
        key.unlink()
        other = self.root / 'untouched'
        other.write_text('private fixture')
        key.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'signing_key_invalid'):
            self.install()
        self.assertEqual(other.read_text(), 'private fixture')

    def test_actual_bundled_manifest_can_be_packaged(self):
        source = Path(__file__).resolve().parents[1] / 'service' / 'youtube_extension'
        package, identity, version = installer.build_crx(source, self.key)
        self.assertEqual(len(identity), 32)
        self.assertEqual(version, json.loads((source / 'manifest.json').read_text())['version'])
        self.assertTrue(package.startswith(b'Cr24'))
