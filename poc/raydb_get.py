#!/usr/bin/env python3
"""Read one exact RayDB path with HELLO + GET; never sends SET or SUBSCRIBE."""
import argparse
import json
import math
import socket
import struct
import sys
import time

from common import FrameReader, port_arg, timeout_arg, utc_now


def request(opcode, path):
    if opcode not in (0, 7):
        raise ValueError('Only HELLO and GET are supported')
    data = path.encode('ascii')
    if not data or len(data) > 4096 or any(b < 32 or b == 127 for b in data):
        raise ValueError('Invalid path')
    flags = bytes((0, 1, 1)) if opcode == 7 else bytes(3)
    body = struct.pack('<IBQ', 1, opcode, len(data)) + data + flags + struct.pack('<IiI', 7, 0, 0)
    return struct.pack('<I', len(body)) + body


def decode_update(body):
    if len(body) < 24:
        raise ValueError('Short RayDB UPDATE')
    version, opcode, size = struct.unpack_from('<IBQ', body)
    if version != 1 or opcode != 4 or size > len(body) - 24:
        raise ValueError('Invalid RayDB header or path length')
    path = body[13:13 + size].decode('ascii')
    offset = 13 + size
    flags = body[offset:offset + 3]
    typ, = struct.unpack_from('<I', body, offset + 3)
    payload = body[offset + 7:-4]
    if typ == 11:
        if len(payload) < 8:
            raise ValueError('Short string length')
        count, = struct.unpack_from('<Q', payload)
        if count != len(payload) - 8:
            raise ValueError('String length mismatch')
        value = payload[8:].decode('utf-8')
    else:
        formats = {0: '<?', 3: '<I', 7: '<i', 9: '<f', 10: '<d'}
        if typ not in formats:
            raise ValueError(f'Unsupported RayDB variant type {typ}; no guessed decoding')
        fmt = formats[typ]
        if len(payload) != struct.calcsize(fmt):
            raise ValueError('Variant payload length mismatch')
        value, = struct.unpack(fmt, payload)
    nonfinite = isinstance(value, float) and not math.isfinite(value)
    return {'path': path, 'type': typ, 'value': None if nonfinite else value,
            'state': 'nonfinite' if nonfinite else 'retrieved',
            'flags': list(flags), 'counter': struct.unpack_from('<I', body, len(body) - 4)[0]}


def query(host, port, path, timeout):
    hello, get = request(7, 'RayDBRemoteClient'), request(0, path)
    with socket.create_connection((host, port), timeout) as sock:
        sock.sendall(hello)
        time.sleep(0.1)
        sock.sendall(get)
        reader = FrameReader(sock, time.monotonic() + timeout)
        while True:
            body = reader.read()
            if len(body) < 13:
                raise ValueError('Short RayDB envelope')
            if body[4] != 4:
                continue  # HELLO/control responses are not values; byte/time caps still apply.
            result = decode_update(body)
            if result['path'] == path:
                result['collected_utc'] = utc_now()
                return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=port_arg, default=23333)
    parser.add_argument('--path', required=True, help='Exact key, including your own identity component when required')
    parser.add_argument('--timeout', type=timeout_arg, default=4)
    args = parser.parse_args()
    try:
        result = query(args.host, args.port, args.path, args.timeout)
    except (OSError, EOFError, ValueError) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['state'] == 'retrieved' else 1


if __name__ == '__main__':
    sys.exit(main())
