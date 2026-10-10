"""Synthetic protocol and lifecycle tests; no hardware or live captures."""
import contextlib
import io
import math
from pathlib import Path
import shlex
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'poc'))
from common import FrameReader
from test_pocs import FragmentedSocket, framed


def reader(data):
    return FrameReader(FragmentedSocket(data), time.monotonic() + 1)


def route_list_frames(payload, status=0x40000, end=b'SYNTHET1'):
    bodies = [struct.pack('<III', 0xf0040, 1, status),
              struct.pack('<II8sI', 0xf0200, 1, b'SYNTHET1', 25),
              struct.pack('<III', 0xf0201, 1, 3) + payload[:3],
              struct.pack('<III', 0xf0201, 1, len(payload)-3) + payload[3:],
              struct.pack('<II8s', 0xf0202, 1, end)]
    return b''.join(map(framed, bodies))


def target_packet():
    r = bytearray(320)  # Entirely synthetic: 1 N, 2 E; fictitious TEST vessel.
    struct.pack_into('<I', r, 0, 123456789)
    struct.pack_into('<I', r, 8, 1)
    struct.pack_into('<HH', r, 0xc, 15708, 250)
    struct.pack_into('<II', r, 0x10, 1234, 600)
    struct.pack_into('<ii', r, 0x18, 10000000, 20000000)
    struct.pack_into('<H', r, 0x38, 31416)
    struct.pack_into('<h', r, 0x44, -320)
    struct.pack_into('<III', r, 0x48, 1000, 200, 35)
    struct.pack_into('<H', r, 0x64, 1)
    struct.pack_into('<I', r, 0x68, 3661)
    r[0x9c:0xa1] = b'TEST\0'
    r[0xd4:0xd8] = b'ABC\0'
    for offset in (0xdc, 0xdd, 0xde, 0xe0, 0xe1, 0xe5, 0xe6, 0xe7, 0xe9):
        r[offset] = 1
    return struct.pack('<IBBH', 0x1e0000, 1, 0, 320) + r


class RoutesTests(unittest.TestCase):
    def test_count_request_and_fragmented_reply(self):
        from routes import count_request, read_count
        self.assertEqual(count_request(1).hex(), '080000004e010f0001000000')
        self.assertEqual(read_count(reader(framed(struct.pack('<III', 0xf004a, 1, 7))), 1), 7)
        for body in (struct.pack('<III', 0xf004a, 2, 7), struct.pack('<II', 0xf004a, 1)):
            with self.assertRaises(ValueError):
                read_count(reader(framed(body)), 1)

    def test_list_request_is_only_read_operations(self):
        from routes import list_request
        data = list_request(1)
        parsed = []
        while data:
            n, = struct.unpack_from('<I', data)
            parsed.append(data[4:4+n]); data = data[4+n:]
        self.assertEqual([struct.unpack_from('<I', b)[0] for b in parsed],
                         [0xf0140, 0xf0200, 0xf0201, 0xf0202])
        self.assertEqual(struct.unpack_from('<I', parsed[2], 12+20)[0], 50)
        self.assertEqual(len(parsed[2][12:]), 40)

    def test_multichunk_guids_are_preserved_as_bytes(self):
        from routes import read_list
        payload = struct.pack('<I', 2) + b'SYNTHET2SYNTHET3'
        self.assertEqual(read_list(reader(route_list_frames(payload)), 1),
                         ['53594e5448455432', '53594e5448455433'])

    def test_list_rejects_failed_status_mismatch_count_and_truncation(self):
        from routes import read_list
        payload = struct.pack('<I', 1) + b'SYNTHET2'
        for raw in (route_list_frames(payload, status=0x80040b03),
                    route_list_frames(payload, end=b'WRONG-ID'),
                    route_list_frames(struct.pack('<I', 51)),
                    route_list_frames(struct.pack('<I', 2)+b'SYNTHET2'),
                    route_list_frames(payload)[:-1]):
            with self.assertRaises((ValueError, EOFError)):
                read_list(reader(raw), 1)


class AisTests(unittest.TestCase):
    def test_units_and_dates_from_independent_synthetic_record(self):
        from ais_decode import decode_datagram
        result = decode_datagram(target_packet())
        self.assertEqual(result['name'], 'TEST')
        self.assertEqual(result['latitude'], 1.0)
        self.assertEqual(result['longitude'], 2.0)
        self.assertAlmostEqual(result['heading_degrees'], 180, places=2)
        self.assertAlmostEqual(result['cog_degrees'], 90, places=2)
        self.assertEqual(result['sog_mps'], 2.5)
        self.assertAlmostEqual(result['rot_degrees_per_minute'], -34.3774677, places=5)
        self.assertEqual(result['length_metres'], 100)
        self.assertEqual(result['beam_metres'], 20)
        self.assertEqual(result['draught_metres'], 3.5)
        self.assertEqual(result['cpa_metres'], 1234)
        self.assertEqual(result['tcpa_seconds'], 600)
        self.assertEqual(result['eta'], {'date': '1970-01-02', 'seconds_of_day': 3661, 'clock': 'utc'})

    def test_invalid_flags_are_not_zero_measurements(self):
        from ais_decode import decode_datagram
        packet = bytearray(target_packet())
        packet[8+0xdc:8+0xeb] = bytes(15)
        result = decode_datagram(packet)
        for name in ('cog_degrees', 'sog_mps', 'heading_degrees', 'cpa_metres',
                     'tcpa_seconds', 'length_metres', 'rot_degrees_per_minute', 'eta'):
            self.assertIsNone(result[name], name)
        self.assertFalse(result['own_motion_certain'])

    def test_deletion_and_malformed_frames(self):
        from ais_decode import decode_datagram
        self.assertEqual(decode_datagram(bytes.fromhex('01001e0015cd5b07')),
                         {'message': 'target_deleted', 'mmsi': 123456789})
        bad = bytearray(target_packet()); bad[6:8] = b'\x01\x00'
        for packet in (b'', target_packet()[:-1], target_packet()+b'x', bad, bytes(8)):
            with self.assertRaises(ValueError):
                decode_datagram(packet)

    def test_invalid_coordinates_and_calendar_seconds(self):
        from ais_decode import decode_datagram
        bad = bytearray(target_packet()); struct.pack_into('<i', bad, 8+0x18, 910000000)
        self.assertIsNone(decode_datagram(bad)['latitude'])
        bad = bytearray(target_packet()); struct.pack_into('<I', bad, 8+0x68, 86400)
        self.assertIsNone(decode_datagram(bad)['eta'])


class RootTests(unittest.TestCase):
    def test_identity_requires_root_not_successful_http_only(self):
        from root_probe import parse_identity
        self.assertEqual(parse_identity(b'POC_UID=0\nPOC_GID=0\n'), {'uid': 0, 'gid': 0})
        for text in (b'', b'POC_UID=1023\nPOC_GID=1023\n', b'POC_UID=0\n'):
            with self.assertRaises(ValueError):
                parse_identity(text)

    def test_png_checks_size_signature_and_header_crc(self):
        from root_probe import validate_png
        header = struct.pack('>IIBBBBB', 800, 480, 8, 2, 0, 0, 0)
        def chunk(kind, data):
            return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))
        png = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', header)+chunk(b'IDAT', zlib.compress(bytes(480*(1+800*3))))+chunk(b'IEND', b'')
        self.assertEqual(validate_png(png), {'width': 800, 'height': 480})
        for data in (b'', png[:32], png[:29]+b'\0\0\0\0'+png[33:]):
            with self.assertRaises(ValueError):
                validate_png(data)

    def test_http_response_requires_complete_unambiguous_body(self):
        from root_probe import parse_http
        self.assertEqual(parse_http(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK'), (200, b'OK'))
        for data in (b'HTTP/1.0 200 OK\r\nContent-Length: 3\r\n\r\nOK',
                     b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n', b'garbage'):
            with self.assertRaises(ValueError):
                parse_http(data)

    def test_failed_trigger_still_removes_mapping_and_owned_directory(self):
        from root_probe import run_probe
        state = set()
        def batch(commands):
            for line in commands:
                words = shlex.split(line)
                op = words[0].lstrip('-')
                if op == 'mkdir': state.add(words[1])
                elif op == 'put': state.add(words[2])
                elif op == 'rm': state.discard(words[1])
                elif op == 'rmdir':
                    self.assertFalse(any(v.startswith(words[1]+'/') for v in state))
                    state.discard(words[1])
        class DeviceDouble:
            def batch(self, commands): batch(commands)
        calls=[]
        def http(host, port, path, timeout, byte_limit=65536):
            calls.append(path)
            if len(calls)==1: raise TimeoutError('synthetic transport loss')
            return 404, b''
        with patch('root_probe.http_get', side_effect=http), patch('root_probe.time.sleep'), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TimeoutError):
                run_probe(DeviceDouble(), 'element.invalid', 8080, 1, 'identity')
        self.assertEqual(state, set())
        self.assertEqual(len(calls), 2)

    def test_screenshot_retrieved_only_after_capture_and_cleanup_proved(self):
        from root_probe import run_probe
        def chunk(kind, payload):
            return struct.pack('>I', len(payload))+kind+payload+struct.pack('>I', zlib.crc32(kind+payload))
        png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB',800,480,8,6,0,0,0))
               +chunk(b'IDAT', zlib.compress(bytes(480*(1+800*4))))+chunk(b'IEND',b''))
        state = {}
        class DeviceDouble:
            def batch(self, commands):
                for line in commands:
                    words = shlex.split(line); op = words[0].lstrip('-')
                    if op == 'mkdir': state[words[1]] = None
                    elif op == 'put': state[words[2]] = Path(words[1]).read_bytes()
                    elif op == 'get': Path(words[2]).write_bytes(state[words[1]])
                    elif op == 'rm': state.pop(words[1], None)
                    elif op == 'rmdir':
                        if any(v.startswith(words[1]+'/') for v in state):
                            raise RuntimeError('Directory not empty')
                        state.pop(words[1])
        def http(host, port, path, timeout, byte_limit=65536):
            if state:
                remote = next(k for k,v in state.items() if v is None)
                if remote+'/httpd.conf' in state:
                    state[remote+'/screen.png'] = png
                    return 200, b'POC_UID=0\nPOC_GID=0\nPOC_CAPTURE_OK\n'
            return 404, b''
        with patch('root_probe.http_get', side_effect=http), contextlib.redirect_stderr(io.StringIO()):
            result = run_probe(DeviceDouble(), 'element.invalid', 8080, 1, 'screenshot')
        self.assertEqual(result['png'], png)
        self.assertTrue(result['cleanup_verified'])
        self.assertEqual(state, {})

    def test_cleanup_failure_cannot_be_reported_as_success(self):
        from root_probe import run_probe
        class DeviceDouble:
            def batch(self, commands): pass
        with patch('root_probe.http_get', side_effect=[(200,b'POC_UID=0\nPOC_GID=0\n'), (200,b'OK')]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'Cleanup unverified'):
                run_probe(DeviceDouble(), 'element.invalid', 8080, 1, 'identity')

    def test_existing_output_rejected_without_remote_contact(self):
        script = Path(__file__).resolve().parents[1]/'poc/root_probe.py'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'existing.png'; output.write_bytes(b'keep this')
            result = subprocess.run([sys.executable, str(script), '--host','element.invalid',
                '--key','missing-key','--known-hosts','missing-hosts','screenshot','--output',str(output)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Output already exists', result.stderr)
            self.assertEqual(output.read_bytes(), b'keep this')
