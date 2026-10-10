#!/usr/bin/env python3
"""Read native route count or GUID list on TCP 2053; never changes routes."""
import argparse
import json
import socket
import struct
import sys
import time

from common import FrameReader, port_arg, timeout_arg, utc_now


def frame(body):
    return struct.pack('<I', len(body)) + body


def count_request(sequence):
    return frame(struct.pack('<II', 0xf014e, sequence))


def list_request(sequence):
    # CRouteFacade defaults: start=0, max=50, no filters, zero GUIDs.
    params = struct.pack('<II8sIII8sI', 0, 0, bytes(8), 0, 50, 0, bytes(8), 0)
    return b''.join(map(frame, [
        struct.pack('<II', 0xf0140, sequence),
        struct.pack('<II8sI', 0xf0200, sequence, bytes(8), 26),
        struct.pack('<III', 0xf0201, sequence, len(params)) + params,
        struct.pack('<II8s', 0xf0202, sequence, bytes(8)),
    ]))


def expect(reader, command, sequence, size=None):
    body = reader.read()
    if len(body) < 8 or struct.unpack_from('<II', body) != (command, sequence):
        raise ValueError('Unexpected route command or sequence')
    if size is not None and len(body) != size:
        raise ValueError('Unexpected route frame length')
    return body


def read_count(reader, sequence):
    return struct.unpack_from('<I', expect(reader, 0xf004a, sequence, 12), 8)[0]


def read_list(reader, sequence):
    status, = struct.unpack_from('<I', expect(reader, 0xf0040, sequence, 12), 8)
    if status != 0x40000:
        raise ValueError(f'Route list failed: status 0x{status:08x}')
    start = expect(reader, 0xf0200, sequence, 20)
    transaction = start[8:16]
    if struct.unpack_from('<I', start, 16)[0] != 25:
        raise ValueError('Unexpected route-list serialization type')
    data = bytearray()
    while True:
        body = reader.read()
        if len(body) < 8:
            raise ValueError('Short route transaction')
        command, seq = struct.unpack_from('<II', body)
        if seq != sequence:
            raise ValueError('Route sequence mismatch')
        if command == 0xf0202:
            if len(body) != 16 or body[8:] != transaction:
                raise ValueError('Route transaction ID mismatch')
            break
        if command != 0xf0201 or len(body) < 12:
            raise ValueError('Unexpected route transaction order')
        count, = struct.unpack_from('<I', body, 8)
        if count != len(body) - 12 or len(data) + count > 404:
            raise ValueError('Invalid route-list chunk length')
        data.extend(body[12:])
    if len(data) < 4:
        raise ValueError('Missing route-list count')
    count, = struct.unpack_from('<I', data)
    if count > 50 or len(data) != 4 + count * 8:
        raise ValueError('Route GUID count/length mismatch')
    # Preserve wire bytes; do not guess UUID endian/format conventions.
    return [data[4+i*8:12+i*8].hex() for i in range(count)]


def query(host, port, operation, timeout):
    with socket.create_connection((host, port), timeout) as sock:
        sock.sendall(count_request(1) if operation == 'count' else list_request(1))
        reader = FrameReader(sock, time.monotonic() + timeout)
        value = read_count(reader, 1) if operation == 'count' else read_list(reader, 1)
    return {'state': 'retrieved', 'collected_utc': utc_now(), operation: value}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=port_arg, default=2053)
    parser.add_argument('--timeout', type=timeout_arg, default=3)
    parser.add_argument('operation', choices=('count', 'list'))
    args = parser.parse_args()
    try:
        result = query(args.host, args.port, args.operation, args.timeout)
    except (OSError, EOFError, ValueError) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
