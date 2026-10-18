"""Offline tests for the libJit execution entry. No camera, no USB, no network.

These pin the three things that would silently break the tool: the wire bytes,
the guards that stop it touching paths outside the work area, and the record
parsers that decide whether the module ran.
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import libjit_requests as jr  # noqa: E402

HEAD = struct.pack("<HH", 0x1001, 0x0F)


def test_select_frame_layout():
    frame = jr.select_frame(jr.LIBJIT_PATH)
    assert frame[:4] == HEAD
    assert frame[4:5] == b"\x00"
    assert frame[5:] == jr.LIBJIT_PATH.encode() + b"\x00"


def test_select_rejects_paths_outside_the_work_area():
    with pytest.raises(ValueError):
        jr.select_frame("/etc/ld.so.preload")
    with pytest.raises(ValueError):
        jr.select_frame("/tmp/jiritsu")  # the directory itself is not a path slot
    with pytest.raises(ValueError):
        jr.select_frame("/tmp/jiritsu/\x00x")
    with pytest.raises(ValueError):
        jr.select_frame("/tmp/jiritsu/中文")
    with pytest.raises(ValueError):
        jr.select_frame("/tmp/jiritsu/" + "a" * 500)
    with pytest.raises(ValueError):
        jr.select_frame(b"/tmp/jiritsu/x")


def test_select_allows_traversal_which_is_the_whole_point():
    frame = jr.select_frame(jr.JMODE_PATH)
    assert b"/tmp/jiritsu/../../tmp/jmode" in frame
    assert frame.endswith(b"\x00")


def test_write_and_read_frames():
    assert jr.READ_FRAME == struct.pack("<HH", 0x1001, 0x11) + b"\x00\xff\x00"
    frame = jr.write_frame(b"abc", 16)
    assert frame == struct.pack("<HH", 0x1001, 0x10) + b"\x00\xff\x00abc"
    with pytest.raises(ValueError):
        jr.write_frame(b"", 16)
    with pytest.raises(ValueError):
        jr.write_frame(b"x" * 17, 16)
    with pytest.raises(ValueError):
        jr.write_frame("not bytes", 16)


def test_script_wraps_the_command_and_records_the_status():
    script = jr.build_script("uname -a", "abc123").decode("ascii")
    assert script == ('uname -a\n'
                      'echo "A7JIT001 abc123 $?" > /tmp/jiritsu/a7c2-exec.status\n')


def test_script_appends_a_newline_only_when_missing():
    assert jr.build_script("true\n", "n1").decode().count("true") == 1
    assert jr.build_script("true", "n1").startswith(b"true\n")


def test_script_rejects_input_the_camera_would_mangle():
    with pytest.raises(ValueError):
        jr.build_script("", "n1")
    with pytest.raises(ValueError):
        jr.build_script("   ", "n1")
    with pytest.raises(ValueError):
        jr.build_script("echo 中文", "n1")
    with pytest.raises(ValueError):
        jr.build_script("true\x00false", "n1")
    with pytest.raises(ValueError):
        jr.build_script("true", "bad nonce")
    with pytest.raises(ValueError):
        jr.build_script("x" * jr.MAX_SCRIPT, "n1")


def test_parse_start_accepts_the_payload_record():
    raw = b"A7JITE01 v1 pid=812 euid=0 status=0\n"
    assert jr.parse_start(raw) == {"pid": 812, "euid": 0, "status": 0}


def test_parse_start_ignores_placeholders_and_foreign_records():
    assert jr.parse_start(jr.START_PENDING) is None
    assert jr.parse_start(b"") is None
    assert jr.parse_start(b"garbage") is None
    assert jr.parse_start(b"A7JITE01 v1 pid=1 euid=0\n") is None
    assert jr.parse_start(b"A7JITE01 v2 pid=1 euid=0 status=0\n") is None
    assert jr.parse_start(b"A7JITE01 v1 pid=x euid=0 status=0\n") is None
    assert jr.parse_start(b"\xff\xfe") is None


def test_parse_start_reports_a_failure_status():
    assert jr.parse_start(b"A7JITE01 v1 pid=5 euid=1000 status=-13\n")["status"] == -13


def test_parse_status_matches_only_this_runs_nonce():
    assert jr.parse_status(b"A7JIT001 abc123 0\n", "abc123") == 0
    assert jr.parse_status(b"A7JIT001 abc123 42\n", "abc123") == 42
    assert jr.parse_status(b"A7JIT001 deadbeef 0\n", "abc123") is None
    assert jr.parse_status(jr.STATUS_PENDING, "abc123") is None
    assert jr.parse_status(b"A7JIT001 abc123\n", "abc123") is None
    assert jr.parse_status(b"A7JIT001 abc123 x\n", "abc123") is None
    assert jr.parse_status(b"\xff", "abc123") is None


def test_frames_satisfy_the_reviewed_transport_allowlist():
    """The sibling module's authorize() must accept exactly what we send."""
    version_read = Path(__file__).resolve().parents[2] / "2026-10-16-system-dump" / "scripts"
    sys.path.insert(0, str(version_read))
    try:
        import jiritsu_version_read as jv
    except ImportError:  # pragma: no cover - the sibling deliverable is optional
        pytest.skip("system-dump transport module not present")

    class Transport:
        _authorized = ()
        _mailbox_channel = False

    requests = (jr.select_frame(jr.LIBJIT_PATH), jr.write_frame(b"x" * 4096, jr.MAX_UPLOAD),
                jr.select_frame(jr.REQUEST_PATH), jr.READ_FRAME)
    transport = Transport()
    jv.authorize(transport, requests, max_bytes=jr.MAX_UPLOAD, max_requests=8)
    assert set(requests).issubset(set(transport._authorized))

    with pytest.raises(ValueError):
        jv.authorize(Transport(), (jr.select_frame(jr.LIBJIT_PATH)[:4] + b"\x01\xff\x00",))
