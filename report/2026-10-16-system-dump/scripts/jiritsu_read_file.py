"""Arbitrary single-file read over the Jiritsu service interface.

Path selection is generic, so any regular file reachable from the service
process can be read. Sizing follows the firmware: the length is the file's
stat size, and there is no offset parameter, so one request reads one whole
file. Large replies arrive as one or more Senser packets; each packet declares
its own length and is drained in bounded chunks.

Device nodes (block devices) report a zero stat size and are therefore not
readable here. That limitation is a property of the firmware, not of this code.
"""
import hashlib
import struct

from jiritsu_version_read import (AUTHORIZED_MAX_BYTES, HEADER, JiritsuResultError,
                                  authorize, exchange)

SENSER_MAX_PAYLOAD = 0x100000      # firmware's per-packet payload cap
READ_CHUNK = 0x8000                # chunk used by the archived host protocol
MAX_READ_BYTES = 0x0C7FFFFF        # largest DMM unit class the firmware accepts
MAX_PATH_BYTES = 0x1FF
PROGRESS_STEP = 256 * 1024      # log transfer progress at this granularity
BASE_PATH = "/tmp/jiritsu"          # service Work area that owns the custom-path slot


def resolve_wire_path(path):
    """Translate a camera-absolute path into the form the firmware resolves.

    The custom-path slot is interpreted relative to the Work area, so an
    absolute target has to climb back out with ".." after the base prefix.
    Paths already written relative to the Work area are passed through.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Path must be absolute")
    if any(ord(character) < 0x20 or ord(character) > 0x7e for character in path):
        raise ValueError("Path must be printable ASCII")
    if path == BASE_PATH or path.startswith(BASE_PATH + "/"):
        wire = path
    else:
        wire = f"{BASE_PATH}/../..{path}"
    if len(wire) > MAX_PATH_BYTES:
        raise ValueError("Wire path exceeds the firmware's path buffer")
    return wire


def select_request(path):
    wire = resolve_wire_path(path).encode("ascii")
    return struct.pack("<HH", 0x1001, 0x0f) + b"\0" + wire + b"\0"


def read_request(masked=False):
    """f2 selects the firmware branch: 0 reads raw file bytes, 1 applies libCommuTalk."""
    return struct.pack("<HH", 0x1001, 0x11) + bytes([0x00, 0xff, 0x01 if masked else 0x00])


def _read_tolerant(transport, size, max_empty, what, logger=None):
    """Read exactly `size` bytes, skipping empty boundary packets.

    The device separates 0x8000-sized body chunks with zero-length USB packets.
    Those carry no data and must be stepped over, not treated as failure. A run
    of empty reads with no progress is still bounded so a stalled device cannot
    hang the session.
    """
    data = bytearray()
    empties = 0
    last_marked = 0
    while len(data) < size:
        want = min(size - len(data), READ_CHUNK)
        chunk = transport.driver.read(want)
        if len(chunk) > want:
            raise ValueError(f"Oversized {what} fragment: got {len(chunk)}, wanted {want}")
        if not chunk:
            empties += 1
            if empties > max_empty:
                raise ValueError(f"No {what} progress after {max_empty} empty packets")
            continue
        empties = 0
        data.extend(chunk)
        if logger is not None and len(data) // PROGRESS_STEP > last_marked:
            last_marked = len(data) // PROGRESS_STEP
            logger("read_progress", what=what, bytes=len(data), declared=size)
    return bytes(data)


def exchange_large(transport, request, limit, log):
    """Like jiritsu_version_read.exchange, but tolerant of multi-packet replies."""
    authorize(transport, (request,), max_bytes=AUTHORIZED_MAX_BYTES)
    if transport.driver.getId() != (0x054c, 0x0336):
        raise ValueError("An authenticated 0336 service transport is required")
    sequence = transport._sequence
    log("read_file_request", sequence=sequence, request_hex=request.hex())
    transport.driver.write(HEADER.pack(len(request), 0x40, sequence, 0, 0, 0, 0) + request)
    total = bytearray()
    status = None
    while True:
        raw = _read_tolerant(transport, HEADER.size, 4, "header", logger=log)
        size, function, reply_sequence, version, micon, offset, status = HEADER.unpack(raw)
        if reply_sequence != sequence or function != 0x40:
            raise ValueError("Unexpected Senser sequence or function")
        remaining_allowance = limit - len(total) + 4
        if min(size, SENSER_MAX_PAYLOAD) > remaining_allowance:
            raise ValueError(f"Reply declares {size} bytes beyond the limit")
        log("read_file_packet", sequence=sequence, declared=size, status=status)
        # One header covers at most one SenserMaxPayload-sized burst; a longer
        # reply continues with another header after that burst.
        burst = min(size, SENSER_MAX_PAYLOAD)
        if burst:
            total.extend(_read_tolerant(transport, burst, 64, "body", logger=log))
        if size <= SENSER_MAX_PAYLOAD:
            break
    transport._sequence += 1
    if status != 0:
        raise ValueError(f"Senser protocol error {status:#x}")
    if len(total) < 4:
        raise ValueError("Missing Jiritsu result code")
    result, = struct.unpack_from("<I", total)
    if result != 0:
        raise JiritsuResultError(result)
    return bytes(total[4:])


def read_file(transport, path, log, max_bytes=MAX_READ_BYTES, masked=False):
    if not 0 < max_bytes <= MAX_READ_BYTES:
        raise ValueError("max_bytes is outside the reviewed range")
    authorize(transport, (select_request(path),), max_bytes=AUTHORIZED_MAX_BYTES)
    exchange(transport, select_request(path), 4, log)
    log("read_file_path_selected", path=path, wire_path=resolve_wire_path(path), masked=masked)
    content = exchange_large(transport, read_request(masked), 4 + max_bytes, log)
    log("read_file_done", path=path, bytes=len(content), masked=masked,
        sha256=hashlib.sha256(content).hexdigest())
    return content


def verify_stream(transport, path, log):
    """Read one file both ways; the XOR of the replies must be the CTR stream.

    This validates the recovered key/IV against the device without trusting any
    external reference.
    """
    from jiritsu_crypto import keystream

    plain = read_file(transport, path, log)
    masked = read_file(transport, path, log, masked=True)
    if len(plain) != len(masked):
        raise ValueError(f"Branch lengths differ: raw {len(plain)}, masked {len(masked)}")
    if not plain:
        raise ValueError("Empty file gives no stream evidence")
    observed = bytes(a ^ b for a, b in zip(plain, masked))
    expected = keystream(len(plain))
    record = {
        "path": path,
        "bytes": len(plain),
        "raw_sha256": hashlib.sha256(plain).hexdigest(),
        "masked_sha256": hashlib.sha256(masked).hexdigest(),
        "stream_matches_recovered_key": observed == expected,
        "observed_stream_prefix": observed[:16].hex(),
        "expected_stream_prefix": expected[:16].hex(),
    }
    log("crypto_self_check", **record)
    return plain, record
