"""Bounded write-path validation for the Jiritsu service file interface.

Writes one fixed-size random payload to a single allowlisted path inside the
service Work area, reads it back, and compares digests. It performs no path
traversal, no preload change, no DEBUG request and no delete: the only device
change is one small file beside the service artifacts the research already
places in that writable area.
"""
import hashlib
import struct

from jiritsu_version_read import authorize, exchange

WRITE_PATH = "/tmp/jiritsu/a7c2-write-check.bin"
WRITE_SIZE = 1024
_READ_REQUEST = struct.pack("<HH", 0x1001, 0x11) + b"\x00\xff\x00"
_MODE_AREA = b"\x00\xff"


def _select(path):
    if not path.startswith("/tmp/jiritsu/") or ".." in path:
        raise ValueError("Write-check path must stay inside the Work area")
    if any(ord(character) < 0x20 or ord(character) > 0x7e or character in "\r\n," for character in path):
        raise ValueError("Write-check path contains a rejected character")
    return struct.pack("<HH", 0x1001, 0x0f) + b"\0" + path.encode("ascii") + b"\0"


def _write(payload):
    if len(payload) != WRITE_SIZE:
        raise ValueError(f"Write-check payload must be exactly {WRITE_SIZE} bytes")
    if not isinstance(payload, bytes):
        raise ValueError("Write-check payload must be bytes")
    return struct.pack("<HH", 0x1001, 0x10) + _MODE_AREA + b"\x00" + payload


def build_requests(payload):
    select = _select(WRITE_PATH)
    write = _write(payload)
    return select, write, _READ_REQUEST


def run_write_check(transport, payload, log):
    select, write, read = build_requests(payload)
    authorize(transport, (select, write, read))
    exchange(transport, select, 4, log)
    log("write_check_path_selected", path=WRITE_PATH)
    exchange(transport, write, 4, log)
    digest = hashlib.sha256(payload).hexdigest()
    log("write_check_written", path=WRITE_PATH, bytes=len(payload), sha256=digest)
    exchange(transport, select, 4, log)
    observed = exchange(transport, read, 4 + WRITE_SIZE, log)
    record = {
        "path": WRITE_PATH,
        "written_bytes": len(payload),
        "written_sha256": digest,
        "readback_bytes": len(observed),
        "readback_sha256": hashlib.sha256(observed).hexdigest(),
        "matches": observed == payload,
    }
    log("write_check_result", **record)
    if not record["matches"]:
        raise ValueError(
            f"Write path did not reproduce the payload: wrote {len(payload)} bytes, read {len(observed)}"
        )
    return record
