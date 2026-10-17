"""Bounded SD-card write check through the service media mount.

The firmware picks a path branch from the mode byte:

  mode 0x00 -> base "/tmp/jiritsu", mounts nflasha6, writes the Work area
  mode 0x01 -> base "/mnt", no Work-area mount

``CFileAccess::open`` then tests the resolved path against "/mnt": paths with
that prefix go through ``mountInfoLoad``/``convertPath`` into the vendor
filesystem for the mounted medium (the SD card), while everything else uses a
POSIX open. So mode 0x01 with a "/mnt/..." path is the SD-card branch.

This module mounts the first medium, writes one fixed-size file at the card
root, reads it back, and unmounts again — including on failure. Only that one
file is created; no existing card content is addressed.
"""
import hashlib
import struct

from jiritsu_version_read import AUTHORIZED_MAX_BYTES, SD_MOUNT, SD_UNMOUNT, authorize, exchange

SD_PATH = "/mnt/A7C2_WRITE_CHECK.BIN"
WRITE_SIZE = 1024
MODE = 0x01            # write/read branch that uses the "/mnt" base
SELECT_MODE = 0x00     # the only value adj_cmd_set_file_path accepts


def _select(path):
    """Path selection only accepts mode 0: adj_cmd_set_file_path rejects anything else.

    The mode byte here gates storing the custom path; it does not choose the
    destination. The destination comes from the mode of the later write/read
    command (0x00 -> Work area, 0x01 -> the mounted medium).
    """
    if not path.startswith("/mnt/"):
        raise ValueError("SD path must live under /mnt")
    if any(ord(character) < 0x20 or ord(character) > 0x7e or character in "\r\n," for character in path):
        raise ValueError("SD path contains a rejected character")
    return struct.pack("<HH", 0x1001, 0x0f) + bytes([SELECT_MODE]) + path.encode("ascii") + b"\0"


def _write(payload):
    if not isinstance(payload, bytes) or len(payload) != WRITE_SIZE:
        raise ValueError(f"SD payload must be exactly {WRITE_SIZE} bytes")
    return struct.pack("<HH", 0x1001, 0x10) + bytes([MODE, 0xFF, 0x00]) + payload


def _read(limit=WRITE_SIZE):
    return struct.pack("<HH", 0x1001, 0x11) + bytes([MODE, 0xFF, 0x00])


def build_requests(payload):
    return {"select": _select(SD_PATH), "write": _write(payload), "read": _read()}


def run_sd_write_check(transport, payload, log):
    requests = build_requests(payload)
    authorize(transport, tuple(requests.values()), allowed_modes=(SELECT_MODE, MODE),
              max_bytes=AUTHORIZED_MAX_BYTES, max_requests=len(requests))
    exchange(transport, SD_MOUNT, 4, log)
    log("sd_medium_mounted", path=SD_PATH)
    try:
        exchange(transport, requests["select"], 4, log)
        exchange(transport, requests["write"], 4, log)
        digest = hashlib.sha256(payload).hexdigest()
        log("sd_file_written", path=SD_PATH, bytes=len(payload), sha256=digest)
        exchange(transport, requests["select"], 4, log)
        observed = exchange(transport, requests["read"], 4 + WRITE_SIZE, log)
    finally:
        exchange(transport, SD_UNMOUNT, 4, log)
        log("sd_medium_unmounted")
    record = {
        "path": SD_PATH,
        "mode": hex(MODE),
        "written_bytes": len(payload),
        "written_sha256": hashlib.sha256(payload).hexdigest(),
        "readback_bytes": len(observed),
        "readback_sha256": hashlib.sha256(observed).hexdigest(),
        "matches": observed == payload,
        "host_verification_pending": True,
    }
    log("sd_write_result", **record)
    if not record["matches"]:
        raise ValueError(
            f"SD write did not reproduce the payload: wrote {len(payload)} bytes, read {len(observed)}"
        )
    return record
