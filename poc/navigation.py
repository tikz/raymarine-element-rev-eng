#!/usr/bin/env python3
"""One-shot native TCP 2051 reads. No subscriptions, writes or stored captures."""
import argparse
import datetime
import json
import math
import socket
import struct
import sys
import time

from common import FrameReader, port_arg, timeout_arg, utc_now

# index, record type, payload format, scale, unit
ITEMS = {
    'position': (0x44, 0x3c, '<ii', 1e7, 'degrees'),
    'quality': (0x7d, 0x60, '<B', 1, 'raw code'),
    'hdop': (0x7f, 0x62, '<h', 100, 'dimensionless'),
    'satellites': (0x77, 0x5b, '<32B', 1, 'satellite IDs'),
    'utc_time': (0xee, 0x04, '<I', 10000, 'seconds since UTC midnight'),
    'utc_date': (0xef, 0x03, '<H', 1, 'UTC date'),
    'sog': (0x04, 0x01, '<H', 100, 'm/s'),
    'cog': (0x1a, 0x2a, '<H', 10000, 'degrees'),
    'depth': (0x09, 0x0e, '<I', 100, 'm'),
    'water_temperature': (0x0f, 0x12, '<H', 100, 'degrees C'),
    'altitude': (0x80, 0x63, '<i', 100, 'm above WGS84 ellipsoid'),
}


def request(sequence, index):
    return struct.pack('<IIII', 12, 0x100105, sequence, index)


def read_transaction(reader, sequence, index):
    status = reader.read()
    if len(status) != 16:
        raise ValueError('Invalid status frame length')
    command, seq, returned_index, result, storage = struct.unpack('<IIIHH', status)
    if (command, seq, returned_index) != (0x100003, sequence, index):
        raise ValueError('Status command, sequence or index mismatch')
    if result != 0:
        return {'status': result, 'storage': storage}
    start = reader.read()
    if len(start) != 20 or struct.unpack_from('<II', start) != (0x100200, sequence):
        raise ValueError('Invalid transaction start')
    transaction_id = start[8:16]
    chunks = bytearray()
    chunk_count = 0
    while True:
        body = reader.read()
        if len(body) < 8:
            raise ValueError('Short transaction frame')
        command, seq = struct.unpack_from('<II', body)
        if seq != sequence:
            raise ValueError('Transaction sequence mismatch')
        if command == 0x100202:
            if len(body) != 16 or body[8:16] != transaction_id or not chunk_count:
                raise ValueError('Invalid transaction end')
            break
        if command != 0x100201 or len(body) < 12:
            raise ValueError('Unexpected transaction command')
        size, = struct.unpack_from('<I', body, 8)
        if size != len(body) - 12:
            raise ValueError('Chunk length mismatch')
        chunks.extend(body[12:])
        chunk_count += 1
    if len(chunks) < 12:
        raise ValueError('Short item header')
    item_index, typ, size, reserved = struct.unpack_from('<IHIH', chunks)
    if item_index != index or reserved != 0 or len(chunks) < 24 + size:
        raise ValueError('Invalid item header or length')
    transport, timeout, source_size, reserved = struct.unpack_from('<IIHH', chunks, 12 + size)
    if reserved != 0 or len(chunks) != 24 + size + source_size:
        raise ValueError('Invalid transport metadata length')
    # Source bytes can identify a unit. Validate their length, but do not print them.
    return {'status': 0, 'storage': storage, 'type': typ,
            'payload': bytes(chunks[12:12 + size]), 'transport_type': transport,
            'timeout_seconds': timeout}


def decode_item(name, record):
    if record['status'] != 0:
        return None
    _, typ, fmt, scale, _ = ITEMS[name]
    if record['type'] != typ or len(record['payload']) != struct.calcsize(fmt):
        raise ValueError('Unexpected item type or payload length')
    values = struct.unpack(fmt, record['payload'])
    if name == 'position':
        lat, lon = (v / scale for v in values)
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('Position outside geographic bounds')
        return {'latitude': lat, 'longitude': lon}
    if name == 'satellites':
        return [v for v in values if v]
    value = values[0] / scale if scale != 1 else values[0]
    if name == 'utc_date':
        return str(datetime.date(1970, 1, 1) + datetime.timedelta(days=value))
    if name == 'utc_time' and not 0 <= value < 86400:
        raise ValueError('UTC time outside day')
    if name == 'cog':
        return math.degrees(value) % 360
    if name == 'water_temperature':
        return round(value - 273.15, 6)
    return value


def fix_state(rows):
    quality = rows.get('quality', {}).get('value')
    if quality == 0:
        return 'no_fix'
    if quality is None or rows.get('position', {}).get('value') is None:
        return 'unavailable'
    return 'fix_indicated'


def query(host, port, name, timeout):
    index = ITEMS[name][0]
    with socket.create_connection((host, port), timeout) as sock:
        sock.sendall(request(1, index))
        record = read_transaction(FrameReader(sock, time.monotonic() + timeout), 1, index)
    value = decode_item(name, record)
    return {'state': 'retrieved' if record['status'] == 0 else 'unavailable',
            'status': record['status'], 'value': value, 'unit': ITEMS[name][4],
            'collected_utc': utc_now(), 'transport_type': record.get('transport_type'),
            'timeout_seconds': record.get('timeout_seconds')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=port_arg, default=2051)
    parser.add_argument('--timeout', type=timeout_arg, default=3)
    parser.add_argument('--items', nargs='+', choices=sorted(ITEMS), default=['position', 'quality', 'hdop', 'utc_time', 'utc_date'])
    args = parser.parse_args()
    names = list(dict.fromkeys(args.items))
    if 'position' in names and 'quality' not in names:
        names.append('quality')
    rows = {}
    failed = False
    for i, name in enumerate(names):
        if i:
            time.sleep(0.25)
        try:
            rows[name] = query(args.host, args.port, name, args.timeout)
            failed |= rows[name]['state'] != 'retrieved'
        except (OSError, EOFError, ValueError) as exc:
            rows[name] = {'state': 'error', 'value': None, 'error': str(exc), 'collected_utc': utc_now()}
            failed = True
    print(json.dumps({'items': rows, 'fix_state': fix_state(rows),
                      'timing': 'Sequential reads; sensor age unknown; no atomic date/time pairing'}, indent=2, allow_nan=False))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
