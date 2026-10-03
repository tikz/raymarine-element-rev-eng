"""Small, bounded transport helpers shared by the standalone PoCs."""
import argparse
import datetime
import math
import struct
import time


class FrameReader:
    def __init__(self, sock, deadline, byte_limit=65536, frame_limit=65532):
        self.sock = sock
        self.deadline = deadline
        self.remaining = byte_limit
        self.frame_limit = frame_limit

    def exact(self, count):
        if count > self.remaining:
            raise ValueError('Response byte budget exceeded')
        result = bytearray()
        while len(result) < count:
            remaining_time = self.deadline - time.monotonic()
            if remaining_time <= 0:
                raise TimeoutError('Response deadline exceeded')
            self.sock.settimeout(remaining_time)
            chunk = self.sock.recv(count - len(result))
            if not chunk:
                raise EOFError('Connection closed before a complete response')
            result.extend(chunk)
            self.remaining -= len(chunk)
        return bytes(result)

    def read(self):
        size, = struct.unpack('<I', self.exact(4))
        if not 4 <= size <= self.frame_limit:
            raise ValueError('Invalid response frame size')
        return self.exact(size)


def timeout_arg(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise argparse.ArgumentTypeError('Timeout must be finite and in (0, 60] seconds')
    return seconds


def port_arg(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('Port must be between 1 and 65535')
    return port


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
