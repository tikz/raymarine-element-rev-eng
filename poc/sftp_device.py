#!/usr/bin/env python3
"""Read supply/brightness or explicitly set brightness using installed OpenSSH sftp."""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from common import port_arg, timeout_arg

ROOT = '/sys/devices/platform/raymarine/backlight:lcd/'
VOLTAGE = '/sys/devices/platform/raymarine/secondary/vbatt'


def quote(value):
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Control character in path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def percent_to_raw(percent, low, high):
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError('Percent must be finite and between 0 and 100')
    if not 0 < low < high:
        raise ValueError('Invalid brightness bounds')
    if percent == 0:
        return low
    if percent == 100:
        return high
    fraction = (10 ** ((percent / 100) ** 1.8) - 1) / 9
    return max(low, min(high, int(low + (high - low) * fraction)))


class Device:
    def __init__(self, host, key, known_hosts, port=22, timeout=8, legacy_rsa=False):
        # Prevent destination option/path interpretation; support plain DNS/IPv4/IPv6 hosts.
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:%_-]*', host):
            raise ValueError('Expected a plain host name or address')
        key = Path(key).expanduser().resolve(strict=True)
        known_hosts = Path(known_hosts).expanduser().resolve(strict=True)
        destination = f'media_rw@[{host}]' if ':' in host else f'media_rw@{host}'
        self.timeout = timeout
        self.command = ['sftp', '-q', '-P', str(port), '-i', str(key),
                        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'UserKnownHostsFile=' + quote(known_hosts),
                        '-o', f'ConnectTimeout={max(1, math.ceil(timeout))}']
        if legacy_rsa:
            self.command += ['-o', 'PubkeyAcceptedAlgorithms=+ssh-rsa', '-o', 'HostKeyAlgorithms=+ssh-rsa']
        self.command += ['-b', '-', destination]

    def batch(self, commands):
        result = subprocess.run(self.command, input='\n'.join(commands) + '\n',
                                text=True, capture_output=True, timeout=self.timeout)
        if result.returncode:
            raise RuntimeError('SFTP failed: ' + result.stderr.strip())

    def read_ints(self, paths):
        with tempfile.TemporaryDirectory(prefix='element-poc-') as directory:
            targets = [Path(directory) / str(i) for i in range(len(paths))]
            self.batch([f'get {quote(remote)} {quote(local)}' for remote, local in zip(paths, targets)])
            return [int(path.read_text(encoding='ascii').strip()) for path in targets]

    def brightness(self):
        actual, low, high = self.read_ints([ROOT + name for name in ('actual_brightness', 'min_brightness', 'max_brightness')])
        if not 0 < low < high or not low <= actual <= high:
            raise ValueError('Unexpected brightness bounds/readback')
        return {'actual': actual, 'minimum': low, 'maximum': high}

    def set_brightness(self, percent=None, raw=None):
        if (percent is None) == (raw is None):
            raise ValueError('Supply exactly one of percent or raw')
        before = self.brightness()
        low, high = before['minimum'], before['maximum']
        target = percent_to_raw(percent, low, high) if percent is not None else raw
        if type(target) is not int or not low <= target <= high:
            raise ValueError('Raw brightness outside device bounds')
        # Record recovery value to stderr before the state-changing operation.
        print(json.dumps({'before_write': before, 'target': target}), file=sys.stderr, flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix='element-poc-') as directory:
                payload = Path(directory) / 'brightness.txt'
                payload.write_text(f'{target}\n', encoding='ascii')
                self.batch([f'put {quote(payload)} {quote(ROOT + "brightness")}'])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('Write failed; hardware state is unknown. Read brightness before retrying.') from exc
        try:
            actual, = self.read_ints([ROOT + 'actual_brightness'])
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('Write sent but readback failed; hardware state is unknown.') from exc
        if actual != target:
            raise RuntimeError(f'Brightness mismatch: requested {target}, read {actual}')
        return {'previous': before['actual'], 'requested': target, 'actual': actual, 'state': 'verified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--known-hosts', required=True)
    parser.add_argument('--port', type=port_arg, default=22)
    parser.add_argument('--timeout', type=timeout_arg, default=8)
    parser.add_argument('--legacy-rsa', action='store_true', help='Enable legacy RSA only for this invocation')
    sub = parser.add_subparsers(dest='operation', required=True)
    sub.add_parser('voltage')
    sub.add_parser('brightness')
    control = sub.add_parser('set-brightness', help='Change hardware brightness and leave it at the requested level')
    group = control.add_mutually_exclusive_group(required=True)
    group.add_argument('--percent', type=float)
    group.add_argument('--raw', type=int)
    args = parser.parse_args()
    try:
        device = Device(args.host, args.key, args.known_hosts, args.port, args.timeout, args.legacy_rsa)
        if args.operation == 'voltage':
            millivolts, = device.read_ints([VOLTAGE])
            result = {'millivolts': millivolts, 'volts': millivolts / 1000}
        elif args.operation == 'brightness':
            result = device.brightness()
        else:
            result = device.set_brightness(args.percent, args.raw)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'state': 'error', 'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
