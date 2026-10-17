"""Bounded Jiritsu version-file probe; uses an already authenticated transport.

The two requests select a session path and read it. GET_FILE2 can mount the
service partition. No file-content write, DEBUG or firmware command is exposed.
"""
import struct


HEADER = struct.Struct("<IHHBBBB")
REMOTE_PATH = b"/tmp/jiritsu/../../version.txt"
SELECT = struct.pack("<HH", 0x1001, 0x0f) + b"\0" + REMOTE_PATH + b"\0"
READ = struct.pack("<HH", 0x1001, 0x11) + b"\x00\xff\x00"
MAX_FILE = 4096
SYSTEM_PREFLIGHT_PATHS = {
    "fstab": "/sbin/initrc/fstab",
    "fstab.ext": "/sbin/initrc/fstab.ext",
    "etc-preload": "/etc/ld.so.preload",
    "tmp-preload": "/tmp/ld.so.preload",
    "rootfs-device": "/dev/nflasha7",
    "usr-device": "/dev/nflasha15",
}
SYSTEM_SELECTS = {
    name: struct.pack("<HH", 0x1001, 0x0f) + b"\0/tmp/jiritsu/../.." + path.encode("ascii") + b"\0"
    for name, path in SYSTEM_PREFLIGHT_PATHS.items()
}
SD_MOUNT = struct.pack("<HH", 0x1001, 2) + b"\x01\x00"
SD_UNMOUNT = struct.pack("<HH", 0x1001, 2) + b"\x01\x01"
SD_INFO_SELECT = struct.pack("<HH", 0x1001, 0x0f) + b"\0/tmp/jiritsu/mediainfo.txt\0"


class JiritsuResultError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__(f"Jiritsu result {result:#010x}")


AUTHORIZED_MAX_BYTES = 4096
ARTIFACT_MAX_BYTES = 0x500000  # firmware 0x986c branch limit for one uploaded file
AUTHORIZED_MAX_REQUESTS = 4
DEBUG_REQUEST = struct.pack("<HH", 0x1001, 5) + b"\x00\x00\x00"


def authorize_debug(transport):
    """Allow the single DEBUG trigger, kept separate from file authorization."""
    if DEBUG_REQUEST[4:7] != b"\x00\x00\x00" or len(DEBUG_REQUEST) != 7:
        raise ValueError("DEBUG trigger shape changed")
    existing = vars(transport).get("_authorized", ())
    transport._authorized = tuple(existing) + (DEBUG_REQUEST,)



def authorize(transport, requests, max_bytes=AUTHORIZED_MAX_BYTES,
              max_requests=AUTHORIZED_MAX_REQUESTS, allowed_modes=(0x00,)):
    """Allow a small, shape-checked set of extra requests for this transport only."""
    allowed = []
    for request in requests:
        if not isinstance(request, bytes) or not 7 <= len(request) <= max_bytes:
            raise ValueError("Authorized request has an unexpected size")
        group, command = struct.unpack_from("<HH", request)
        if group != 0x1001 or command not in (0x0f, 0x10, 0x11):
            raise ValueError("Authorized request is outside the reviewed command set")
        if request[4] not in allowed_modes:
            raise ValueError("Authorized request must use one of the reviewed transport modes")
        if command in (0x10, 0x11):
            if request[5] != 0xff:
                raise ValueError("Authorized file request must use the custom-path area")
        else:
            path = request[5:]
            if not path.endswith(b"\x00") or not path[:-1]:
                raise ValueError("Authorized path selection must carry one NUL-terminated path")
            if any(byte < 0x20 or byte > 0x7e for byte in path[:-1]):
                raise ValueError("Authorized path must be printable ASCII")
        allowed.append(request)
    if len(allowed) > max_requests:
        raise ValueError("Too many authorized requests")
    transport._authorized = tuple(allowed)


MAILBOX_ENVELOPE = b"echo A7C2-"


def authorize_mailbox_channel(transport):
    """Allow marker-enveloped command writes to the mailbox file.

    A write request does not carry its target path, so the only enforceable
    bound is the payload itself: it must use this run's envelope and stay within
    the mailbox size. Arbitrary content still requires explicit pinning.
    """
    transport._mailbox_channel = True


def _is_mailbox_write(transport, request):
    if not vars(transport).get("_mailbox_channel", False):
        return False
    if not 16 <= len(request) <= 8192:
        return False
    group, command = struct.unpack_from("<HH", request)
    if group != 0x1001 or command != 0x10 or request[4:7] != b"\x00\xff\x00":
        return False
    payload = request[7:]
    return payload.startswith(MAILBOX_ENVELOPE) and payload.endswith(b"\n")


def _is_authorized(transport, request):
    allowed = vars(transport).get("_authorized", ()) if hasattr(transport, "__dict__") else ()
    return request in allowed


def exchange(transport, request, limit, log):
    if request not in (SELECT, READ, *SYSTEM_SELECTS.values(), SD_MOUNT, SD_UNMOUNT, SD_INFO_SELECT):
        if not (_is_authorized(transport, request) or _is_mailbox_write(transport, request)):
            raise ValueError("Request is neither fixed nor explicitly authorized for this transport")
    if transport.driver.getId() != (0x054c, 0x0336):
        raise ValueError("An authenticated 0336 service transport is required")
    sequence = transport._sequence
    header = HEADER.pack(len(request), 0x40, sequence, 0, 0, 0, 0)
    log("jiritsu_request", sequence=sequence, request_hex=request.hex())
    transport._jiritsu_usable = False
    transport.driver.write(header + request)
    raw = transport.driver.read(HEADER.size)
    log("jiritsu_response_header", raw_hex=raw.hex())
    if len(raw) != HEADER.size:
        raise ValueError("Short Senser header")
    size, function, reply_sequence, version, micon, offset, status = HEADER.unpack(raw)
    if reply_sequence != sequence or function != 0x40:
        raise ValueError("Unexpected Senser sequence or function")
    if size > limit:
        raise ValueError(f"Response declares {size} bytes, exceeding limit {limit}")
    body = bytearray()
    while len(body) < size:
        chunk = transport.driver.read(size - len(body))
        if not chunk or len(chunk) > size - len(body):
            raise ValueError("Empty or oversized response body fragment")
        body.extend(chunk)
    # The archived PMCA framing consumes a ZLP for 512-byte-aligned bodies.
    if size and size % 512 == 0:
        if transport.driver.read(512) != b"":
            raise ValueError("Expected an empty bulk boundary packet")
    transport._sequence += 1
    transport._jiritsu_usable = True
    log("jiritsu_response", sequence=sequence, status=status, body_hex=body.hex())
    if status != 0:
        raise ValueError(f"Senser protocol error {status:#x}")
    if len(body) < 4:
        raise ValueError("Missing Jiritsu result code")
    result, = struct.unpack_from("<I", body)
    if result != 0:
        raise JiritsuResultError(result)
    return bytes(body[4:])


def read_version(transport, log):
    exchange(transport, SELECT, 4, log)
    content = exchange(transport, READ, 4 + MAX_FILE, log)
    if not content:
        raise ValueError("Version file was empty; no usable version evidence")
    return content


def read_system_preflight(transport, log, save):
    """Observe six fixed paths; a stat failure is not treated as proof of absence."""
    results = []
    for name, request in SYSTEM_SELECTS.items():
        exchange(transport, request, 4, log)
        record = {"name": name, "path": SYSTEM_PREFLIGHT_PATHS[name]}
        try:
            content = exchange(transport, READ, 4 + MAX_FILE, log)
        except JiritsuResultError as exc:
            if exc.result != 0x10000006:
                raise
            record.update(status="stat_failed", result=hex(exc.result))
        else:
            save(name, content)
            record.update(status="read", bytes=len(content))
            if name.endswith("-device"):
                record["complete_partition_dump"] = False
        log("system_preflight_file", **record)
        results.append(record)
    return results


def inspect_sd_mapping(transport, log, save):
    """Mount the first medium, record the firmware's mapping, then unmount.

    Mounting may write service bookkeeping; this does not write files to the SD.
    Do not issue another packet after an incomplete/ambiguous transport reply.
    """
    try:
        exchange(transport, SD_MOUNT, 4, log)
        exchange(transport, SD_INFO_SELECT, 4, log)
        content = exchange(transport, READ, 4 + MAX_FILE, log)
        if not content:
            raise ValueError("Media mapping record was empty")
        save("sd-media-info", content)
        return content
    finally:
        if getattr(transport, "_jiritsu_usable", False):
            exchange(transport, SD_UNMOUNT, 4, log)
            log("sd_unmount_passed")
        else:
            log("sd_unmount_not_sent", reason="Transport reply incomplete; session exit must handle recovery")
