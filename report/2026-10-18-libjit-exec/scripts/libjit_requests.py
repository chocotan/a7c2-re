"""Frame construction and parsers for the Jiritsu libJit execution entry.

Deliberately free of USB imports: everything here is a pure function so the
byte layout, the path guard and the result parsers can be tested without a
camera. The transport lives in libjit_exec.py.

Wire format (derived from this project's decompiled service daemon):

    request  = <HH category=0x1001, command> + payload
    command  0x0f  select path      payload = 0x00 + <path> + 0x00
    command  0x10  write file       payload = 0x00 0xff 0x00 + <bytes>
    command  0x11  read file        payload = 0x00 0xff 0x00

A write and a read act on the path chosen by the preceding select. The service
uses the path verbatim when it starts with /tmp/jiritsu, which is why the
`select` guard below only pins the prefix: `..` traversal is how this project
reaches anything outside the work area, and refactoring it away would break
every existing tool.
"""
import struct

CATEGORY = 0x1001
CMD_SELECT = 0x0F
CMD_WRITE = 0x10
CMD_READ = 0x11

WORK_AREA = "/tmp/jiritsu"
WORK_PREFIX = WORK_AREA + "/"

# The firmware's own single-file write limit (the 0x986c branch in the service
# daemon). Anything larger must be split or written on the camera side.
MAX_UPLOAD = 0x500000

# The payload object stays far below MAX_UPLOAD; this bound only guards against
# someone pointing the tool at a huge file by accident.
MAX_PAYLOAD = 0x40000

# The request file must fit the 2048-byte buffer the payload reads.
MAX_SCRIPT = 1800

ABSENT = 0x10000006  # "stat failed": the path does not exist or is unreadable

# Hardcoded on the camera side: IM_JIRITSU::CImJiritsu::run() builds its load
# request with this literal. Writing any other path does not feed the loader.
LIBJIT_PATH = WORK_PREFIX + "libJit.so"
# The loader refuses to run unless the camera wrote this mode file first.
JMODE_PATH = "/tmp/jiritsu/../../tmp/jmode"
# adj_cmd_set_file2 refuses the libJit write outright when this marker exists.
JBUSY_PATH = "/tmp/jiritsu/../../tmp/jbusy"
# Returned by the write handler when it wrote and fsynced the file but dlopen
# failed. The file is on disk and the module did not run.
DLOPEN_FAILED = 0x10000000
REQUEST_PATH = WORK_PREFIX + "a7c2-exec.request"
START_PATH = WORK_PREFIX + "a7c2-exec.start"
STATUS_PATH = WORK_PREFIX + "a7c2-exec.status"
OUTPUT_PATH = WORK_PREFIX + "a7c2-exec.out"

# Distinct tags for "we truncated this file but nothing ran yet", so a stale
# record from an earlier run can never be mistaken for a fresh one.
START_TAG = b"A7JITE01"
START_PENDING = b"A7JITE00 pending\n"
STATUS_TAG = b"A7JIT001"
STATUS_PENDING = b"A7JIT000 pending\n"

READ_FRAME = struct.pack("<HH", CATEGORY, CMD_READ) + b"\x00\xff\x00"


def select_frame(path):
    """Build a path-selection frame, refusing anything outside the work area."""
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not path.startswith(WORK_PREFIX):
        raise ValueError(f"path must start with {WORK_PREFIX}")
    if "\x00" in path or len(path) > 500:
        raise ValueError("path must be NUL-free and at most 500 bytes")
    encoded = path.encode("ascii", "strict")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise ValueError("path must be printable ASCII")
    return struct.pack("<HH", CATEGORY, CMD_SELECT) + b"\x00" + encoded + b"\x00"


def write_frame(data, limit):
    """Build a file-write frame; `limit` is the largest payload we accept."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("write payload must be non-empty bytes")
    if len(data) > limit:
        raise ValueError(f"write payload is {len(data)} bytes, limit is {limit}")
    return struct.pack("<HH", CATEGORY, CMD_WRITE) + b"\x00\xff\x00" + data


def build_script(command, nonce, status_path=STATUS_PATH):
    """Wrap a command so the shell itself records the exit status.

    The payload only starts `/bin/sh -c <this>`. Appending the status line here
    keeps the object byte-identical for every command, and the nonce means a
    line left over from an earlier run cannot match.

    Limits that come from the camera side, not from taste:
      - ASCII only: a UTF-8 command once failed to encode on the way in.
      - no `exit`: it would skip the status line. Use `return` or nothing.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if not isinstance(nonce, str) or not nonce.isalnum():
        raise ValueError("nonce must be alphanumeric")
    try:
        command.encode("ascii", "strict")
    except UnicodeEncodeError:
        raise ValueError("command must be ASCII") from None
    if "\x00" in command:
        raise ValueError("command must not contain NUL")
    body = command if command.endswith("\n") else command + "\n"
    script = f'{body}echo "{STATUS_TAG.decode()} {nonce} $?" > {status_path}\n'
    encoded = script.encode("ascii")
    if len(encoded) > MAX_SCRIPT:
        raise ValueError(f"script is {len(encoded)} bytes, limit is {MAX_SCRIPT}")
    return encoded


def parse_start(raw):
    """Parse the payload's own record. Returns None if it is not one yet.

    Format: b'A7JITE01 v1 pid=<int> euid=<int> status=<int>\\n'
    A pending placeholder or another run's tag yields None rather than an error,
    because "the module has not run yet" is a normal state to poll through.
    """
    if not isinstance(raw, bytes) or not raw.startswith(START_TAG):
        return None
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    fields = text.split()
    if len(fields) != 5 or fields[0] != START_TAG.decode() or fields[1] != "v1":
        return None
    values = {}
    for field in fields[2:]:
        key, _, value = field.partition("=")
        try:
            values[key] = int(value)
        except ValueError:
            return None
    if set(values) != {"pid", "euid", "status"}:
        return None
    return values


def parse_status(raw, nonce):
    """Parse the completion line. Returns the exit status, or None if absent.

    Format: b'A7JIT001 <nonce> <exit status>\\n'. A mismatched nonce is not an
    error: it means the file still holds another run's line.
    """
    if not isinstance(raw, bytes) or not raw.startswith(STATUS_TAG):
        return None
    try:
        fields = raw.decode("ascii").split()
    except UnicodeDecodeError:
        return None
    if len(fields) != 3 or fields[0] != STATUS_TAG.decode() or fields[1] != nonce:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None
