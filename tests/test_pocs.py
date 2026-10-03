"""Offline tests. All measurements, source bytes and responses are synthetic."""
import importlib.util
import math
import contextlib
import io
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'poc'))


def framed(body):
    return struct.pack('<I', len(body)) + body


class FragmentedSocket:
    """Socket-shaped byte source; never opens a network connection."""
    def __init__(self, data):
        self.data = data

    def settimeout(self, timeout):
        if timeout <= 0:
            raise socket.timeout()

    def recv(self, count):
        result, self.data = self.data[:min(count, 3)], self.data[min(count, 3):]
        return result


def native_frames(status=0, sequence=1, end_id=b'SYNTHET2'):
    status_body = struct.pack('<IIIHH', 0x100003, sequence, 0x44, status, 0)
    if status:
        return framed(status_body)
    # Entirely synthetic position (1 degree N, 2 degrees E), no source identity.
    record = struct.pack('<IHIHiiIIHH', 0x44, 0x3c, 8, 0,
                         10000000, 20000000, 19, 10, 0, 0)
    parts = [status_body, struct.pack('<II8sI', 0x100200, sequence, b'SYNTHET2', 0)]
    for chunk in (record[:9], record[9:]):
        parts.append(struct.pack('<III', 0x100201, sequence, len(chunk)) + chunk)
    parts.append(struct.pack('<II8s', 0x100202, sequence, end_id))
    return b''.join(map(framed, parts))


class PoCTests(unittest.TestCase):
    def test_scripts_exist(self):
        for name in ('navigation', 'raydb_get', 'sftp_device'):
            self.assertIsNotNone(importlib.util.find_spec(name), name)

    def reader(self, raw, **kwargs):
        from common import FrameReader
        return FrameReader(FragmentedSocket(raw), time.monotonic() + 1, **kwargs)

    def test_fragmented_multichunk_navigation(self):
        from navigation import read_transaction, decode_item
        record = read_transaction(self.reader(native_frames()), 1, 0x44)
        self.assertEqual(decode_item('position', record), {'latitude': 1.0, 'longitude': 2.0})
        self.assertEqual(record['transport_type'], 19)
        self.assertNotIn('source', record)

    def test_missing_is_not_zero(self):
        from navigation import read_transaction, decode_item
        record = read_transaction(self.reader(native_frames(status=1)), 1, 0x44)
        self.assertIsNone(decode_item('position', record))

    def test_transaction_mismatch_rejected(self):
        from navigation import read_transaction
        for raw in (native_frames(sequence=2), native_frames(end_id=b'WRONG-ID')):
            with self.assertRaises(ValueError):
                read_transaction(self.reader(raw), 1, 0x44)

    def test_truncation_oversize_and_budget(self):
        for raw, options in ((b'\x10\x00', {}), (struct.pack('<I', 999999), {}),
                             (framed(b'12345678'), {'byte_limit': 10})):
            with self.assertRaises((ValueError, EOFError)):
                self.reader(raw, **options).read()

    def test_deadline(self):
        from common import FrameReader
        reader = FrameReader(FragmentedSocket(b''), time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            reader.read()

    def test_navigation_type_and_size(self):
        from navigation import decode_item
        for typ, payload in ((0x3c, b'\x00'), (0x01, bytes(8))):
            with self.assertRaises(ValueError):
                decode_item('position', {'status': 0, 'type': typ, 'payload': payload})

    def test_fix_needs_both_position_and_quality(self):
        from navigation import fix_state
        self.assertEqual(fix_state({'position': {'value': {'latitude': 1, 'longitude': 2}}}), 'unavailable')
        self.assertEqual(fix_state({'quality': {'value': 0}}), 'no_fix')
        self.assertEqual(fix_state({'quality': {'value': 1}, 'position': {'value': {}}}), 'fix_indicated')

    def test_native_request_wire_bytes(self):
        from navigation import request
        self.assertEqual(request(1, 0x44).hex(), '0c000000050110000100000044000000')

    def test_raydb_hello_and_get(self):
        from raydb_get import request
        body = request(7, 'RayDBRemoteClient')[4:]
        self.assertEqual(body[:13].hex(), '01000000071100000000000000')
        self.assertEqual(body[13:30], b'RayDBRemoteClient')
        self.assertEqual(body[30:].hex(), '000101070000000000000000000000')
        with self.assertRaises(ValueError):
            request(1, 'test')

    def update(self, typ, payload, path=b'synthetic'):
        return (struct.pack('<IBQ', 1, 4, len(path)) + path + bytes(3)
                + struct.pack('<I', typ) + payload + bytes(4))

    def test_raydb_signedness_and_nan(self):
        from raydb_get import decode_update
        self.assertEqual(decode_update(self.update(3, b'\xff'*4))['value'], 4294967295)
        self.assertEqual(decode_update(self.update(7, b'\xff'*4))['value'], -1)
        result = decode_update(self.update(10, struct.pack('<d', math.nan)))
        self.assertIsNone(result['value'])
        self.assertEqual(result['state'], 'nonfinite')

    def test_raydb_string_and_invalid_lengths(self):
        from raydb_get import decode_update
        self.assertEqual(decode_update(self.update(11, struct.pack('<Q', 3)+b'abc'))['value'], 'abc')
        for body in (self.update(3, b'\x00'), self.update(11, struct.pack('<Q', 999)),
                     self.update(999, b''), self.update(3, bytes(4)) + b'extra'):
            with self.assertRaises(ValueError):
                decode_update(body)

    def test_brightness_curve_and_validation(self):
        from sftp_device import percent_to_raw, quote
        self.assertEqual([percent_to_raw(p, 1, 10000) for p in (0, 50, 100)], [1, 1042, 10000])
        for value in (-1, 101, math.nan, math.inf):
            with self.assertRaises(ValueError):
                percent_to_raw(value, 1, 10000)
        with self.assertRaises(ValueError):
            percent_to_raw(50, 0, 10000)
        with self.assertRaises(ValueError):
            quote('name\nput anything')


    def test_sftp_voltage_write_and_readback(self):
        from sftp_device import Device, ROOT, VOLTAGE
        with tempfile.TemporaryDirectory() as directory:
            key, hosts = Path(directory) / 'key', Path(directory) / 'known hosts'
            key.touch()
            hosts.touch()
            values = {ROOT + 'actual_brightness': 500, ROOT + 'min_brightness': 1,
                      ROOT + 'max_brightness': 10000, VOLTAGE: 12000}
            writes = []

            def run(command, *, input, text, capture_output, timeout):
                self.assertIn('StrictHostKeyChecking=yes', command)
                self.assertIn('BatchMode=yes', command)
                self.assertIn('UserKnownHostsFile="' + str(hosts.resolve()) + '"', command)
                for line in input.splitlines():
                    operation, source, target = shlex.split(line)
                    if operation == 'get':
                        Path(target).write_text(str(values[source]) + '\n')
                    elif operation == 'put':
                        self.assertEqual(target, ROOT + 'brightness')
                        value = int(Path(source).read_text())
                        writes.append(value)
                        values[ROOT + 'actual_brightness'] = value
                    else:
                        self.fail('Unexpected SFTP operation')
                return SimpleNamespace(returncode=0, stderr='')

            device = Device('element.invalid', key, hosts)
            with patch('sftp_device.subprocess.run', side_effect=run), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(device.read_ints([VOLTAGE]), [12000])
                self.assertEqual(device.set_brightness(percent=50),
                                 {'previous': 500, 'requested': 1042, 'actual': 1042, 'state': 'verified'})
                with self.assertRaises(ValueError):
                    device.set_brightness(raw=10001)
            self.assertEqual(writes, [1042])

    def test_sftp_failed_write_reports_unknown_without_retry(self):
        from sftp_device import Device, ROOT
        with tempfile.TemporaryDirectory() as directory:
            key, hosts = Path(directory) / 'key', Path(directory) / 'hosts'
            key.touch()
            hosts.touch()
            writes = []

            def run(command, *, input, **kwargs):
                for line in input.splitlines():
                    operation, source, target = shlex.split(line)
                    if operation == 'get':
                        name = source.removeprefix(ROOT)
                        Path(target).write_text(str({'actual_brightness': 500,
                            'min_brightness': 1, 'max_brightness': 10000}[name]))
                    else:
                        writes.append(int(Path(source).read_text()))
                        raise subprocess.TimeoutExpired(command, 1)
                return SimpleNamespace(returncode=0, stderr='')

            device = Device('element.invalid', key, hosts)
            with patch('sftp_device.subprocess.run', side_effect=run), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'state is unknown'):
                    device.set_brightness(raw=1000)
            self.assertEqual(writes, [1000])

    def test_scripts_help_without_network(self):
        base = Path(__file__).resolve().parents[1]
        for name in ('navigation', 'raydb_get', 'sftp_device'):
            result = subprocess.run([sys.executable, str(base / 'poc' / (name + '.py')), '--help'],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--host', result.stdout)


if __name__ == '__main__':
    unittest.main()
