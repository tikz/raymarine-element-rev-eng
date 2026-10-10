#!/usr/bin/env python3
"""Decode one saved native AIS datagram locally; opens no network connection."""
import argparse
import datetime
import json
import math
from pathlib import Path
import struct
import sys

FLAGS = {'cog': 0xdc, 'sog': 0xdd, 'cpa': 0xde, 'tcpa_adjacent': 0xdf,
         'heading': 0xe0, 'rot': 0xe1, 'cargo': 0xe2, 'ship_type': 0xe3,
         'imo': 0xe4, 'eta': 0xe5, 'length': 0xe6, 'beam': 0xe7,
         'draught': 0xe9, 'last_seen': 0xea}


def decode_datagram(packet):
    if len(packet) < 4:
        raise ValueError('Short AIS datagram')
    command, = struct.unpack_from('<I', packet)
    if command == 0x1e0001:
        if len(packet) != 8:
            raise ValueError('Invalid target-deletion length')
        return {'message': 'target_deleted', 'mmsi': struct.unpack_from('<I', packet, 4)[0]}
    if command != 0x1e0000 or len(packet) != 328:
        raise ValueError('Expected one 328-byte TargetChanged or 8-byte deletion datagram')
    if packet[4] not in (0, 1) or packet[5] != 0 or struct.unpack_from('<H', packet, 6)[0] != 320:
        raise ValueError('Invalid TargetChanged envelope')
    record = packet[8:]
    flags = {name: record[offset] for name, offset in FLAGS.items()}

    def get(fmt, offset):
        return struct.unpack_from('<' + fmt, record, offset)[0]

    def measurement(fmt, offset, flag, divisor=1):
        return get(fmt, offset)/divisor if flags[flag] == 1 else None

    def text(offset, end):
        raw = record[offset:end]
        if b'\0' not in raw:
            raise ValueError('Unterminated AIS text field')
        return raw.split(b'\0', 1)[0].decode('utf-8')

    def calendar(offset, flag, clock):
        if flags[flag] != 1:
            return None
        days, seconds = get('H', offset), get('I', offset+4)
        if seconds >= 86400 or (days == 0 and seconds == 0):
            return None
        date = datetime.date(1970, 1, 1) + datetime.timedelta(days=days)
        return {'date': date.isoformat(), 'seconds_of_day': seconds, 'clock': clock}

    latitude, longitude = get('i', 0x18)/1e7, get('i', 0x1c)/1e7
    to_degrees = 180/math.pi/10000
    heading = measurement('H', 0x38, 'heading')
    cog = measurement('H', 0x0c, 'cog')
    rot = measurement('h', 0x44, 'rot', 32000)
    length = measurement('I', 0x48, 'length', 10)
    return {
        'message': 'target_changed', 'new': bool(packet[4]), 'mmsi': get('I', 0),
        'target_state': get('I', 8), 'navigation_status': get('I', 0x3c),
        'name': text(0x9c, 0xbf), 'destination': text(0xbf, 0xd4), 'callsign': text(0xd4, 0xdc),
        'latitude': latitude if -90 <= latitude <= 90 else None,
        'longitude': longitude if -180 <= longitude <= 180 else None,
        'cog_degrees': cog*to_degrees if cog is not None else None,
        'heading_degrees': heading*to_degrees if heading is not None else None,
        'sog_mps': measurement('H', 0x0e, 'sog', 100),
        'rot_degrees_per_minute': rot*180/math.pi*60 if rot is not None else None,
        'cpa_metres': measurement('I', 0x10, 'cpa'),
        'tcpa_seconds': measurement('I', 0x14, 'cpa'),
        'length_metres': length if length else None,
        'beam_metres': measurement('I', 0x4c, 'beam', 10),
        'draught_metres': measurement('I', 0x50, 'draught', 10),
        'imo': get('I', 0x6c), 'ship_type_internal': get('I', 0x70),
        'cargo_type_internal': get('I', 0x74), 'monotonic_tick': get('I', 0x78),
        'last_seen': calendar(0x5c, 'last_seen', 'local'), 'eta': calendar(0x64, 'eta', 'utc'),
        'own_motion_certain': record[0x10b] != 0, 'validity_bytes': flags,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--file', type=Path, help='Binary file containing exactly one UDP datagram')
    group.add_argument('--hex', help='Hexadecimal bytes of one datagram')
    args = parser.parse_args()
    try:
        if args.file:
            with args.file.open('rb') as file:
                packet = file.read(329)
        else:
            if len(args.hex) > 2048:
                raise ValueError('Hex input exceeds one-datagram budget')
            packet = bytes.fromhex(args.hex)
        result = decode_datagram(packet)
    except (OSError, ValueError) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
