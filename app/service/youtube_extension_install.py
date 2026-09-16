"""Install the bundled YouTube extension using Chrome's Linux CRX support.

The signing key belongs to this installation and is never part of the image,
browser profile, public metadata, or extension ZIP. Chrome's native-messaging
manifest trusts only the resulting extension ID.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import zipfile

from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15


HOST_NAME = 'com.doubletake.youtube'
APP_DIRECTORY = Path('/opt/browser-app')
INSTALL_METADATA = APP_DIRECTORY / 'youtube-extension-install.json'
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _directory(directory, mode):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('youtube_install_directory_invalid')
    directory.mkdir(parents=True, exist_ok=True, mode=mode)
    details = directory.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise ValueError('youtube_install_directory_invalid')
    directory.chmod(mode)
    return directory


def _write(path, content, mode):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('youtube_install_file_invalid')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _signing_key(directory):
    directory = _directory(directory, 0o700)
    path = directory / 'key.pem'
    if path.is_symlink():
        raise ValueError('youtube_signing_key_invalid')
    if path.exists():
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_size > 16384):
                raise ValueError('youtube_signing_key_invalid')
            key = RSA.import_key(stream.read(16385))
        if not key.has_private() or key.size_in_bits() < 2048:
            raise ValueError('youtube_signing_key_invalid')
        path.chmod(0o600)
        return key
    key = RSA.generate(2048)
    _write(path, key.export_key(format='PEM', pkcs=8), 0o600)
    return key


def _varint(value):
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number, value):
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _manifest(source, public_key):
    path = source / 'manifest.json'
    if path.is_symlink() or path.stat().st_size > 32768:
        raise ValueError('youtube_extension_manifest_invalid')
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get('manifest_version') != 3:
        raise ValueError('youtube_extension_manifest_invalid')
    version = manifest.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){0,3}', version):
        raise ValueError('youtube_extension_version_invalid')
    parts = [int(part) for part in version.split('.')]
    if not any(parts) or max(parts) > 65535:
        raise ValueError('youtube_extension_version_invalid')
    permissions = manifest.get('permissions', [])
    if (not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions)
            or 'nativeMessaging' not in permissions or not set(permissions) <= {'nativeMessaging', 'storage'}
            or manifest.get('host_permissions') != ['https://www.youtube.com/*']
            or 'externally_connectable' in manifest or 'optional_host_permissions' in manifest):
        raise ValueError('youtube_extension_permissions_invalid')
    scripts = manifest.get('content_scripts', [])
    if not isinstance(scripts, list) or not scripts:
        raise ValueError('youtube_extension_permissions_invalid')
    for script in scripts:
        if (not isinstance(script, dict) or script.get('matches') != ['https://www.youtube.com/*']
                or script.get('all_frames', False) or script.get('world', 'ISOLATED') != 'ISOLATED'):
            raise ValueError('youtube_extension_permissions_invalid')
    manifest['key'] = base64.b64encode(public_key).decode('ascii')
    return manifest


def build_crx(source_directory, key):
    """Return deterministic CRX3 bytes, extension ID and bundled version."""
    source = Path(source_directory)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('youtube_extension_source_invalid')
    public_key = key.public_key().export_key(format='DER')
    digest = hashlib.sha256(public_key).digest()[:16]
    extension_id = ''.join(chr(ord('a') + int(nibble, 16)) for nibble in digest.hex())
    manifest = _manifest(source, public_key)
    archive = io.BytesIO()
    total = 0
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zipped:
        for path in sorted(source.rglob('*')):
            if path.is_symlink():
                raise ValueError('youtube_extension_source_invalid')
            if path.is_dir():
                continue
            if not path.is_file() or path.suffix.lower() in {'.pem', '.key', '.crx'}:
                raise ValueError('youtube_extension_source_invalid')
            total += path.stat().st_size
            if total > MAX_SOURCE_BYTES:
                raise ValueError('youtube_extension_source_too_large')
            name = path.relative_to(source).as_posix()
            content = _json_bytes(manifest) if name == 'manifest.json' else path.read_bytes()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100444 << 16
            zipped.writestr(entry, content)
    zip_bytes = archive.getvalue()
    signed_header = _field(1, digest)
    signed = b'CRX3 SignedData\x00' + struct.pack('<I', len(signed_header)) + signed_header + zip_bytes
    # Chromium's actual creator/verifier use RSA PKCS#1 v1.5 with SHA-256,
    # despite the historical PSS comment in crx3.proto.
    signature = pkcs1_15.new(key).sign(SHA256.new(signed))
    proof = _field(1, public_key) + _field(2, signature)
    header = _field(2, proof) + _field(10000, signed_header)
    return b'Cr24' + struct.pack('<II', 3, len(header)) + header + zip_bytes, extension_id, manifest['version']


def install_extension(state_directory='/data/doubletake', source_directory=APP_DIRECTORY / 'youtube_extension',
                      external_directory='/opt/google/chrome/extensions',
                      native_directory='/etc/opt/chrome/native-messaging-hosts',
                      metadata_path=INSTALL_METADATA, host_path=APP_DIRECTORY / 'youtube_native_host.py'):
    """Run once at container startup, before dropping root or opening Chrome."""
    metadata_path = Path(metadata_path)
    output_directory = _directory(metadata_path.parent, 0o755)
    external_directory = _directory(external_directory, 0o755)
    native_directory = _directory(native_directory, 0o755)
    key = _signing_key(Path(state_directory) / 'youtube-signing')
    crx, extension_id, version = build_crx(source_directory, key)
    package = output_directory / 'youtube-extension.crx'
    _write(package, crx, 0o644)
    metadata = {'extension_id': extension_id, 'version': version}
    _write(metadata_path, _json_bytes(metadata), 0o644)
    _write(native_directory / (HOST_NAME + '.json'), _json_bytes({
        'name': HOST_NAME, 'description': 'Doubletake YouTube controls',
        'path': str(Path(host_path).absolute()), 'type': 'stdio',
        'allowed_origins': ['chrome-extension://' + extension_id + '/'],
    }), 0o644)
    _write(external_directory / (extension_id + '.json'), _json_bytes({
        'external_crx': str(package.absolute()), 'external_version': version,
    }), 0o644)
    return metadata


if __name__ == '__main__':
    try:
        install_extension()
    except Exception:
        # Installer failures must never stringify keys or source/manifest data.
        raise SystemExit('youtube_extension_install_failed')
